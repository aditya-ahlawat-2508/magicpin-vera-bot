"""compose(category, merchant, trigger, customer) -> message dict.

Pipeline:
  1. deterministic playbook for the trigger kind (always grounded, always available)
  2. optional LLM polish of that draft (only if LLM configured)
  3. validator: grounding (numbers must exist in contexts), taboos, URLs, CTA,
     length, name present.  If the LLM draft fails any check -> keep playbook draft.
"""
from __future__ import annotations

import re

from . import facts as F
from .playbooks import Ctx, run_playbook

URL_RE = re.compile(r"(https?://|www\.)\S+", re.I)


def _taboos(category: dict) -> list[str]:
    v = (category or {}).get("voice") or {}
    raw = (v.get("vocab_taboo") or []) + (v.get("taboos") or [])
    return [re.sub(r"\s*\(.*\)", "", t).strip().lower() for t in raw if t]


def validate(body: str, c: Ctx, reference: str | None = None) -> list[str]:
    """Return a list of problems (empty == OK)."""
    problems = []
    if not body or not body.strip():
        return ["empty body"]
    if URL_RE.search(body):
        problems.append("contains URL")
    low = body.lower()
    for t in _taboos(c.category):
        if t and t in low:
            problems.append(f"taboo word: {t}")
    allowed = F.allowed_numbers(c.category, c.merchant, c.trigger, c.customer or {}, c.now)
    if reference:
        allowed |= F.numbers_in(reference)
    bad = [n for n in F.numbers_in(body) if n not in allowed]
    if bad:
        problems.append(f"ungrounded numbers: {bad[:5]}")
    if len(body) > 900:
        problems.append("too long")
    if c.customer is None:
        first = F.owner_first(c.merchant)
        if first and first.lower() not in low:
            problems.append("missing owner name")
    return problems


def _template_name(kind: str, scope: str) -> str:
    return f"{'merchant' if scope == 'customer' else 'vera'}_{kind}_v1"


def build_ctx(category, merchant, trigger, customer=None, now=None) -> Ctx:
    return Ctx(category=category or {}, merchant=merchant or {}, trigger=trigger or {},
               customer=customer, now=now or F.DEFAULT_NOW)


def compose_draft(c: Ctx) -> dict:
    body, cta, rationale = run_playbook(c)
    scope = "customer" if c.customer else "merchant"
    return {
        "body": body.strip(),
        "cta": cta,
        "send_as": "merchant_on_behalf" if scope == "customer" else "vera",
        "suppression_key": c.trigger.get("suppression_key") or f"{c.trigger.get('kind')}:{c.merchant.get('merchant_id')}",
        "rationale": rationale,
        "template_name": _template_name(c.trigger.get("kind", "generic"), scope),
        "template_params": _template_params(c, body),
    }


def _template_params(c: Ctx, body: str) -> list[str]:
    """First touch must go as a pre-approved template: {{1}} name, {{2}} core, {{3}} ask."""
    who = F.customer_first(c.customer) if c.customer else c.sal
    sentences = re.split(r"(?<=[.?!])\s+", body.strip())
    ask = sentences[-1] if sentences else ""
    core = " ".join(sentences[1:-1]) if len(sentences) > 2 else (sentences[0] if sentences else "")
    return [who, core, ask]


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None, now: str | None = None) -> dict:
    """Pure, deterministic composition (the function required by the brief)."""
    c = build_ctx(category, merchant, trigger, customer, now)
    return compose_draft(c)


# ============================================================================
# LLM layer (optional) — used for unknown trigger kinds, or to polish when
# VERA_LLM_POLISH=1.  Output must pass validate(); otherwise the playbook wins.
# ============================================================================
import json as _json
import os as _os

from . import llm as _llm
from .playbooks import PLAYBOOKS as _PLAYBOOKS

SYSTEM_PROMPT = """You are Vera, magicpin's merchant-growth assistant on WhatsApp (India).
Write ONE WhatsApp message. Hard rules:
- Use ONLY facts present in the CONTEXT JSON. Every number, date, name, price, source must appear there. Never invent competitors, prices, research, slots or offers.
- Open with the salutation given (no "I hope you're well", no self-introduction).
- First sentence states WHY NOW (the trigger). Anchor on at least one verifiable number/date/source.
- Match the category voice (tone, allowed vocab; never use taboo words). Clinical categories = peer tone, no hype.
- If the recipient speaks Hindi, a light natural Hindi-English mix in the closing line is good; customers with language_pref 'hi' or 'hi-en mix' get Hinglish.
- Add one piece of judgment the data supports (what to do / what NOT to do), not just a restatement.
- Exactly ONE call-to-action, in the last sentence, low-friction (e.g. "Reply YES ..."). No URLs. Under 600 characters.
Return JSON only: {"body": "...", "rationale": "one sentence on why this message, why now"}"""


def _compact(c) -> dict:
    cat = {k: c.category.get(k) for k in ("slug", "voice", "peer_stats", "offer_catalog", "seasonal_beats") if k in c.category}
    cat["digest"] = [{k: d.get(k) for k in ("id", "kind", "title", "source", "summary", "actionable", "trial_n", "date", "credits")}
                     for d in c.category.get("digest") or []]
    m = {k: v for k, v in c.merchant.items()}
    return {"now": c.now, "salutation": c.sal, "category": cat, "merchant": m, "trigger": c.trigger,
            "customer": c.customer, "send_as": "merchant_on_behalf" if c.customer else "vera"}


async def compose_action_async(c, force_llm: bool = False) -> dict:
    draft = compose_draft(c)
    kind = c.trigger.get("kind", "")
    use_llm = _llm.llm_available() and (force_llm or kind not in _PLAYBOOKS or _os.getenv("VERA_LLM_POLISH") == "1")
    if not use_llm:
        return draft
    user = ("CONTEXT:\n" + _json.dumps(_compact(c), ensure_ascii=False, default=str)[:24000] +
            "\n\nREFERENCE DRAFT (grounded; improve it, keep every fact correct, keep one CTA):\n" + draft["body"])
    out = _llm.parse_json(await _llm.complete(SYSTEM_PROMPT, user, timeout=float(_os.getenv("LLM_TIMEOUT", "12"))))
    if not out or not out.get("body"):
        return draft
    body = str(out["body"]).strip()
    if validate(body, c, reference=draft["body"]):
        return draft
    new = dict(draft)
    new["body"] = body
    new["rationale"] = str(out.get("rationale") or draft["rationale"])[:300]
    new["template_params"] = _template_params(c, body)
    return new
