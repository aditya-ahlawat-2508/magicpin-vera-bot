"""Submission entry point required by the brief (§7.1).

    from bot import compose
    compose(category, merchant, trigger, customer=None) -> {body, cta, send_as, suppression_key, rationale}

Deterministic (no randomness, no LLM needed), < 5 ms per call.
"""
from vera.compose import compose as _compose


def compose(category: dict, merchant: dict, trigger: dict, customer: dict | None = None) -> dict:
    out = _compose(category, merchant, trigger, customer)
    return {k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale", "template_name", "template_params")}
