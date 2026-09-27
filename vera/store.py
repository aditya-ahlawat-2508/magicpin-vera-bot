"""In-memory, versioned context store + conversation/suppression state.

Single-process by design: run uvicorn with ONE worker so every request sees
the same memory.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

VALID_SCOPES = ("category", "merchant", "customer", "trigger")


class Store:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.started = time.time()
        self.reset()

    def reset(self) -> None:
        with getattr(self, "lock", threading.RLock()):
            self.contexts: dict[tuple[str, str], dict] = {}
            self.conversations: dict[str, dict] = {}
            self.sent_suppression: set[str] = set()      # suppression keys already used
            self.sent_triggers: set[str] = set()         # trigger ids already acted on
            self.sent_kinds: set[tuple] = set()          # (merchant, customer, kind) already messaged
            self.merchant_optout: dict[str, float] = {}  # merchant_id -> epoch until muted
            self.customer_optout: set[str] = set()
            self.merchant_autoreply: dict[str, dict] = {}  # merchant_id -> {text: count}
            self.merchant_last_send: dict[str, str] = {}   # merchant_id -> iso time

    # ---------- contexts ----------
    def put(self, scope: str, cid: str, version: int, payload: dict) -> tuple[bool, int | None]:
        with self.lock:
            key = (scope, cid)
            cur = self.contexts.get(key)
            if cur and cur["version"] >= version:
                return False, cur["version"]
            self.contexts[key] = {"version": version, "payload": payload,
                                  "stored_at": datetime.now(timezone.utc).isoformat()}
            return True, None

    def get(self, scope: str, cid: str | None) -> dict | None:
        if not cid:
            return None
        rec = self.contexts.get((scope, cid))
        return rec["payload"] if rec else None

    def all(self, scope: str) -> list[dict]:
        return [v["payload"] for (s, _), v in self.contexts.items() if s == scope]

    def counts(self) -> dict:
        out = {s: 0 for s in VALID_SCOPES}
        for (s, _) in self.contexts:
            out[s] = out.get(s, 0) + 1
        return out

    def triggers_for_merchant(self, merchant_id: str) -> list[dict]:
        return [t for t in self.all("trigger") if t.get("merchant_id") == merchant_id]


STORE = Store()
