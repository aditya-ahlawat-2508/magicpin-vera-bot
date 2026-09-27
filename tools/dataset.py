"""Load the expanded dataset (runs generate_dataset.py if needed)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DS = ROOT / "dataset"
EXP = DS / "expanded"


def ensure_expanded() -> Path:
    if not (EXP / "test_pairs.json").exists():
        subprocess.run([sys.executable, str(DS / "generate_dataset.py"), "--seed-dir", str(DS), "--out", str(EXP)], check=True)
    return EXP


def load_all() -> dict:
    ensure_expanded()
    out = {"category": {}, "merchant": {}, "customer": {}, "trigger": {}}
    for f in (EXP / "categories").glob("*.json"):
        d = json.loads(f.read_text()); out["category"][d["slug"]] = d
    for f in (EXP / "merchants").glob("*.json"):
        d = json.loads(f.read_text()); out["merchant"][d["merchant_id"]] = d
    for f in (EXP / "customers").glob("*.json"):
        d = json.loads(f.read_text()); out["customer"][d["customer_id"]] = d
    for f in (EXP / "triggers").glob("*.json"):
        d = json.loads(f.read_text()); out["trigger"][d["id"]] = d
    out["test_pairs"] = json.loads((EXP / "test_pairs.json").read_text())["pairs"]
    return out
