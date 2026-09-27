"""Vera challenge bot — HTTP surface.

Run:  uvicorn app:app --host 0.0.0.0 --port 8080 --workers 1
(ONE worker: state lives in memory.)
"""
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import glob
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from vera import facts as F
from vera import llm
from vera.compose import build_ctx, compose_action_async, compose_draft
from vera.conversation import handle_reply
from vera.store import STORE, VALID_SCOPES

app = FastAPI(title="Vera — magicpin AI Challenge bot")

MAX_ACTIONS = 20
TICK_BUDGET_S = float(os.getenv("TICK_BUDGET_S", "22"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ health / meta
@app.get("/v1/healthz")
async def healthz():
    return {"status": "ok", "uptime_seconds": int(time.time() - STORE.started), "contexts_loaded": STORE.counts()}


@app.get("/v1/metadata")
async def metadata():
    return {
        "team_name": os.getenv("TEAM_NAME", "Aditya Ahlawat"),
        "team_members": [m.strip() for m in os.getenv("TEAM_MEMBERS", "Aditya Ahlawat").split(",")],
        "model": llm.model_name(),
        "approach": ("Grounded playbook composer (one per trigger kind) over a fact layer that only emits numbers present "
                     "in the pushed contexts; optional LLM rewrite gated by a validator (grounding, taboos, URL, CTA); "
                     "rule-based conversation engine for auto-reply, opt-out, intent-to-action and off-topic handling."),
        "contact_email": os.getenv("CONTACT_EMAIL", "adityaahlawat544@gmail.com"),
        "version": "1.0.0",
        "submitted_at": os.getenv("SUBMITTED_AT", "2026-09-27T00:00:00Z"),
    }


# ------------------------------------------------------------------ context
class CtxBody(BaseModel):
    scope: str
    context_id: str
    version: int
    payload: dict[str, Any]
    delivered_at: str | None = None


@app.post("/v1/context")
async def push_context(body: CtxBody):
    if body.scope not in VALID_SCOPES:
        return JSONResponse(status_code=400, content={"accepted": False, "reason": "invalid_scope",
                                                      "details": f"scope must be one of {list(VALID_SCOPES)}"})
    ok, cur = STORE.put(body.scope, body.context_id, body.version, body.payload)
    if not ok:
        return JSONResponse(status_code=409, content={"accepted": False, "reason": "stale_version", "current_version": cur})
    return {"accepted": True, "ack_id": f"ack_{body.context_id}_v{body.version}", "stored_at": _now_iso()}


# ------------------------------------------------------------------ tick
class TickBody(BaseModel):
    now: str | None = None
    available_triggers: list[str] = []


def _conv_id(trig: dict, merchant_id: str) -> str:
    short = "_".join(merchant_id.split("_")[:2])
    who = trig.get("customer_id") or ""
    who = "_".join(who.split("_")[:3]) if who else ""
    base = re.sub(r"[^a-z0-9_]+", "_", f"conv_{short}_{who + '_' if who else ''}{trig.get('kind', 'msg')}".lower()).strip("_")
    cid, n = base, 2
    while cid in STORE.conversations:
        cid, n = f"{base}_{n}", n + 1
    return cid


def _muted(merchant_id: str, now: str) -> bool:
    until = STORE.merchant_optout.get(merchant_id)
    if not until:
        return False
    d = F.parse_dt(now)
    return (d.timestamp() if d else time.time()) < until


def select_triggers(ids: list[str], now: str) -> list[tuple[dict, dict, dict, dict | None]]:
    cands = []
    for tid in dict.fromkeys(ids):  # de-dupe, keep order
        trig = STORE.get("trigger", tid)
        if not trig:
            continue
        trig = {**trig, "id": trig.get("id") or tid}
        if tid in STORE.sent_triggers or F.is_expired(trig, now):
            continue
        sk = trig.get("suppression_key")
        if sk and sk in STORE.sent_suppression:
            continue
        mid = trig.get("merchant_id")
        merchant = STORE.get("merchant", mid)
        if not merchant or _muted(mid, now):
            continue
        category = STORE.get("category", merchant.get("category_slug"))
        if not category:
            continue
        customer = None
        if trig.get("scope") == "customer" or trig.get("customer_id"):
            customer = STORE.get("customer", trig.get("customer_id"))
            if not customer or trig.get("customer_id") in STORE.customer_optout or not F.customer_can_receive(customer):
                continue
        cands.append((trig, merchant, category, customer))
    # most urgent first; one merchant-facing send per merchant per tick (restraint), one per customer
    cands.sort(key=lambda x: -(x[0].get("urgency") or 0))
    picked, seen_m, seen_c = [], set(), set()
    for trig, m, cat, cu in cands:
        key_m = m["merchant_id"]
        if cu is None:
            if key_m in seen_m:
                continue
            seen_m.add(key_m)
        else:
            if cu["customer_id"] in seen_c:
                continue
            seen_c.add(cu["customer_id"])
        picked.append((trig, m, cat, cu))
        if len(picked) >= MAX_ACTIONS:
            break
    return picked


@app.post("/v1/tick")
async def tick(body: TickBody):
    now = body.now or _now_iso()
    picked = select_triggers(body.available_triggers, now)
    ctxs = [build_ctx(cat, m, trig, cu, now) for trig, m, cat, cu in picked]

    async def one(c):
        try:
            return await compose_action_async(c)
        except Exception:
            return compose_draft(c)

    try:
        results = await asyncio.wait_for(asyncio.gather(*(one(c) for c in ctxs)), timeout=TICK_BUDGET_S)
    except asyncio.TimeoutError:
        results = [compose_draft(c) for c in ctxs]

    actions = []
    for c, out in zip(ctxs, results):
        trig, mid = c.trigger, c.merchant["merchant_id"]
        body_key = (mid, trig.get("customer_id"), out["body"])
        if body_key in STORE.sent_kinds:  # would repeat a message this recipient already got -> stay quiet
            STORE.sent_triggers.add(trig["id"])
            continue
        cid = _conv_id(trig, mid)
        action = {"conversation_id": cid, "merchant_id": mid,
                  "customer_id": (c.customer or {}).get("customer_id"),
                  "send_as": out["send_as"], "trigger_id": trig["id"],
                  "template_name": out["template_name"], "template_params": out["template_params"],
                  "body": out["body"], "cta": out["cta"], "suppression_key": out["suppression_key"],
                  "rationale": out["rationale"]}
        actions.append(action)
        STORE.sent_triggers.add(trig["id"])
        STORE.sent_suppression.add(out["suppression_key"])
        STORE.sent_kinds.add(body_key)
        STORE.conversations[cid] = {"conversation_id": cid, "merchant_id": mid, "customer_id": action["customer_id"],
                                    "trigger_id": trig["id"], "kind": trig.get("kind"), "bodies": [out["body"]],
                                    "inbound": [], "auto_count": 0, "nudged": False, "status": "open", "stage": "pitch",
                                    "created": time.time()}
    return {"actions": actions}


# ------------------------------------------------------------------ reply
class ReplyBody(BaseModel):
    conversation_id: str
    merchant_id: str | None = None
    customer_id: str | None = None
    from_role: str = "merchant"
    message: str = ""
    received_at: str | None = None
    turn_number: int | None = None


@app.post("/v1/reply")
async def reply(body: ReplyBody):
    try:
        return await asyncio.wait_for(handle_reply(body.model_dump()), timeout=25)
    except Exception:
        return {"action": "wait", "wait_seconds": 3600, "rationale": "Internal hiccup; backing off briefly instead of sending a bad message."}


# ------------------------------------------------------------------ teardown
@app.post("/v1/teardown")
async def teardown():
    STORE.reset()
    return {"ok": True, "wiped_at": _now_iso()}


# ------------------------------------------------------------------ demo UI (not part of the judge API)
_DATASET_DIR = os.path.join(os.path.dirname(__file__), "dataset", "expanded")


@app.post("/demo/seed")
async def demo_seed():
    """Load the expanded seed dataset into STORE so the demo frontend has data to tick over."""
    id_field = {"category": "slug", "merchant": "merchant_id", "customer": "customer_id", "trigger": "id"}
    counts = {"category": 0, "merchant": 0, "customer": 0, "trigger": 0}
    for scope, subdir in [("category", "categories"), ("merchant", "merchants"),
                          ("customer", "customers"), ("trigger", "triggers")]:
        for path in sorted(glob.glob(os.path.join(_DATASET_DIR, subdir, "*.json"))):
            payload = json.load(open(path, encoding="utf-8"))
            cid = payload.get(id_field[scope])
            STORE.put(scope, cid, 1, payload)
            counts[scope] += 1
    return {"loaded": counts}


@app.get("/demo/triggers")
async def demo_triggers():
    """List trigger ids currently in the store, for the demo UI to feed into /v1/tick."""
    out = [{"id": t.get("id"), "kind": t.get("kind"), "merchant_id": t.get("merchant_id"),
            "urgency": t.get("urgency")} for t in STORE.all("trigger")]
    out.sort(key=lambda t: -(t["urgency"] or 0))
    return {"triggers": out}


app.mount("/", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static"), html=True), name="static")


@app.exception_handler(Exception)
async def on_error(request: Request, exc: Exception):
    if request.url.path.endswith("/tick"):
        return JSONResponse(status_code=200, content={"actions": []})
    return JSONResponse(status_code=500, content={"error": "internal", "detail": str(exc)[:200]})
