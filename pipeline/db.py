"""
Persistence: SQLite store for run history and the PO ledger (how much of each PO has been invoiced).

The approved-vendor master and the PO master live in CSV files under data/ so they are easy to inspect
and edit. The ledger (consumed balances) and the run history live in data/runs.db.
"""
from __future__ import annotations

import csv
import json
import os
import sqlite3
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(HERE, "data")
DB_PATH = os.path.join(DATA_DIR, "runs.db")
VENDORS_CSV = os.path.join(DATA_DIR, "approved_vendors.csv")
POS_CSV = os.path.join(DATA_DIR, "purchase_orders.csv")


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL,
            file_name TEXT, source_type TEXT, extractor TEXT, model TEXT,
            vendor_name TEXT, vendor_id TEXT, invoice_number TEXT, invoice_date TEXT,
            currency TEXT, total REAL, po_number TEXT, po_match_method TEXT,
            decision TEXT NOT NULL, summary TEXT, next_action TEXT,
            extracted_json TEXT, checks_json TEXT, stages_json TEXT, duration_ms INTEGER
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS po_ledger (
            po_number TEXT PRIMARY KEY,
            invoiced_amount REAL NOT NULL DEFAULT 0,
            invoice_numbers TEXT NOT NULL DEFAULT ''
        )""")
        for po in _read_csv(POS_CSV):
            c.execute("INSERT OR IGNORE INTO po_ledger (po_number) VALUES (?)", (po["po_number"],))


def reset_db() -> None:
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    init_db()


def _read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# --------------------------------------------------------------------------- masters
def load_vendors() -> list[dict]:
    rows = _read_csv(VENDORS_CSV)
    for r in rows:
        r["aliases"] = [a.strip() for a in r.get("aliases", "").split(";") if a.strip()]
    return rows


def load_pos() -> list[dict]:
    """PO master joined with the ledger -> adds invoiced_amount, remaining, invoice_numbers."""
    init_db()
    pos = _read_csv(POS_CSV)
    with _conn() as c:
        ledger = {r["po_number"]: r for r in c.execute("SELECT * FROM po_ledger")}
    for po in pos:
        po["po_amount"] = float(po["po_amount"])
        led = ledger.get(po["po_number"])
        po["invoiced_amount"] = float(led["invoiced_amount"]) if led else 0.0
        po["invoice_numbers"] = [x for x in (led["invoice_numbers"] if led else "").split("|") if x]
        po["remaining"] = round(po["po_amount"] - po["invoiced_amount"], 2)
    return pos


def get_po(po_number: str) -> dict | None:
    return next((p for p in load_pos() if p["po_number"] == po_number), None)


def consume_po(po_number: str, amount: float, invoice_number: str) -> None:
    with _conn() as c:
        row = c.execute("SELECT * FROM po_ledger WHERE po_number=?", (po_number,)).fetchone()
        nums = [x for x in (row["invoice_numbers"] if row else "").split("|") if x]
        if invoice_number not in nums:
            nums.append(invoice_number)
        c.execute("INSERT INTO po_ledger (po_number, invoiced_amount, invoice_numbers) VALUES (?,?,?) "
                  "ON CONFLICT(po_number) DO UPDATE SET invoiced_amount=invoiced_amount+excluded.invoiced_amount, invoice_numbers=excluded.invoice_numbers",
                  (po_number, amount, "|".join(nums)))


# --------------------------------------------------------------------------- runs
def find_previous_invoice(vendor_id: str | None, vendor_name: str | None, invoice_number: str | None) -> dict | None:
    """A previously registered (approved or pending) invoice with the same number from the same vendor."""
    if not invoice_number:
        return None
    init_db()
    with _conn() as c:
        if vendor_id:
            row = c.execute("SELECT * FROM runs WHERE vendor_id=? AND UPPER(invoice_number)=UPPER(?) AND decision IN ('APPROVED','NEEDS_REVIEW') ORDER BY id LIMIT 1",
                            (vendor_id, invoice_number)).fetchone()
        else:
            row = c.execute("SELECT * FROM runs WHERE UPPER(vendor_name)=UPPER(?) AND UPPER(invoice_number)=UPPER(?) AND decision IN ('APPROVED','NEEDS_REVIEW') ORDER BY id LIMIT 1",
                            (vendor_name or "", invoice_number)).fetchone()
    return dict(row) if row else None


def save_run(run: dict) -> int:
    init_db()
    run = dict(run)
    run.setdefault("created_at", datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for k in ("extracted_json", "checks_json", "stages_json"):
        if not isinstance(run.get(k), str):
            run[k] = json.dumps(run.get(k), default=str)
    cols = ["created_at", "file_name", "source_type", "extractor", "model", "vendor_name", "vendor_id", "invoice_number", "invoice_date",
            "currency", "total", "po_number", "po_match_method", "decision", "summary", "next_action", "extracted_json", "checks_json", "stages_json", "duration_ms"]
    with _conn() as c:
        cur = c.execute(f"INSERT INTO runs ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})", [run.get(k) for k in cols])
        return cur.lastrowid


def list_runs() -> list[dict]:
    init_db()
    with _conn() as c:
        return [dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC")]


def get_run(run_id: int) -> dict | None:
    init_db()
    with _conn() as c:
        row = c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in ("extracted_json", "checks_json", "stages_json"):
        try:
            d[k] = json.loads(d[k]) if d.get(k) else None
        except json.JSONDecodeError:
            pass
    return d


def metrics() -> dict:
    runs = list_runs()
    by = {"APPROVED": 0, "NEEDS_REVIEW": 0, "REJECTED": 0}
    approved_value: dict[str, float] = {}
    for r in runs:
        by[r["decision"]] = by.get(r["decision"], 0) + 1
        if r["decision"] == "APPROVED" and r.get("total"):
            approved_value[r.get("currency") or "?"] = approved_value.get(r.get("currency") or "?", 0) + float(r["total"])
    avg_ms = int(sum((r.get("duration_ms") or 0) for r in runs) / len(runs)) if runs else 0
    return {"total": len(runs), "by_decision": by, "approved_value": approved_value, "avg_duration_ms": avg_ms}
