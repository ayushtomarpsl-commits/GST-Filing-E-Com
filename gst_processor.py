"""Orchestrates multi-platform GST report parsing and GSTR-1 B2CS export."""
from __future__ import annotations

import csv
import io
import json
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from gst_parsers import (
    GSTR_VERSION,
    PORTAL_B2CS_KEYS,
    PORTAL_JSON_HASH,
    PORTAL_JSON_TYP,
    GSTConsolidator,
    ReportParsers,
    TaxRecord,
)

CSV_HEADERS = [
    "Type",
    "Place Of Supply",
    "Rate",
    "Applicable % of Tax Rate",
    "Taxable Value",
    "Cess Amount",
    "E-Commerce GSTIN",
]

PORTAL_ROOT_KEYS = ("gstin", "fp", "version", "hash", "b2cs")


@dataclass
class PlatformResult:
    platform: str
    seller_gstin: str
    eco_gstin: str
    records: List[TaxRecord] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class ProcessResult:
    seller_gstin: str
    period: str
    platforms: List[PlatformResult]
    csv_rows: List[Dict[str, Any]]
    json_rows: List[Dict[str, Any]]
    breakdown_stats: Dict[str, Any]
    flipkart_records: List[TaxRecord]
    amazon_records: List[TaxRecord]
    meesho_records: List[TaxRecord]
    validation_errors: List[str] = field(default_factory=list)
    gstin_warnings: List[str] = field(default_factory=list)


def _format_rate(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.1f}".rstrip("0").rstrip(".")


def _format_taxable(value: float) -> str:
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.2f}".rstrip("0").rstrip(".")


def csv_rows_for_export(csv_rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Convert internal consolidated rows to offline-tool CSV format."""
    exported: List[Dict[str, str]] = []
    for row in csv_rows:
        rate = row.get("rate", row.get("Rate"))
        if isinstance(rate, str):
            rate_str = rate
        else:
            rate_str = _format_rate(float(rate))
        exported.append(
            {
                "Type": row["Type"],
                "Place Of Supply": row["Place Of Supply"],
                "Rate": rate_str,
                "Applicable % of Tax Rate": row.get("Applicable % of Tax Rate", ""),
                "Taxable Value": _format_taxable(float(row["Taxable Value"])),
                "Cess Amount": _format_taxable(float(row["Cess Amount"])),
                "E-Commerce GSTIN": row["E-Commerce GSTIN"],
            }
        )
    return exported


def build_json_payload(
    seller_gstin: str,
    return_period: str,
    b2cs_rows: List[Dict[str, Any]],
) -> Dict[str, Any]:
    return {
        "gstin": seller_gstin,
        "fp": return_period,
        "version": GSTR_VERSION,
        "hash": PORTAL_JSON_HASH,
        "b2cs": b2cs_rows,
    }


def validate_portal_payload(payload: Dict[str, Any]) -> List[str]:
    errors: List[str] = []
    if list(payload.keys()) != list(PORTAL_ROOT_KEYS):
        errors.append(
            f"Root keys must be {list(PORTAL_ROOT_KEYS)}, got {list(payload.keys())}"
        )
    if payload.get("hash") != PORTAL_JSON_HASH:
        errors.append(f'hash must be "{PORTAL_JSON_HASH}"')
    if "ret_period" in payload:
        errors.append("ret_period must not be present")
    for index, row in enumerate(payload.get("b2cs", [])):
        if list(row.keys()) != list(PORTAL_B2CS_KEYS):
            errors.append(
                f"b2cs[{index}] keys must be {list(PORTAL_B2CS_KEYS)}, got {list(row.keys())}"
            )
        for forbidden in ("etin", "diff_percent"):
            if forbidden in row:
                errors.append(f"b2cs[{index}] must not contain {forbidden}")
        if row.get("typ") != PORTAL_JSON_TYP:
            errors.append(f'b2cs[{index}].typ must be "{PORTAL_JSON_TYP}"')
    return errors


def write_csv_bytes(rows: List[Dict[str, str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow(CSV_HEADERS)
    for row in rows:
        writer.writerow([row[col] for col in CSV_HEADERS])
    return buffer.getvalue().encode("utf-8")


def write_json_bytes(payload: Dict[str, Any], pretty: bool = False) -> bytes:
    if pretty:
        text = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return (text + "\n").encode("utf-8")


def normalize_period(period: str) -> str:
    """Accept MMYYYY or MM-YYYY and return MMYYYY."""
    cleaned = period.strip().replace("-", "").replace("/", "")
    if len(cleaned) != 6 or not cleaned.isdigit():
        raise ValueError("Return period must be MMYYYY (e.g. 042026 for April 2026).")
    month = int(cleaned[:2])
    if month < 1 or month > 12:
        raise ValueError("Month in return period must be between 01 and 12.")
    return cleaned


def _save_upload(upload_bytes: bytes, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.write(upload_bytes)
    handle.close()
    return Path(handle.name)


def process_reports(
    period: str,
    flipkart_bytes: Optional[bytes] = None,
    amazon_bytes: Optional[bytes] = None,
    meesho_sales_bytes: Optional[bytes] = None,
    meesho_returns_bytes: Optional[bytes] = None,
) -> ProcessResult:
    """Parse uploaded platform reports and produce consolidated GSTR-1 B2CS outputs."""
    if not any([flipkart_bytes, amazon_bytes, meesho_sales_bytes]):
        raise ValueError("Upload at least one platform report (Flipkart, Amazon, or Meesho).")

    period_norm = normalize_period(period)
    platforms: List[PlatformResult] = []
    flipkart_records: List[TaxRecord] = []
    amazon_records: List[TaxRecord] = []
    meesho_records: List[TaxRecord] = []
    seller_gstin = ""
    gstin_warnings: List[str] = []
    temp_paths: List[Path] = []

    try:
        if flipkart_bytes:
            path = _save_upload(flipkart_bytes, ".xlsx")
            temp_paths.append(path)
            try:
                sg, eco, recs = ReportParsers.parse_flipkart(path)
                platforms.append(
                    PlatformResult("Flipkart", sg, eco, recs)
                )
                flipkart_records = recs
                seller_gstin = sg
            except Exception as exc:
                platforms.append(
                    PlatformResult("Flipkart", "", "", [], str(exc))
                )

        if amazon_bytes:
            # Ready-to-File uploads are xlsx (ZIP "PK" signature); MTR is csv.
            amazon_suffix = ".xlsx" if amazon_bytes[:2] == b"PK" else ".csv"
            path = _save_upload(amazon_bytes, amazon_suffix)
            temp_paths.append(path)
            try:
                sg, eco, recs = ReportParsers.parse_amazon(path)
                platforms.append(
                    PlatformResult("Amazon", sg, eco, recs)
                )
                amazon_records = recs
                if not seller_gstin:
                    seller_gstin = sg
                elif sg != seller_gstin:
                    gstin_warnings.append(
                        f"Amazon seller GSTIN ({sg}) differs from primary ({seller_gstin})."
                    )
            except Exception as exc:
                platforms.append(
                    PlatformResult("Amazon", "", "", [], str(exc))
                )

        if meesho_sales_bytes:
            sales_path = _save_upload(meesho_sales_bytes, ".xlsx")
            temp_paths.append(sales_path)
            returns_path: Optional[Path] = None
            if meesho_returns_bytes:
                returns_path = _save_upload(meesho_returns_bytes, ".xlsx")
                temp_paths.append(returns_path)
            try:
                sg, eco, recs = ReportParsers.parse_meesho(sales_path, returns_path)
                platforms.append(
                    PlatformResult("Meesho", sg, eco, recs)
                )
                meesho_records = recs
                if not seller_gstin:
                    seller_gstin = sg
                elif sg != seller_gstin:
                    gstin_warnings.append(
                        f"Meesho seller GSTIN ({sg}) differs from primary ({seller_gstin})."
                    )
            except Exception as exc:
                platforms.append(
                    PlatformResult("Meesho", "", "", [], str(exc))
                )

        parsed_ok = [
            p for p in platforms if not p.error and p.records
        ]
        if not parsed_ok:
            errors = "; ".join(
                f"{p.platform}: {p.error}" for p in platforms if p.error
            )
            raise ValueError(f"No platform reports could be parsed. {errors}")

        if not seller_gstin:
            raise ValueError("Seller GSTIN could not be determined from uploaded reports.")

        csv_rows, json_rows, breakdown_stats = GSTConsolidator.consolidate(
            flipkart_records,
            amazon_records,
            meesho_records,
            seller_gstin,
        )

        if not csv_rows:
            raise ValueError("No B2CS rows produced after consolidation.")

        payload = build_json_payload(seller_gstin, period_norm, json_rows)
        validation_errors = validate_portal_payload(payload)

        return ProcessResult(
            seller_gstin=seller_gstin,
            period=period_norm,
            platforms=platforms,
            csv_rows=csv_rows,
            json_rows=json_rows,
            breakdown_stats=breakdown_stats,
            flipkart_records=flipkart_records,
            amazon_records=amazon_records,
            meesho_records=meesho_records,
            validation_errors=validation_errors,
            gstin_warnings=gstin_warnings,
        )
    finally:
        for path in temp_paths:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass


def generate_excel_bytes(result: ProcessResult) -> bytes:
    """Build consolidated Excel workbook in memory."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx") as handle:
        out_path = Path(handle.name)

    try:
        GSTConsolidator.generate_excel(
            seller_gstin=result.seller_gstin,
            period=result.period,
            csv_rows=result.csv_rows,
            stats=result.breakdown_stats,
            flipkart_records=result.flipkart_records,
            amazon_records=result.amazon_records,
            meesho_records=result.meesho_records,
            excel_path=out_path,
        )
        return out_path.read_bytes()
    finally:
        out_path.unlink(missing_ok=True)
