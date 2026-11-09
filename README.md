# Invoice Decision Engine (ASA Case Study, PS-1)

Takes a vendor invoice PDF as input and produces a clear, reasoned accounts-payable decision as output:
**APPROVED**, **NEEDS_REVIEW** or **REJECTED**, with every stage and every check visible.

```
PDF  ->  Ingest  ->  Extract  ->  Validate  ->  Resolve vendor  ->  Match PO  ->  Apply rules  ->  Persist
                     (Claude                    (vendor master)   (PO master +   (11 checks,      (SQLite run
                      vision +                                      ledger)       3 outcomes)      history +
                      JSON schema)                                                                 PO ledger)
```

## Quick start

```bash
cd invoice-processor
pip install -r requirements.txt
copy .env.example .env          # then put your GEMINI_API_KEY (free, aistudio.google.com/apikey) in .env
python generate_samples.py      # creates samples/*.pdf (already included)
streamlit run app.py
```

The extractor is chosen automatically: Claude if `ANTHROPIC_API_KEY` is set, otherwise Gemini if
`GEMINI_API_KEY` is set, otherwise a local regex parser. The local parser handles clean PDFs only;
scanned invoices (sample 02) have no text layer and need one of the vision models.

Command-line alternative, useful for rehearsal:

```bash
python cli.py --reset samples/01_happy_path_acme.pdf samples/05_duplicate_acme.pdf
```

## What is in the box

| Path | Purpose |
|---|---|
| `app.py` | Streamlit UI: live run view, dashboard, rules page, master data |
| `pipeline/extract.py` | PDF -> structured JSON. Claude or Gemini with a JSON schema and per-field confidence; local fallback parser |
| `pipeline/matching.py` | Vendor resolution (tax id, name, alias, fuzzy) and PO matching (explicit or inferred) |
| `pipeline/rules.py` | The 11 business checks, thresholds, and the decision with next action |
| `pipeline/db.py` | SQLite run history and PO ledger; CSV masters |
| `pipeline/runner.py` | Orchestrates the 7 stages and streams progress events to the UI |
| `generate_samples.py` | Builds the 10 sample invoices, including the image-only scan |
| `data/approved_vendors.csv`, `data/purchase_orders.csv` | Stand-ins for the procurement system |
| `samples/*.pdf` | Happy path plus edge cases |

## The decision policy

Any hard failure -> REJECTED. Otherwise any warning -> NEEDS_REVIEW. Otherwise -> APPROVED, and the
invoice amount is consumed from the PO ledger (that is how split invoices are tracked).

| Rule | Check | Outcome when it trips |
|---|---|---|
| R1 | Vendor, invoice number, date, total present | review |
| R2 | Extraction confidence >= 70% (clean) / 85% (scanned) per critical field | review |
| R3 | Same vendor + same invoice number already registered | reject |
| R4 | Vendor in approved master and active | reject |
| R5 | Quoted PO exists; if none quoted, infer a unique open PO by vendor + amount | reject / review |
| R6 | PO belongs to this vendor and is open | reject |
| R7 | Invoice currency equals PO currency | reject |
| R8 | Amount vs PO remaining balance: <= 2% over approve, <= 10% review, beyond reject | review / reject |
| R9 | Line items add to subtotal, subtotal + tax equals total | review |
| R10 | Date not in the future, not older than a year | review |
| R11 | Extractor anomalies (copy banner, changed bank details, urgency) | shown to reviewer |

## Edge cases (and what each one proves)

| Sample | Scenario | Expected |
|---|---|---|
| 01 | Clean PDF, exact PO match | APPROVED |
| 02 | Scanned image, no text layer | Vision extraction, stricter confidence floor; APPROVED if legible |
| 03 | No PO quoted | NEEDS_REVIEW with the inferred PO suggested, never auto-paid |
| 04a, 04b | One PO split across two milestone invoices | Both APPROVED, ledger shows 100% consumed |
| 04c | Third invoice after the PO is used up | REJECTED |
| 05 | Reminder copy of an already-approved invoice | REJECTED as duplicate |
| 06 | Unknown vendor quoting Acme's PO with "updated bank details" | REJECTED, fraud signal surfaced |
| 07 | 4.9% above PO | NEEDS_REVIEW (between the 2% and 10% bands) |
| 08 | No invoice number, no date | NEEDS_REVIEW, vendor asked for the fields |

## Assumptions

- Invoice-to-PO matching only; goods receipts are out of scope (next thing to add).
- Tolerances are a percentage of the PO amount, not of the invoice.
- A previously rejected invoice may be resubmitted; only approved or pending ones count as duplicates.
- Held invoices do not reserve PO balance.
- Masters are CSV files; a real deployment would read them from the ERP.

## Demo order

Reset demo data in the sidebar, then run: 01, 04a, 04b, 04c, 05, 06, 03, 02. Finish on the Dashboard.
