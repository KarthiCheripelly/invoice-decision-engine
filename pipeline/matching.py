"""
Stage 4/5: Vendor resolution and PO matching.

Vendor resolution order (strongest evidence first):
  1. tax id on the invoice equals a master tax id
  2. normalised legal name equals a master name
  3. invoice name equals a known alias
  4. fuzzy name similarity >= FUZZY_THRESHOLD (catches "Acme Industrial Supplies" vs "...Pvt Ltd")

PO matching:
  - explicit: the invoice quotes a PO number -> look it up (tolerant of "PO 2026-0101", "po#2026-0101" ...)
  - inferred: no PO quoted -> look for exactly one open PO of this vendor, same currency,
              whose remaining balance is within INFER_TOLERANCE_PCT of the invoice total.
"""
from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field

FUZZY_THRESHOLD = 0.86
INFER_TOLERANCE_PCT = 2.0

_SUFFIXES = {"pvt", "private", "ltd", "limited", "llp", "inc", "incorporated", "co", "corp", "corporation", "llc", "plc", "gmbh", "company"}


def normalise_name(name: str | None) -> str:
    if not name:
        return ""
    s = re.sub(r"[^a-z0-9 ]+", " ", name.lower())
    return " ".join(w for w in s.split() if w not in _SUFFIXES)


def normalise_po(ref: str | None) -> str:
    if not ref:
        return ""
    s = ref.upper().replace("#", "").replace(":", "").strip()
    s = re.sub(r"^P\.?O\.?\s*(NO\.?|NUMBER)?\s*", "PO-", s)
    s = re.sub(r"[\s_]+", "-", s)
    s = re.sub(r"-{2,}", "-", s)
    if not s.startswith("PO-"):
        s = "PO-" + s
    return s


@dataclass
class VendorMatch:
    vendor: dict | None
    method: str            # tax_id | exact_name | alias | fuzzy | none
    score: float
    candidates: list[tuple[str, float]] = field(default_factory=list)


def resolve_vendor(name: str | None, tax_id: str | None, vendors: list[dict]) -> VendorMatch:
    if tax_id:
        t = tax_id.replace(" ", "").upper()
        for v in vendors:
            if v["tax_id"].replace(" ", "").upper() == t:
                return VendorMatch(v, "tax_id", 1.0)
    n = normalise_name(name)
    if not n:
        return VendorMatch(None, "none", 0.0)
    for v in vendors:
        if normalise_name(v["vendor_name"]) == n:
            return VendorMatch(v, "exact_name", 1.0)
    for v in vendors:
        if any(normalise_name(a) == n for a in v["aliases"]):
            return VendorMatch(v, "alias", 0.98)
    scored = sorted(((v, difflib.SequenceMatcher(None, n, normalise_name(v["vendor_name"])).ratio()) for v in vendors), key=lambda x: -x[1])
    cands = [(v["vendor_name"], round(s, 2)) for v, s in scored[:3]]
    if scored and scored[0][1] >= FUZZY_THRESHOLD:
        return VendorMatch(scored[0][0], "fuzzy", round(scored[0][1], 2), cands)
    return VendorMatch(None, "none", round(scored[0][1], 2) if scored else 0.0, cands)


@dataclass
class POMatch:
    po: dict | None
    method: str            # explicit | inferred | not_found | none | ambiguous
    quoted_reference: str | None
    candidates: list[dict] = field(default_factory=list)


def find_po(quoted: str | None, pos: list[dict]) -> POMatch:
    if not quoted:
        return POMatch(None, "none", None)
    key = normalise_po(quoted)
    po = next((p for p in pos if normalise_po(p["po_number"]) == key), None)
    return POMatch(po, "explicit" if po else "not_found", quoted)


def infer_po(vendor_id: str, total: float | None, currency: str | None, pos: list[dict]) -> POMatch:
    if total is None:
        return POMatch(None, "none", None)
    open_pos = [p for p in pos if p["vendor_id"] == vendor_id and p["status"] == "open" and p["remaining"] > 0
                and (not currency or p["currency"] == currency)]
    hits = [p for p in open_pos if abs(p["remaining"] - total) <= p["po_amount"] * INFER_TOLERANCE_PCT / 100]
    if len(hits) == 1:
        return POMatch(hits[0], "inferred", None, open_pos)
    if len(hits) > 1:
        return POMatch(None, "ambiguous", None, hits)
    return POMatch(None, "none", None, open_pos)
