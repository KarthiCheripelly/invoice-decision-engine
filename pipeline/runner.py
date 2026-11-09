"""
Orchestrates one invoice through every stage and streams progress events so the UI can render
each stage as it executes.

    for event in run_pipeline(pdf_bytes, "invoice.pdf"):
        ...   # {"stage": "Extract", "status": "running"|"done"|"error", "detail": str, "data": {...}}
    # final event: {"stage": "__result__", "result": RunResult}
"""
from __future__ import annotations

import io
import time
import traceback
from dataclasses import asdict, dataclass
from typing import Iterator

from pypdf import PdfReader

from . import db
from .extract import ExtractionResult, active_extractor_label, api_key_present, detect_source_type, extract_invoice
from .matching import POMatch, VendorMatch, find_po, infer_po, resolve_vendor
from .rules import Decision, evaluate

STAGES = ["Ingest", "Extract", "Validate", "Resolve vendor", "Match PO", "Apply rules", "Persist"]


@dataclass
class RunResult:
    run_id: int | None
    file_name: str
    source_type: str
    extractor: str
    model: str | None
    extracted: dict
    vendor: dict | None
    vendor_method: str
    po: dict | None
    po_method: str
    decision: dict
    stages: list[dict]
    duration_ms: int


def _event(stage: str, status: str, detail: str = "", data: dict | None = None) -> dict:
    return {"stage": stage, "status": status, "detail": detail, "data": data or {}, "t": time.time()}


def run_pipeline(pdf_bytes: bytes, file_name: str) -> Iterator[dict]:
    t0 = time.time()
    stages: list[dict] = []

    def log(stage: str, status: str, detail: str = "", data: dict | None = None):
        ev = _event(stage, status, detail, data)
        if status != "running":
            stages.append({"stage": stage, "status": status, "detail": detail, "ms": int((time.time() - t0) * 1000)})
        return ev

    # 1 Ingest ------------------------------------------------------------------------------
    yield log("Ingest", "running", "Reading file and checking for a text layer")
    try:
        pages = len(PdfReader(io.BytesIO(pdf_bytes)).pages)
    except Exception as e:  # not a PDF
        yield log("Ingest", "error", f"Not a readable PDF: {e}")
        return
    source_type, text = detect_source_type(pdf_bytes)
    yield log("Ingest", "done", f"{pages} page(s), {len(pdf_bytes)/1024:.0f} KB. " + ("Text layer found: clean digital PDF." if source_type == "text" else "No text layer: treating as a scanned image, vision extraction required."),
              {"pages": pages, "bytes": len(pdf_bytes), "source_type": source_type, "text_chars": len(text.strip())})

    # 2 Extract -----------------------------------------------------------------------------
    extractor_label = active_extractor_label() + (" vision + structured output" if api_key_present() else " (no API key set)")
    yield log("Extract", "running", f"Extracting fields with {extractor_label}")
    try:
        ex: ExtractionResult = extract_invoice(pdf_bytes, file_name)
    except Exception as e:
        yield log("Extract", "error", f"Extraction failed: {e}", {"traceback": traceback.format_exc()})
        # still record a run so the dashboard shows the failure
        decision = {"status": "NEEDS_REVIEW", "summary": f"Extraction failed: {e}", "next_action": "Process manually or re-run with the vision model configured.",
                    "reasons": [f"Extraction error: {e}"], "checks": [], "po_number": None, "po_match_method": "none", "amount_to_consume": None}
        run_id = db.save_run({"file_name": file_name, "source_type": source_type, "extractor": "none", "model": None, "decision": "NEEDS_REVIEW",
                              "summary": decision["summary"], "next_action": decision["next_action"], "extracted_json": {}, "checks_json": [],
                              "stages_json": stages, "duration_ms": int((time.time() - t0) * 1000)})
        yield {"stage": "__result__", "result": RunResult(run_id, file_name, source_type, "none", None, {}, None, "none", None, "none", decision, stages, int((time.time() - t0) * 1000))}
        return
    d = ex.data
    conf = d.get("field_confidence") or {}
    yield log("Extract", "done",
              f"Vendor '{d.get('vendor_name')}', invoice {d.get('invoice_number') or '(none)'}, dated {d.get('invoice_date') or '(none)'}, PO {d.get('po_reference') or '(none)'}, total {d.get('total')} {d.get('currency') or ''}. {len(d.get('line_items') or [])} line item(s).",
              {"extractor": ex.extractor, "model": ex.model, "usage": ex.usage, "extracted": d})

    # 3 Validate ----------------------------------------------------------------------------
    yield log("Validate", "running", "Checking required fields, confidence and arithmetic")
    missing = [f for f in ("vendor_name", "invoice_number", "invoice_date", "total") if d.get(f) in (None, "", 0)]
    low_conf = {k: v for k, v in conf.items() if v is not None and v < 0.85}
    msg = ("Missing: " + ", ".join(missing) + ". " if missing else "All required fields present. ")
    msg += ("Lower-confidence fields: " + ", ".join(f"{k} {v:.0%}" for k, v in low_conf.items()) + "." if low_conf else "Confidence is high on every field.")
    yield log("Validate", "done", msg, {"missing": missing, "confidence": conf, "document_quality": d.get("document_quality")})

    # 4 Vendor ------------------------------------------------------------------------------
    yield log("Resolve vendor", "running", "Looking up the approved-vendor master")
    vendors = db.load_vendors()
    vm: VendorMatch = resolve_vendor(d.get("vendor_name"), d.get("vendor_tax_id"), vendors)
    if vm.vendor:
        yield log("Resolve vendor", "done", f"Matched to {vm.vendor['vendor_name']} ({vm.vendor['vendor_id']}, {vm.vendor['status']}) by {vm.method.replace('_', ' ')}.", {"vendor": vm.vendor, "method": vm.method, "score": vm.score})
    else:
        yield log("Resolve vendor", "done", f"No approved vendor matches '{d.get('vendor_name')}'. Closest: " + (", ".join(f"{n} ({s:.0%})" for n, s in vm.candidates) or "none") + ".", {"method": vm.method, "candidates": vm.candidates})

    # 5 PO ----------------------------------------------------------------------------------
    yield log("Match PO", "running", "Matching against open purchase orders and the ledger")
    pos = db.load_pos()
    pm: POMatch = find_po(d.get("po_reference"), pos)
    if pm.method == "none" and vm.vendor:
        pm = infer_po(vm.vendor["vendor_id"], d.get("total"), d.get("currency"), pos)
    if pm.po:
        p = pm.po
        yield log("Match PO", "done", f"{pm.method.title()} match: {p['po_number']} ({p['description']}), {p['currency']} {p['po_amount']:,.2f}, invoiced so far {p['invoiced_amount']:,.2f}, remaining {p['remaining']:,.2f}, status {p['status']}.", {"po": p, "method": pm.method})
    elif pm.method == "not_found":
        yield log("Match PO", "done", f"Quoted PO '{pm.quoted_reference}' not found in the procurement system.", {"method": pm.method})
    elif pm.method == "ambiguous":
        yield log("Match PO", "done", "No PO quoted; several open POs match the amount: " + ", ".join(p["po_number"] for p in pm.candidates), {"method": pm.method})
    else:
        yield log("Match PO", "done", "No PO quoted and no unique open PO matches the amount.", {"method": pm.method, "open_pos": [p["po_number"] for p in pm.candidates]})

    # 6 Rules -------------------------------------------------------------------------------
    yield log("Apply rules", "running", "Running duplicate, tolerance, currency and arithmetic checks")
    previous = db.find_previous_invoice(vm.vendor["vendor_id"] if vm.vendor else None, d.get("vendor_name"), d.get("invoice_number"))
    decision: Decision = evaluate(d, source_type, vm, pm, previous)
    counts = {s: sum(1 for c in decision.checks if c.status == s) for s in ("pass", "info", "warn", "fail", "skip")}
    yield log("Apply rules", "done", f"{counts['pass']} passed, {counts['warn']} need review, {counts['fail']} failed, {counts['skip']} skipped. Decision: {decision.status}.",
              {"checks": [asdict(c) for c in decision.checks], "decision": decision.status})

    # 7 Persist -----------------------------------------------------------------------------
    yield log("Persist", "running", "Saving the run and updating the PO ledger")
    duration_ms = int((time.time() - t0) * 1000)
    run_id = db.save_run({
        "file_name": file_name, "source_type": source_type, "extractor": ex.extractor, "model": ex.model,
        "vendor_name": vm.vendor["vendor_name"] if vm.vendor else d.get("vendor_name"), "vendor_id": vm.vendor["vendor_id"] if vm.vendor else None,
        "invoice_number": d.get("invoice_number"), "invoice_date": d.get("invoice_date"), "currency": d.get("currency"), "total": d.get("total"),
        "po_number": decision.po_number, "po_match_method": decision.po_match_method, "decision": decision.status,
        "summary": decision.summary, "next_action": decision.next_action, "extracted_json": d, "checks_json": [asdict(c) for c in decision.checks],
        "stages_json": stages, "duration_ms": duration_ms,
    })
    ledger_msg = ""
    if decision.status == "APPROVED" and decision.po_number and decision.amount_to_consume:
        db.consume_po(decision.po_number, decision.amount_to_consume, d.get("invoice_number") or f"run-{run_id}")
        after = db.get_po(decision.po_number)
        ledger_msg = f" Ledger: {decision.po_number} remaining balance now {after['remaining']:,.2f} {after['currency']}."
    yield log("Persist", "done", f"Saved as run #{run_id}.{ledger_msg}", {"run_id": run_id})

    dec = asdict(decision)
    yield {"stage": "__result__", "result": RunResult(run_id, file_name, source_type, ex.extractor, ex.model, d, vm.vendor, vm.method, pm.po, pm.method, dec, stages, duration_ms)}


def run_to_completion(pdf_bytes: bytes, file_name: str) -> RunResult | None:
    """Convenience for tests / CLI: consume all events and return the final result."""
    result = None
    for ev in run_pipeline(pdf_bytes, file_name):
        if ev["stage"] == "__result__":
            result = ev["result"]
    return result
