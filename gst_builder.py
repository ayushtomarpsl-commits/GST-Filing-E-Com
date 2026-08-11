"""GST Helper build pipeline.

Takes auto-detected platform files and produces the full GSTR-1 package:
Table 7 (B2CS), Table 5 (B2CL), Table 12 (HSN B2C) and Table 13 (documents),
as one combined portal JSON plus per-table JSONs — with per-platform status,
guidance for missing/wrong files, and hard validation.

Design rule learned the hard way: NEVER drop data silently. Anything that
cannot be processed becomes a visible error or warning.
"""
from __future__ import annotations

import io
import json
import re
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from gst_autodetect import DetectedFile
from gst3b_builder import (
    TAX_HEADS,
    Gstr2bResult,
    build_gstr3b_payload,
    compute_31a_and_32,
    parse_gstr2b_json,
    validate_gstr3b,
)
from gst_ext_parsers import (
    B2clInvoice,
    DocEntry,
    HsnRow,
    SeriesRow,
    parse_amazon_mtr_extras,
    parse_amazon_rtf_extras,
    parse_flipkart_gstr_extras,
    parse_flipkart_sales_report,
    parse_meesho_hsn,
    parse_meesho_invoice_details,
    reconcile_hsn_tax,
)
from gst_parsers import (
    GSTR_VERSION,
    PORTAL_JSON_HASH,
    STATE_NAMES,
    GSTConsolidator,
    ReportParsers,
    TaxRecord,
)

DOC_NUM_INVOICE = 1      # "Invoices for outward supply"
DOC_NUM_DEBIT = 4        # "Debit Note"
DOC_NUM_CREDIT = 5       # "Credit Note"


# ---------------------------------------------------------------------------
# Result models
# ---------------------------------------------------------------------------

@dataclass
class Guidance:
    level: str    # 'error' | 'warning' | 'info'
    title: str
    detail: str


@dataclass
class PlatformStatus:
    name: str
    status: str          # 'ok' | 'partial' | 'error' | 'missing'
    detail: str = ""
    records: int = 0
    taxable: float = 0.0
    tax: float = 0.0


@dataclass
class TableCard:
    id: str
    table_no: str
    title: str
    subtitle: str
    stats: List[Dict[str, Any]]
    headers: List[str]
    rows: List[List[Any]]
    json_str: str
    filename: str


@dataclass
class BuildResult:
    period: str
    seller_gstin: str
    scan_entries: List[Dict[str, Any]]
    guidance: List[Guidance]
    platforms: List[PlatformStatus]
    totals: Dict[str, float]
    cards: List[TableCard]
    payload_full: Dict[str, Any]
    csv_rows: List[Dict[str, Any]]
    breakdown_stats: Dict[str, Any]
    records_by_platform: Dict[str, List[TaxRecord]]
    ok: bool = True
    payload_3b: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _num(v: float) -> Any:
    rv = round(v, 2)
    return int(rv) if rv == int(rv) else rv


def _save_temp(data: bytes, suffix: str) -> Path:
    handle = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    handle.write(data)
    handle.close()
    return Path(handle.name)


def _fmt_period(period: str) -> str:
    return f"{period[:2]}/{period[2:]}" if len(period) == 6 else period


_SERIES_RE = re.compile(r"^(.*?)(\d+)$")


def build_series(docs: List[DocEntry], policy: str) -> List[SeriesRow]:
    """Convert individual documents into Table 13 series rows.

    policy='runs' (Amazon/Flipkart): platform series are shared across sellers,
        so report only the seller's own documents as contiguous runs.
    policy='span' (Meesho): the series is exclusive to the seller, so report
        min..max with numbering gaps counted as cancelled documents.
    """
    out: List[SeriesRow] = []
    grouped: Dict[Tuple[str, str, int], List[Tuple[int, str, bool]]] = {}
    singletons: List[SeriesRow] = []

    seen: set = set()
    for d in docs:
        if d.doc_id in seen:
            continue  # multi-line invoices appear once per line item
        seen.add(d.doc_id)
        m = _SERIES_RE.match(d.doc_id)
        if not m:
            singletons.append(SeriesRow(d.kind, d.doc_id, d.doc_id, 1,
                                        1 if d.cancelled else 0))
            continue
        prefix, num_s = m.group(1), m.group(2)
        grouped.setdefault((d.kind, prefix, len(num_s)), []).append(
            (int(num_s), d.doc_id, d.cancelled)
        )

    for (kind, _prefix, _width), items in grouped.items():
        items.sort(key=lambda x: x[0])
        if policy == "span":
            nums = [n for n, _, _ in items]
            span = nums[-1] - nums[0] + 1
            explicit_cancel = sum(1 for _, _, c in items if c)
            gaps = span - len(nums)
            out.append(SeriesRow(kind, items[0][1], items[-1][1],
                                 span, gaps + explicit_cancel))
        else:  # contiguous runs
            run: List[Tuple[int, str, bool]] = []
            def flush() -> None:
                if run:
                    out.append(SeriesRow(kind, run[0][1], run[-1][1], len(run),
                                         sum(1 for _, _, c in run if c)))
            for item in items:
                if run and item[0] != run[-1][0] + 1:
                    flush()
                    run = []
                run.append(item)
            flush()

    out.extend(singletons)
    out.sort(key=lambda s: (s.kind, s.frm))
    return out


def merge_hsn(rows: List[HsnRow]) -> List[HsnRow]:
    """Merge platform HSN rows on (hsn, uqc, rate) — the portal's unique key."""
    merged: Dict[Tuple[str, str, float], HsnRow] = {}
    for r in rows:
        key = (r.hsn, r.uqc, r.rate)
        m = merged.get(key)
        if m is None:
            merged[key] = HsnRow(r.hsn, r.uqc, r.qty, r.txval, r.iamt, r.camt,
                                 r.samt, r.csamt, r.rate, r.desc)
        else:
            m.qty += r.qty
            m.txval += r.txval
            m.iamt += r.iamt
            m.camt += r.camt
            m.samt += r.samt
            m.csamt += r.csamt
            if not m.desc:
                m.desc = r.desc
    out = []
    for m in merged.values():
        m.qty = round(m.qty, 2)
        m.txval = round(m.txval, 2)
        m.iamt = round(m.iamt, 2)
        m.camt = round(m.camt, 2)
        m.samt = round(m.samt, 2)
        m.csamt = round(m.csamt, 2)
        out.append(m)
    out.sort(key=lambda h: (-abs(h.txval), h.hsn))
    return out


# ---------------------------------------------------------------------------
# JSON section builders
# ---------------------------------------------------------------------------

def hsn_section(rows: List[HsnRow]) -> Dict[str, Any]:
    data = []
    for i, r in enumerate(rows, start=1):
        entry = {
            "num": i,
            "hsn_sc": r.hsn,
            "uqc": r.uqc,
            "qty": _num(r.qty),
            "txval": _num(r.txval),
            "iamt": _num(r.iamt),
            "camt": _num(r.camt),
            "samt": _num(r.samt),
            "csamt": _num(r.csamt),
            "rt": _num(r.rate),
        }
        if r.desc:
            entry["desc"] = r.desc
        data.append(entry)
    return {"hsn_b2c": data}


def doc_issue_section(series: List[SeriesRow]) -> Dict[str, Any]:
    doc_det = []
    for doc_num, kind in ((DOC_NUM_INVOICE, "invoice"), (DOC_NUM_DEBIT, "debit"),
                          (DOC_NUM_CREDIT, "credit")):
        rows = [s for s in series if s.kind == kind]
        if not rows:
            continue
        doc_det.append({
            "doc_num": doc_num,
            "docs": [
                {"num": i, "from": s.frm, "to": s.to, "totnum": s.totnum,
                 "cancel": s.cancel, "net_issue": s.net}
                for i, s in enumerate(rows, start=1)
            ],
        })
    return {"doc_det": doc_det}


def b2cl_section(invoices: List[B2clInvoice]) -> List[Dict[str, Any]]:
    by_pos: Dict[str, List[B2clInvoice]] = {}
    for inv in invoices:
        by_pos.setdefault(inv.pos, []).append(inv)
    out = []
    for pos in sorted(by_pos):
        entry_invs = []
        for inv in by_pos[pos]:
            item: Dict[str, Any] = {
                "inum": inv.inum,
                "idt": inv.idt,
                "val": _num(inv.val),
                "itms": [{"num": 1, "itm_det": {
                    "rt": _num(inv.rate), "txval": _num(inv.txval),
                    "iamt": _num(inv.iamt), "csamt": _num(inv.csamt),
                }}],
            }
            if inv.etin:
                item["etin"] = inv.etin
            entry_invs.append(item)
        out.append({"pos": pos, "inv": entry_invs})
    return out


def make_payload(gstin: str, period: str, **sections: Any) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "gstin": gstin,
        "fp": period,
        "version": GSTR_VERSION,
        "hash": PORTAL_JSON_HASH,
    }
    for key, value in sections.items():
        if value:
            payload[key] = value
    return payload


# ---------------------------------------------------------------------------
# Validation v2
# ---------------------------------------------------------------------------

def validate_full_payload(payload: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Returns (errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []

    for key in ("gstin", "fp", "version", "hash"):
        if key not in payload:
            errors.append(f"Missing root key {key!r}.")
    if len(str(payload.get("gstin", ""))) != 15:
        errors.append("GSTIN must be 15 characters.")
    fp = str(payload.get("fp", ""))
    if not (len(fp) == 6 and fp.isdigit() and 1 <= int(fp[:2]) <= 12):
        errors.append(f"Return period {fp!r} is not MMYYYY.")

    seen: set = set()
    for i, row in enumerate(payload.get("b2cs", [])):
        key = (row.get("pos"), row.get("rt"), row.get("sply_ty"))
        if key in seen:
            errors.append(f"b2cs[{i}]: duplicate POS+rate row {key} — portal will reject.")
        seen.add(key)
        expected = round(float(row.get("txval", 0)) * float(row.get("rt", 0)) / 100.0, 2)
        got = round(float(row.get("iamt", 0)) + float(row.get("camt", 0))
                    + float(row.get("samt", 0)), 2)
        if abs(expected - got) > 0.02:
            errors.append(
                f"b2cs[{i}] POS {row.get('pos')}: tax {got} does not match "
                f"txval×rate = {expected}."
            )
        if float(row.get("txval", 0)) < 0:
            pos = str(row.get("pos"))
            warnings.append(
                f"B2CS row for {pos}-{STATE_NAMES.get(pos, '?')} is NEGATIVE "
                f"({row.get('txval')}): returns exceeded sales this month for that state. "
                "The portal may reject negative B2CS rows — consult your tax advisor "
                "(usually adjusted against next month)."
            )

    hsn_rows = payload.get("hsn", {}).get("hsn_b2c", [])
    hsn_seen: set = set()
    for i, row in enumerate(hsn_rows):
        hsn_sc = str(row.get("hsn_sc", ""))
        if not hsn_sc or not hsn_sc.isdigit() or len(hsn_sc) not in (4, 6, 8):
            errors.append(
                f"HSN row {i + 1}: code {hsn_sc!r} is not a valid 4/6/8-digit HSN."
            )
        hkey = (hsn_sc, row.get("uqc"), row.get("rt"))
        if hkey in hsn_seen:
            errors.append(
                f"HSN row {i + 1}: duplicate HSN+UQC+rate {hkey} — portal will reject."
            )
        hsn_seen.add(hkey)
        expected = round(float(row.get("txval", 0)) * float(row.get("rt", 0)) / 100.0, 2)
        got = round(float(row.get("iamt", 0)) + float(row.get("camt", 0))
                    + float(row.get("samt", 0)), 2)
        if abs(expected - got) > 1.0:
            warnings.append(
                f"HSN {row.get('hsn_sc')}: tax {got} vs txval×rate {expected} "
                f"differs by more than Rs 1 — verify."
            )

    for det in payload.get("doc_issue", {}).get("doc_det", []):
        for row in det.get("docs", []):
            if row.get("net_issue") != row.get("totnum") - row.get("cancel"):
                errors.append(
                    f"Table 13 row {row.get('from')}: net must equal total - cancelled."
                )

    # Cross-check: HSN taxable vs B2CS+B2CL taxable
    if hsn_rows:
        hsn_total = sum(float(r.get("txval", 0)) for r in hsn_rows)
        b2cs_total = sum(float(r.get("txval", 0)) for r in payload.get("b2cs", []))
        b2cl_total = sum(
            float(itm["itm_det"]["txval"])
            for grp in payload.get("b2cl", [])
            for inv in grp.get("inv", [])
            for itm in inv.get("itms", [])
        )
        if abs(hsn_total - (b2cs_total + b2cl_total)) > 1.0:
            warnings.append(
                f"HSN summary total ({hsn_total:.2f}) differs from B2CS+B2CL total "
                f"({b2cs_total + b2cl_total:.2f}) by more than Rs 1 — the portal "
                "cross-checks these. Verify before filing."
            )
    return errors, warnings


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

DOWNLOAD_HELP = {
    "amazon_mtr": "Amazon Seller Central → Reports → Tax Document Library → "
                  "Merchant Tax Report (MTR) B2C for the month.",
    "amazon_rtf": "Amazon Seller Central → GST Reports → 'GST Ready-to-File Reports' "
                  "card → Download Report.",
    "flipkart_gstr": "Flipkart Seller Hub → Reports → GST Reports → GSTR return report.",
    "flipkart_sales": "Flipkart Seller Hub → Reports → Sales Report for the month.",
    "meesho_sales": "Meesho Supplier Panel → Download → GST Report (contains "
                    "tcs_sales.xlsx + tcs_sales_return.xlsx).",
    "meesho_invoices": "Meesho Supplier Panel → Download → Supplier Tax Invoice "
                       "(contains Tax_invoice_details.xlsx).",
    "gstr2b": "GST portal → Returns Dashboard → select the month → GSTR-2B tile → "
              "Download → 'GENERATE JSON FILE TO DOWNLOAD' (a .json file).",
}


def resolve_period(detected: List[DetectedFile], requested: str) -> Tuple[str, List[Guidance]]:
    guidance: List[Guidance] = []
    hints = [d.period_hint for d in detected if d.period_hint and d.kind not in ("ignored", "unknown")]
    if requested:
        if not (len(requested) == 6 and requested.isdigit()
                and 1 <= int(requested[:2]) <= 12):
            raise ValueError(
                f"Return period {requested!r} is not valid — use MMYYYY, e.g. 062026."
            )
        period = requested
    elif hints:
        period = Counter(hints).most_common(1)[0][0]
        guidance.append(Guidance(
            "info", f"Return period auto-detected: {_fmt_period(period)}",
            "Detected from the dates inside your files. Set it explicitly if this is wrong."))
    else:
        raise ValueError(
            "Could not auto-detect the return period from the files — "
            "enter it explicitly (MMYYYY)."
        )
    # Exclude files that clearly belong to another month (the April-vs-June trap).
    for d in detected:
        if d.kind in ("ignored", "unknown"):
            continue
        if d.period_hint and d.period_hint != period:
            d.excluded = True
            d.message = (f"File is for {_fmt_period(d.period_hint)} but you are filing "
                         f"{_fmt_period(period)} — EXCLUDED to protect your filing.")
    excluded = [d for d in detected if d.excluded]
    if excluded:
        guidance.append(Guidance(
            "warning", f"{len(excluded)} file(s) from a different month excluded",
            "\n".join(f"• {d.name} → {d.message}" for d in excluded)))
    return period, guidance


def build(detected: List[DetectedFile], requested_period: str = "") -> BuildResult:
    guidance: List[Guidance] = []
    period, period_guidance = resolve_period(detected, requested_period.strip())
    guidance.extend(period_guidance)

    active: Dict[str, List[DetectedFile]] = {}
    for d in detected:
        if not d.excluded and d.kind not in ("ignored", "unknown"):
            active.setdefault(d.kind, []).append(d)

    for kind, files in active.items():
        if len(files) > 1:
            # Prefer a file whose detected period matches the filing period;
            # otherwise fall back to the last one scanned.
            matching = [f for f in files if f.period_hint == period]
            keep = (matching or files)[-1]
            for extra in files:
                if extra is keep:
                    continue
                extra.excluded = True
                extra.message = f"Duplicate {extra.label} — using {keep.name} instead."
            guidance.append(Guidance(
                "warning", f"Multiple {KINDS_TITLE.get(kind, kind)} files found",
                f"Using {keep.name}; skipped: "
                f"{', '.join(f.name for f in files if f is not keep)}. Remove stale "
                "copies from the folder to avoid mistakes."))
            active[kind] = [keep]

    unknown = [d for d in detected if d.kind == "unknown"]
    if unknown:
        guidance.append(Guidance(
            "warning", f"{len(unknown)} file(s) not recognized",
            "\n".join(f"• {d.name}: {d.message}" for d in unknown)
            + "\nIf one of these is a platform GST report, tell the developer — "
              "it may be a new format."))

    temp_paths: List[Path] = []

    def path_of(kind: str, suffix: str) -> Optional[Path]:
        if kind not in active:
            return None
        p = _save_temp(active[kind][0].data, suffix)
        temp_paths.append(p)
        return p

    platforms: List[PlatformStatus] = []
    records_by_platform: Dict[str, List[TaxRecord]] = {
        "Flipkart": [], "Amazon": [], "Meesho": []}
    all_hsn: List[HsnRow] = []
    all_series: List[SeriesRow] = []
    all_b2cl: List[B2clInvoice] = []
    seller_gstins: Dict[str, str] = {}

    def add_hsn(rows: List[HsnRow], source: str) -> None:
        """Add a platform's HSN rows after enforcing tax == taxable x rate."""
        fixed, fixes = reconcile_hsn_tax(rows, source)
        all_hsn.extend(fixed)
        for msg in fixes:
            guidance.append(Guidance(
                "warning", f"{source}: HSN tax corrected to match taxable value",
                msg))

    try:
        # ------------------------------------------------- Amazon
        amz_rtf = path_of("amazon_rtf", ".xlsx")
        amz_mtr = path_of("amazon_mtr", ".csv")
        if amz_rtf or amz_mtr:
            status = PlatformStatus("Amazon", "ok")
            notes: List[str] = []
            try:
                if amz_rtf:
                    # B2C Small only — B2C Large invoices are routed to Table 5
                    # below; including both here would double-report them.
                    sg, _eco, recs = ReportParsers.parse_amazon_ready_to_file(
                        amz_rtf, sheets=("B2C Small",))
                    extras = parse_amazon_rtf_extras(amz_rtf)
                    records_by_platform["Amazon"] = recs
                    add_hsn(extras["hsn"], "Amazon Ready-to-File")
                    all_b2cl.extend(extras["b2cl"])
                    notes.extend(extras["warnings"])
                    seller_gstins["Amazon"] = sg
                    if extras["b2cl"]:
                        # B2C Large already excluded from B2C Small by Amazon.
                        recs_large = {i.inum for i in extras["b2cl"]}
                        notes.append(
                            f"{len(recs_large)} Amazon B2C Large invoice(s) routed to Table 5.")
                if amz_mtr:
                    mtr = parse_amazon_mtr_extras(amz_mtr)
                    all_series.extend(build_series(mtr["docs"], policy="runs"))
                    notes.extend(mtr["warnings"])
                    if not amz_rtf:
                        sg, _eco, recs = ReportParsers.parse_amazon(amz_mtr)
                        records_by_platform["Amazon"] = recs
                        add_hsn(mtr["hsn"], "Amazon MTR")
                        seller_gstins["Amazon"] = sg
                        notes.append(
                            "Values computed from MTR (Ready-to-File not provided — "
                            "recommended for Amazon's official HSN summary).")
                    elif mtr.get("seller_gstin") and mtr["seller_gstin"] != seller_gstins.get("Amazon"):
                        notes.append(
                            f"Amazon MTR GSTIN {mtr['seller_gstin']} differs from "
                            f"Ready-to-File GSTIN {seller_gstins.get('Amazon')} — check!")
                if amz_rtf and not amz_mtr:
                    status.status = "partial"
                    guidance.append(Guidance(
                        "warning", "Amazon: invoice numbers missing (Table 13)",
                        "The Ready-to-File report has no invoice numbers. Download the "
                        "MTR B2C CSV as well:\n" + DOWNLOAD_HELP["amazon_mtr"]))
                status.detail = " ".join(notes)
            except Exception as exc:
                status = PlatformStatus("Amazon", "error", str(exc))
                guidance.append(Guidance("error", "Amazon report failed to process", str(exc)))
            platforms.append(status)
        else:
            platforms.append(PlatformStatus("Amazon", "missing", "No Amazon report found."))
            guidance.append(Guidance(
                "info", "No Amazon report in the folder",
                "If you sell on Amazon, download:\n• " + DOWNLOAD_HELP["amazon_rtf"]
                + "\n• " + DOWNLOAD_HELP["amazon_mtr"]))

        # ------------------------------------------------- Flipkart
        fk_gstr = path_of("flipkart_gstr", ".xlsx")
        fk_sales = path_of("flipkart_sales", ".xlsx")
        if fk_gstr or fk_sales:
            status = PlatformStatus("Flipkart", "ok")
            notes = []
            try:
                if fk_gstr:
                    sg, _eco, recs = ReportParsers.parse_flipkart(fk_gstr)
                    extras = parse_flipkart_gstr_extras(fk_gstr)
                    records_by_platform["Flipkart"] = recs
                    add_hsn(extras["hsn"], "Flipkart GSTR report")
                    all_series.extend(extras["series"])
                    all_b2cl.extend(extras["b2cl"])
                    notes.extend(extras["warnings"])
                    seller_gstins["Flipkart"] = sg
                    if fk_sales:
                        sales = parse_flipkart_sales_report(fk_sales)
                        note_docs = [d for d in sales["docs"] if d.kind in ("credit", "debit")]
                        all_series.extend(build_series(note_docs, policy="runs"))
                        notes.extend(sales["warnings"])
                        gstr_total = round(sum(r.txval for r in recs), 2)
                        sales_total = round(sum(r.txval for r in sales["records"]), 2)
                        if abs(gstr_total - sales_total) > 1.0:
                            guidance.append(Guidance(
                                "warning", "Flipkart reports disagree",
                                f"GSTR return report total {gstr_total} vs Sales Report "
                                f"total {sales_total}. Using the official GSTR report "
                                "values; investigate the difference before filing."))
                    else:
                        status.status = "partial"
                        guidance.append(Guidance(
                            "warning", "Flipkart: credit-note series missing (Table 13)",
                            "The GSTR return report lists only the invoice series. For "
                            "returns/cashback credit-note series also add the Sales "
                            "Report:\n" + DOWNLOAD_HELP["flipkart_sales"]))
                else:
                    sales = parse_flipkart_sales_report(fk_sales)
                    records_by_platform["Flipkart"] = sales["records"]
                    add_hsn(sales["hsn"], "Flipkart Sales Report")
                    all_series.extend(build_series(sales["docs"], policy="runs"))
                    all_b2cl.extend(sales["b2cl"])
                    notes.extend(sales["warnings"])
                    seller_gstins["Flipkart"] = sales["seller_gstin"]
                    notes.append("Computed from the per-invoice Sales Report "
                                 "(sales − returns − cashback credit notes).")
                status.detail = " ".join(notes)
            except Exception as exc:
                status = PlatformStatus("Flipkart", "error", str(exc))
                guidance.append(Guidance("error", "Flipkart report failed to process", str(exc)))
            platforms.append(status)
        else:
            platforms.append(PlatformStatus("Flipkart", "missing", "No Flipkart report found."))
            guidance.append(Guidance(
                "info", "No Flipkart report in the folder",
                "If you sell on Flipkart, download:\n• " + DOWNLOAD_HELP["flipkart_gstr"]
                + "\n• " + DOWNLOAD_HELP["flipkart_sales"]))

        # ------------------------------------------------- Meesho
        ms_sales = path_of("meesho_sales", ".xlsx")
        ms_returns = path_of("meesho_returns", ".xlsx")
        ms_inv = path_of("meesho_invoices", ".xlsx")
        if ms_returns and not ms_sales:
            platforms.append(PlatformStatus(
                "Meesho", "error",
                "Found the RETURNS file but not the sales file (tcs_sales.xlsx) — "
                "returns cannot be netted against nothing."))
            guidance.append(Guidance(
                "error", "Meesho: returns file found but sales file missing",
                "tcs_sales_return.xlsx was detected, but tcs_sales.xlsx was not "
                "(missing, or excluded as wrong-month). Meesho was NOT included in "
                "this build. Add the sales file and re-run:\n"
                + DOWNLOAD_HELP["meesho_sales"]))
        elif ms_sales or ms_inv:
            status = PlatformStatus("Meesho", "ok")
            notes = []
            try:
                if ms_sales:
                    sg, _eco, recs = ReportParsers.parse_meesho(ms_sales, ms_returns)
                    records_by_platform["Meesho"] = recs
                    add_hsn(parse_meesho_hsn(ms_sales, ms_returns), "Meesho")
                    seller_gstins["Meesho"] = sg
                    if not ms_returns:
                        notes.append("No returns file — assuming zero Meesho returns.")
                        guidance.append(Guidance(
                            "warning", "Meesho returns file not found",
                            "Only tcs_sales.xlsx was found. If you had returns this "
                            "month, also include tcs_sales_return.xlsx (it is inside "
                            "the same GST Report zip)."))
                else:
                    status.status = "partial"
                    guidance.append(Guidance(
                        "error", "Meesho: values missing",
                        "Found the Supplier Tax Invoice file but not the GST Report "
                        "(tcs_sales.xlsx) which carries the amounts:\n"
                        + DOWNLOAD_HELP["meesho_sales"]))
                if ms_inv:
                    inv = parse_meesho_invoice_details(ms_inv)
                    all_series.extend(build_series(inv["docs"], policy="span"))
                    notes.extend(inv["warnings"])
                else:
                    status.status = "partial"
                    guidance.append(Guidance(
                        "warning", "Meesho: invoice series missing (Table 13)",
                        "Download the Supplier Tax Invoice file too:\n"
                        + DOWNLOAD_HELP["meesho_invoices"]))
                status.detail = " ".join(notes)
            except Exception as exc:
                status = PlatformStatus("Meesho", "error", str(exc))
                guidance.append(Guidance("error", "Meesho report failed to process", str(exc)))
            platforms.append(status)
        else:
            platforms.append(PlatformStatus("Meesho", "missing", "No Meesho report found."))
            guidance.append(Guidance(
                "info", "No Meesho report in the folder",
                "If you sell on Meesho, download:\n• " + DOWNLOAD_HELP["meesho_sales"]
                + "\n• " + DOWNLOAD_HELP["meesho_invoices"]))
    finally:
        for p in temp_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:
                pass

    # ------------------------------------------------- Seller GSTIN
    gstin_values = list(seller_gstins.values())
    if not gstin_values:
        raise ValueError(
            "No platform report could be processed — nothing to build. "
            "Check the guidance messages for what went wrong."
        )
    seller_gstin = gstin_values[0]
    mismatched = {p: g for p, g in seller_gstins.items() if g != seller_gstin}
    if mismatched:
        guidance.append(Guidance(
            "error", "Seller GSTIN differs between platforms!",
            f"Primary: {seller_gstin}. Mismatches: {mismatched}. "
            "All reports must belong to the same GSTIN for one GSTR-1."))

    # ------------------------------------------------- Consolidate B2CS
    csv_rows, json_rows, stats = GSTConsolidator.consolidate(
        records_by_platform["Flipkart"],
        records_by_platform["Amazon"],
        records_by_platform["Meesho"],
        seller_gstin,
    )
    for p in platforms:
        if p.name in stats and p.status in ("ok", "partial"):
            p.records = len(records_by_platform.get(p.name, []))
            p.taxable = stats[p.name]["taxable"]
            p.tax = stats[p.name]["tax"]

    merged_hsn = merge_hsn(all_hsn)
    all_series.sort(key=lambda s: (s.kind, s.frm))

    # ------------------------------------------------- Payloads
    sections: Dict[str, Any] = {"b2cs": json_rows}
    if all_b2cl:
        sections["b2cl"] = b2cl_section(all_b2cl)
    if merged_hsn:
        sections["hsn"] = hsn_section(merged_hsn)
    if all_series:
        sections["doc_issue"] = doc_issue_section(all_series)
    payload_full = make_payload(seller_gstin, period, **sections)

    errors, val_warnings = validate_full_payload(payload_full)
    for e in errors:
        guidance.append(Guidance("error", "Validation failed", e))
    for w in val_warnings:
        guidance.append(Guidance("warning", "Check before filing", w))

    # ------------------------------------------------- GSTR-3B (filed after GSTR-1)
    itc: Optional[Gstr2bResult] = None
    if "gstr2b" in active:
        f2b = active["gstr2b"][0]
        tile = PlatformStatus("GSTR-2B (ITC)", "ok")
        try:
            itc = parse_gstr2b_json(f2b.data)
            if itc.gstin and itc.gstin != seller_gstin:
                guidance.append(Guidance(
                    "error", "GSTR-2B belongs to a different GSTIN!",
                    f"GSTR-2B is for {itc.gstin} but the sales reports are for "
                    f"{seller_gstin}. ITC was NOT included — use your own GSTR-2B."))
                itc = None
                tile = PlatformStatus("GSTR-2B (ITC)", "error",
                                      "Different GSTIN — not used.")
            elif itc.rtnprd != period:
                # Defense in depth: auto-detection already excludes wrong-month
                # files, but a malformed/missing rtnprd must never slip through.
                guidance.append(Guidance(
                    "error", "GSTR-2B is not for this filing period!",
                    f"The GSTR-2B file reports period "
                    f"{itc.rtnprd or 'unknown'} but you are filing "
                    f"{_fmt_period(period)}. ITC was NOT included — download the "
                    f"GSTR-2B for {_fmt_period(period)}:\n"
                    + DOWNLOAD_HELP["gstr2b"]))
                itc = None
                tile = PlatformStatus("GSTR-2B (ITC)", "error",
                                      "Wrong or unreadable period — not used.")
            else:
                for msg in itc.errors:
                    guidance.append(Guidance("error", "GSTR-2B needs manual attention", msg))
                for msg in itc.warnings:
                    guidance.append(Guidance("warning", "GSTR-2B check", msg))
                itc_tax = sum(itc.itc_oth[h] + itc.itc_isrc[h] for h in TAX_HEADS)
                tile.records = itc.doc_count
                tile.taxable = round(itc.itc_oth["txval"] + itc.itc_isrc["txval"], 2)
                tile.tax = round(itc_tax, 2)
                tile.detail = (f"{itc.doc_count} document(s) from "
                               f"{itc.supplier_count} supplier(s) — ITC for Table 4.")
                if itc.errors:
                    tile.status = "partial"
        except Exception as exc:
            tile = PlatformStatus("GSTR-2B (ITC)", "error", str(exc))
            guidance.append(Guidance("error", "GSTR-2B file failed to process", str(exc)))
        platforms.append(tile)
    else:
        platforms.append(PlatformStatus(
            "GSTR-2B (ITC)", "missing",
            "No GSTR-2B JSON — GSTR-3B Table 4 (ITC) not computed."))
        guidance.append(Guidance(
            "info", "For GSTR-3B ITC, add your GSTR-2B JSON",
            "GSTR-3B Table 4 (your Input Tax Credit — mainly the GST on Amazon/"
            "Flipkart/Meesho commissions and fees) comes from the portal's "
            "auto-drafted GSTR-2B, not from the marketplace sales reports.\n"
            "Download it and drop it in the same folder, then re-run:\n• "
            + DOWNLOAD_HELP["gstr2b"]
            + "\nUntil then the GSTR-3B JSON contains NO Table 4, so the "
              "portal's auto-drafted ITC stays untouched — verify it there."))

    b2cs_taxable = round(sum(float(r["txval"]) for r in json_rows), 2)
    b2cs_tax = round(sum(float(r["iamt"]) + float(r["camt"]) + float(r["samt"])
                         for r in json_rows), 2)
    b2cl_taxable = round(sum(i.txval for i in all_b2cl), 2)
    b2cl_tax = round(sum(i.iamt + i.csamt for i in all_b2cl), 2)

    t31a, inter_rows = compute_31a_and_32(json_rows, all_b2cl)
    payload_3b = build_gstr3b_payload(seller_gstin, period, t31a, inter_rows, itc)
    errors_3b, warnings_3b = validate_gstr3b(
        payload_3b, round(b2cs_taxable + b2cl_taxable, 2),
        round(b2cs_tax + b2cl_tax, 2))
    for e in errors_3b:
        guidance.append(Guidance("error", "GSTR-3B validation failed", e))
    for w in warnings_3b:
        guidance.append(Guidance("warning", "GSTR-3B — check before filing", w))
    errors.extend(errors_3b)

    tcs_estimate = round(0.005 * t31a["txval"], 2) if t31a["txval"] > 0 else 0.0
    if tcs_estimate:
        guidance.append(Guidance(
            "info", f"TCS credit to accept separately (≈ ₹{tcs_estimate:,.2f})",
            "Marketplaces deduct TCS u/s 52 (0.5% of net taxable sales). It is "
            "NOT part of GSTR-3B — accept it on the portal under Services → "
            "Returns → TDS and TCS credit received, and it lands in your cash "
            "ledger before you pay GSTR-3B tax."))

    # ------------------------------------------------- Cards
    cards: List[TableCard] = []

    def dump(payload: Dict[str, Any]) -> str:
        return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)

    b2cs_payload = make_payload(seller_gstin, period, b2cs=json_rows)
    cards.append(TableCard(
        id="b2cs", table_no="Table 7", title="B2CS — B2C (Others)",
        subtitle="State-wise net taxable supplies through e-commerce",
        stats=[{"label": "Rows", "value": len(json_rows)},
               {"label": "Taxable ₹", "value": f"{stats['Consolidated']['taxable']:,.2f}"},
               {"label": "Tax ₹", "value": f"{stats['Consolidated']['tax']:,.2f}"}],
        headers=["POS", "Type", "Rate %", "Taxable ₹", "IGST ₹", "CGST ₹", "SGST ₹"],
        rows=[[f"{r['pos']}-{STATE_NAMES.get(r['pos'], '?')}", r["sply_ty"], r["rt"],
               r["txval"], r["iamt"], r["camt"], r["samt"]] for r in json_rows],
        json_str=dump(b2cs_payload), filename="b2cs.json",
    ))

    if all_b2cl:
        b2cl_rows = b2cl_section(all_b2cl)
        cards.append(TableCard(
            id="b2cl", table_no="Table 5", title="B2CL — Large B2C invoices",
            subtitle="Inter-state invoices above ₹1,00,000 (per invoice)",
            stats=[{"label": "Invoices", "value": len(all_b2cl)},
                   {"label": "Taxable ₹", "value": f"{sum(i.txval for i in all_b2cl):,.2f}"}],
            headers=["POS", "Invoice", "Date", "Value ₹", "Rate %", "Taxable ₹", "IGST ₹"],
            rows=[[f"{i.pos}-{STATE_NAMES.get(i.pos, '?')}", i.inum, i.idt, i.val,
                   i.rate, i.txval, i.iamt] for i in all_b2cl],
            json_str=dump(make_payload(seller_gstin, period, b2cl=b2cl_rows)),
            filename="b2cl.json",
        ))

    if merged_hsn:
        cards.append(TableCard(
            id="hsn", table_no="Table 12", title="HSN summary (B2C)",
            subtitle="Enter in the B2C tab; HSN must be picked from the portal dropdown",
            stats=[{"label": "HSN rows", "value": len(merged_hsn)},
                   {"label": "Qty", "value": f"{sum(h.qty for h in merged_hsn):,.0f}"},
                   {"label": "Taxable ₹", "value": f"{sum(h.txval for h in merged_hsn):,.2f}"}],
            headers=["HSN", "UQC", "Qty", "Rate %", "Taxable ₹", "IGST ₹", "CGST ₹", "SGST ₹"],
            rows=[[h.hsn, h.uqc, h.qty, h.rate, h.txval, h.iamt, h.camt, h.samt]
                  for h in merged_hsn],
            json_str=dump(make_payload(seller_gstin, period, hsn=hsn_section(merged_hsn))),
            filename="hsn.json",
        ))

    if all_series:
        inv_rows = [s for s in all_series if s.kind == "invoice"]
        cn_rows = [s for s in all_series if s.kind == "credit"]
        kind_labels = {"invoice": "Invoice", "credit": "Credit note", "debit": "Debit note"}
        cards.append(TableCard(
            id="docs", table_no="Table 13", title="Documents issued",
            subtitle="Invoice and credit/debit-note series (from/to/cancelled)",
            stats=[{"label": "Invoice series", "value": len(inv_rows)},
                   {"label": "Net invoices", "value": sum(s.net for s in inv_rows)},
                   {"label": "Credit notes", "value": sum(s.net for s in cn_rows)}],
            headers=["Type", "From", "To", "Total", "Cancelled", "Net"],
            rows=[[kind_labels.get(s.kind, s.kind),
                   s.frm, s.to, s.totnum, s.cancel, s.net] for s in all_series],
            json_str=dump(make_payload(seller_gstin, period,
                                       doc_issue=doc_issue_section(all_series))),
            filename="docs.json",
        ))

    cards.append(TableCard(
        id="full", table_no="GSTR-1", title="Complete GSTR-1 JSON",
        subtitle="One file, all tables — upload via Prepare Offline on the portal",
        stats=[{"label": "Sections", "value": ", ".join(
            k for k in ("b2cs", "b2cl", "hsn", "doc_issue") if k in payload_full)},
               {"label": "Taxable ₹", "value": f"{stats['Consolidated']['taxable']:,.2f}"},
               {"label": "Tax ₹", "value": f"{stats['Consolidated']['tax']:,.2f}"}],
        headers=[], rows=[],
        json_str=dump(payload_full), filename=f"gstr1_full_{period}.json",
    ))

    # GSTR-3B card — every portal field with the exact value to enter/verify
    det = payload_3b["sup_details"]["osup_det"]
    rcm = payload_3b["sup_details"]["isup_rev"]
    out_tax = round(float(det["iamt"]) + float(det["camt"])
                    + float(det["samt"]) + float(det["csamt"]), 2)
    rows_3b: List[List[Any]] = [
        ["3.1(a)", "Outward taxable supplies (other than zero/nil/exempt)",
         det["txval"], det["iamt"], det["camt"], det["samt"], det["csamt"]],
        ["3.1(b)", "Outward zero-rated (exports/SEZ)", 0, 0, "—", "—", 0],
        ["3.1(c)", "Other outward (nil-rated, exempted)", 0, "—", "—", "—", "—"],
        ["3.1(d)", "Inward supplies liable to reverse charge",
         rcm["txval"], rcm["iamt"], rcm["camt"], rcm["samt"], rcm["csamt"]],
        ["3.1(e)", "Non-GST outward supplies", 0, "—", "—", "—", "—"],
        ["3.1.1", "Supplies u/s 9(5) via e-commerce operator — services only, "
                  "not applicable to goods sellers", 0, 0, 0, 0, 0],
    ]
    for r in payload_3b.get("inter_sup", {}).get("unreg_details", []):
        rows_3b.append([
            "3.2", f"Inter-state to unregistered — {r['pos']}-"
                   f"{STATE_NAMES.get(str(r['pos']), '?')}",
            r["txval"], r["iamt"], "—", "—", "—"])
    if itc is not None:
        rows_3b.extend([
            ["4(A)(3)", "ITC — inward supplies liable to reverse charge", "—",
             _num(itc.itc_isrc["iamt"]), _num(itc.itc_isrc["camt"]),
             _num(itc.itc_isrc["samt"]), _num(itc.itc_isrc["csamt"])],
            ["4(A)(5)", "ITC — all other ITC (from GSTR-2B)", "—",
             _num(itc.itc_oth["iamt"]), _num(itc.itc_oth["camt"]),
             _num(itc.itc_oth["samt"]), _num(itc.itc_oth["csamt"])],
            ["4(B)", "ITC reversed", "—", 0, 0, 0, 0],
            ["4(C)", "Net ITC available", "—",
             payload_3b["itc_elg"]["itc_net"]["iamt"],
             payload_3b["itc_elg"]["itc_net"]["camt"],
             payload_3b["itc_elg"]["itc_net"]["samt"],
             payload_3b["itc_elg"]["itc_net"]["csamt"]],
        ])
        if any(itc.itc_unavail[h] for h in TAX_HEADS):
            rows_3b.append(
                ["4(D)(2)", "ITC unavailable per GSTR-2B (informational)", "—",
                 _num(itc.itc_unavail["iamt"]), _num(itc.itc_unavail["camt"]),
                 _num(itc.itc_unavail["samt"]), _num(itc.itc_unavail["csamt"])])
    else:
        rows_3b.append(["4", "ITC not computed — add the GSTR-2B JSON and re-run; "
                             "meanwhile keep the portal's auto-drafted Table 4",
                        "—", "—", "—", "—", "—"])
    rows_3b.extend([
        ["5", "Exempt/nil-rated/non-GST inward supplies", 0, "—", "—", "—", "—"],
        ["5.1", "Interest & late fee (if filing on time: zero)", "—", 0, 0, 0, 0],
    ])
    itc_total = (round(sum(itc.itc_oth[h] + itc.itc_isrc[h] for h in TAX_HEADS), 2)
                 if itc is not None else None)
    stats_3b = [{"label": "Output tax ₹", "value": f"{out_tax:,.2f}"},
                {"label": "ITC ₹", "value": (f"{itc_total:,.2f}" if itc_total is not None
                                             else "add GSTR-2B")},
                {"label": "Net payable ₹ (indicative)",
                 "value": (f"{max(0.0, round(out_tax - itc_total, 2)):,.2f}"
                           if itc_total is not None else "—")}]
    if tcs_estimate:
        stats_3b.append({"label": "TCS credit ≈ ₹ (accept separately)",
                         "value": f"{tcs_estimate:,.2f}"})
    cards.append(TableCard(
        id="gstr3b", table_no="GSTR-3B", title="GSTR-3B — summary return",
        subtitle="File AFTER GSTR-1. Values below match your GSTR-1 exactly — "
                 "verify them against the portal's auto-drafted 3B, table by table",
        stats=stats_3b,
        headers=["Table", "Description", "Taxable ₹", "IGST ₹", "CGST ₹",
                 "SGST ₹", "Cess ₹"],
        rows=rows_3b,
        json_str=dump(payload_3b), filename=f"gstr3b_{period}.json",
    ))

    scan_entries = []
    for d in detected:
        status = "ok"
        if d.excluded:
            status = "error"
        elif d.kind == "unknown":
            status = "warning"
        elif d.kind == "ignored":
            status = "ignored"
        scan_entries.append({
            "name": d.name, "status": status, "platform": d.platform,
            "kind": d.label, "message": d.message,
        })

    totals = dict(stats["Consolidated"])
    return BuildResult(
        period=period,
        seller_gstin=seller_gstin,
        scan_entries=scan_entries,
        guidance=sorted(guidance, key=lambda g: {"error": 0, "warning": 1, "info": 2}[g.level]),
        platforms=platforms,
        totals=totals,
        cards=cards,
        payload_full=payload_full,
        csv_rows=csv_rows,
        breakdown_stats=stats,
        records_by_platform=records_by_platform,
        ok=not errors,
        payload_3b=payload_3b,
    )


KINDS_TITLE = {
    "amazon_mtr": "Amazon MTR",
    "amazon_rtf": "Amazon Ready-to-File",
    "flipkart_gstr": "Flipkart GSTR report",
    "flipkart_sales": "Flipkart Sales Report",
    "meesho_sales": "Meesho GST sales",
    "meesho_returns": "Meesho GST returns",
    "meesho_invoices": "Meesho invoice details",
    "gstr2b": "GSTR-2B (ITC statement)",
}
