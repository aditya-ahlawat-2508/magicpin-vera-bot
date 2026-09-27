"""Generate submission.jsonl — one line per canonical test pair (30).

    python make_submission.py            # writes submission.jsonl
"""
import json

from bot import compose
from tools.dataset import load_all
from vera.store import STORE

D = load_all()
for tid, t in D["trigger"].items():  # lets cross-trigger judgment work (e.g. refill vs. live batch recall)
    STORE.put("trigger", tid, 1, t)

with open("submission.jsonl", "w") as f:
    for p in D["test_pairs"]:
        t = D["trigger"][p["trigger_id"]]
        m = D["merchant"][p["merchant_id"]]
        cat = D["category"][m["category_slug"]]
        cu = D["customer"].get(p["customer_id"]) if p.get("customer_id") else None
        out = compose(cat, m, t, cu)
        f.write(json.dumps({"test_id": p["test_id"], **{k: out[k] for k in ("body", "cta", "send_as", "suppression_key", "rationale")}},
                           ensure_ascii=False) + "\n")
print(f"wrote submission.jsonl ({len(D['test_pairs'])} lines)")
