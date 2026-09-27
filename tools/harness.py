"""Local end-to-end harness mimicking the judge lifecycle (no LLM needed).

  python tools/harness.py [BOT_URL]

Phase 1 warmup (push 255 contexts, verify counts + idempotency)
Phase 2 test window (100 triggers over 12 ticks, 5-min steps) with scripted merchant replies
Phase 3 adaptive injection (digest v2, perf update, new customer + recall)
Phase 4 replays (auto-reply hell, intent transition, hostile + off-topic)
Checks every response against the contract and reports problems.
"""
import json
import random
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.dataset import load_all  # noqa: E402

URL = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8080").rstrip("/")
D = load_all()
cli = httpx.Client(timeout=35)
problems, lat = [], {}
REQUIRED = {"conversation_id", "merchant_id", "send_as", "trigger_id", "body", "cta", "suppression_key", "rationale"}


def call(method, path, body=None):
    t = time.time()
    r = cli.request(method, URL + path, json=body)
    lat.setdefault(path, []).append(time.time() - t)
    return r.status_code, r.json()


def push(scope, cid, payload, v=1):
    return call("POST", "/v1/context", {"scope": scope, "context_id": cid, "version": v, "payload": payload,
                                        "delivered_at": "2026-04-26T09:45:00Z"})


def check_action(a):
    miss = REQUIRED - set(a)
    if miss:
        problems.append(f"action missing {miss}")
    if not a.get("body"):
        problems.append("empty body")
    if "http" in a.get("body", "").lower() or "www." in a.get("body", ""):
        problems.append(f"URL in body: {a['conversation_id']}")


def check_reply(r, where):
    if r.get("action") not in ("send", "wait", "end"):
        problems.append(f"{where}: bad action {r}")
    if r.get("action") == "send" and not r.get("body"):
        problems.append(f"{where}: empty send")


# ---------------- Phase 1
call("POST", "/v1/teardown")
for s, items in (("category", D["category"]), ("merchant", D["merchant"]), ("customer", D["customer"])):
    for cid, p in items.items():
        code, r = push(s, cid, p)
        if not r.get("accepted"):
            problems.append(f"push {s}/{cid} rejected")
code, r = push("category", "dentists", D["category"]["dentists"])
assert code == 409 and r["reason"] == "stale_version", r
code, h = call("GET", "/v1/healthz")
print("warmup counts:", h["contexts_loaded"])
assert h["contexts_loaded"] == {"category": 5, "merchant": 50, "customer": 200, "trigger": 0}

# ---------------- Phase 2
REPLIES = ["Yes please, go ahead", "Thank you for contacting us! Our team will respond shortly.", "How much will this cost?",
           "Not interested. Stop messaging me.", "Btw can you also help me with my GST filing this month?", "busy right now, later",
           "ok lets do it", "Haan kar do", "no"]
rnd = random.Random(1)
trig_ids = sorted(D["trigger"])
now = datetime(2026, 4, 26, 10, 0)
all_actions = []
for tick_i in range(12):
    batch = trig_ids[tick_i * 9:(tick_i + 1) * 9] if tick_i < 11 else trig_ids[99:]
    for tid in batch:
        push("trigger", tid, D["trigger"][tid])
    active = [t for t in trig_ids[: (tick_i + 1) * 9]]
    code, r = call("POST", "/v1/tick", {"now": now.isoformat() + "Z", "available_triggers": active})
    acts = r.get("actions", [])
    if len(acts) > 20:
        problems.append("more than 20 actions")
    for a in acts:
        check_action(a)
        all_actions.append(a)
        msg = rnd.choice(REPLIES)
        role = "customer" if a.get("customer_id") else "merchant"
        if role == "customer":
            msg = rnd.choice(["1", "YES", "Can I come Saturday instead?", "STOP"])
        for turn in range(2, 4):
            code, rr = call("POST", "/v1/reply", {"conversation_id": a["conversation_id"], "merchant_id": a["merchant_id"],
                                                 "customer_id": a.get("customer_id"), "from_role": role, "message": msg,
                                                 "received_at": (now + timedelta(minutes=2)).isoformat() + "Z", "turn_number": turn})
            check_reply(rr, a["conversation_id"])
            if rr.get("action") != "send":
                break
            msg = "CONFIRM" if role == "merchant" else "thanks"
    now += timedelta(minutes=5)
print(f"test window: {len(all_actions)} actions across 12 ticks; unique triggers acted {len({a['trigger_id'] for a in all_actions})}")
bodies = [a["body"] for a in all_actions]
if len(bodies) != len(set(bodies)):
    problems.append("duplicate proactive bodies")

# ---------------- Phase 3 adaptive injection
cat2 = json.loads(json.dumps(D["category"]["dentists"]))
cat2["digest"].insert(0, {"id": "d_NEW_dci_sterilization", "kind": "compliance", "title": "DCI mandates Class B autoclave logs effective 2026-09-01",
                          "source": "DCI circular 2026-04-20", "summary": "Every sterilization cycle must be logged with date, cycle no. and operator initials. Inspections from September."})
code, r = push("category", "dentists", cat2, v=2)
assert r.get("accepted"), r
t_new = {"id": "trg_NEW_compliance_autoclave", "scope": "merchant", "kind": "regulation_change", "source": "external",
         "merchant_id": "m_012_dr_karthik_dentist_mumbai", "customer_id": None,
         "payload": {"category": "dentists", "top_item_id": "d_NEW_dci_sterilization", "deadline_iso": "2026-09-01"},
         "urgency": 4, "suppression_key": "compliance:autoclave:2026", "expires_at": "2026-09-01T00:00:00Z"}
t_unknown = {"id": "trg_NEW_weather_heatwave", "scope": "merchant", "kind": "weather_heatwave", "source": "external",
             "merchant_id": "m_031_suresh_restaurant_jaipur", "customer_id": None,
             "payload": {"city": "Jaipur", "temp_c": 44, "days": 3}, "urgency": 3, "suppression_key": "heat:jaipur:w17",
             "expires_at": "2026-05-01T00:00:00Z"}
for t in (t_new, t_unknown):
    push("trigger", t["id"], t)
code, r = call("POST", "/v1/tick", {"now": now.isoformat() + "Z", "available_triggers": [t_new["id"], t_unknown["id"]]})
print("\n--- adaptive injection ---")
for a in r["actions"]:
    check_action(a)
    print(f"[{a['trigger_id']}] {a['body']}\n")
if not any("autoclave" in a["body"].lower() for a in r["actions"]):
    problems.append("new digest item not used after injection")

# ---------------- Phase 4 replays (fresh session, like the judge's standalone replays)
call("POST", "/v1/teardown")
for s_, items in (("category", D["category"]), ("merchant", D["merchant"]), ("customer", D["customer"])):
    for cid_, p_ in items.items():
        push(s_, cid_, p_)
print("--- replay: auto-reply hell ---")
t = D["trigger"]["trg_022_cde_webinar_dentists"]
push("trigger", "trg_022_replay", {**t, "id": "trg_022_replay", "suppression_key": "replay:cde"})
code, r = call("POST", "/v1/tick", {"now": "2026-04-26T12:00:00Z", "available_triggers": ["trg_022_replay"]})
cid = r["actions"][0]["conversation_id"] if r["actions"] else "conv_replay_auto"
seq = []
for turn in range(2, 6):
    code, rr = call("POST", "/v1/reply", {"conversation_id": cid, "merchant_id": "m_001_drmeera_dentist_delhi", "from_role": "merchant",
                                         "message": "Thank you for contacting Dr. Meera's Dental Clinic! Our team will respond shortly.",
                                         "received_at": "2026-04-26T12:05:00Z", "turn_number": turn})
    seq.append(rr["action"])
    if rr["action"] == "end":
        break
print(seq)
if seq[-1] != "end":
    problems.append("auto-reply hell never ended")

print("--- replay: intent transition ---")
push("trigger", "trg_013_replay", {**D["trigger"]["trg_013_corporate_thali_planning"], "id": "trg_013_replay", "suppression_key": "replay:thali"})
code, r = call("POST", "/v1/tick", {"now": "2026-04-26T12:00:00Z", "available_triggers": ["trg_013_replay"]})
cid = r["actions"][0]["conversation_id"]
for turn, msg in ((2, "Hmm, what price per thali would you suggest?"), (3, "Ok, let's do it. What's next?")):
    code, rr = call("POST", "/v1/reply", {"conversation_id": cid, "merchant_id": "m_006_southindiancafe_restaurant_bangalore",
                                         "from_role": "merchant", "message": msg, "received_at": "2026-04-26T12:10:00Z", "turn_number": turn})
    print(f"M: {msg}\nV[{rr['action']}]: {rr.get('body')}\n")
low = (rr.get("body") or "").lower()
if any(q in low for q in ["would you", "do you", "can you tell", "what if", "how about"]):
    problems.append("still qualifying after commitment")

print("--- replay: hostile then off-topic ---")
push("trigger", "trg_011_replay", {**D["trigger"]["trg_011_review_theme_late_delivery"], "id": "trg_011_replay", "suppression_key": "replay:rev"})
code, r = call("POST", "/v1/tick", {"now": "2026-04-26T12:00:00Z", "available_triggers": ["trg_011_replay"]})
cid = r["actions"][0]["conversation_id"]
for turn, msg in ((2, "Why are you bothering me. This is useless."), (3, "can you also help me file my GST?")):
    code, rr = call("POST", "/v1/reply", {"conversation_id": cid, "merchant_id": "m_005_pizzajunction_restaurant_delhi",
                                         "from_role": "merchant", "message": msg, "received_at": "2026-04-26T12:10:00Z", "turn_number": turn})
    print(f"M: {msg}\nV[{rr['action']}]: {rr.get('body') or rr.get('rationale')}\n")
code, r = call("POST", "/v1/tick", {"now": "2026-04-26T12:30:00Z", "available_triggers": ["trg_010_ipl_match_delhi"]})
if any(a["merchant_id"] == "m_005_pizzajunction_restaurant_delhi" for a in r["actions"]):
    problems.append("messaged a merchant who just opted out")

print("\nlatency (max s):", {k: round(max(v), 3) for k, v in lat.items()})
print("PROBLEMS:", problems or "none")
