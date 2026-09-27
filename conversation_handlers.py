"""Optional multi-turn handler (brief §7.4). Wraps the same engine the HTTP bot uses.

    respond(state, merchant_message) -> {"action": "send"|"wait"|"end", "body"?, "cta"?, "rationale"}
state = {"conversation_id", "merchant_id", "customer_id"?, "from_role"?}
"""
import asyncio

from vera.conversation import handle_reply


def respond(state: dict, merchant_message: str) -> dict:
    req = {**state, "message": merchant_message, "from_role": state.get("from_role", "merchant")}
    return asyncio.run(handle_reply(req))
