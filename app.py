"""
Invoice Decision Engine - Streamlit UI

    streamlit run app.py

Tabs:
  Run invoice          live, stage-by-stage execution of one invoice
  Dashboard            history, status mix, approved value, PO ledger, drill-down into any run
  Rules & assumptions  the policy the engine applies, edge cases, demo script
  Master data          approved vendors and purchase orders
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

load_dotenv()

from pipeline import db  # noqa: E402
from pipeline.extract import active_extractor_label, api_key_present  # noqa: E402
from pipeline.rules import (AUTO_APPROVE_TOLERANCE_PCT, MIN_CONFIDENCE_SCANNED, MIN_CONFIDENCE_TEXT, REVIEW_TOLERANCE_PCT, RULE_CATALOGUE)  # noqa: E402
from pipeline.runner import STAGES, run_pipeline  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES_DIR = os.path.join(HERE, "samples")

st.set_page_config(page_title="Invoice Decision Engine", page_icon="🧾", layout="wide")
db.init_db()

SAMPLE_LABELS = {
    "01_happy_path_acme.pdf": "01 · Happy path · Acme, clean PDF, exact PO match",
    "02_scanned_northwind.pdf": "02 · Edge · Northwind, scanned image (no text layer)",
    "03_missing_po_globex.pdf": "03 · Edge · Globex, no PO quoted on invoice",
    "04a_split_initech_1of2.pdf": "04a · Edge · Initech, split PO - invoice 1 of 2",
    "04b_split_initech_2of2.pdf": "04b · Edge · Initech, split PO - invoice 2 of 2",
    "04c_split_initech_overrun.pdf": "04c · Edge · Initech, third invoice after PO is consumed",
    "05_duplicate_acme.pdf": "05 · Edge · Acme, reminder copy of an already-paid invoice",
    "06_unknown_vendor.pdf": "06 · Edge · Unknown vendor quoting someone else's PO",
    "07_over_tolerance_acme.pdf": "07 · Edge · Acme, 4.9% over the PO amount",
    "08_missing_fields_globex.pdf": "08 · Edge · Globex, no invoice number or date",
}
DECISION_STYLE = {
    "APPROVED": ("✅", "success", "Approved - release for payment"),
    "NEEDS_REVIEW": ("🟡", "warning", "Needs review - routed to AP reviewer"),
    "REJECTED": ("⛔", "error", "Rejected - do not pay"),
}
CHECK_ICON = {"pass": "✅", "info": "ℹ️", "warn": "🟡", "fail": "⛔", "skip": "➖"}


# --------------------------------------------------------------------------- helpers
def money(v, cur=None) -> str:
    if v is None:
        return "-"
    return f"{v:,.2f}" + (f" {cur}" if cur else "")


def decision_banner(decision: dict) -> None:
    icon, kind, label = DECISION_STYLE[decision["status"]]
    body = f"### {icon} {label}\n\n**Why:** {decision['summary']}\n\n**Next action:** {decision['next_action']}"
    getattr(st, kind)(body)


def checks_table(checks: list[dict]) -> None:
    rows = [{"": CHECK_ICON.get(c["status"], ""), "Rule": c["id"], "Check": c["name"], "Result": c["status"].upper(), "Detail": c["message"]} for c in checks]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 column_config={"": st.column_config.TextColumn(width="small"), "Rule": st.column_config.TextColumn(width="small"),
                                "Result": st.column_config.TextColumn(width="small"), "Detail": st.column_config.TextColumn(width="large")})


def extracted_panel(ex: dict, source_type: str, extractor: str, model: str | None) -> None:
    conf = ex.get("field_confidence") or {}
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Header fields**")
        fields = [("Vendor", ex.get("vendor_name"), conf.get("vendor_name")), ("Vendor tax id", ex.get("vendor_tax_id"), None),
                  ("Invoice number", ex.get("invoice_number"), conf.get("invoice_number")), ("Invoice date", ex.get("invoice_date"), conf.get("invoice_date")),
                  ("Due date", ex.get("due_date"), None), ("PO reference", ex.get("po_reference"), conf.get("po_reference")),
                  ("Currency", ex.get("currency"), None), ("Subtotal", money(ex.get("subtotal")), None),
                  (f"Tax ({ex.get('tax_rate_percent') or 0:g}%)", money(ex.get("tax_amount")), None), ("Total", money(ex.get("total")), conf.get("total"))]
        st.dataframe(pd.DataFrame([{"Field": f, "Value": v if v not in (None, "") else "(missing)", "Confidence": f"{c:.0%}" if c is not None else ""} for f, v, c in fields]),
                     hide_index=True, width="stretch")
    with c2:
        st.markdown("**Line items**")
        items = ex.get("line_items") or []
        if items:
            st.dataframe(pd.DataFrame(items), hide_index=True, width="stretch")
        else:
            st.caption("No line items extracted.")
        st.markdown("**Document**")
        st.caption(f"Source: {'scanned image' if source_type == 'scanned' else 'digital PDF with text layer'} · Extractor: {extractor}" + (f" · Model: {model}" if model else ""))
        if ex.get("document_quality"):
            st.caption(ex["document_quality"])
        if ex.get("notes"):
            st.info(f"Extractor notes: {ex['notes']}")
        if ex.get("bank_details"):
            st.caption(f"Bank details on invoice: {ex['bank_details']}")


def render_result(res) -> None:
    decision_banner(res.decision)
    t1, t2, t3 = st.tabs(["Checks", "Extracted data", "Raw JSON"])
    with t1:
        checks_table(res.decision["checks"])
        if res.po:
            p = db.get_po(res.po["po_number"]) or res.po
            st.caption(f"PO {p['po_number']} · {p['description']} · amount {money(p['po_amount'], p['currency'])} · invoiced {money(p['invoiced_amount'])} · remaining {money(p['remaining'])} · invoices: {', '.join(p['invoice_numbers']) or 'none'}")
    with t2:
        extracted_panel(res.extracted, res.source_type, res.extractor, res.model)
    with t3:
        st.json({"decision": res.decision, "extracted": res.extracted, "vendor": res.vendor, "po": res.po, "stages": res.stages, "duration_ms": res.duration_ms})


# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.title("🧾 Invoice Decision Engine")
    st.caption("PDF in → reasoned AP decision out. Every stage visible.")
    st.divider()
    if api_key_present():
        st.success(f"Extractor: {active_extractor_label()}\n\nHandles clean PDFs and scans.")
    else:
        st.warning("Extractor: local parser (no API key).\n\nClean PDFs work; scanned invoices need a vision model. Put `GEMINI_API_KEY` or `ANTHROPIC_API_KEY` in `.env` and restart.")
    pace = st.slider("Stage pacing for demo (seconds)", 0.0, 2.0, 0.6, 0.1, help="Adds a short pause between stages so viewers can follow the live run.")
    st.divider()
    runs_metric = st.empty()
    if st.button("Reset demo data", help="Clears run history and PO ledger balances. Use before a fresh demo."):
        db.reset_db()
        st.session_state.pop("last_result", None)
        st.toast("History and ledger reset.")
        st.rerun()

tab_run, tab_dash, tab_rules, tab_data = st.tabs(["▶  Run invoice", "📊  Dashboard", "📖  Rules & assumptions", "🗂  Master data"])

# --------------------------------------------------------------------------- RUN
with tab_run:
    left, right = st.columns([1, 2])
    with left:
        st.subheader("Input")
        mode = st.radio("Source", ["Sample invoice", "Upload a PDF"], horizontal=True, label_visibility="collapsed")
        pdf_bytes, file_name = None, None
        if mode == "Sample invoice":
            files = sorted(f for f in os.listdir(SAMPLES_DIR) if f.lower().endswith(".pdf")) if os.path.isdir(SAMPLES_DIR) else []
            if not files:
                st.error("No samples found. Run `python generate_samples.py` first.")
            else:
                choice = st.selectbox("Sample", files, format_func=lambda f: SAMPLE_LABELS.get(f, f))
                with open(os.path.join(SAMPLES_DIR, choice), "rb") as fh:
                    pdf_bytes, file_name = fh.read(), choice
        else:
            up = st.file_uploader("Invoice PDF", type=["pdf"], label_visibility="collapsed")
            if up is not None:
                pdf_bytes, file_name = up.getvalue(), up.name
        if pdf_bytes:
            st.caption(f"{file_name} · {len(pdf_bytes)/1024:.0f} KB")
            st.download_button("Open / download this PDF", pdf_bytes, file_name=file_name, mime="application/pdf")
        run_clicked = st.button("Run pipeline ▶", type="primary", disabled=pdf_bytes is None, width="stretch")

    with right:
        st.subheader("Live run")
        if run_clicked and pdf_bytes:
            st.session_state.pop("last_result", None)
            boxes: dict[str, object] = {}
            result = None
            for ev in run_pipeline(pdf_bytes, file_name):
                if ev["stage"] == "__result__":
                    result = ev["result"]
                    continue
                stage, status = ev["stage"], ev["status"]
                idx = STAGES.index(stage) + 1 if stage in STAGES else 0
                if status == "running":
                    boxes[stage] = st.status(f"{idx}/{len(STAGES)}  {stage} - {ev['detail']}", expanded=True)
                    if pace:
                        time.sleep(pace)
                else:
                    box = boxes.get(stage) or st.status(stage)
                    with box:
                        st.write(ev["detail"])
                        if stage == "Extract" and ev["data"].get("extracted"):
                            d = ev["data"]["extracted"]
                            st.caption(f"Confidence: " + ", ".join(f"{k} {v:.0%}" for k, v in (d.get("field_confidence") or {}).items()))
                            if ev["data"].get("usage") and ev["data"]["usage"].get("input_tokens"):
                                u = ev["data"]["usage"]
                                st.caption(f"{u.get('model')} · {u['input_tokens']} in / {u['output_tokens']} out tokens")
                        if stage == "Apply rules" and ev["data"].get("checks"):
                            st.write(" ".join(f"{CHECK_ICON[c['status']]} {c['id']}" for c in ev["data"]["checks"]))
                    box.update(label=f"{idx}/{len(STAGES)}  {stage} - {ev['detail'][:110]}", state="complete" if status == "done" else "error", expanded=False)
            if result:
                st.session_state["last_result"] = result
        elif "last_result" in st.session_state:
            res = st.session_state["last_result"]
            for i, s in enumerate(res.stages, 1):
                st.status(f"{i}/{len(STAGES)}  {s['stage']} - {s['detail'][:110]}", state="complete" if s["status"] == "done" else "error", expanded=False)
        else:
            st.info("Pick a sample (or upload a PDF) and press **Run pipeline**. Each stage appears here as it executes.")

    if "last_result" in st.session_state:
        st.divider()
        st.subheader("Decision")
        render_result(st.session_state["last_result"])

# --------------------------------------------------------------------------- DASHBOARD
with tab_dash:
    runs = db.list_runs()
    m = db.metrics()
    c = st.columns(6)
    c[0].metric("Total runs", m["total"])
    c[1].metric("Approved", m["by_decision"].get("APPROVED", 0))
    c[2].metric("Needs review", m["by_decision"].get("NEEDS_REVIEW", 0))
    c[3].metric("Rejected", m["by_decision"].get("REJECTED", 0))
    c[4].metric("Approved value", " · ".join(f"{v:,.0f} {k}" for k, v in m["approved_value"].items()) or "0")
    c[5].metric("Avg run time", f"{m['avg_duration_ms']/1000:.1f}s")

    st.subheader("Run history")
    if not runs:
        st.info("No runs yet.")
    else:
        flt = st.multiselect("Filter by decision", ["APPROVED", "NEEDS_REVIEW", "REJECTED"], default=["APPROVED", "NEEDS_REVIEW", "REJECTED"])
        df = pd.DataFrame([{
            "Run": r["id"], "When": datetime.fromisoformat(r["created_at"]).strftime("%Y-%m-%d %H:%M:%S"), "File": r["file_name"],
            "Vendor": r["vendor_name"], "Invoice": r["invoice_number"], "Total": r["total"], "Cur": r["currency"], "PO": r["po_number"],
            "PO match": r["po_match_method"], "Source": r["source_type"], "Decision": f"{DECISION_STYLE[r['decision']][0]} {r['decision']}", "Summary": r["summary"],
        } for r in runs if r["decision"] in flt])
        st.dataframe(df, hide_index=True, width="stretch",
                     column_config={"Total": st.column_config.NumberColumn(format="%.2f"), "Summary": st.column_config.TextColumn(width="large")})

        st.subheader("Run detail")
        pick = st.selectbox("Open a run", [r["id"] for r in runs], format_func=lambda i: next(f"#{r['id']} · {r['file_name']} · {r['decision']}" for r in runs if r["id"] == i))
        r = db.get_run(pick)
        if r:
            decision_banner({"status": r["decision"], "summary": r["summary"], "next_action": r["next_action"]})
            d1, d2 = st.tabs(["Checks", "Extracted data"])
            with d1:
                if r["checks_json"]:
                    checks_table(r["checks_json"])
                if r["stages_json"]:
                    st.caption("Stages: " + " → ".join(f"{s['stage']} ({s['ms']} ms)" for s in r["stages_json"]))
            with d2:
                if r["extracted_json"]:
                    extracted_panel(r["extracted_json"], r["source_type"], r["extractor"], r["model"])

    st.subheader("PO ledger")
    pos = db.load_pos()
    st.dataframe(pd.DataFrame([{
        "PO": p["po_number"], "Vendor": p["vendor_name"], "Description": p["description"], "Cur": p["currency"], "PO amount": p["po_amount"],
        "Invoiced": p["invoiced_amount"], "Remaining": p["remaining"], "Consumed": (p["invoiced_amount"] / p["po_amount"]) if p["po_amount"] else 0,
        "Status": p["status"], "Invoices": ", ".join(p["invoice_numbers"]),
    } for p in pos]), hide_index=True, width="stretch",
        column_config={"Consumed": st.column_config.ProgressColumn(min_value=0, max_value=1, format="%.0f%%"),
                       "PO amount": st.column_config.NumberColumn(format="%.2f"), "Invoiced": st.column_config.NumberColumn(format="%.2f"), "Remaining": st.column_config.NumberColumn(format="%.2f")})

# --------------------------------------------------------------------------- RULES
with tab_rules:
    st.subheader("How a decision is made")
    st.markdown(f"""
Every invoice goes through **7 stages**: Ingest → Extract → Validate → Resolve vendor → Match PO → Apply rules → Persist.
The rules stage runs the 11 checks below. **Any hard failure → REJECTED. Otherwise any warning → NEEDS_REVIEW. Otherwise → APPROVED**,
and an approved invoice consumes its amount from the PO ledger (which is how split invoices are tracked).

| Threshold | Value | Why |
|---|---|---|
| Auto-approve tolerance | {AUTO_APPROVE_TOLERANCE_PCT:g}% of PO | Covers rounding, freight and minor rate changes without a human |
| Review tolerance | {REVIEW_TOLERANCE_PCT:g}% of PO | Plausible quantity changes; a human confirms with the PO owner |
| Confidence floor, clean PDF | {MIN_CONFIDENCE_TEXT:.0%} | Text-layer PDFs are reliable |
| Confidence floor, scanned | {MIN_CONFIDENCE_SCANNED:.0%} | OCR-style reads deserve more scrutiny before money moves |
""")
    st.dataframe(pd.DataFrame([{"Rule": i, "Check": n, "Policy": p} for i, n, p in RULE_CATALOGUE]), hide_index=True, width="stretch",
                 column_config={"Policy": st.column_config.TextColumn(width="large")})

    st.subheader("Edge cases built into the samples")
    st.markdown("""
| Sample | What is different | Expected outcome |
|---|---|---|
| 02 scanned | Image-only PDF, no text layer. Extraction switches to the vision model and applies the stricter confidence floor. | APPROVED if every field reads at ≥ 85%, otherwise NEEDS_REVIEW |
| 03 no PO | Vendor did not quote a PO. Engine infers the single open PO whose balance matches the amount, but refuses to auto-pay on an inference. | NEEDS_REVIEW with the suggested PO pre-filled |
| 04a / 04b split | Two milestone invoices against one PO. The ledger tracks the consumed balance so the second invoice is judged against what is left. | Both APPROVED; PO shows 100% consumed |
| 04c overrun | A third invoice arrives after the PO is fully consumed. | REJECTED: nothing left to bill against |
| 05 duplicate | A "payment reminder" copy of an invoice that was already approved. | REJECTED: duplicate invoice number for the same vendor |
| 06 unknown vendor | A party not in the vendor master quotes Acme's PO and gives "updated bank details". | REJECTED: unknown vendor and PO ownership mismatch; bank-detail note surfaced as a fraud signal |
| 07 over tolerance | Invoice is 4.9% above the PO (quantity increased at site). | NEEDS_REVIEW: above 2% auto-approve, below 10% hard limit |
| 08 missing fields | Invoice has no number and no date. | NEEDS_REVIEW: cannot register; vendor is asked for the missing fields |
""")

    st.subheader("Assumptions")
    st.markdown("""
- The buyer runs a **3-way-lite match**: invoice vs PO (no goods-receipt data in scope). Receipt matching would be the next stage to add.
- A vendor is identified by **tax id first**, then legal name, alias, or fuzzy similarity ≥ 86%. Fuzzy matches are shown with their score.
- **Duplicate** means same vendor + same invoice number already approved or pending. A previously *rejected* invoice may be resubmitted.
- An **inferred PO is never auto-paid**. Paying against a PO the vendor did not cite is how mis-postings happen.
- Only **approved** invoices consume PO balance. Held invoices do not reserve balance (simplification; a real system would).
- Tolerances are **% of PO amount**, so a 2% band on a 10,000 PO is 200, regardless of how much of the PO is already used.
- Purchase orders and the vendor master are **CSV files** standing in for the procurement system; the ledger and run history are SQLite.
""")

    st.subheader("Demo script (recommended order)")
    st.markdown("""
1. **Reset demo data** in the sidebar.
2. Run **01** → APPROVED. Walk through the seven stages and the 11 checks.
3. Run **04a** then **04b** → both APPROVED; open the Dashboard and show PO-2026-0104 at 100% consumed.
4. Run **04c** → REJECTED, PO fully consumed. Point at the ledger line in the rule message.
5. Run **05** → REJECTED as a duplicate of run #1.
6. Run **06** → REJECTED, unknown vendor, PO belongs to someone else, bank-detail warning.
7. Run **03** → NEEDS_REVIEW with the inferred PO. Explain why it is not auto-approved.
8. Run **02** → scanned path with the vision model and the stricter confidence floor.
9. Finish on the Dashboard: status mix, approved value, drill into any run.
""")

# --------------------------------------------------------------------------- MASTER DATA
with tab_data:
    st.subheader("Approved vendors")
    v = pd.DataFrame(db.load_vendors())
    v["aliases"] = v["aliases"].apply(lambda a: ", ".join(a))
    st.dataframe(v, hide_index=True, width="stretch")
    st.subheader("Purchase orders")
    st.dataframe(pd.DataFrame([{k: p[k] for k in ("po_number", "vendor_id", "vendor_name", "description", "currency", "po_amount", "status", "issue_date")} for p in db.load_pos()]),
                 hide_index=True, width="stretch", column_config={"po_amount": st.column_config.NumberColumn(format="%.2f")})
    st.caption("Edit data/approved_vendors.csv and data/purchase_orders.csv to change the masters. Reset demo data afterwards so the ledger picks up new POs.")

# fill the sidebar counter last so it reflects the run that just finished
runs_metric.metric("Runs so far", db.metrics()["total"])
