"""Print the composed message for every test pair (or all triggers with --all) + validator verdict."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.dataset import load_all  # noqa: E402
from vera.compose import build_ctx, compose_draft, validate  # noqa: E402
from vera.store import STORE  # noqa: E402

D = load_all()
for tid, t in D["trigger"].items():
    STORE.put("trigger", tid, 1, t)

pairs = D["test_pairs"] if "--all" not in sys.argv else [
    {"test_id": tid, "trigger_id": tid, "merchant_id": t["merchant_id"], "customer_id": t.get("customer_id")} for tid, t in sorted(D["trigger"].items())]
bad = 0
for p in pairs:
    t = D["trigger"][p["trigger_id"]]
    m = D["merchant"][p["merchant_id"]]
    cat = D["category"][m["category_slug"]]
    cu = D["customer"].get(p["customer_id"]) if p.get("customer_id") else None
    c = build_ctx(cat, m, t, cu)
    out = compose_draft(c)
    probs = validate(out["body"], c)
    bad += bool(probs)
    print(f"\n=== {p['test_id']} {t['kind']} | {m['identity']['name']} | {out['send_as']} | {len(out['body'])}ch {'!! ' + str(probs) if probs else ''}")
    print(out["body"])
print(f"\n{len(pairs)} composed, {bad} with validator problems")
