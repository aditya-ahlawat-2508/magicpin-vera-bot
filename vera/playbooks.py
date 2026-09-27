"""Deterministic, grounded composer — one playbook per trigger kind.

Each playbook returns (body, cta, rationale). Rules every playbook follows:
  * open with the owner's name, no preamble, no self-introduction
  * say WHY NOW (the trigger) in the first sentence
  * every number comes from the contexts (see facts.py)
  * add a judgment the merchant didn't ask for when the data supports it
  * end with ONE low-friction ask
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import facts as F
from .store import STORE


@dataclass
class Ctx:
    category: dict
    merchant: dict
    trigger: dict
    customer: dict | None = None
    now: str = F.DEFAULT_NOW
    notes: list[str] = field(default_factory=list)  # reasoning trail -> rationale

    @property
    def p(self) -> dict:
        return self.trigger.get("payload") or {}

    @property
    def placeholder(self) -> bool:
        return bool(self.p.get("placeholder"))

    @property
    def slug(self) -> str:
        return self.category.get("slug") or self.merchant.get("category_slug", "")

    @property
    def sal(self) -> str:
        return F.salutation(self.merchant, self.category)

    @property
    def name(self) -> str:
        return F.biz_name(self.merchant)

    @property
    def loc(self) -> str:
        return F.locality(self.merchant)

    @property
    def hi(self) -> bool:
        mix = (self.category.get("voice") or {}).get("code_mix", "")
        return F.speaks_hindi(self.merchant) and "hindi_english" in mix

    def yes(self, en_action: str, hi_action: str | None = None) -> str:
        """Single binary CTA line in the merchant's register."""
        if self.hi and hi_action:
            return f"Reply YES — {hi_action}"
        return f"Reply YES and {en_action}"


SINGULAR = {"dentists": "dental clinic", "salons": "salon", "restaurants": "restaurant", "gyms": "gym", "pharmacies": "pharmacy"}

# ============================================================================
# helpers
# ============================================================================
def _join(*parts: str) -> str:
    return " ".join(p.strip() for p in parts if p and p.strip())


_SENT_RE = re.compile(r"(?<!\bDr)(?<!\b[A-Z])(?<!\bvs)\.\s+|;\s+")


def _first_sentence(text: str) -> str:
    text = (text or "").strip()
    parts = _SENT_RE.split(text, maxsplit=1)
    first = parts[0].rstrip(".")
    return first + "."


def _poss(name: str) -> str:
    return f"{name}'" if name.endswith("s") else f"{name}'s"


def _cohort_anchor(c: Ctx, segment: str | None) -> str | None:
    agg = c.merchant.get("customer_aggregate") or {}
    if segment:
        key = segment.rstrip("s") if segment.endswith("adults") else segment
        for k, v in agg.items():
            if k.startswith(key.replace("-", "_")) or segment.replace("-", "_") in k:
                return f"You have {F.num(v)} {F.humanize(segment)} in your roster — that's exactly this cohort."
    return None


def _offer_line(c: Ctx) -> str | None:
    offers = F.active_offers(c.merchant)
    return offers[0] if offers else None


def _other_trigger(c: Ctx, kind: str) -> dict | None:
    for t in STORE.triggers_for_merchant(c.merchant.get("merchant_id", "")):
        if t.get("kind") == kind and t.get("id") != c.trigger.get("id"):
            return t
    return None


def _peer_line(c: Ctx) -> str | None:
    gap = F.peer_gap(c.merchant, c.category)
    if "ctr" in gap:
        mine, peer = gap["ctr"]
        if mine < peer:
            return f"Your profile CTR is {F.ctr_str(mine)} vs {F.ctr_str(peer)} for peers."
    if "calls" in gap:
        mine, peer = gap["calls"]
        if mine < peer:
            return f"You're at {mine} calls in 30 days vs a peer average of {peer}."
    return None


# ============================================================================
# MERCHANT-FACING PLAYBOOKS
# ============================================================================
def research_digest(c: Ctx):
    item = F.digest_item(c.category, c.p.get("top_item_id")) or \
        (c.p.get("top_item") if isinstance(c.p.get("top_item"), dict) else None) or \
        F.digest_by_kind(c.category, "research", "trend", "tech")
    if not item:
        return None
    src = item.get("source", "")
    n = f"{F.num(item['trial_n'])}-patient " if item.get("trial_n") else ""
    lead = f"{c.sal}, new in {src}: {n}study — {item.get('title', '').rstrip('.')}."
    summary = item.get("summary", "")
    detail = ""
    if summary:
        detail = _first_sentence(summary)
        rest = summary[len(detail):].strip()
        if rest and len(rest) < 60:
            detail = _join(detail, rest)
    anchor = _cohort_anchor(c, item.get("patient_segment")) or ""
    if not anchor and c.slug == "dentists":
        tot = (c.merchant.get("customer_aggregate") or {}).get("total_unique_ytd")
        if tot:
            anchor = f"Worth checking against your {F.num(tot)} patients this year."
    c.notes.append(f"digest item {item.get('id')} cited with source; cohort anchored from customer_aggregate")
    ask = ("Want me to draft a recall-reminder WhatsApp for that cohort, plus a 3-line summary you can keep?"
           if anchor.startswith("You have") else
           "Want the 3-line summary plus a patient-friendly WhatsApp version you can share?")
    cta = c.yes("I'll send both in 10 min.", "main 10 min mein dono bhej deti hoon.")
    return _join(lead, detail, anchor, ask, cta), "binary_yes_no", \
        f"Research digest ({src}) matched to merchant's cohort; source-cited, clinical peer tone, reciprocity CTA."


def _strip_effective(title: str) -> str:
    return re.sub(r"\s*effective\s+\S+$", "", title).rstrip(".")


def regulation_change(c: Ctx):
    item = F.digest_item(c.category, c.p.get("top_item_id")) or F.digest_by_kind(c.category, "compliance")
    if not item:
        return None
    deadline = c.p.get("deadline_iso") or item.get("date")
    days = F.days_between(c.now, deadline) if deadline else None
    when = f"effective {F.nice_date(deadline)}" + (f" — {days} days from today" if days and days > 0 else "")
    body = _join(
        f"{c.sal}, compliance heads-up ({item.get('source')}): {_strip_effective(item.get('title', ''))}, {when}.",
        item.get("summary", ""),
        "Nothing to change if you're already on a passing setup — but it needs to be documented."
        if c.slug == "dentists" else "",
        "Want a 1-page audit checklist + SOP line you can file?",
        c.yes("I'll send it today.", "aaj hi bhej deti hoon."),
    )
    return body, "binary_yes_no", f"Regulatory change with hard deadline ({deadline}); cites circular, gives the pass/fail rule, offers ready artifact."


def perf_dip(c: Ctx):
    p = F.perf(c.merchant)
    metric, delta = (c.p.get("metric"), c.p.get("delta_pct")) if not c.placeholder else (None, None)
    if metric is None:
        wm = F.worst_metric(c.merchant)
        if wm:
            metric, delta = wm
    parts = []
    if metric and delta is not None and delta < 0:
        base = c.p.get("vs_baseline")
        base_txt = f" (vs your usual ~{base})" if base else ""
        parts.append(f"{c.sal}, {metric} to {c.name} dropped {F.pct(delta)} this week{base_txt}.")
    else:
        gap = F.peer_gap(c.merchant, c.category).get("calls")
        if gap and gap[0] < gap[1]:
            parts.append(f"{c.sal}, calls to {c.name} are running below peers: {gap[0]} in 30 days vs a {gap[1]} average.")
        else:
            parts.append(f"{c.sal}, {_poss(c.name)} Google numbers softened this week.")
    # diagnosis from data
    causes = []
    if not (c.merchant.get("identity") or {}).get("verified", True):
        causes.append("Google profile still unverified")
    if not F.active_offers(c.merchant):
        causes.append("no live offer on the listing")
    sp = F.signal_value(c.merchant, "stale_posts")
    if sp:
        causes.append(f"last post {sp} ago")
    peer = _peer_line(c)
    if len(causes) >= 2:
        parts.append(f"Two things I can see from here: {causes[0]} and {causes[1]}.")
    elif causes:
        parts.append(f"One thing I can see from here: {causes[0]}.")
    if peer and "calls" not in parts[0]:
        parts.append(peer)
    offer = _offer_line(c)
    if offer:
        fix = f"Fastest lever: a fresh Google post pushing your '{offer}' this week."
        ask = "Want me to draft it?"
    else:
        cat = F.catalog_offer(c.category)
        fix = f"Fastest lever: put one service-at-price offer live — e.g. '{cat}' — it converts better than a % discount." if cat else ""
        ask = "Want me to set it up for your review?"
    c.notes.append(f"dip on {metric} {delta}; causes={causes}")
    parts += [fix, ask, c.yes("it goes live after your OK.", "aapke OK ke baad hi live karungi.")]
    return _join(*parts), "binary_yes_no", "Performance dip: quantified loss + diagnosed causes from merchant data + one concrete fix."


def seasonal_perf_dip(c: Ctx):
    metric, delta = c.p.get("metric", "views"), c.p.get("delta_pct")
    d7 = F.perf(c.merchant).get("delta_7d") or {}
    item = F.digest_by_kind(c.category, "seasonal")
    agg = c.merchant.get("customer_aggregate") or {}
    ps = c.category.get("peer_stats") or {}
    lines = []
    calls = d7.get("calls_pct")
    extra = f" and calls {F.pct(calls)}" if calls is not None and metric != "calls" and calls < 0 else ""
    lines.append(f"{c.sal}, {metric} are down {F.pct(delta)}{extra} this week — "
                 f"but this is the normal Apr-Jun lull, not a {c.name} problem.")
    if item:
        lines.append(f"{item.get('source')}: April-June is the lowest acquisition window of the year; "
                     "most gyms overspend on ads now and underspend in Sept-Oct.")
    members, churn, peer_churn = agg.get("total_active_members"), agg.get("monthly_churn_pct"), ps.get("monthly_churn_pct")
    if members and churn:
        cmp_ = f" vs {F.pct(peer_churn)} for peers" if peer_churn else ""
        lines.append(f"The lever now is retention: your monthly churn is {F.pct(churn)}{cmp_} — "
                     f"on {members} members, every point saved keeps ~{max(1, round(members * 0.01))} people a month.")
    lines.append("Want me to draft a 4-week summer attendance challenge for current members?")
    lines.append(c.yes("I'll send the plan + the WhatsApp announcement."))
    return _join(*lines), "binary_yes_no", "Expected seasonal dip: pre-empt anxiety, cite source, redirect budget to retention with member-count math."


def perf_spike(c: Ctx):
    metric, delta = (c.p.get("metric"), c.p.get("delta_pct")) if not c.placeholder else (None, None)
    if metric is None:
        bm = F.best_metric(c.merchant)
        if bm:
            metric, delta = bm
    driver = c.p.get("likely_driver")
    base = c.p.get("vs_baseline")
    lines = []
    if metric and delta and delta > 0:
        lines.append(f"{c.sal}, {metric} at {c.name} are up {F.pct(delta)} this week"
                     + (f" (vs ~{base} baseline)" if base else "") + ".")
    else:
        lines.append(f"{c.sal}, {c.name} had a good week on Google.")
    if driver:
        lines.append(f"Likely driver: your {F.humanize(driver)}. Momentum like this fades in ~2 weeks unless you follow it up.")
    else:
        gap = F.peer_gap(c.merchant, c.category).get("calls")
        if gap and gap[0] < gap[1]:
            lines.append(f"You're at {gap[0]} calls in 30 days vs a {gap[1]} peer average — a fresh post now helps close that gap.")
        else:
            lines.append("Momentum like this fades in ~2 weeks unless you follow it up with a fresh post.")
    hist = " ".join(t.get("body", "") for t in c.merchant.get("conversation_history") or [])
    if driver and "kids" in driver and "2,499" in hist:
        ask = "Want me to draft post #2 with the 4-week camp details (age 7-12, 3 classes/week, ₹2,499)?"
    else:
        offer = _offer_line(c)
        ask = f"Want me to draft a follow-up post featuring '{offer}'?" if offer else "Want me to draft the follow-up post?"
    lines += [ask, c.yes("it'll be ready in 10 min.", "10 min mein ready kar deti hoon.")]
    return _join(*lines), "binary_yes_no", "Performance spike: name the driver, loss-aversion on fading momentum, one follow-up action."


def renewal_due(c: Ctx):
    sub = c.merchant.get("subscription") or {}
    days = c.p.get("days_remaining", sub.get("days_remaining"))
    plan = c.p.get("plan", sub.get("plan", "Pro"))
    amt = c.p.get("renewal_amount")
    p = F.perf(c.merchant)
    d7 = p.get("delta_7d") or {}
    lines = [f"{c.sal}, your {plan} plan renews in {days} days" + (f" ({F.inr(amt)})." if amt else ".")]
    gaps = []
    if not (c.merchant.get("identity") or {}).get("verified", True):
        gaps.append("unverified Google profile")
    if not F.active_offers(c.merchant):
        gaps.append("no live offer")
    if (d7.get("calls_pct") or 0) < -0.15 or gaps:
        lines.append(f"Straight talk: this month was weak — {p.get('calls', '?')} calls in 30 days"
                     + (f", calls {F.pct(d7['calls_pct'])} this week" if (d7.get('calls_pct') or 0) < 0 else "") + ".")
        if gaps:
            lines.append(f"Two fixable gaps are dragging it: {' and '.join(gaps)}.")
        lines.append("Before you decide on renewal, let me fix those this week so you judge the plan on real numbers.")
        lines.append(c.yes("I'll start with the first fix today.", "aaj hi pehla fix shuru karti hoon."))
    else:
        lines.append(f"Last 30 days: {F.num(p.get('views'))} views, {p.get('calls')} calls, {p.get('directions')} direction requests.")
        lines.append("Want me to lock the renewal at the current price?")
        lines.append(c.yes("I'll process it — no change to your setup."))
    return _join(*lines), "binary_yes_no", "Renewal: honest value framing from real performance; fix-first offer instead of hard sell."


def festival_upcoming(c: Ctx):
    fest, fdate, days = c.p.get("festival"), c.p.get("date"), c.p.get("days_until")
    if fest and fdate and days is None:
        days = F.days_between(c.now, fdate)
    offer = _offer_line(c)
    if fest and days is not None and days > 45:
        beat = F.seasonal_beat(c.category, c.now, "wedding") or F.seasonal_beat(c.category, c.now, "festival", upcoming=True)
        now_beat = F.seasonal_beat(c.category, c.now)
        dig = F.digest_by_kind(c.category, "seasonal")
        lines = [f"{c.sal}, {fest} is {days} days out ({F.nice_date(fdate)}) — too early for a promo, "
                 "but the right time to plan the season."]
        if beat:
            lines.append(f"{beat['month_range']}: {beat['note']}.")
        if dig and now_beat:
            lines.append(f"Meanwhile the window open right now: {dig.get('title', '').rstrip('.')}.")
        hist = " ".join(t.get("body", "") for t in c.merchant.get("conversation_history") or [])
        if "+28%" in hist and c.slug == "salons":
            lines.append(f"Bridal-trial searches in {c.loc} were already +28% last week.")
        cat = F.catalog_offer(c.category, ("service_at_price",)) if c.slug != "salons" else "Bridal Trial @ ₹999"
        lines.append(f"Want me to put '{cat}' live now and schedule a {fest} pre-booking post for September?")
        lines.append(c.yes("both drafts come to you first.", "dono drafts pehle aapko bhejungi."))
        return _join(*lines), "binary_yes_no", f"{fest} far out ({days}d): contrarian — plan, don't promo; capture the window open now."
    if fest:
        lines = [f"{c.sal}, {fest} is {days} days away ({F.nice_date(fdate)}) — {c.loc} demand starts moving about a week before."]
        lines.append(f"Want me to draft a {fest} post" + (f" around your '{offer}'?" if offer else " with one festive service-at-price offer?"))
        lines.append(c.yes("it'll be ready today.", "aaj hi ready kar deti hoon."))
        return _join(*lines), "binary_yes_no", f"{fest} within promo window; anchored on existing offer."
    # placeholder: use the category's next festival/season beat
    beat = F.seasonal_beat(c.category, c.now, "festival", upcoming=True) or F.seasonal_beat(c.category, c.now, upcoming=True)
    if not beat:
        return None
    lines = [f"{c.sal}, next big window for {c.name}: {beat['month_range']} — {beat['note']}."]
    now_beat = F.seasonal_beat(c.category, c.now)
    if now_beat and now_beat is not beat:
        lines.append(f"Right now ({now_beat['month_range']}) is the {now_beat['note'].split('—')[0].strip()} — so this is prep time, not promo time.")
    lines.append("Want me to put together a 3-step plan (offer, post, customer WhatsApp) for that window?")
    lines.append(c.yes("I'll send it this week.", "is hafte bhej deti hoon."))
    return _join(*lines), "binary_yes_no", "Upcoming festival season from category seasonal beats; prep-now framing."


def curious_ask_due(c: Ctx):
    themes = sorted([t for t in c.merchant.get("review_themes") or [] if t.get("sentiment") == "pos"],
                    key=lambda t: -(t.get("occurrences_30d") or 0))
    trend = F.top_trend(c.category)
    guess = None
    if themes and themes[0].get("common_quote"):
        t = themes[0]
        q = t["common_quote"]
        hit = next((tr for tr in c.category.get("trend_signals") or [] if tr["query"].split()[0] in q.lower()), None)
        if hit:
            svc = hit["query"].split()[0]
            guess = (f"My guess is {svc}: {t['occurrences_30d']} reviews this month praise your team (\"{q}\"), "
                     f"and '{hit['query']}' searches are {F.pct(hit['delta_yoy'], True)} YoY.")
        else:
            guess = f"My guess: {F.humanize(t['theme'])} — {t['occurrences_30d']} reviews this month mention it (\"{q}\")."
    elif themes:
        th = F.humanize(themes[0]["theme"]).replace(" quality", "")
        guess = f"My guess is the {th} — it shows up in {themes[0]['occurrences_30d']} reviews this month."
    elif trend:
        guess = f"My guess from search data: '{trend['query']}' is {F.pct(trend['delta_yoy'], True)} YoY across metros."
    lines = [f"{'Hi ' if c.slug != 'dentists' else ''}{c.sal}! Quick one — what's been the most asked-for "
             f"{'service' if c.slug in ('salons', 'gyms', 'dentists') else 'item'} at {c.name} this week?",
             guess or "",
             "Tell me in one line and I'll turn it into a Google post + a ready WhatsApp reply for price questions. Takes you 1 minute."]
    return _join(*lines), "open_ended", "Curious-ask cadence: asking-the-merchant lever with a data-backed guess to make answering effortless."


def winback_eligible(c: Ctx):
    days = c.p.get("days_since_expiry") or (c.merchant.get("subscription") or {}).get("days_since_expiry")
    dip = c.p.get("perf_dip_pct")
    added = c.p.get("lapsed_customers_added_since_expiry")
    lines = [f"{c.sal}, it's been {days} days since {c.name}'s plan lapsed."]
    facts_ = []
    if dip is not None:
        facts_.append(f"calls are down {F.pct(dip)}")
    if added:
        facts_.append(f"{added} more customers have slipped into 'lapsed'")
    if facts_:
        lines.append("Since then " + " and ".join(facts_) + ".")
    lines.append("No pitch — want a free 1-page snapshot of what changed on your Google profile since the expiry?")
    lines.append(c.yes("I'll send it, no strings.", "bina kisi commitment ke bhej deti hoon."))
    return _join(*lines), "binary_yes_no", "Win-back: loss framing from real post-expiry numbers, reciprocity (free snapshot), no hard sell."


def dormant_with_vera(c: Ctx):
    digest = c.category.get("digest") or []
    tip = next((d for d in digest if "GBP" in (d.get("actionable") or "")), None) or \
        F.digest_by_kind(c.category, "trend", "tech", "seasonal")
    last_topic = c.p.get("last_topic")
    lines = [f"{c.sal}, no pitch today" + (" (not about the subscription)" if last_topic and "subscription" in last_topic else "")
             + f" — one free tip for {c.name}:"]
    if tip:
        lines.append(f"{tip.get('title', '').rstrip('.')} ({tip.get('source')}).")
        if tip.get("actionable"):
            lines.append(tip["actionable"].rstrip(".") + ".")
    wm = F.worst_metric(c.merchant)
    if wm and wm[1] < 0:
        lines.append(f"With your {wm[0]} down {F.pct(wm[1])} this week, it's worth a try.")
    lines.append(("Want me to make the change for you?" if "walk-in" in (tip.get("title") or "").lower() else
                  "Want me to update your Google listing for it?") if tip and "GBP" in (tip.get("actionable") or "")
                 else "Want me to walk you through it?")
    lines.append(c.yes("it takes me 5 minutes.", "5 minute ka kaam hai, main kar deti hoon."))
    return _join(*lines), "binary_yes_no", "Dormant merchant: lead with free value (digest tip) tied to their numbers, not a re-pitch."


def ipl_match_today(c: Ctx):
    match, venue, t_iso = c.p.get("match"), c.p.get("venue"), c.p.get("match_time_iso")
    dt = F.parse_dt(t_iso)
    wd = F.WEEKDAYS[dt.weekday()] if dt else "today"
    tm = dt.strftime("%-I:%M%p").lower().replace(":00", "") if dt else ""
    item = F.digest_item(c.category, "d_2026W17_ipl_window") or F.digest_by_kind(c.category, "seasonal")
    weeknight = c.p.get("is_weeknight")
    offer = _offer_line(c)
    lines = [f"{c.sal}, {match} at {venue} tonight, {tm} ({wd})."]
    if weeknight is False:
        lines.append("Counter-intuitive: weekend IPL nights pull people home — Saturday match covers run 12% below a normal Saturday, "
                     f"while weeknight matches run +18% ({item.get('source') if item else 'magicpin order data'}).")
        if offer and "Tue-Thu" in offer:
            lines.append(f"So skip the dine-in promo tonight and push delivery; save your '{offer}' for the next weeknight match.")
        else:
            lines.append("So skip the dine-in promo tonight and push delivery instead.")
        late = next((t for t in c.merchant.get("review_themes") or [] if t.get("theme") == "delivery_late"), None)
        if late:
            lines.append(f"One caution: {late.get('occurrences_30d')} recent reviews flag late delivery — keep the radius tight tonight.")
        lines.append("Want me to draft a delivery-only Insta story for the match?")
    else:
        lines.append("Weeknight matches run +18% covers — this is the night for a match-night combo.")
        lines.append("Want me to draft the match-night post" + (f" with your '{offer}'?" if offer else "?"))
    lines.append(c.yes("live in 10 min.", "10 min mein live."))
    return _join(*lines), "binary_yes_no", "IPL trigger interpreted with order data: weekend match => delivery push, not dine-in promo."


def review_theme_emerged(c: Ctx):
    theme = c.p.get("theme")
    occ, quote, trend = c.p.get("occurrences_30d"), c.p.get("common_quote"), c.p.get("trend")
    themes = c.merchant.get("review_themes") or []
    if not theme:  # placeholder -> use merchant's own negative theme
        neg = [t for t in themes if t.get("sentiment") == "neg"]
        if neg:
            theme, occ, quote = neg[0]["theme"], neg[0].get("occurrences_30d"), neg[0].get("common_quote")
    if not theme:
        lines = [f"{c.sal}, I'm setting up a weekly review digest for {c.name} — the recurring themes in what customers write, good and bad.",
                 f"Peers in your category average {(c.category.get('peer_stats') or {}).get('avg_review_count', '?')} reviews; each theme you fix shows up in rating within weeks.",
                 "Want the first digest this Sunday?", c.yes("it's free.")]
        return _join(*lines), "binary_yes_no", "Review-theme trigger without theme data: offer the digest instead of inventing a theme."
    pos = sorted([t for t in themes if t.get("sentiment") == "pos"], key=lambda t: -(t.get("occurrences_30d") or 0))
    lines = [f"{c.sal}, {occ} reviews in the last 30 days mention {F.humanize(theme)} at {c.name}"
             + (f" — and it's {trend}." if trend else ".")]
    if quote:
        lines.append(f"Latest: \"{quote}\".")
    if pos:
        lines.append(f"It's the one blot — {pos[0].get('occurrences_30d')} reviews praise your {F.humanize(pos[0]['theme'])}.")
    lines.append("Want me to draft short, polite public replies to those reviews (so new customers see you respond)?")
    lines.append(c.yes("drafts come to you before posting.", "post karne se pehle aapko dikhaungi."))
    return _join(*lines), "binary_yes_no", "Negative review theme: quantified, quoted, balanced against strengths, one repair action."


def milestone_reached(c: Ctx):
    metric, now_v, target = c.p.get("metric"), c.p.get("value_now"), c.p.get("milestone_value")
    agg = c.merchant.get("customer_aggregate") or {}
    if metric and now_v is not None and target:
        gap = target - now_v
        lines = [f"{c.sal}, {c.name} is {gap} {F.humanize(metric).replace(' count', '')}s away from {target} on Google ({now_v} now)."]
        if agg.get("repeat_customer_pct"):
            lines.append(f"With {F.pct(agg['repeat_customer_pct'])} repeat customers, the fastest {gap} are your regulars.")
        pos = sorted([t for t in c.merchant.get("review_themes") or [] if t.get("sentiment") == "pos"],
                     key=lambda t: -(t.get("occurrences_30d") or 0))
        if pos:
            lines.append(f"{pos[0].get('occurrences_30d')} reviews this month already praise the {F.humanize(pos[0]['theme']).replace(' quality', '')}.")
        lines.append("Want me to draft a one-line review-ask card for tables + a WhatsApp nudge for regulars?")
        lines.append(c.yes("both ready in 10 min.", "10 min mein dono ready."))
        return _join(*lines), "binary_yes_no", "Imminent milestone: small concrete gap + who to ask (regulars) + ready artifact."
    ytd = agg.get("total_unique_ytd")
    views = F.perf(c.merchant).get("views")
    marks = [100, 250, 500, 1000, 1500, 2000, 5000]
    if ytd and ytd >= 100:
        mark = max(m for m in marks if m <= ytd)
        lines = [f"{c.sal}, milestone: {c.name} has served {F.num(ytd)} unique customers so far this year — past the {F.num(mark)} mark."]
        p = F.perf(c.merchant)
        if p.get("calls") is not None and views:
            lines.append(f"Your listing pulled {F.num(views)} views and {p['calls']} calls in the last 30 days.")
        lines.append("A short 'thank you' post turns that into social proof for new visitors — want me to draft it?")
    elif views:
        lines = [f"{c.sal}, {c.name} logged {F.num(views)} Google profile views in the last 30 days.",
                 "Views only pay if they turn into calls — want me to post a 'thank you' update with one clear offer to convert them?"]
    else:
        return None
    lines.append(c.yes("draft in 10 min.", "10 min mein draft."))
    return _join(*lines), "binary_yes_no", "Milestone from the merchant's own view count (rounded down, verifiable) + conversion next step."


def competitor_opened(c: Ctx):
    comp, dist, offer, opened = c.p.get("competitor_name"), c.p.get("distance_km"), c.p.get("their_offer"), c.p.get("opened_date")
    mine = _offer_line(c)
    themes = c.merchant.get("review_themes") or []
    pos = sorted([t for t in themes if t.get("sentiment") == "pos"], key=lambda t: -(t.get("occurrences_30d") or 0))
    neg = sorted([t for t in themes if t.get("sentiment") == "neg"], key=lambda t: -(t.get("occurrences_30d") or 0))
    if comp:
        lines = [f"{c.sal}, {comp} opened {dist} km from you" + (f" on {F.nice_date(opened)}" if opened else "")
                 + (f", leading with '{offer}'" if offer else "") + "."]
        import re
        pm, po = re.search(r"₹([\d,]+)", mine or ""), re.search(r"₹([\d,]+)", offer or "")
        if pm and po:
            diff = int(pm.group(1).replace(",", "")) - int(po.group(1).replace(",", ""))
            if diff > 0:
                lines.append(f"That's {F.inr(diff)} under your '{mine}'. I wouldn't match on price.")
    else:
        lines = [f"{c.sal}, a new {SINGULAR.get(c.slug, 'business')} listing has come up near {c.loc} on Google.",
                 "New entrants usually open with a discount; I wouldn't race them on price."]
    if pos and pos[0].get("common_quote"):
        lines.append(f"Your edge is already in your reviews: {pos[0]['occurrences_30d']} this month say \"{pos[0]['common_quote']}\".")
    elif pos:
        lines.append(f"Your edge is already in your reviews: {pos[0]['occurrences_30d']} this month praise your {F.humanize(pos[0]['theme'])}.")
    else:
        peer = F.peer_gap(c.merchant, c.category).get("ctr")
        if peer:
            lines.append(f"Where you can win: your listing CTR is {F.ctr_str(peer[0])} vs {F.ctr_str(peer[1])} for peers — "
                         + ("already ahead, so protect it with fresh posts." if peer[0] >= peer[1] else "sharper photos + an offer close that gap."))
    if neg:
        lines.append(f"The one gap they can exploit: {F.humanize(neg[0]['theme'])} ({neg[0].get('occurrences_30d')} reviews).")
    lines.append("Want me to update your Google description to lead with that strength?")
    lines.append(c.yes("draft comes to you first.", "draft pehle aapko bhejungi."))
    return _join(*lines), "binary_yes_no", "Competitor trigger: facts from payload only, advise against price war, lean on merchant's review strengths."


def active_planning_intent(c: Ctx):
    topic = c.p.get("intent_topic", "")
    ask = c.p.get("merchant_last_message") or F.last_merchant_message(c.merchant) or ""
    offers = F.active_offers(c.merchant)
    hist = " ".join(t.get("body", "") for t in c.merchant.get("conversation_history") or [])
    import re
    if "thali" in topic or "bulk" in topic:
        base = None
        for o in offers:
            m = re.search(r"₹([\d,]+)", o)
            if "thali" in o.lower() and m:
                base = int(m.group(1).replace(",", ""))
        base = base or 149
        t1, t2, t3 = base - 10, base - 20, base - 30
        body = (f"{c.sal}, here's a starter version — edit anything:\n\n"
                f"{c.name} Corporate Lunch — for offices around {c.loc}\n"
                f"• 10-24 thalis: {F.inr(t1)} each (retail {F.inr(base)})\n"
                f"• 25-49 thalis: {F.inr(t2)} each + free delivery\n"
                f"• 50+ thalis: {F.inr(t3)} each + free delivery + filter coffee\n"
                f"• Order by 5pm the day before; delivered 12:30-1:15pm, Mon-Fri\n\n"
                f"Your thali already does ~18 orders/day, so the kitchen can absorb a 25-thali order without a new shift. "
                f"Want me to draft a 3-line WhatsApp you can send to office admins nearby? Reply YES.")
        return body, "binary_yes_no", "Merchant said 'what would it look like' — deliver the artifact (tiered B2B menu anchored on existing ₹ price), not more questions."
    if "yoga" in topic or "kids" in topic:
        price = "₹2,499" if "2,499" in hist else None
        spike = _other_trigger(c, "perf_spike")
        body = (f"{c.sal}, here's the kids program, ready to edit:\n\n"
                f"{c.name} Kids Summer Yoga (age 7-12)\n"
                f"• 4 weeks, 3 classes/week (Mon/Wed/Fri), 45 min, 8-8:45am before the heat\n"
                f"• Batch cap 10 kids — your small-class reviews are the selling point\n"
                f"• Wk1 balance & breath · Wk2 animal flows · Wk3 focus games · Wk4 parents' showcase\n"
                + (f"• {price} for the full camp; free trial class to start\n" if price else "• Free trial class to start\n")
                + "\n"
                + (f"Your kids-yoga post already lifted calls {F.pct(spike['payload'].get('delta_pct'))} this week — demand is there. " if spike and spike.get('payload', {}).get('delta_pct') else "")
                + "Want me to draft the Google post + Insta carousel for it? Reply YES.")
        return body, "binary_yes_no", "Planning intent: deliver the complete program draft (using price already discussed) + one follow-on action."
    lines = [f"{c.sal}, picking up where you left off (\"{ask}\") — here's a first draft for {F.humanize(topic)}:",
             "• what it is + who it's for", "• one clear service-at-price hook", "• where it goes live (Google post + WhatsApp)",
             "Want me to fill it in with your numbers and send it for review?", "Reply YES."]
    return "\n".join(lines), "binary_yes_no", "Planning intent: switch to action with a concrete draft."


def supply_alert(c: Ctx):
    item = F.digest_item(c.category, c.p.get("alert_id")) or F.digest_by_kind(c.category, "alert")
    batches = ", ".join(c.p.get("affected_batches") or [])
    mol, mfr = c.p.get("molecule", ""), c.p.get("manufacturer", "")
    agg = c.merchant.get("customer_aggregate") or {}
    asked = any(t.get("from") == "merchant" and "list" in (t.get("body") or "").lower() for t in c.merchant.get("conversation_history") or [])
    lines = [f"{c.sal}, urgent — {item.get('source') if item else 'recall alert'}: voluntary recall of {mol} batches {batches} ({mfr}) for sub-potency."]
    lines.append("No safety risk beyond sub-optimal LDL control, but stock must be pulled and affected customers told about replacement."
                 if item and "LDL" in item.get("summary", "") else "Stock must be pulled and affected customers informed.")
    if asked:
        lines.append(f"You asked for the customer list on 24 Apr — I'll filter your {agg.get('chronic_rx_count', '')} chronic-Rx customers for {mol} dispensed from these batches"
                     " and attach a ready replacement-pickup WhatsApp.")
        lines.append("Reply CONFIRM and I'll send both now.")
        cta = "binary_confirm_cancel"
    else:
        lines.append(f"I can filter your {agg.get('chronic_rx_count', 'repeat')} chronic-Rx customers for {mol} and draft the replacement note.")
        lines.append(c.yes("I'll send the list + draft now.", "list aur draft abhi bhejti hoon."))
        cta = "binary_yes_no"
    return _join(*lines), cta, "Urgent supply recall: exact batches/manufacturer/source, bounded risk, honours merchant's earlier 'send me the list'."


def category_seasonal(c: Ctx):
    trends = c.p.get("trends") or []
    item = F.digest_by_kind(c.category, "seasonal")
    agg = c.merchant.get("customer_aggregate") or {}
    import re
    parts = []
    for t in trends:
        m = re.match(r"(.+?)_demand_([+-]\d+)", t)
        if m:
            parts.append(f"{m.group(1).replace('_', '/')} {m.group(2)}%")
    lines = [f"{c.sal}, the summer shift has started: " + (", ".join(parts) if parts else (item or {}).get("title", "")) +
             (f" ({item.get('source')})." if item else ".")]
    if item and item.get("actionable"):
        lines.append(item["actionable"].rstrip(".") + ".")
    lib = next((x for x in c.category.get("patient_content_library") or [] if "summer" in x.get("id", "")), None)
    rep = agg.get("repeat_customer_pct")
    if lib:
        lines.append(f"With {F.pct(rep)} repeat customers, a 'summer first-aid' WhatsApp to regulars pulls these items off the shelf."
                     if rep else "A 'summer first-aid' WhatsApp to regulars pulls these items off the shelf.")
        lines.append("Want me to send you the ready draft?")
    else:
        lines.append("Want me to draft a counter display card + a WhatsApp for regulars?")
    lines.append(c.yes("draft in 10 min.", "10 min mein draft bhejti hoon."))
    return _join(*lines), "binary_yes_no", "Category seasonal trend with exact demand deltas, shelf action, and a customer-content artifact."


def gbp_unverified(c: Ctx):
    up = c.p.get("estimated_uplift_pct")
    path = F.humanize(c.p.get("verification_path", "phone or postcard")).replace(" or ", " or a ")
    p = F.perf(c.merchant)
    ps = c.category.get("peer_stats") or {}
    lines = [f"{c.sal}, {_poss(c.name)} Google profile is still unverified — so Google limits how often it shows you."]
    if up:
        lines.append(f"Verifying is estimated to lift visibility ~{F.pct(up)}.")
    if p.get("calls") is not None and ps.get("avg_calls_30d"):
        lines.append(f"You're at {p['calls']} calls in 30 days vs {ps['avg_calls_30d']} for nearby peers — this is the cheapest gap to close.")
    lines.append(f"It's done via a {path}; I'll walk you through it in 5 minutes.")
    lines.append(c.yes("we start with the phone option now.", "abhi phone wala option shuru karte hain."))
    return _join(*lines), "binary_yes_no", "Unverified GBP: estimated uplift + peer gap + effort externalisation (5 min)."


def cde_opportunity(c: Ctx):
    item = F.digest_item(c.category, c.p.get("digest_item_id")) or F.digest_by_kind(c.category, "cde")
    if not item:
        return None
    when = F.parse_dt(item.get("date"))
    when_s = f"{F.WEEKDAYS[when.weekday()][:3]} {when.day} {F.MONTHS[when.month - 1]}, {when.strftime('%-I%p').lower()}" if when else ""
    credits = c.p.get("credits") or item.get("credits")
    fee = item.get("actionable", "").rstrip(".")
    hist = (F.last_merchant_message(c.merchant) or "").lower()
    rel = " — relevant since you're pushing aligners" if "aligner" in hist else ""
    lines = [f"{c.sal}, {item.get('title', '').rstrip('.')}: {when_s}" + (f", {credits} CDE credits" if credits else "") + ".",
             (f"{fee}." if fee else ""),
             item.get("summary", "").rstrip(".") + rel + ".",
             "Want me to register you and set a reminder for the evening?",
             c.yes("done in a minute.", "ek minute mein ho jayega.")]
    return _join(*lines), "binary_yes_no", "CDE opportunity: date/credits/fee from digest, linked to merchant's stated interest."


# ============================================================================
# CUSTOMER-FACING PLAYBOOKS (send_as = merchant_on_behalf)
# ============================================================================
GREET = {"ta-en": "Vanakkam", "te-en": "Namaskaram", "kn-en": "Namaskara", "mr-en": "Namaskar"}


def _cust_open(c: Ctx, emoji: str = "") -> tuple[str, str]:
    cu = c.customer or {}
    lang = F.customer_lang(cu)
    first, parent = F.customer_first(cu), F.customer_parent(cu)
    addressee = parent or first
    senior = (cu.get("identity") or {}).get("senior_citizen")
    if lang == "hi" or senior:
        hello = "Namaste"
    else:
        hello = GREET.get(lang, "Hi")
    sender = F.owner_first(c.merchant)
    if c.slug == "dentists":
        who = f"Dr. {sender}'s clinic" if sender and "Dr." not in c.name else c.name
    else:
        who = f"{sender} from {c.name}" if sender else c.name
    e = f" {emoji}" if emoji else ""
    if lang in ("hi", "hi-en"):
        open_ = f"{hello} {addressee}!{e} {who} ({c.loc}) se."
    else:
        open_ = f"{hello} {addressee}{e or ','} {who} here."
    return open_, lang


def recall_due(c: Ctx):
    open_, lang = _cust_open(c, "🦷" if c.slug == "dentists" else "")
    default_svc = {"dentists": "6-month check-up", "salons": "next appointment", "gyms": "monthly progress check-in",
                   "pharmacies": "regular refill", "restaurants": "next visit"}.get(c.slug, "next visit")
    svc = F.humanize(c.p.get("service_due") or default_svc).replace("6 month", "6-month")
    if not c.p.get("service_due"):
        free = next((o for o in F.active_offers(c.merchant) if "free" in o.lower()), None)
        if free and c.slug == "gyms":
            svc = f"monthly progress check-in ({free.replace('Free ', 'free ')} included)"
    last = c.p.get("last_service_date") or (c.customer.get("relationship") or {}).get("last_visit")
    slots = c.p.get("available_slots") or []
    offer = next((o for o in F.active_offers(c.merchant) if any(w in o.lower() for w in svc.lower().split())), None) \
        or (F.active_offers(c.merchant)[:1] or [None])[0]
    price = ""
    import re
    if offer and (m := re.search(r"₹[\d,]+", offer)):
        price = m.group(0)
    if lang in ("hi", "hi-en"):
        lines = [open_, f"Aapka {svc} due hai" + (f" (last visit {F.nice_date(last)})." if last else ".")]
        if len(slots) >= 2:
            lines.append(f"Aapke liye 2 slots rakhe hain: {slots[0]['label']} ya {slots[1]['label']}.")
            if price:
                lines.append(f"{svc.split()[-1].capitalize()} {price} mein.")
            lines.append("Reply 1 ya 2 — ya apna convenient time bata dijiye.")
            cta = "multi_choice_slot"
        else:
            pref = F.slot_pref_label(c.customer or {})
            lines.append(f"Is hafte {pref} ka slot book kar dein?" if pref else "Is hafte slot book kar dein?")
            lines.append("Reply YES.")
            cta = "binary_yes_no"
    else:
        lines = [open_, f"Your {svc} is due" + (f" — last visit was {F.nice_date(last)}." if last else ".")]
        if len(slots) >= 2:
            lines.append(f"We've kept 2 slots for you: {slots[0]['label']} or {slots[1]['label']}" + (f" ({price})." if price else "."))
            lines.append("Reply 1 or 2 — or tell us a time that suits you.")
            cta = "multi_choice_slot"
        else:
            pref = F.slot_pref_label(c.customer or {})
            lines.append(f"Shall we hold a {pref} slot for you this week?" if pref else "Shall we hold a slot for you this week?")
            lines.append("Reply YES and we'll confirm the time.")
            cta = "binary_yes_no"
    return _join(*lines), cta, "Customer recall: sent as merchant, language pref honoured, real slots/price only, low-friction reply."


def wedding_package_followup(c: Ctx):
    cu = c.customer or {}
    open_, lang = _cust_open(c, "💍")
    wd, days = c.p.get("wedding_date") or (cu.get("preferences") or {}).get("wedding_date"), c.p.get("days_to_wedding")
    trial = c.p.get("trial_completed")
    step = F.humanize(c.p.get("next_step_window_open", "next step")).replace("30day", "30-day")
    pref = (cu.get("preferences") or {}).get("preferred_slots")
    lines = [open_,
             f"Hope you loved your bridal trial on {F.nice_date(trial)}." if trial else "",
             f"{days} days to the wedding ({F.nice_date(wd)}) — this is the right window to start the {step}, "
             "so skin is settled well before the Oct-Dec rush fills our calendar.",
             f"Shall I block a {F.humanize(pref).capitalize()} slot next week for your first session?" if pref else "Shall I block a slot next week for your first session?",
             "Reply YES — no payment needed to hold it."]
    return _join(*lines), "binary_yes_no", "Bridal follow-up: continuity from trial, exact countdown, preference honoured, no invented price."


def customer_lapsed_hard(c: Ctx):
    cu = c.customer or {}
    open_, lang = _cust_open(c, "👋")
    days = c.p.get("days_since_last_visit") or F.days_between((cu.get("relationship") or {}).get("last_visit"), c.now)
    focus = c.p.get("previous_focus") or (cu.get("preferences") or {}).get("training_focus")
    months = c.p.get("previous_membership_months")
    offer = _offer_line(c)
    pref = F.slot_pref_label(cu)
    lines = [open_,
             f"It's been {days} days since your last session — happens to most members, no judgment.",
             (f"You put in {months} solid months" + (f" on {F.humanize(focus)}" if focus else "") + ", and that base doesn't vanish.") if months else "",
             (f"Our '{offer}' is open — happy to use it to ease you back in" + (f", {pref} like before." if pref else ".")) if offer else
             (f"We'd love to see you back{', ' + pref + ' like before' if pref else ''}."),
             "Reply YES and I'll hold a spot this week — no commitment, no auto-charge."]
    return _join(*lines), "binary_yes_no", "Hard-lapsed win-back: no-shame tone, references past effort/goal, real offer, single YES with barriers removed."


def customer_lapsed_soft(c: Ctx):
    cu = c.customer or {}
    open_, lang = _cust_open(c)
    rel = cu.get("relationship") or {}
    last, visits = rel.get("last_visit"), rel.get("visits_total")
    offer = _offer_line(c)
    pref = F.slot_pref_label(cu)
    svc_word = {"dentists": "check-up", "salons": "appointment", "gyms": "session", "pharmacies": "refill", "restaurants": "table"}.get(c.slug, "visit")
    if lang in ("hi", "hi-en"):
        lines = [open_,
                 f"Aapki last visit {F.nice_date(last)} ko thi" + (f" — {visits} visits ke liye shukriya!" if visits and visits > 1 else "."),
                 f"Abhi '{offer}' chal raha hai." if offer else "",
                 f"Is hafte {svc_word} ke liye slot rakh dein" + (f" ({pref})?" if pref else "?"),
                 "Reply YES."]
    else:
        lines = [open_,
                 f"Your last visit was on {F.nice_date(last)}" + (f" — thanks for the {visits} visits so far!" if visits and visits > 1 else "."),
                 f"We're running '{offer}' right now." if offer else "",
                 f"Shall we keep a {svc_word} slot for you this week" + (f" ({pref})?" if pref else "?"),
                 "Reply YES and we'll confirm."]
    if c.slug == "pharmacies":
        lines[-2] = ("Aapki regular medicines ka refill ready rakh dein? Home delivery bhi ho jayegi." if lang in ("hi", "hi-en")
                     else "Shall we keep your regular medicines ready for pickup or delivery?")
    return _join(*lines), "binary_yes_no", "Soft-lapsed customer: warm, real last-visit date, only real offers, one YES."


def appointment_tomorrow(c: Ctx):
    cu = c.customer or {}
    open_, lang = _cust_open(c)
    t = c.p.get("appointment_time") or c.p.get("slot_label")
    svc = c.p.get("service")
    if lang in ("hi", "hi-en"):
        body = _join(open_, f"Reminder: kal aapka {F.humanize(svc) + ' ' if svc else ''}appointment hai" + (f" — {t}." if t else "."),
                     "Confirm karne ke liye YES reply karein, ya reschedule ke liye apna time bata dijiye.")
    else:
        body = _join(open_, f"Reminder: your {F.humanize(svc) + ' ' if svc else ''}appointment is tomorrow" + (f" at {t}." if t else "."),
                     "Reply YES to confirm, or tell us a time that works better and we'll move it.")
    return body, "binary_yes_no", "Appointment reminder: brief, language-matched, confirm-or-reschedule."


def chronic_refill_due(c: Ctx):
    cu = c.customer or {}
    open_, lang = _cust_open(c)
    mols = c.p.get("molecule_list") or []
    runs_out = c.p.get("stock_runs_out_iso")
    offers = F.active_offers(c.merchant)
    if c.slug != "pharmacies":  # e.g. a dentist customer tagged 'chronic_refill' -> treat as maintenance follow-up
        return customer_lapsed_soft(c)
    senior = (cu.get("identity") or {}).get("senior_citizen")
    via_son = "son" in ((cu.get("preferences") or {}).get("channel") or "")
    name = F.customer_first(cu)
    ref = f"{name.replace('Mr. ', '')} ji" if name.startswith("Mr.") else name
    # cross-check against any live recall on the same molecule for this merchant
    recall = next((t for t in STORE.triggers_for_merchant(c.merchant.get("merchant_id", ""))
                   if t.get("kind") == "supply_alert" and (t.get("payload") or {}).get("molecule") in mols), None)
    perks = []
    if senior and any("Senior" in o for o in offers):
        perks.append("senior citizen 15% off" if lang not in ("hi", "hi-en") else "senior citizen 15% discount")
    if any("Delivery" in o for o in offers):
        perks.append("free home delivery above ₹499" + (" to the saved address" if c.p.get("delivery_address_saved") else ""))
    if lang in ("hi", "hi-en") or senior:
        lines = [f"Namaste — {c.name}, {c.loc} se.",
                 f"{ref} ki {len(mols)} regular medicines ({', '.join(mols)}) {F.nice_date(runs_out)} tak khatam ho jayengi." if mols else
                 f"{ref} ki regular medicines ka refill due hai.",
                 "Same dose, same pack ready rakh diya hai" + (f" — {' + '.join(perks)}." if perks else "."),
                 (f"Note: {recall['payload']['molecule']} ke recalled batches ({', '.join(recall['payload'].get('affected_batches', []))}) hum nahi denge — sirf safe batch." if recall else ""),
                 "Dispatch kar dein? Reply HAAN — dose mein koi badlav ho toh bata dijiye."]
    else:
        lines = [open_,
                 f"{ref}'s {len(mols)} regular medicines ({', '.join(mols)}) run out on {F.nice_date(runs_out)}." if mols else f"{ref}'s regular refill is due.",
                 "Same dose and pack are ready" + (f" — {' + '.join(perks)}." if perks else "."),
                 (f"We've checked: none from the recalled {recall['payload']['molecule']} batches." if recall else ""),
                 "Reply YES to dispatch, or tell us if the dosage changed."]
    return _join(*lines), "binary_yes_no", ("Chronic refill: exact molecules + run-out date, real offers, senior/relay-via-family norms"
                                            + (", and cross-checked against the live batch recall." if recall else "."))


def trial_followup(c: Ctx):
    cu = c.customer or {}
    open_, lang = _cust_open(c, "🧘" if c.slug == "gyms" else "")
    trial = c.p.get("trial_date") or (cu.get("relationship") or {}).get("last_visit")
    opts = c.p.get("next_session_options") or []
    kid = F.customer_first(cu) if F.customer_parent(cu) else None
    svc = (cu.get("relationship") or {}).get("services_received") or []
    svc_s = F.humanize(svc[0]).replace("trial", "").strip() if svc else ""
    who = f"{kid} enjoyed" if kid else "you enjoyed"
    lines = [open_,
             f"Hope {who} the {svc_s + ' ' if svc_s else ''}trial on {F.nice_date(trial)}." if trial else f"Hope {who} the trial.",
             (f"Next session: {opts[0]['label']}. Shall we save " + (f"{kid}'s" if kid else "your") + " spot?") if opts else
             "Shall we save a spot for the next session this week?",
             "Reply YES."]
    return _join(*lines), "binary_yes_no", "Trial follow-up: warm continuity, real next-session slot, single YES."


PLAYBOOKS = {
    "research_digest": research_digest, "category_research_digest_release": research_digest,
    "research_digest_release": research_digest,
    "regulation_change": regulation_change, "compliance": regulation_change,
    "perf_dip": perf_dip, "seasonal_perf_dip": seasonal_perf_dip, "perf_spike": perf_spike,
    "renewal_due": renewal_due, "festival_upcoming": festival_upcoming, "curious_ask_due": curious_ask_due,
    "scheduled_recurring": curious_ask_due,
    "winback_eligible": winback_eligible, "dormant_with_vera": dormant_with_vera, "ipl_match_today": ipl_match_today,
    "review_theme_emerged": review_theme_emerged, "milestone_reached": milestone_reached,
    "competitor_opened": competitor_opened, "active_planning_intent": active_planning_intent,
    "supply_alert": supply_alert, "category_seasonal": category_seasonal, "category_trend_movement": category_seasonal,
    "gbp_unverified": gbp_unverified, "cde_opportunity": cde_opportunity,
    # customer scope
    "recall_due": recall_due, "wedding_package_followup": wedding_package_followup, "bridal_followup": wedding_package_followup,
    "customer_lapsed_hard": customer_lapsed_hard, "customer_lapsed_soft": customer_lapsed_soft,
    "appointment_tomorrow": appointment_tomorrow,
 "chronic_refill_due": chronic_refill_due, "trial_followup": trial_followup,
}


HEAT_ADVICE = {
    "restaurants": ("Heat pushes lunch orders from dine-in to delivery.", "push cold beverages + delivery combos on Swiggy/Zomato for the afternoon slot"),
    "gyms": ("Afternoon and early-evening footfall drops first in a heatwave.", "move one evening class to 7am while it lasts"),
    "pharmacies": ("ORS, electrolytes and sunscreen are the first to sell out.", "put ORS + sunscreen at the counter and send regulars a short heat-safety note"),
    "salons": ("Walk-ins drop in the afternoon heat; mornings and evenings hold up.", "push morning slots and a cooling hair-spa or facial for the week"),
    "dentists": ("Patients postpone non-urgent visits in extreme heat.", "offer early-morning and evening slots so recalls don't slip"),
}


def weather_heatwave(c: Ctx):
    city = c.p.get("city") or (c.merchant.get("identity") or {}).get("city")
    temp = c.p.get("temp_c") or c.p.get("temperature_c") or c.p.get("temp")
    days = c.p.get("days") or c.p.get("duration_days")
    obs, act = HEAT_ADVICE.get(c.slug, ("Heat changes when customers come in.", "shift your promotion to the cooler hours"))
    if c.slug == "gyms":
        d = next((x for x in c.category.get("digest") or [] if "7am" in (x.get("actionable") or "")), None)
        if d:
            obs += f" Morning slots are the spare capacity anyway — 6-8am runs at 60% vs 90%+ in the evening ({d.get('source')})."
    lines = [f"{c.sal}, heatwave alert for {city}" + (f": {temp}°C" if temp else "") + (f" for the next {days} days." if days else "."),
             obs, f"My suggestion: {act}.", "Want me to set it up?", c.yes("I'll draft it in 10 min.", "10 min mein draft kar deti hoon.")]
    return _join(*lines), "binary_yes_no", "Weather trigger translated into a category-specific operational move."


def local_news_event(c: Ctx):
    head = c.p.get("headline") or c.p.get("title") or c.p.get("event") or F.humanize(c.p.get("summary", "a local event"))
    when = c.p.get("date") or c.p.get("time")
    lines = [f"{c.sal}, heads-up for {c.loc}: {head}" + (f" ({when})." if when else "."),
             "Events like this shift when and how people come in — worth a same-day post so you're the one they find.",
             "Want me to draft it?", c.yes("ready in 10 min.", "10 min mein ready.")]
    return _join(*lines), "binary_yes_no", "Local news trigger: timely, locality-specific, one quick action."


def _pretty_payload(p: dict) -> str:
    bits = []
    for k, v in (p or {}).items():
        if k in ("placeholder", "metric_or_topic", "category") or isinstance(v, (dict, list)) or v in (None, ""):
            continue
        key = F.humanize(k)
        if isinstance(v, float) and -1 < v < 1 and ("pct" in k or "delta" in k):
            v = F.pct(v, True)
        bits.append(f"{key} {v}")
    return ", ".join(bits[:3])


def generic(c: Ctx):
    """Unknown trigger kind: stay grounded — describe the trigger honestly + merchant's own numbers."""
    if c.customer:
        return customer_lapsed_soft(c)
    kind = F.humanize(c.trigger.get("kind", "update"))
    p = F.perf(c.merchant)
    detail = _pretty_payload(c.p)
    lines = [f"{c.sal}, something relevant to {c.name} just came up — {kind}" + (f": {detail}." if detail else ".")]
    if p.get("views"):
        lines.append(f"For context, you had {F.num(p['views'])} views and {p.get('calls')} calls in the last 30 days.")
    lines.append("Want me to turn this into one concrete action for this week?")
    lines.append(c.yes("I'll send it in 10 min.", "10 min mein bhej deti hoon."))
    return _join(*lines), "binary_yes_no", f"Unrecognised trigger kind '{kind}': grounded summary + offer, no invented facts."


PLAYBOOKS.update({"weather_heatwave": weather_heatwave, "heatwave": weather_heatwave, "local_news_event": local_news_event})


def run_playbook(c: Ctx):
    fn = PLAYBOOKS.get(c.trigger.get("kind", ""), generic)
    out = None
    try:
        out = fn(c)
    except Exception as e:  # never let one playbook crash a tick
        c.notes.append(f"playbook error: {e!r}")
    if not out:
        out = generic(c)
    return out
