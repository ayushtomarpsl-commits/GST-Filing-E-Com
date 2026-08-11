"""Auto-detection of e-commerce platform GST report files.

Scans a folder (or uploaded files), looks inside zips, fingerprints every
file by its actual content (sheets/columns — never just the filename), and
detects the return period each file belongs to so wrong-month files can be
caught before they poison a filing.
"""
from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

# ---------------------------------------------------------------------------
# Result model
# ---------------------------------------------------------------------------

# kind -> (platform, human label)
KIND_INFO = {
    "amazon_mtr": ("Amazon", "MTR tax report (CSV) — invoice numbers + transactions"),
    "amazon_rtf": ("Amazon", "GST Ready-to-File report — B2CS/B2CL/HSN summaries"),
    "flipkart_gstr": ("Flipkart", "GSTR return report — official aggregates + HSN + invoice series"),
    "flipkart_sales": ("Flipkart", "Sales Report — per-invoice sales/returns + cashback credit notes"),
    "meesho_sales": ("Meesho", "GST report — TCS sales"),
    "meesho_returns": ("Meesho", "GST report — TCS sales returns"),
    "meesho_invoices": ("Meesho", "Supplier Tax Invoice details — invoice/credit-note series"),
    "gstr2b": ("GST Portal", "GSTR-2B auto-drafted ITC statement (JSON) — fills GSTR-3B Table 4"),
    "ignored": ("—", "Ignored"),
    "unknown": ("—", "Not recognized"),
}

MONTH_NAMES = {
    "JANUARY": 1, "FEBRUARY": 2, "MARCH": 3, "APRIL": 4, "MAY": 5, "JUNE": 6,
    "JULY": 7, "AUGUST": 8, "SEPTEMBER": 9, "OCTOBER": 10, "NOVEMBER": 11,
    "DECEMBER": 12,
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "SEPT": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}


@dataclass
class DetectedFile:
    name: str                       # display name, e.g. "report.zip ▸ MTR_B2C.csv"
    kind: str                       # key of KIND_INFO
    data: bytes = b""
    period_hint: Optional[str] = None   # "MMYYYY" if determinable
    message: str = ""
    excluded: bool = False          # set by the processor (e.g. wrong month)

    @property
    def platform(self) -> str:
        return KIND_INFO.get(self.kind, ("—", ""))[0]

    @property
    def label(self) -> str:
        return KIND_INFO.get(self.kind, ("—", "Not recognized"))[1]


# ---------------------------------------------------------------------------
# Period detection
# ---------------------------------------------------------------------------

def _period_from_filename(name: str) -> Optional[str]:
    up = name.upper()
    # "JUNE-2026", "JUNE_2026", "June 2026"
    for mname, mnum in MONTH_NAMES.items():
        m = re.search(rf"\b{mname}[\s_\-]*(20\d{{2}})", up)
        if m:
            return f"{mnum:02d}{m.group(1)}"
    # "2026-06-01_2026-06-30" (date-range style)
    m = re.search(r"(20\d{2})-(\d{2})-\d{2}[_\- ]+(20\d{2})-(\d{2})-\d{2}", up)
    if m and m.group(1) == m.group(3) and m.group(2) == m.group(4):
        return f"{m.group(2)}{m.group(1)}"
    # "gst_3823967_6_2026" (meesho zip style) — single month digit
    m = re.search(r"_(\d{1,2})_(20\d{2})", up)
    if m and 1 <= int(m.group(1)) <= 12:
        return f"{int(m.group(1)):02d}{m.group(2)}"
    return None


def _period_from_dates(series: pd.Series) -> Optional[str]:
    """Mode month of a date-like column -> MMYYYY."""
    try:
        dates = pd.to_datetime(series.dropna(), errors="coerce").dropna()
    except Exception:
        return None
    if dates.empty:
        return None
    key = dates.dt.strftime("%m%Y")
    mode = key.mode()
    return str(mode.iloc[0]) if not mode.empty else None


def _period_from_fy_month(df: pd.DataFrame) -> Optional[str]:
    """Meesho files carry financial_year + month_number columns."""
    if "month_number" not in df.columns or "financial_year" not in df.columns:
        return None
    try:
        month = int(pd.to_numeric(df["month_number"].dropna()).mode().iloc[0])
        fy = str(df["financial_year"].dropna().iloc[0])  # e.g. "2026-2027"
        years = re.findall(r"20\d{2}", fy)
        if not years or not (1 <= month <= 12):
            return None
        # Indian FY: Apr(4)-Dec(12) -> first year, Jan(1)-Mar(3) -> second year
        year = years[0] if month >= 4 else (years[1] if len(years) > 1 else str(int(years[0]) + 1))
        return f"{month:02d}{year}"
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Fingerprinting
# ---------------------------------------------------------------------------

def _detect_csv(name: str, data: bytes) -> DetectedFile:
    try:
        df = pd.read_csv(io.BytesIO(data), nrows=2000)
    except Exception as exc:
        return DetectedFile(name, "unknown", data, message=f"CSV could not be read: {exc}")

    cols = set(df.columns)
    if "Transaction Type" in cols and "Invoice Number" in cols and (
        "Seller Gstin" in cols or "Ship To State" in cols
    ):
        hint = None
        for c in ("Invoice Date", "Order Date"):
            if c in cols:
                hint = _period_from_dates(df[c])
                if hint:
                    break
        return DetectedFile(name, "amazon_mtr", data,
                            period_hint=hint or _period_from_filename(name))
    return DetectedFile(name, "unknown", data,
                        message="CSV columns do not match any known platform report.")


def _detect_excel(name: str, data: bytes) -> DetectedFile:
    try:
        xl = pd.ExcelFile(io.BytesIO(data))
        sheets = set(xl.sheet_names)
    except Exception as exc:
        return DetectedFile(name, "unknown", data, message=f"Excel could not be read: {exc}")

    # Amazon Ready-to-File: GSTIN + B2C Small sheets
    if "GSTIN" in sheets and "B2C Small" in sheets:
        return DetectedFile(name, "amazon_rtf", data,
                            period_hint=_period_from_filename(name))

    # Flipkart GSTR return report
    if "Section 7(A)(2) in GSTR-1" in sheets or "Section 7(B)(2) in GSTR-1" in sheets:
        return DetectedFile(name, "flipkart_gstr", data,
                            period_hint=_period_from_filename(name))

    # Flipkart Sales Report (per-invoice)
    if "Sales Report" in sheets:
        try:
            df = pd.read_excel(xl, sheet_name="Sales Report", nrows=500)
        except Exception:
            df = pd.DataFrame()
        if "Buyer Invoice ID" in df.columns or "Event Type" in df.columns:
            hint = None
            for c in ("Buyer Invoice Date", "Order Date"):
                if c in df.columns:
                    hint = _period_from_dates(df[c])
                    if hint:
                        break
            return DetectedFile(name, "flipkart_sales", data,
                                period_hint=hint or _period_from_filename(name))

    # Meesho Supplier Tax Invoice details
    if "Invoice_Info" in sheets:
        try:
            df = pd.read_excel(xl, sheet_name="Invoice_Info", nrows=500)
        except Exception:
            df = pd.DataFrame()
        if "Invoice No." in df.columns:
            hint = _period_from_dates(df["Order Date"]) if "Order Date" in df.columns else None
            return DetectedFile(name, "meesho_invoices", data,
                                period_hint=hint or _period_from_filename(name))

    # Meesho GST report (tcs_sales / tcs_sales_return)
    data_sheets = [s for s in xl.sheet_names if s != "Help"]
    if data_sheets:
        try:
            df = pd.read_excel(xl, sheet_name=data_sheets[0], nrows=500)
        except Exception:
            df = pd.DataFrame()
        needed = {"gstin", "sub_order_num", "end_customer_state_new", "total_taxable_sale_value"}
        if needed.issubset(set(df.columns)):
            # Classify by the file's own BASENAME only — parent folders or a
            # wrapper zip named "returns june.zip" must not flip the type.
            base = name.split(" ▸ ")[-1].replace("\\", "/").rsplit("/", 1)[-1].lower()
            kind = "meesho_returns" if "return" in base else "meesho_sales"
            hint = _period_from_fy_month(df) or _period_from_filename(name)
            msg = ""
            if "tcs_sales" not in base:
                msg = ("Classified as Meesho "
                       + ("returns" if kind == "meesho_returns" else "sales")
                       + " by content, but the filename is non-standard — keep Meesho's "
                         "original names (tcs_sales.xlsx / tcs_sales_return.xlsx) so "
                         "sales and returns cannot be confused.")
            return DetectedFile(name, kind, data, period_hint=hint, message=msg)

    msg = "Excel sheets/columns do not match any known platform report."
    if "gstr2b" in re.sub(r"[^a-z0-9]", "", name.lower()):
        msg = ("Looks like the GSTR-2B Excel — this tool reads the GSTR-2B JSON "
               "instead: portal → Returns Dashboard → GSTR-2B → Download → "
               "'GENERATE JSON FILE TO DOWNLOAD', then put that .json here.")
    return DetectedFile(name, "unknown", data, message=msg)


_PERIOD_RE = re.compile(r"^(0[1-9]|1[0-2])20\d{2}$")


def _detect_json(name: str, data: bytes) -> DetectedFile:
    try:
        root = json.loads(data.decode("utf-8-sig"))
    except Exception as exc:
        return DetectedFile(name, "unknown", data,
                            message=f"JSON could not be parsed: {exc}")
    if not isinstance(root, dict):
        return DetectedFile(name, "unknown", data,
                            message="JSON root is not an object — not a GST file.")

    inner = root.get("data") if isinstance(root.get("data"), dict) else root
    # GSTR-2B download: {"chksum": ..., "data": {"rtnprd": "MMYYYY", "docdata": {...}}}
    if isinstance(inner.get("docdata"), dict) or (
        "rtnprd" in inner and ("itcsumm" in inner or "docdata" in inner)
    ):
        rtnprd = str(inner.get("rtnprd", ""))
        hint = rtnprd if _PERIOD_RE.fullmatch(rtnprd) else _period_from_filename(name)
        return DetectedFile(name, "gstr2b", data, period_hint=hint)

    # This tool's own outputs, if the user keeps them in the same folder.
    if {"gstin", "fp", "hash"} <= set(root):
        return DetectedFile(name, "ignored", b"",
                            message="GSTR-1 JSON generated by this tool — ignored.")
    if {"gstin", "ret_period"} <= set(root):
        return DetectedFile(name, "ignored", b"",
                            message="GSTR-3B JSON generated by this tool — ignored.")
    return DetectedFile(
        name, "unknown", data,
        message="JSON does not match the GSTR-2B format (expected the file from "
                "portal → GSTR-2B → Download → 'GENERATE JSON FILE TO DOWNLOAD').")


def _detect_one(name: str, data: bytes) -> DetectedFile:
    lower = name.lower()
    if lower.endswith(".pdf"):
        return DetectedFile(name, "ignored", b"", message="PDF invoice copy — not needed for filing data.")
    if lower.endswith((".png", ".jpg", ".jpeg", ".txt", ".docx")):
        return DetectedFile(name, "ignored", b"", message="Not a report file.")
    if data[:2] == b"PK" and lower.endswith((".xlsx", ".xls", ".xlsm")):
        return _detect_excel(name, data)
    if lower.endswith((".xlsx", ".xls", ".xlsm")):
        return _detect_excel(name, data)
    if lower.endswith(".csv"):
        return _detect_csv(name, data)
    if lower.endswith(".json"):
        return _detect_json(name, data)
    return DetectedFile(name, "unknown", b"", message="Unsupported file type.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

MAX_FILE_BYTES = 200 * 1024 * 1024  # sanity cap


MAX_ZIP_DEPTH = 3


def _scan_blob(name: str, data: bytes, results: List[DetectedFile],
               zip_depth: int = 0) -> None:
    """Detect one blob, expanding zips recursively (bounded depth)."""
    if len(data) > MAX_FILE_BYTES:
        results.append(DetectedFile(
            name, "ignored",
            message=f"File is larger than {MAX_FILE_BYTES // (1024*1024)} MB and was "
                    "not scanned — if this is a platform report, contact support."))
        return
    if name.lower().endswith(".zip"):
        if zip_depth >= MAX_ZIP_DEPTH:
            results.append(DetectedFile(
                name, "ignored", message="Zip nested too deep — not scanned."))
            return
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as zf:
                pdf_count = 0
                for info in zf.infolist():
                    if info.is_dir():
                        continue
                    member = info.filename.rsplit("/", 1)[-1]
                    display = f"{name} ▸ {member}"
                    if info.file_size > MAX_FILE_BYTES:
                        results.append(DetectedFile(
                            display, "ignored",
                            message="Zip member too large — not scanned."))
                        continue
                    if member.lower().endswith(".pdf"):
                        pdf_count += 1
                        continue  # summarize instead of listing hundreds of PDFs
                    _scan_blob(display, zf.read(info), results, zip_depth + 1)
                if pdf_count:
                    results.append(DetectedFile(
                        f"{name} ▸ ({pdf_count} PDF invoice copies)", "ignored",
                        message="PDF invoice copies — not needed for filing data."))
        except zipfile.BadZipFile:
            results.append(DetectedFile(
                name, "unknown",
                message="File has a .zip name but is not a readable zip archive."))
        return
    results.append(_detect_one(name, data))


def scan_files(named_blobs: List[Tuple[str, bytes]]) -> List[DetectedFile]:
    """Detect a list of (filename, bytes) — zips are expanded recursively."""
    results: List[DetectedFile] = []
    for name, data in named_blobs:
        _scan_blob(name, data, results)
    return results


def scan_folder(folder: Path) -> List[DetectedFile]:
    """Recursively scan a folder on disk (zips included).

    Every file becomes a visible scan entry — unreadable or oversized files
    are reported, never silently skipped."""
    results: List[DetectedFile] = []
    for path in sorted(folder.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(folder))
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                results.append(DetectedFile(
                    rel, "ignored",
                    message=f"File is larger than {MAX_FILE_BYTES // (1024*1024)} MB "
                            "and was not scanned."))
                continue
            _scan_blob(rel, path.read_bytes(), results)
        except OSError as exc:
            results.append(DetectedFile(
                rel, "unknown",
                message=f"File could not be read ({exc}) — check permissions/locks "
                        "and re-run."))
    return results
