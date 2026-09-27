"""Fact helpers: turn raw contexts into grounded, verifiable facts.

Every number the bot prints must come from here (i.e. from the pushed
contexts), never from thin air.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

DEFAULT_NOW = "2026-04-26T10:30:00Z"
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# ---------------- dates ----------------
def parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except ValueError:
        try:
            return datetime.fromisoformat(str(s)[:10])
        except ValueError:
            return None


def to_date(s) -> date | None:
    d = parse_dt(s) if not isinstance(s, (date, datetime)) else s
    if d is None:
        return None
    return d.date() if isinstance(d, datetime) else d


def nice_date(s) -> str:
    d = to_date(s)
    return f"{d.day} {MONTHS[d.month - 1]}" if d else str(s)


def nice_date_wd(s) -> str:
    d = to_date(s)
    return f"{WEEKDAYS[d.weekday()][:3]} {d.day} {MONTHS[d.month - 1]}" if d else str(s)


def days_between(a, b) -> int | None:
    da, db = to_date(a), to_date(b)
    if not da or not db:
        return None
    return (db - da).days


def is_expired(trigger: dict, now: str) -> bool:
    exp, n = parse_dt(trigger.get("expires_at")), parse_dt(now)
    if not exp or not n:
        return False
    try:
        return exp < n
    except TypeError:  # naive vs aware
        return exp.replace(tzinfo=None) < n.replace(tzinfo=None)


# ---------------- numbers ----------------
def pct(x: float | None, signed: bool = False) -> str:
    if x is None:
        return "?"
    v = round(abs(x) * 100) if not signed else round(x * 100)
    return f"{v:+d}%" if signed else f"{v}%"


def ctr_str(x: float | None) -> str:
    return f"{x * 100:.1f}%" if x is not None else "?"


def inr(n) -> str:
    try:
        n = int(float(n))
    except (TypeError, ValueError):
        return f"₹{n}"
    s = str(n)
    if len(s) > 3:  # Indian grouping
        head, tail = s[:-3], s[-3:]
        head = re.sub(r"(\d)(?=(\d{2})+$)", r"\1,", head)
        s = head + "," + tail
    return f"₹{s}"


def num(n) -> str:
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def humanize(slug: str) -> str:
    return str(slug).replace("_", " ").strip()


# ---------------- merchant ----------------
def owner_first(merchant: dict) -> str:
    name = (merchant.get("identity") or {}).get("owner_first_name") or ""
    return re.sub(r"^(dr\.?\s*)", "", name, flags=re.I).strip()


def salutation(merchant: dict, category: dict) -> str:
    first = owner_first(merchant)
    slug = (category or {}).get("slug") or merchant.get("category_slug")
    if not first:
        return f"{merchant.get('identity', {}).get('name', 'there')} team"
    return f"Dr. {first}" if slug == "dentists" else first


def biz_name(merchant: dict) -> str:
    return (merchant.get("identity") or {}).get("name", "your business")


def locality(merchant: dict) -> str:
    i = merchant.get("identity") or {}
    return i.get("locality") or i.get("city") or "your area"


def speaks_hindi(merchant: dict) -> bool:
    return "hi" in ((merchant.get("identity") or {}).get("languages") or [])


def active_offers(merchant: dict) -> list[str]:
    return [o["title"] for o in merchant.get("offers") or [] if o.get("status") == "active" and o.get("title")]


def expired_offers(merchant: dict) -> list[str]:
    return [o["title"] for o in merchant.get("offers") or [] if o.get("status") in ("expired", "paused")]


def catalog_offer(category: dict, prefer_types=("service_at_price",), audience=None) -> str | None:
    for o in (category or {}).get("offer_catalog") or []:
        if o.get("type") in prefer_types and (audience is None or o.get("audience") == audience):
            return o.get("title")
    cat = (category or {}).get("offer_catalog") or []
    return cat[0]["title"] if cat else None


def perf(merchant: dict) -> dict:
    return merchant.get("performance") or {}


def worst_metric(merchant: dict) -> tuple[str, float] | None:
    d = perf(merchant).get("delta_7d") or {}
    items = [(k.replace("_pct", ""), v) for k, v in d.items() if isinstance(v, (int, float)) and k != "ctr_pct"]
    return min(items, key=lambda x: x[1]) if items else None


def best_metric(merchant: dict) -> tuple[str, float] | None:
    d = perf(merchant).get("delta_7d") or {}
    items = [(k.replace("_pct", ""), v) for k, v in d.items() if isinstance(v, (int, float)) and k != "ctr_pct"]
    return max(items, key=lambda x: x[1]) if items else None


def peer_gap(merchant: dict, category: dict) -> dict:
    """Compare merchant 30d numbers with category peer stats."""
    p, ps = perf(merchant), (category or {}).get("peer_stats") or {}
    out = {}
    if p.get("ctr") is not None and ps.get("avg_ctr"):
        out["ctr"] = (p["ctr"], ps["avg_ctr"])
    if p.get("calls") is not None and ps.get("avg_calls_30d"):
        out["calls"] = (p["calls"], ps["avg_calls_30d"])
    if p.get("views") is not None and ps.get("avg_views_30d"):
        out["views"] = (p["views"], ps["avg_views_30d"])
    return out


def signal_value(merchant: dict, prefix: str) -> str | None:
    for s in merchant.get("signals") or []:
        if s.startswith(prefix):
            return s.split(":", 1)[1] if ":" in s else s
    return None


def has_signal(merchant: dict, prefix: str) -> bool:
    return any(s.startswith(prefix) for s in merchant.get("signals") or [])


def last_merchant_message(merchant: dict) -> str | None:
    for turn in reversed(merchant.get("conversation_history") or []):
        if turn.get("from") == "merchant":
            return turn.get("body")
    return None


# ---------------- category ----------------
def digest_item(category: dict, item_id: str | None) -> dict | None:
    if not item_id:
        return None
    for d in (category or {}).get("digest") or []:
        if d.get("id") == item_id:
            return d
    return None


def digest_by_kind(category: dict, *kinds: str) -> dict | None:
    for k in kinds:
        for d in (category or {}).get("digest") or []:
            if d.get("kind") == k:
                return d
    return None


def month_in_range(month: int, rng: str) -> bool:
    """rng like 'Apr-Jun', 'Nov-Feb', 'Jan', 'Feb 14'."""
    names = [m.lower() for m in MONTHS]
    parts = [p.strip()[:3].lower() for p in rng.split("-")]
    try:
        idx = [names.index(p) + 1 for p in parts if p in names]
    except ValueError:
        return False
    if not idx:
        return False
    if len(idx) == 1:
        return month == idx[0]
    a, b = idx[0], idx[1]
    return a <= month <= b if a <= b else (month >= a or month <= b)


def seasonal_beat(category: dict, now: str, keyword: str | None = None, upcoming: bool = False) -> dict | None:
    d = to_date(now) or to_date(DEFAULT_NOW)
    beats = (category or {}).get("seasonal_beats") or []
    if keyword:
        beats_k = [b for b in beats if keyword in b.get("note", "").lower()]
        beats = beats_k or beats
    if not upcoming:
        for b in beats:
            if month_in_range(d.month, b.get("month_range", "")):
                return b
    for offset in range(1, 13):
        m = (d.month - 1 + offset) % 12 + 1
        for b in beats:
            if month_in_range(m, b.get("month_range", "")):
                return b
    return beats[0] if beats else None


def top_trend(category: dict) -> dict | None:
    ts = (category or {}).get("trend_signals") or []
    return max(ts, key=lambda t: t.get("delta_yoy", 0)) if ts else None


# ---------------- customer ----------------
def customer_first(customer: dict) -> str:
    n = (customer.get("identity") or {}).get("name") or ""
    return re.sub(r"\s*\(.*\)", "", n).strip()


def customer_parent(customer: dict) -> str | None:
    n = (customer.get("identity") or {}).get("name") or ""
    m = re.search(r"parent:\s*([^)]+)", n)
    return m.group(1).strip() if m else None


def customer_lang(customer: dict) -> str:
    """Returns one of: en, hi, hi-en, ta-en, te-en, kn-en."""
    lp = ((customer.get("identity") or {}).get("language_pref") or "en").lower()
    if lp in ("hi",):
        return "hi"
    if "hi" in lp:
        return "hi-en"
    for code in ("ta", "te", "kn", "mr"):
        if lp.startswith(code):
            return f"{code}-en"
    return "en"


def customer_can_receive(customer: dict | None) -> bool:
    if not customer:
        return False
    c = customer.get("consent") or {}
    prefs = customer.get("preferences") or {}
    if not c.get("scope") and not prefs.get("reminder_opt_in"):
        return False
    if (prefs.get("channel") or "") in ("none_recorded", "none"):
        return False
    if (customer.get("identity") or {}).get("phone_redacted") is None:
        return False
    return True


def slot_pref_label(customer: dict) -> str | None:
    s = (customer.get("preferences") or {}).get("preferred_slots")
    return humanize(s) if s else None


# ---------------- grounding: numbers allowed in output ----------------
_NUM_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "").rstrip(".") for n in _NUM_RE.findall(text or "")}


def allowed_numbers(*objs) -> set[str]:
    """All numbers that appear anywhere in the given contexts, plus % forms."""
    out: set[str] = set(str(i) for i in range(0, 11))
    def walk(o):
        if isinstance(o, dict):
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
        elif isinstance(o, bool) or o is None:
            return
        elif isinstance(o, (int, float)):
            out.add(str(o).rstrip("0").rstrip(".") if isinstance(o, float) else str(o))
            if isinstance(o, float) and -5 < o < 5:
                out.add(str(round(abs(o) * 100)))
                out.add(f"{abs(o) * 100:.1f}".rstrip("0").rstrip("."))
                out.add(f"{abs(o) * 100:.1f}")
            out.add(str(int(abs(o))))
        else:
            out.update(numbers_in(str(o)))
            d = to_date(o) if isinstance(o, str) and re.match(r"\d{4}-\d{2}-\d{2}", o) else None
            if d:
                out.update({str(d.day), str(d.year), str(d.month)})
    for o in objs:
        walk(o)
    return out
