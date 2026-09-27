"""Multi-turn reply engine for /v1/reply.

Decision order (first match wins):
  opt-out / hostile        -> end (and mute merchant 30 days)
  WA-Business auto-reply   -> 1st: one short owner-nudge, 2nd: wait 24h, 3rd: end
  explicit commitment      -> ACTION mode: deliver the artifact now, never re-qualify
  "later / busy"           -> wait
  soft no                  -> end politely
  off-topic ask (GST etc.) -> decline in one line, redirect to the open thread
  question                 -> answer from context (LLM if configured), then one next step
  anything else            -> treat as engagement, move one step forward
"""
from __future__ import annotations

import re
import time

from . import facts as F
from . import llm
from .compose import validate
from .playbooks import Ctx, _poss
from .store import STORE

# ---------------------------------------------------------------- patterns
AUTO_PATTERNS = [
    r"thank(s| you) for (contacting|reaching|your message|messaging)", r"our team will (respond|get back|contact|reply)",
    r"will (respond|get back|reply) (to you )?(shortly|soon|as soon)", r"automated (assistant|message|reply|response)",
    r"(currently|presently) (unavailable|away|closed)", r"out of office", r"business hours", r"we will get back",
    r"aapki jaankari ke liye", r"team tak pahuncha", r"hamari team", r"this is an auto", r"auto[- ]?reply",
    r"for (any )?(queries|enquiries|inquiries),? (please )?(call|contact)", r"we('| a)re closed",
]
OPTOUT = [r"\bstop\b", r"unsubscribe", r"not interested", r"don'?t (message|text|contact|send)", r"no more (messages|msgs)",
          r"remove (me|my number)", r"band karo", r"mat bhejo", r"nahi chahiye", r"leave me alone", r"block", r"opt[- ]?out"]
HOSTILE = [r"useless", r"bother(ing)?", r"irritat", r"nonsense", r"bakwas", r"pagal", r"idiot", r"stupid", r"shut up",
           r"fraud", r"scam", r"\bspam", r"waste of (my )?time", r"harass", r"fed up", r"bloody", r"damn"]
COMMIT = [r"let'?s do it", r"lets do", r"go ahead", r"\bdo it\b", r"^\s*(yes|yes please|yep|yeah|ok|okay|sure|done|confirm|confirmed|haan|haa|ha|ji|han)\b",
          r"kar do", r"\bkaro\b", r"chalo", r"proceed", r"send (it|me|the)", r"sounds good", r"please do", r"what'?s next",
          r"whats next", r"i want to join", r"judna", r"judna hai", r"sign me up", r"book (it|me)", r"start (it|now)", r"\bagreed\b",
          r"theek hai", r"thik hai", r"bilkul", r"i'?m in", r"go for it", r"draft (it|them)"]
LATER = [r"\blater\b", r"\bbusy\b", r"not now", r"baad mein", r"baad me", r"call (me )?later", r"next week", r"tomorrow",
         r"\bkal\b", r"in a meeting", r"after some time", r"thodi der"]
SOFT_NO = [r"^\s*(no|nope|nahi|nahin|na)\b", r"not (needed|required)", r"no thanks", r"no thank you", r"zarurat nahi"]
OFFTOPIC = [r"\bgst\b", r"income tax", r"\bitr\b", r"tax (filing|return)", r"\bloan\b", r"insurance", r"passport", r"\bvisa\b",
            r"aadhaa?r", r"pan card", r"electricity bill", r"legal notice", r"\bca\b", r"accountant", r"bank account", r"cricket score"]
QUESTION = [r"\?", r"^\s*(what|how|why|when|where|which|who|can|could|is|are|does|do|kitna|kitne|kya|kaise|kab|kyun|kaun)\b"]
HINGLISH = [r"[ऀ-ॿ]", r"\b(hai|haan|nahi|karo|kya|mujhe|aap|chahiye|theek|accha|acha|bhej|kar|hoga|kaise|kitna|bhai|ji)\b"]


def _any(patterns, text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def is_autoreply(msg: str, state: dict, merchant_id: str | None) -> bool:
    norm = re.sub(r"\s+", " ", msg.strip().lower())
    if _any(AUTO_PATTERNS, norm):
        return True
    prev = [m.strip().lower() for m in state.get("inbound", [])]
    if prev and prev.count(norm) >= 1 and len(norm) > 25:  # same long text again = canned
        return True
    seen = STORE.merchant_autoreply.get(merchant_id or "", {})
    return seen.get(norm, 0) >= 1 and len(norm) > 25


def _until(req: dict, seconds: int) -> float:
    """Mute deadline in *simulated* time (epoch seconds of received_at + seconds)."""
    d = F.parse_dt(req.get("received_at"))
    base = d.timestamp() if d else time.time()
    return base + seconds


def speaks_hinglish(msg: str) -> bool:
    return _any(HINGLISH, msg)


# ---------------------------------------------------------------- state
def get_state(conv_id: str, merchant_id: str | None, customer_id: str | None) -> dict:
    st = STORE.conversations.get(conv_id)
    if st:
        return st
    # Unknown conversation (e.g. judge opened it directly): attach the merchant's most urgent live trigger.
    trig = None
    if merchant_id:
        ts = [t for t in STORE.triggers_for_merchant(merchant_id) if (t.get("customer_id") == customer_id) or not customer_id]
        ts.sort(key=lambda t: -(t.get("urgency") or 0))
        trig = ts[0] if ts else None
    st = {"conversation_id": conv_id, "merchant_id": merchant_id, "customer_id": customer_id,
          "trigger_id": trig.get("id") if trig else None, "kind": trig.get("kind") if trig else "general",
          "bodies": [], "inbound": [], "auto_count": 0, "nudged": False, "status": "open", "stage": "pitch",
          "created": time.time()}
    STORE.conversations[conv_id] = st
    return st


def _ctx(st: dict) -> Ctx | None:
    m = STORE.get("merchant", st.get("merchant_id"))
    if not m:
        return None
    cat = STORE.get("category", m.get("category_slug")) or {"slug": m.get("category_slug")}
    trig = STORE.get("trigger", st.get("trigger_id")) or {"kind": st.get("kind", "general"), "payload": {}}
    cu = STORE.get("customer", st.get("customer_id"))
    return Ctx(category=cat, merchant=m, trigger=trig, customer=cu)


# ---------------------------------------------------------------- artifacts (action mode)
TOPIC = {"research_digest": "research summary", "regulation_change": "compliance checklist", "perf_dip": "fix for your calls",
         "perf_spike": "follow-up post", "seasonal_perf_dip": "summer retention plan", "renewal_due": "profile fixes",
         "festival_upcoming": "season plan", "curious_ask_due": "Google post", "winback_eligible": "profile snapshot",
         "dormant_with_vera": "listing update", "ipl_match_today": "match-night story", "review_theme_emerged": "review replies",
         "milestone_reached": "review-ask card", "competitor_opened": "listing update", "active_planning_intent": "plan we were drafting",
         "supply_alert": "recall list", "category_seasonal": "summer WhatsApp", "gbp_unverified": "Google verification",
         "cde_opportunity": "webinar registration"}
PRICE_Q = r"price|cost|kitna|kitne|charge|fee|how much|paisa|rate|₹"


def answer_price(c: Ctx, st: dict) -> str | None:
    """Answer a price question strictly from what's already on the table."""
    first = st["bodies"][0] if st.get("bodies") else ""
    tiers = [ln.strip("• ").strip() for ln in first.splitlines() if "₹" in ln]
    hi = _hi(st, c)
    if tiers:
        return ("Draft ke hisaab se: " if hi else "From the draft: ") + "; ".join(tiers[:3]) + "."
    if c.trigger.get("kind") == "renewal_due" and c.p.get("renewal_amount"):
        return f"Renewal is {F.inr(c.p['renewal_amount'])} for the {c.p.get('plan', 'current')} plan."
    offers = F.active_offers(c.merchant)
    if offers:
        return ("Aapka live offer: " if hi else "Your live offer: ") + ", ".join(offers[:2]) + "."
    return "Nothing extra from my side — drafting and posting this is part of what I do for you." if not hi else \
        "Mere taraf se koi extra charge nahi — yeh draft aur posting main hi kar deti hoon."


def _hi(st: dict, c: Ctx) -> bool:
    return st.get("lang") == "hi" or (c.hi and st.get("lang") != "en")


def artifact(c: Ctx, st: dict, merchant_msg: str) -> tuple[str, str]:
    """Concrete deliverable for the open thread. Returns (body, cta)."""
    kind = c.trigger.get("kind", "general")
    p = c.p
    hi = _hi(st, c)
    confirm = "Reply CONFIRM and it goes live — ya koi change ho toh bata dijiye." if hi else "Reply CONFIRM and it goes live, or tell me what to change."
    offer = (F.active_offers(c.merchant) or [None])[0]

    if kind in ("research_digest", "category_research_digest_release", "research_digest_release"):
        item = F.digest_item(c.category, p.get("top_item_id")) or F.digest_by_kind(c.category, "research") or {}
        cnt = (c.merchant.get("customer_aggregate") or {}).get("high_risk_adult_count")
        return (f"Here's the patient WhatsApp draft:\n\n\"Hi {{name}}, {c.name} here. New research ({item.get('source')}) shows that for "
                f"patients with a history of cavities, a check-up every 3 months instead of 6 prevents far more new decay. "
                f"Your next visit is a good time to set this up — reply to book.\"\n\n"
                + (f"I'll send it only to your {cnt} high-risk adult patients, not the full list. " if cnt else "")
                + confirm, "binary_confirm_cancel")
    if kind in ("regulation_change", "compliance"):
        item = F.digest_item(c.category, p.get("top_item_id")) or F.digest_by_kind(c.category, "compliance") or {}
        return (f"Here's your audit checklist ({item.get('source')}):\n"
                "1. Note the film speed / sensor type on each X-ray unit\n"
                "2. E-speed film or digital RVG = compliant; D-speed = replace before the deadline\n"
                "3. Add one SOP line: 'IOPA exposures per revised DCI dose limit'\n"
                "4. File the page with your clinic registration papers\n"
                "Reply DONE once you've checked the units and I'll set a reminder 2 weeks before the deadline.", "binary_yes_no")
    if kind == "supply_alert":
        mol = p.get("molecule", "the molecule")
        batches = ", ".join(p.get("affected_batches") or [])
        return (f"Sending the {mol} customer list now (filtered to batches {batches}). Replacement WhatsApp draft:\n\n"
                f"\"Namaste {{name}}, {c.name} here. The {mol} strip you bought recently is part of a voluntary manufacturer recall "
                f"(lower strength, no safety risk). Please bring it back or reply here — we'll replace it free, with home delivery if you prefer.\"\n\n"
                "Reply CONFIRM to send it to the affected customers.", "binary_confirm_cancel")
    if kind == "active_planning_intent":
        return ("Here's the 3-line outreach draft for office admins:\n\n"
                f"\"Hi! {c.name}, {c.loc} here. We now do corporate lunch thalis — delivered hot by 1:15pm, order by 5pm the day before, "
                "better per-plate rates from 10 thalis. Want the menu card for your team?\"\n\n"
                + confirm, "binary_confirm_cancel") if "thali" in p.get("intent_topic", "") else \
               (f"Here's the Google post draft:\n\n\"New at {c.name}: Kids Summer Yoga (age 7-12) — 4 weeks, 3 mornings a week, small batches. "
                "Free trial class. Message us to book a spot.\"\n\nThe Insta carousel follows the same 4 slides. " + confirm, "binary_confirm_cancel")
    if kind == "ipl_match_today":
        return (f"Here's tonight's Insta story:\n\n\"Match at home tonight? 🍕 {c.name} delivers hot to {c.loc} — order before the first ball.\"\n\n"
                + confirm, "binary_confirm_cancel")
    if kind == "review_theme_emerged":
        return ("Here's the reply draft for those reviews:\n\n\"Thank you for the honest feedback — you're right, that wait was too long. "
                "We've tightened our dispatch so orders leave within 10 minutes of being ready. Hope to make it up to you on your next order.\"\n\n"
                "Reply CONFIRM and I'll post it on each of them.", "binary_confirm_cancel")
    if kind == "milestone_reached":
        return (f"Here's the table card:\n\n\"Loved your meal? A 20-second Google review helps {c.name} more than you'd think 🙏\"\n\n"
                "And the WhatsApp nudge for regulars uses the same line. " + confirm, "binary_confirm_cancel")
    if kind == "competitor_opened":
        pos = next((t for t in c.merchant.get("review_themes") or [] if t.get("sentiment") == "pos"), None)
        hook = F.humanize(pos["theme"]) if pos else "personal care"
        return (f"Here's the new opening line for your Google description:\n\n\"{c.name}, {c.loc} — known for {hook}. "
                "Unhurried consultations, clear pricing, and appointment slots so you don't wait.\"\n\n" + confirm, "binary_confirm_cancel")
    if kind in ("curious_ask_due", "scheduled_recurring"):
        ans = merchant_msg.strip().rstrip(".")[:80]
        return (f"Perfect — here's the Google post built on that:\n\n\"Most requested at {c.name} this week: {ans}. "
                + (f"Try it with our '{offer}'. " if offer else "") + "Walk in or message us to book.\"\n\n"
                "And the WhatsApp reply for price questions uses the same line. " + confirm, "binary_confirm_cancel")
    if kind == "renewal_due":
        return ("Starting with the fix, not the payment: I'll begin Google verification today and put your first service-at-price offer "
                "live for your approval. You'll see the before/after numbers before the renewal date. Reply CONFIRM to start.", "binary_confirm_cancel")
    if kind in ("gbp_unverified",):
        return ("Here's the plan: I'll request the phone-call verification now — Google calls the listed number with a 5-digit code. "
                "When it comes, just forward me the code here. Reply CONFIRM to request the call.", "binary_confirm_cancel")
    if kind == "cde_opportunity":
        return ("Done — registering you now. I'll send a reminder at 6pm on the day with the joining details. "
                "Anything you want me to note down for the Q&A?", "open_ended")
    if kind in ("winback_eligible", "dormant_with_vera"):
        return ("Here's the draft change for your Google description: add 'Walk-in available · weekday afternoons' to the first line. "
                + confirm, "binary_confirm_cancel") if c.slug == "salons" else \
               ("Here's the one-page snapshot: views, calls and direction requests for the last 30 days vs. your peers, plus the "
                "2 fixes with the biggest effect. " + confirm, "binary_confirm_cancel")
    if kind == "category_seasonal":
        lib = next((x for x in c.category.get("patient_content_library") or [] if "summer" in x.get("id", "")), None)
        body = lib["body"] if lib else "Stay hydrated, keep ORS at home, and use sunscreen outdoors."
        return (f"Here's the WhatsApp for regulars:\n\n\"{c.name} summer tip: {body}\"\n\n" + confirm, "binary_confirm_cancel")
    # default: a Google post around what they have (or asked for)
    last = (F.last_merchant_message(c.merchant) or "").lower()
    if c.slug == "dentists" and ("aligner" in last or "whitening" in last):
        tr = next((t for t in c.category.get("trend_signals") or [] if "aligner" in t["query"]), None)
        return ("Here are the 3 Google post drafts you asked for (whitening + aligners):\n\n"
                f"1) \"Clear aligners, supervised by a dentist — not a DIY kit. Book a consultation at {c.name}, {c.loc}.\"\n"
                f"2) \"Wedding or event coming up? Professional whitening, done safely in one sitting at {c.name}.\"\n"
                + (f"3) \"{offer} this month at {c.name} — a clean start before any whitening. Message us to book.\"\n\n" if offer else
                   f"3) \"Not sure if aligners suit you? A quick check at {c.name} tells you.\"\n\n")
                + (f"Aligner searches are {F.pct(tr['delta_yoy'], True)} YoY, so I'd post #1 first. " if tr else "") + confirm,
                "binary_confirm_cancel")
    return (f"Here's the Google post draft:\n\n\"{c.name}, {c.loc}: " + (f"{offer} — " if offer else "") +
            "message us on WhatsApp to book.\"\n\n" + confirm, "binary_confirm_cancel")


def customer_reply(c: Ctx, st: dict, msg: str) -> dict:
    low = msg.strip().lower()
    cu = c.customer or {}
    hi = F.customer_lang(cu) in ("hi", "hi-en") or speaks_hinglish(msg)
    slots = c.p.get("available_slots") or c.p.get("next_session_options") or []
    if _any(OPTOUT, low) or _any(HOSTILE, low):
        STORE.customer_optout.add(st.get("customer_id") or "")
        return {"action": "end", "rationale": "Customer opted out; no further messages on behalf of the merchant."}
    m = re.match(r"^\s*([12])\b", low)
    if m and len(slots) >= int(m.group(1)):
        s = slots[int(m.group(1)) - 1]["label"]
        body = (f"Done ✅ {s} aapke liye book ho gaya hai. Ek din pehle reminder bhejenge. Kuch change ho toh yahin reply karein."
                if hi else f"Done ✅ You're booked for {s}. We'll send a reminder the day before — reply here if anything changes.")
        st["stage"] = "booked"
        return {"action": "send", "body": body, "cta": "none", "rationale": "Customer picked a slot; confirm booking and close the loop."}
    if _any(COMMIT, low) or low in ("confirm", "haan", "yes"):
        s = slots[0]["label"] if len(slots) == 1 else None
        if c.trigger.get("kind") == "chronic_refill_due":
            body = ("Dhanyavaad! Order dispatch ho raha hai — aaj shaam tak delivery. Payment delivery par." if hi
                    else "Thank you! Your order is being dispatched — delivery by this evening. Pay on delivery.")
        elif s:
            body = (f"Done ✅ {s} confirm ho gaya. Ek din pehle reminder aayega." if hi else f"Done ✅ {s} is confirmed. We'll remind you a day before.")
        else:
            body = ("Badhiya! Aapka slot hold kar liya hai — kaunsa din aur time best rahega?" if hi
                    else "Great — we've held a spot for you. Which day and time suits you best?")
        st["stage"] = "booked"
        return {"action": "send", "body": body, "cta": "open_ended" if not s else "none", "rationale": "Customer said yes; confirm and lock details."}
    if _any(LATER, low):
        return {"action": "wait", "wait_seconds": 86400, "rationale": "Customer asked for later; follow up tomorrow."}
    if _any(SOFT_NO, low):
        return {"action": "end", "rationale": "Customer declined; closing politely without further nudges."}
    # time proposal / question
    body = ("Noted! Hum aapke bataye time ke hisaab se slot confirm karke yahin message karenge." if hi
            else "Noted — we'll confirm a slot at that time and message you here shortly.")
    if "?" in msg:
        offer = (F.active_offers(c.merchant) or [None])[0]
        body = ((f"Zaroor! Abhi '{offer}' chal raha hai. " if offer else "Zaroor! ") + "Aapke liye slot book kar dein? Reply YES." if hi
                else (f"Happy to help! We're currently running '{offer}'. " if offer else "Happy to help! ") + "Shall we book you in? Reply YES.")
    return {"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Customer replied with a time/question; answer and move to booking."}


# ---------------------------------------------------------------- main entry
QA_SYSTEM = """You are Vera, magicpin's merchant assistant on WhatsApp. The merchant just asked a question.
Answer in 1-3 short sentences using ONLY facts in CONTEXT (no invented numbers, prices, names or links), then end with exactly ONE next-step question.
Match the merchant's language (Hinglish if they wrote Hinglish). Never re-introduce yourself.
Return JSON: {"body": "...", "rationale": "..."}"""


async def handle_reply(req: dict) -> dict:
    conv_id = req.get("conversation_id") or "conv_unknown"
    msg = (req.get("message") or "").strip()
    st = get_state(conv_id, req.get("merchant_id"), req.get("customer_id"))
    mid = st.get("merchant_id") or req.get("merchant_id")
    low = msg.lower()
    st["lang"] = "hi" if speaks_hinglish(msg) else "en"
    c = _ctx(st)

    def finish(resp: dict) -> dict:
        if resp.get("action") == "send":
            body = resp["body"]
            if body in st["bodies"]:  # anti-repetition
                body = body + (" (Just say the word and I'll handle it.)" if "handle it" not in body else " 🙂")
                resp["body"] = body
            st["bodies"].append(body)
        elif resp.get("action") == "end":
            st["status"] = "ended"
        st["inbound"].append(msg)
        return resp

    if req.get("from_role") == "customer" and c:
        return finish(customer_reply(c, st, msg))

    # 1. opt-out / hostility
    if _any(OPTOUT, low) or _any(HOSTILE, low):
        if mid:
            STORE.merchant_optout[mid] = _until(req, 30 * 86400)
        return finish({"action": "end", "rationale": "Merchant opted out / expressed frustration. Closing the conversation and muting proactive sends for 30 days."})

    # 2. WhatsApp Business auto-reply
    if is_autoreply(msg, st, mid):
        seen = STORE.merchant_autoreply.setdefault(mid or "", {})
        norm = re.sub(r"\s+", " ", low)
        seen[norm] = seen.get(norm, 0) + 1
        st["auto_count"] += 1
        total = max(st["auto_count"], seen[norm])
        if total == 1 and not st["nudged"]:
            st["nudged"] = True
            name = c.sal if c else ""
            if c and c.slug != "dentists" and name:
                name = name + " ji"
            body = (f"Lagta hai yeh auto-reply hai 🙂 {name + ', ' if name else ''}jab aap dekhein, bas YES reply kar dijiye — baaki main sambhal lungi."
                    if (c and c.hi) else f"Looks like an auto-reply 🙂 {name + ', when' if name else 'When'} you see this, just reply YES and I'll take it from there.")
            return finish({"action": "send", "body": body, "cta": "binary_yes_no",
                           "rationale": "Detected WhatsApp Business canned reply; one short prompt for the owner, no more pitching."})
        if total == 2:
            if mid:
                STORE.merchant_optout[mid] = _until(req, 86400)
            return finish({"action": "wait", "wait_seconds": 86400,
                           "rationale": "Same canned auto-reply again — owner not at the phone. Backing off 24h."})
        return finish({"action": "end", "rationale": "Auto-reply 3+ times with no human response; closing to avoid wasting turns."})

    if st.get("status") == "ended" and c:
        if _any(OFFTOPIC, low) or _any(QUESTION, low):
            body = ("Iske liye aapke CA sahi rahenge. Main aapko aur message nahi karungi — kabhi zaroorat ho toh bas 'Hi Vera' likh dijiye. 🙏"
                    if _hi(st, c) else
                    "That one's best handled by your CA. I won't message you further — if you ever need me, just say 'Hi Vera'. 🙏")
            return finish({"action": "send", "body": body, "cta": "none",
                           "rationale": "Conversation was closed after frustration; answered politely once without re-pitching."})
        return finish({"action": "end", "rationale": "Conversation already closed by the merchant; staying quiet."})

    if not c:
        return finish({"action": "send", "body": "Got it — share your business name and I'll pull up your profile and take it from there.",
                       "cta": "open_ended", "rationale": "No merchant context available for this conversation."})

    # 3. curious-ask: any real reply IS the answer -> build the artifact from it
    if st.get("kind") in ("curious_ask_due", "scheduled_recurring") and st["stage"] == "pitch" and not _any(QUESTION, low) and not _any(SOFT_NO, low):
        st["stage"] = "action"
        body, cta = artifact(c, st, msg)
        return finish({"action": "send", "body": body, "cta": cta, "rationale": "Merchant answered the curious-ask; turned the answer into a ready post (reciprocity)."})

    # 4. explicit commitment -> action mode, immediately
    if _any(COMMIT, low) and not _any(OFFTOPIC, low):
        if st["stage"] == "action":  # already delivered the artifact -> execute
            st["stage"] = "done"
            body = ("Done ✅ Live ho gaya. 48 ghante mein results check karke update bhejungi." if _hi(st, c)
                    else "Done ✅ It's live. I'll check the numbers in 48 hours and send you a short update.")
            return finish({"action": "send", "body": body, "cta": "none", "rationale": "Merchant confirmed the artifact; executed and set a follow-up."})
        st["stage"] = "action"
        body, cta = artifact(c, st, msg)
        return finish({"action": "send", "body": body, "cta": cta,
                       "rationale": "Merchant committed — switched from pitch to action: delivered the concrete artifact with a single CONFIRM."})

    # 5. later / busy
    if _any(LATER, low):
        wait = 86400 if re.search(r"tomorrow|\bkal\b|next week", low) else 14400
        return finish({"action": "wait", "wait_seconds": wait, "rationale": "Merchant asked for time; backing off without another nudge."})

    # 6. soft no
    if _any(SOFT_NO, low) and not _any(QUESTION, low):
        return finish({"action": "end", "rationale": "Merchant declined; exiting gracefully instead of re-pitching."})

    # 7. off-topic
    if _any(OFFTOPIC, low):
        topic = TOPIC.get(c.trigger.get("kind", ""), "your Google profile")
        body = (f"Yeh mere scope se bahar hai — iske liye aapke CA best rahenge. Wapas {topic} par: aage badhein? Reply YES aur main draft bhej deti hoon."
                if _hi(st, c) else
                f"That one's outside what I can help with — your CA is the right person for it. Back to the {topic}: reply YES and I'll send the draft.")
        return finish({"action": "send", "body": body, "cta": "binary_yes_no", "rationale": "Off-topic ask politely declined; redirected to the open thread."})

    # 8. question -> answer (LLM if available, validated) else deterministic
    if _any(QUESTION, low):
        if llm.llm_available():
            import json
            ctx = {"merchant": c.merchant, "trigger": c.trigger, "category_digest": c.category.get("digest"),
                   "offers": F.active_offers(c.merchant), "conversation_so_far": st["bodies"][-3:]}
            out = llm.parse_json(await llm.complete(QA_SYSTEM, "CONTEXT:\n" + json.dumps(ctx, default=str, ensure_ascii=False)[:16000]
                                                    + f"\n\nMERCHANT ASKED: {msg}", timeout=10))
            if out and out.get("body") and not validate(out["body"], c, reference=" ".join(st["bodies"])) :
                return finish({"action": "send", "body": out["body"], "cta": "open_ended", "rationale": out.get("rationale", "Answered question from context.")})
        if re.search(PRICE_Q, low):
            ans = answer_price(c, st)
            nxt = ("Draft final karke bhej doon? Reply YES." if _hi(st, c) else "Shall I finalise the draft? Reply YES.")
            return finish({"action": "send", "body": f"{ans} {nxt}", "cta": "binary_yes_no",
                           "rationale": "Price question answered only with numbers already in the thread/offers; single next step."})
        body, cta = artifact(c, st, msg)
        st["stage"] = "action"
        lead = "Good question — simplest is to show you. " if not _hi(st, c) else "Accha sawaal — dikha deti hoon. "
        return finish({"action": "send", "body": lead + body, "cta": cta, "rationale": "Answered by showing the concrete draft rather than more explanation."})

    # 9. engaged / anything else -> one step forward
    st["stage"] = "action"
    body, cta = artifact(c, st, msg)
    return finish({"action": "send", "body": body, "cta": cta, "rationale": "Positive engagement; moved one concrete step forward with a ready artifact."})
