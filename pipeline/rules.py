"""
Stage 6: Business rules and the decision.

Every rule returns a Check with a status:
  pass  - rule satisfied
  info  - nothing wrong, but worth showing a human (e.g. "PO was inferred")
  warn  - cannot auto-approve; a human must look          -> NEEDS_REVIEW
  fail  - hard stop; do not pay                            -> REJECTED
  skip  - rule not applicable (e.g. no PO to compare against)

Decision = REJECTED if any fail, else NEEDS_REVIEW if any warn, else APPROVED.
Thresholds are deliberately in one place so they can be defended in the interview.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

from .matching import POMatch, VendorMatch

# ---- thresholds (the "policy") -------------------------------------------------------------
AUTO_APPROVE_TOLERANCE_PCT = 2.0     # invoice may exceed PO remaining balance by up to 2% and still auto-approve
REVIEW_TOLERANCE_PCT = 10.0          # 2% - 10% over -> human review;  > 10% over -> reject
ARITHMETIC_TOLERANCE = 1.00          # rounding slack (currency units) when re-adding line items / tax
MIN_CONFIDENCE_TEXT = 0.70           # per critical field, clean PDFs
MIN_CONFIDENCE_SCANNED = 0.85        # per critical field, scans (vision OCR is less reliable)
MAX_INVOICE_AGE_DAYS = 365
FUTURE_DATE_SLACK_DAYS = 2

CRITICAL_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "total"]

RULE_CATALOGUE = [
    ("R1", "Required fields present", "Vendor, invoice number, invoice date and total must all be readable. Missing -> review (we cannot register an invoice without them)."),
    ("R2", "Extraction confidence", f"Each critical field needs confidence >= {MIN_CONFIDENCE_TEXT:.0%} (clean PDF) or >= {MIN_CONFIDENCE_SCANNED:.0%} (scanned). Below -> review."),
    ("R3", "Duplicate invoice", "Same vendor + same invoice number already registered -> reject (classic double-payment risk)."),
    ("R4", "Approved vendor", "Vendor must resolve to the approved-vendor master (tax id, exact name, alias or fuzzy >= 86%). Unknown or suspended -> reject."),
    ("R5", "PO lookup", "Quoted PO must exist. Not found -> reject. No PO quoted -> try to infer a unique open PO for this vendor; inferred -> review, never auto-pay."),
    ("R6", "PO ownership and status", "PO must belong to the resolved vendor and be open. Mismatch or closed -> reject."),
    ("R7", "Currency", "Invoice currency must equal PO currency. Mismatch -> reject."),
    ("R8", "Amount vs PO remaining balance", f"Handles split invoices via the ledger. Within {AUTO_APPROVE_TOLERANCE_PCT:g}% -> approve; up to {REVIEW_TOLERANCE_PCT:g}% over -> review; beyond -> reject."),
    ("R9", "Arithmetic", f"Line items must add to subtotal and subtotal + tax must equal total (slack {ARITHMETIC_TOLERANCE:.2f}). Mismatch -> review."),
    ("R10", "Date sanity", f"Invoice date not in the future and not older than {MAX_INVOICE_AGE_DAYS} days. Otherwise -> review."),
    ("R11", "Anomaly notes", "Extractor remarks (copy/reminder banner, changed bank details, urgency language) are surfaced to the reviewer."),
]


@dataclass
class Check:
    id: str
    name: str
    status: str                 # pass | info | warn | fail | skip
    message: str
    details: dict = field(default_factory=dict)


@dataclass
class Decision:
    status: str                 # APPROVED | NEEDS_REVIEW | REJECTED
    summary: str
    next_action: str
    reasons: list[str]
    checks: list[Check]
    po_number: str | None
    po_match_method: str
    amount_to_consume: float | None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def evaluate(extracted: dict, source_type: str, vendor: VendorMatch, po: POMatch, previous: dict | None, today: date | None = None) -> Decision:
    today = today or date.today()
    checks: list[Check] = []
    conf = extracted.get("field_confidence") or {}
    total = extracted.get("total")
    currency = extracted.get("currency")

    # R1 required fields
    missing = [f for f in CRITICAL_FIELDS if extracted.get(f) in (None, "", 0)]
    if missing:
        checks.append(Check("R1", "Required fields present", "warn", f"Missing on the document: {', '.join(missing)}.", {"missing": missing}))
    else:
        checks.append(Check("R1", "Required fields present", "pass", "Vendor, invoice number, date and total all present."))

    # R2 confidence
    threshold = MIN_CONFIDENCE_SCANNED if source_type == "scanned" else MIN_CONFIDENCE_TEXT
    low = {f: conf.get(f) for f in CRITICAL_FIELDS if f not in missing and conf.get(f) is not None and conf.get(f) < threshold}
    if low:
        checks.append(Check("R2", "Extraction confidence", "warn", f"Low confidence (< {threshold:.0%}) on: " + ", ".join(f"{k} ({v:.0%})" for k, v in low.items()), {"threshold": threshold, "low": low}))
    else:
        checks.append(Check("R2", "Extraction confidence", "pass", f"All critical fields >= {threshold:.0%} ({'scanned document' if source_type == 'scanned' else 'clean PDF'} threshold).", {"threshold": threshold, "confidence": conf}))

    # R3 duplicate
    if previous:
        checks.append(Check("R3", "Duplicate invoice", "fail", f"Invoice {extracted.get('invoice_number')} from this vendor was already registered on {previous['created_at'][:10]} (run #{previous['id']}, decision {previous['decision']}).", {"previous_run_id": previous["id"]}))
    else:
        checks.append(Check("R3", "Duplicate invoice", "pass", "No earlier invoice with this number from this vendor."))

    # R4 vendor
    if vendor.vendor is None:
        checks.append(Check("R4", "Approved vendor", "fail", f"'{extracted.get('vendor_name')}' is not in the approved-vendor master (best similarity {vendor.score:.0%}).", {"candidates": vendor.candidates}))
    elif vendor.vendor.get("status") != "active":
        checks.append(Check("R4", "Approved vendor", "fail", f"Vendor {vendor.vendor['vendor_name']} is {vendor.vendor['status']} in the master.", {"vendor_id": vendor.vendor["vendor_id"]}))
    else:
        checks.append(Check("R4", "Approved vendor", "pass", f"Resolved to {vendor.vendor['vendor_name']} ({vendor.vendor['vendor_id']}) via {vendor.method.replace('_', ' ')}.", {"vendor_id": vendor.vendor["vendor_id"], "method": vendor.method, "score": vendor.score}))

    # R5 PO lookup
    if po.method == "explicit":
        checks.append(Check("R5", "PO lookup", "pass", f"Quoted {po.quoted_reference} found as {po.po['po_number']}: {po.po['description']}.", {"po": po.po["po_number"]}))
    elif po.method == "not_found":
        checks.append(Check("R5", "PO lookup", "fail", f"Quoted PO '{po.quoted_reference}' does not exist in the procurement system.", {}))
    elif po.method == "inferred":
        checks.append(Check("R5", "PO lookup", "warn", f"No PO quoted on the invoice. Exactly one open PO for this vendor matches the amount: {po.po['po_number']} ({po.po['description']}, remaining {po.po['remaining']:,.2f} {po.po['currency']}). Suggested, not confirmed.", {"suggested_po": po.po["po_number"]}))
    elif po.method == "ambiguous":
        checks.append(Check("R5", "PO lookup", "warn", "No PO quoted and several open POs match the amount: " + ", ".join(p["po_number"] for p in po.candidates) + ".", {"candidates": [p["po_number"] for p in po.candidates]}))
    else:
        if vendor.vendor is None:
            checks.append(Check("R5", "PO lookup", "skip", "No PO quoted; cannot infer because the vendor is unknown."))
        else:
            checks.append(Check("R5", "PO lookup", "warn", "No PO quoted and no open PO for this vendor matches the amount. Non-PO invoice needs an approver.", {"open_pos": [p["po_number"] for p in po.candidates]}))

    # R6 ownership + status
    if po.po is None:
        checks.append(Check("R6", "PO ownership and status", "skip", "No PO to check."))
    elif vendor.vendor and po.po["vendor_id"] != vendor.vendor["vendor_id"]:
        checks.append(Check("R6", "PO ownership and status", "fail", f"{po.po['po_number']} belongs to {po.po['vendor_name']}, not to the invoicing vendor.", {}))
    elif vendor.vendor is None:
        checks.append(Check("R6", "PO ownership and status", "fail", f"{po.po['po_number']} belongs to {po.po['vendor_name']}; the invoicing party is unknown.", {}))
    elif po.po["status"] != "open":
        checks.append(Check("R6", "PO ownership and status", "fail", f"{po.po['po_number']} is {po.po['status']}.", {}))
    else:
        checks.append(Check("R6", "PO ownership and status", "pass", f"{po.po['po_number']} belongs to this vendor and is open."))

    # R7 currency
    if po.po is None or not currency:
        checks.append(Check("R7", "Currency", "skip", "No PO or no currency to compare."))
    elif currency != po.po["currency"]:
        checks.append(Check("R7", "Currency", "fail", f"Invoice is in {currency}, PO is in {po.po['currency']}.", {}))
    else:
        checks.append(Check("R7", "Currency", "pass", f"Invoice and PO both in {currency}."))

    # R8 amount vs remaining balance
    amount_to_consume = None
    if po.po is None or total is None:
        checks.append(Check("R8", "Amount vs PO remaining balance", "skip", "No PO or no total to compare."))
    else:
        p = po.po
        remaining, po_amt = p["remaining"], p["po_amount"]
        over = total - remaining
        over_pct = (over / po_amt * 100) if po_amt else 0.0
        split_note = ""
        if p["invoice_numbers"]:
            split_note = f" PO already invoiced {p['invoiced_amount']:,.2f} of {po_amt:,.2f} by {', '.join(p['invoice_numbers'])}; remaining {remaining:,.2f}."
        if remaining <= 0:
            checks.append(Check("R8", "Amount vs PO remaining balance", "fail", f"PO is fully consumed; nothing left to bill against.{split_note}", {"remaining": remaining, "invoice_total": total}))
        elif over <= po_amt * AUTO_APPROVE_TOLERANCE_PCT / 100:
            msg = f"Invoice {total:,.2f} vs remaining {remaining:,.2f}: " + ("exact match." if abs(over) < 0.005 else f"{over_pct:+.2f}% of PO, within the {AUTO_APPROVE_TOLERANCE_PCT:g}% auto-approve tolerance.")
            checks.append(Check("R8", "Amount vs PO remaining balance", "pass", msg + split_note, {"remaining": remaining, "invoice_total": total, "variance_pct": round(over_pct, 2)}))
            amount_to_consume = total
        elif over <= po_amt * REVIEW_TOLERANCE_PCT / 100:
            checks.append(Check("R8", "Amount vs PO remaining balance", "warn", f"Invoice {total:,.2f} exceeds remaining {remaining:,.2f} by {over:,.2f} ({over_pct:+.2f}% of PO). Above the {AUTO_APPROVE_TOLERANCE_PCT:g}% auto-approve tolerance, below the {REVIEW_TOLERANCE_PCT:g}% hard limit.{split_note}", {"remaining": remaining, "invoice_total": total, "variance_pct": round(over_pct, 2)}))
        else:
            checks.append(Check("R8", "Amount vs PO remaining balance", "fail", f"Invoice {total:,.2f} exceeds remaining {remaining:,.2f} by {over:,.2f} ({over_pct:+.2f}% of PO), beyond the {REVIEW_TOLERANCE_PCT:g}% limit.{split_note}", {"remaining": remaining, "invoice_total": total, "variance_pct": round(over_pct, 2)}))

    # R9 arithmetic
    items = extracted.get("line_items") or []
    subtotal, tax = extracted.get("subtotal"), extracted.get("tax_amount") or 0.0
    problems = []
    if items and subtotal is not None and all(i.get("amount") is not None for i in items):
        s = sum(i["amount"] for i in items)
        if abs(s - subtotal) > ARITHMETIC_TOLERANCE:
            problems.append(f"line items add to {s:,.2f} but subtotal says {subtotal:,.2f}")
    if subtotal is not None and total is not None and abs(subtotal + tax - total) > ARITHMETIC_TOLERANCE:
        problems.append(f"subtotal {subtotal:,.2f} + tax {tax:,.2f} = {subtotal + tax:,.2f}, but total says {total:,.2f}")
    if problems:
        checks.append(Check("R9", "Arithmetic", "warn", "; ".join(problems) + ".", {}))
    elif total is None:
        checks.append(Check("R9", "Arithmetic", "skip", "No total to verify."))
    else:
        checks.append(Check("R9", "Arithmetic", "pass", f"{len(items)} line item(s) + tax reconcile to the total."))

    # R10 dates
    d = _parse_date(extracted.get("invoice_date"))
    if d is None:
        checks.append(Check("R10", "Date sanity", "skip" if "invoice_date" in missing else "warn", "Invoice date missing or unreadable." if "invoice_date" in missing else f"Could not parse date '{extracted.get('invoice_date')}'."))
    elif (d - today).days > FUTURE_DATE_SLACK_DAYS:
        checks.append(Check("R10", "Date sanity", "warn", f"Invoice is dated in the future ({d})."))
    elif (today - d).days > MAX_INVOICE_AGE_DAYS:
        checks.append(Check("R10", "Date sanity", "warn", f"Invoice is {(today - d).days} days old; older than {MAX_INVOICE_AGE_DAYS} days."))
    else:
        checks.append(Check("R10", "Date sanity", "pass", f"Dated {d}, {(today - d).days} day(s) ago."))

    # R11 anomaly notes
    notes = (extracted.get("notes") or "").strip()
    flag_words = ("copy", "reminder", "duplicate", "bank", "urgent", "handwritten", "altered")
    if notes and any(w in notes.lower() for w in flag_words):
        checks.append(Check("R11", "Anomaly notes", "info", notes, {}))
    elif notes:
        checks.append(Check("R11", "Anomaly notes", "pass", notes))
    else:
        checks.append(Check("R11", "Anomaly notes", "pass", "Nothing unusual noted on the document."))

    # ---- decision -------------------------------------------------------------------------
    fails = [c for c in checks if c.status == "fail"]
    warns = [c for c in checks if c.status == "warn"]
    infos = [c for c in checks if c.status == "info"]
    vendor_label = vendor.vendor["vendor_name"] if vendor.vendor else (extracted.get("vendor_name") or "unknown vendor")
    inv_label = extracted.get("invoice_number") or "(no invoice number)"
    amt_label = f"{total:,.2f} {currency or ''}".strip() if total is not None else "unknown amount"

    if fails:
        status = "REJECTED"
        summary = f"Reject {inv_label} from {vendor_label} for {amt_label}: " + " ".join(c.message for c in fails)
        if any(c.id == "R3" for c in fails):
            next_action = "Do not pay. Reply to the vendor that this invoice is already registered and quote the original run reference."
        elif any(c.id == "R4" for c in fails):
            next_action = "Do not pay. Forward to procurement: vendor is not onboarded. Treat changed bank details or urgency language as a fraud signal."
        elif any(c.id in ("R5", "R6", "R7") for c in fails):
            next_action = "Return to vendor: the quoted PO is invalid for this invoice. Ask them to re-issue with the correct PO."
        else:
            next_action = "Return to vendor: amount exceeds what the PO allows. Ask for a credit note or a PO amendment from the buyer."
        amount_to_consume = None
    elif warns:
        status = "NEEDS_REVIEW"
        summary = f"Hold {inv_label} from {vendor_label} for {amt_label}: " + " ".join(c.message for c in warns)
        if any(c.id == "R1" for c in warns):
            next_action = "Email the vendor for the missing fields before registering the invoice. Nothing is posted until it arrives."
        elif any(c.id == "R5" for c in warns):
            next_action = "AP reviewer: confirm the suggested PO with the requester, then approve against it. Never auto-pay on an inferred PO."
        elif any(c.id == "R8" for c in warns):
            next_action = "AP reviewer: confirm the overage with the PO owner (quantity change?) and get a PO amendment, or short-pay to the PO amount."
        elif any(c.id == "R2" for c in warns):
            next_action = "AP reviewer: verify the low-confidence fields against the scanned image before posting."
        else:
            next_action = "AP reviewer: resolve the flagged checks, then approve or reject manually."
        amount_to_consume = None
    else:
        status = "APPROVED"
        po_txt = f" against {po.po['po_number']}" if po.po else ""
        summary = f"Approve {inv_label} from {vendor_label} for {amt_label}{po_txt}. All {len(checks)} checks passed."
        terms = vendor.vendor.get("payment_terms", "NET30") if vendor.vendor else "NET30"
        next_action = f"Post to the ledger and schedule payment on {terms} terms" + (f" (due {extracted.get('due_date')})" if extracted.get("due_date") else "") + "."
        if infos:
            summary += " Note: " + " ".join(c.message for c in infos)

    reasons = [f"{c.id} {c.name}: {c.message}" for c in fails + warns + infos]
    return Decision(status, summary, next_action, reasons, checks, po.po["po_number"] if po.po else None, po.method, amount_to_consume)
