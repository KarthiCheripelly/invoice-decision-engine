"""
Stage 2: Extraction.

Turns an invoice PDF into a structured dict (see EXTRACTION_SCHEMA).

Two extractors:
  1. ClaudeExtractor  - sends the PDF to Claude (handles clean text PDFs *and* scanned images,
                        returns per-field confidence). Used whenever ANTHROPIC_API_KEY is set.
  2. GeminiExtractor  - same job via Google Gemini (free tier available). Used when GEMINI_API_KEY
                        is set and no Anthropic key is.
  3. LocalExtractor   - deterministic regex parser over the PDF text layer. Used as an offline
                        fallback for text PDFs when no API key is configured. Cannot read scans.
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
from dataclasses import dataclass

from pypdf import PdfReader

DEFAULT_MODEL = os.environ.get("INVOICE_MODEL", "claude-opus-5-5")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash")
# tried in order when the primary model is overloaded (503) or rate limited (429)
GEMINI_FALLBACK_MODELS = [m.strip() for m in os.environ.get("GEMINI_FALLBACK_MODELS", "gemini-3.1-flash-lite,gemini-flash-latest").split(",") if m.strip()]

CRITICAL_FIELDS = ["vendor_name", "invoice_number", "invoice_date", "total"]

EXTRACTION_SCHEMA = {
    "type": "object",
    "properties": {
        "vendor_name": {"type": ["string", "null"], "description": "Legal name of the party issuing the invoice (the seller), not the buyer"},
        "vendor_tax_id": {"type": ["string", "null"], "description": "GSTIN / VAT / EIN of the vendor"},
        "invoice_number": {"type": ["string", "null"]},
        "invoice_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD, null if absent"},
        "due_date": {"type": ["string", "null"], "description": "ISO date YYYY-MM-DD, null if absent"},
        "po_reference": {"type": ["string", "null"], "description": "Purchase order number quoted on the invoice, exactly as written, null if none"},
        "currency": {"type": ["string", "null"], "description": "ISO 4217 code, e.g. INR, USD"},
        "line_items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": ["number", "null"]},
                    "unit_price": {"type": ["number", "null"]},
                    "amount": {"type": ["number", "null"]},
                },
                "required": ["description", "quantity", "unit_price", "amount"],
                "additionalProperties": False,
            },
        },
        "subtotal": {"type": ["number", "null"]},
        "tax_rate_percent": {"type": ["number", "null"]},
        "tax_amount": {"type": ["number", "null"]},
        "total": {"type": ["number", "null"], "description": "Grand total payable"},
        "bank_details": {"type": ["string", "null"], "description": "Bank account details printed on the invoice, if any"},
        "notes": {"type": ["string", "null"], "description": "Any remarks, reminders, 'copy'/'duplicate' banners or unusual text worth flagging"},
        "field_confidence": {
            "type": "object",
            "description": "0-1 confidence that each field was read correctly from the document",
            "properties": {
                "vendor_name": {"type": "number"},
                "invoice_number": {"type": "number"},
                "invoice_date": {"type": "number"},
                "po_reference": {"type": "number"},
                "total": {"type": "number"},
            },
            "required": ["vendor_name", "invoice_number", "invoice_date", "po_reference", "total"],
            "additionalProperties": False,
        },
        "document_quality": {"type": "string", "description": "One sentence on legibility: clean digital PDF, scanned, skewed, low resolution, handwritten, etc."},
    },
    "required": ["vendor_name", "vendor_tax_id", "invoice_number", "invoice_date", "due_date", "po_reference", "currency",
                 "line_items", "subtotal", "tax_rate_percent", "tax_amount", "total", "bank_details", "notes",
                 "field_confidence", "document_quality"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """You are an accounts-payable document specialist. You read vendor invoices and return exactly the fields in the JSON schema.
Rules:
- Extract only what is printed. Never invent an invoice number, date, PO number or total that is not on the document; use null.
- The vendor is the party issuing the invoice (usually at the top, with its own tax id). The 'Bill To' party is the buyer, not the vendor.
- Normalise dates to YYYY-MM-DD. Normalise amounts to plain numbers without currency symbols or thousands separators.
- If the document is a scan or photo, read it carefully and lower the confidence for any field that is blurry, skewed or partially obscured.
- Put anything unusual (reminder/copy banners, changed bank details, urgency language, handwritten changes) into 'notes'."""


@dataclass
class ExtractionResult:
    data: dict
    source_type: str          # "text" | "scanned"
    extractor: str            # "claude" | "local"
    model: str | None
    raw_text_chars: int
    usage: dict | None = None


def detect_source_type(pdf_bytes: bytes) -> tuple[str, str]:
    """Returns (source_type, text). 'scanned' when the PDF has no usable text layer."""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        text = "\n".join((p.extract_text() or "") for p in reader.pages)
    except Exception:
        text = ""
    return ("text" if len(text.strip()) > 80 else "scanned"), text


# --------------------------------------------------------------------------- Claude
class ClaudeExtractor:
    name = "claude"

    def __init__(self, model: str = DEFAULT_MODEL):
        import anthropic  # imported lazily so the app still loads without the SDK configured
        self.model = model
        self.client = anthropic.Anthropic()

    def extract(self, pdf_bytes: bytes, file_name: str) -> tuple[dict, dict]:
        b64 = base64.standard_b64encode(pdf_bytes).decode("ascii")
        messages = [{
            "role": "user",
            "content": [
                {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}, "title": file_name},
                {"type": "text", "text": "Extract this invoice into the required JSON structure."},
            ],
        }]
        kwargs = dict(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=messages,
            output_config={"format": {"type": "json_schema", "schema": EXTRACTION_SCHEMA}, "effort": "low"},
        )
        try:
            # Server-side refusal fallback: if a safety classifier declines, the API re-runs on a fallback model.
            response = self.client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
        except TypeError:
            # Older SDK without the fallbacks parameter
            response = self.client.messages.create(**kwargs)
        if response.stop_reason == "refusal":
            raise RuntimeError("Model declined to process this document (stop_reason=refusal).")
        text = next(b.text for b in response.content if b.type == "text")
        usage = {"input_tokens": response.usage.input_tokens, "output_tokens": response.usage.output_tokens, "model": response.model}
        return json.loads(text), usage


# --------------------------------------------------------------------------- Gemini
def _gemini_schema(node):
    """Gemini's schema dialect: no type lists, no additionalProperties; nullability via 'nullable'."""
    if isinstance(node, dict):
        out = {}
        for k, v in node.items():
            if k == "additionalProperties":
                continue
            if k == "type" and isinstance(v, list):
                non_null = [t for t in v if t != "null"]
                out["type"] = non_null[0] if non_null else "string"
                if "null" in v:
                    out["nullable"] = True
                continue
            out[k] = _gemini_schema(v)
        return out
    if isinstance(node, list):
        return [_gemini_schema(x) for x in node]
    return node


class GeminiExtractor:
    name = "gemini"

    def __init__(self, model: str = GEMINI_MODEL):
        from google import genai
        self.model = model
        self.client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

    def extract(self, pdf_bytes: bytes, file_name: str) -> tuple[dict, dict]:
        """Try the primary model, then the fallbacks, retrying once on transient 429/503 errors."""
        import time
        last = None
        for model in [self.model] + [m for m in GEMINI_FALLBACK_MODELS if m != self.model]:
            for attempt in (1, 2):
                try:
                    data, usage = self._extract_with(model, pdf_bytes)
                    self.model = model
                    return data, usage
                except Exception as e:
                    last = e
                    transient = any(code in str(e) for code in ("503", "429", "UNAVAILABLE", "RESOURCE_EXHAUSTED"))
                    if not transient:
                        break               # e.g. 404 model retired -> go straight to the next model
                    if attempt == 1:
                        time.sleep(2)
        raise RuntimeError(f"Gemini extraction failed on every model ({self.model}, {', '.join(GEMINI_FALLBACK_MODELS)}): {last}")

    def _extract_with(self, model: str, pdf_bytes: bytes) -> tuple[dict, dict]:
        from google.genai import types
        pdf_part = types.Part.from_bytes(data=pdf_bytes, mime_type="application/pdf")
        prompt = "Extract this invoice into the required JSON structure. Use null for anything not printed on the document."
        base = dict(system_instruction=SYSTEM_PROMPT, temperature=0, response_mime_type="application/json")
        try:
            resp = self.client.models.generate_content(
                model=model, contents=[pdf_part, prompt],
                config=types.GenerateContentConfig(response_schema=_gemini_schema(EXTRACTION_SCHEMA), **base))
        except Exception as first_error:
            # schema dialect rejected by this model version -> describe the schema in the prompt instead
            try:
                resp = self.client.models.generate_content(
                    model=model, contents=[pdf_part, prompt + "\nReturn JSON matching exactly this JSON schema:\n" + json.dumps(EXTRACTION_SCHEMA)],
                    config=types.GenerateContentConfig(**base))
            except Exception:
                raise first_error
        text = (resp.text or "").strip()
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0]
        data = json.loads(text)
        um = getattr(resp, "usage_metadata", None)
        usage = {"input_tokens": getattr(um, "prompt_token_count", None), "output_tokens": getattr(um, "candidates_token_count", None), "model": model}
        # make sure every expected key exists even if the model omitted it
        for k in EXTRACTION_SCHEMA["properties"]:
            data.setdefault(k, [] if k == "line_items" else ({} if k == "field_confidence" else None))
        fc = data.get("field_confidence") or {}
        data["field_confidence"] = {f: float(fc.get(f, 0.9) or 0) for f in ("vendor_name", "invoice_number", "invoice_date", "po_reference", "total")}
        return data, usage


# --------------------------------------------------------------------------- local fallback
_MONEY = r"(?:Rs\.?|INR|\$|USD|EUR)?\s*([\d,]+\.\d{2})"


def _num(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


class LocalExtractor:
    """Regex parser for text-layer PDFs. Deliberately simple: it exists so the demo runs offline."""
    name = "local"
    model = None

    def extract(self, pdf_bytes: bytes, file_name: str) -> tuple[dict, dict]:
        _, text = detect_source_type(pdf_bytes)
        if len(text.strip()) <= 80:
            raise RuntimeError("No text layer found. The local extractor cannot read scanned invoices; set ANTHROPIC_API_KEY to use the vision model.")
        lines = [l.strip() for l in text.splitlines() if l.strip()]

        def after(label: str) -> str | None:
            for i, l in enumerate(lines):
                if l.lower().startswith(label.lower()):
                    rest = l[len(label):].strip(" :")
                    if rest:
                        return rest
                    if i + 1 < len(lines):
                        return lines[i + 1]
            return None

        # vendor = first non-heading line
        vendor = next((l for l in lines if l.upper() not in ("TAX INVOICE", "INVOICE") and not l.upper().startswith("PAYMENT REMINDER")), None)
        tax_id = None
        m = re.search(r"(?:GSTIN|EIN|VAT)\s*:\s*([A-Z0-9-]+)", text)
        if m:
            tax_id = m.group(1)
        currency = after("Currency:") or ("USD" if "$" in text else "INR")

        def money_after(label: str) -> float | None:
            m2 = re.search(re.escape(label) + r"[^\n]*\n?\s*" + _MONEY, text)
            return _num(m2.group(1)) if m2 else None

        subtotal = money_after("Subtotal")
        total = money_after("Total Due")
        tax_m = re.search(r"(GST|Tax|VAT)\s*\((\d+(?:\.\d+)?)%\)\s*\n?\s*" + _MONEY, text)
        tax_rate = float(tax_m.group(2)) if tax_m else 0.0
        tax_amount = _num(tax_m.group(3)) if tax_m else 0.0

        items = []
        for m3 in re.finditer(r"\n(\d+)\n(.+?)\n(\d+(?:\.\d+)?)\n" + _MONEY + r"\n" + _MONEY, text):
            items.append({"description": m3.group(2), "quantity": float(m3.group(3)), "unit_price": _num(m3.group(4)), "amount": _num(m3.group(5))})

        notes = []
        if re.search(r"reminder|copy of original|duplicate", text, re.I):
            notes.append("Document carries a reminder/copy banner.")
        if re.search(r"updated bank details|new bank", text, re.I):
            notes.append("Invoice mentions updated bank details.")
        bank = re.search(r"Bank:.*", text)
        data = {
            "vendor_name": vendor, "vendor_tax_id": tax_id,
            "invoice_number": after("Invoice No:"), "invoice_date": after("Invoice Date:"), "due_date": after("Due Date:"),
            "po_reference": after("PO Reference:"), "currency": currency,
            "line_items": items, "subtotal": subtotal, "tax_rate_percent": tax_rate, "tax_amount": tax_amount, "total": total,
            "bank_details": bank.group(0) if bank else None, "notes": " ".join(notes) or None,
            "field_confidence": {k: (0.95 if v else 0.0) for k, v in {
                "vendor_name": vendor, "invoice_number": after("Invoice No:"), "invoice_date": after("Invoice Date:"),
                "po_reference": after("PO Reference:"), "total": total}.items()},
            "document_quality": "Clean digital PDF with text layer (parsed locally, no AI model).",
        }
        return data, {"model": None}


# --------------------------------------------------------------------------- entry point
def api_key_present() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("GEMINI_API_KEY"))


def active_extractor_label() -> str:
    if os.environ.get("ANTHROPIC_API_KEY"):
        return f"Claude ({DEFAULT_MODEL})"
    if os.environ.get("GEMINI_API_KEY"):
        return f"Gemini ({GEMINI_MODEL})"
    return "local parser"


def get_extractor():
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ClaudeExtractor()
    if os.environ.get("GEMINI_API_KEY"):
        return GeminiExtractor()
    return LocalExtractor()


def extract_invoice(pdf_bytes: bytes, file_name: str) -> ExtractionResult:
    source_type, text = detect_source_type(pdf_bytes)
    extractor = get_extractor()
    data, usage = extractor.extract(pdf_bytes, file_name)
    # light normalisation
    for k in ("invoice_number", "po_reference", "vendor_name", "currency"):
        if isinstance(data.get(k), str):
            data[k] = data[k].strip() or None
    if data.get("currency"):
        data["currency"] = data["currency"].upper()
    return ExtractionResult(data=data, source_type=source_type, extractor=extractor.name, model=getattr(extractor, "model", None),
                            raw_text_chars=len(text.strip()), usage=usage)
