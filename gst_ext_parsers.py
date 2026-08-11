"""Extended platform parsers — HSN summaries, document series, B2CL invoices.

Complements gst_parsers.py (which handles B2CS records) with everything the
GST Helper auto-pipeline needs to fill Tables 5, 12 and 13.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from gst_parsers import TaxRecord, normalize_state_to_code, rate_to_percent


def _normalize_headers(df: pd.DataFrame) -> pd.DataFrame:
    """Collapse whitespace/newlines inside column names.

    Real Flipkart GSTR reports wrap headers across lines (e.g.
    'Invoice Series \nTo'), which breaks plain-string lookups.
    """
    df.columns = [re.sub(r"\s+", " ", str(c)).strip() for c in df.columns]
    return df

FLIPKART_ECO_GSTIN_DEFAULT = "29AAACF1435D1Z5"
B2CL_THRESHOLD = 100000.0
KNOWN_GST_RATES = (0.0, 0.1, 0.25, 1.0, 1.5, 3.0, 5.0, 6.0, 7.5, 12.0, 18.0, 28.0)

DELIVERY_STATE_COL = "Customer's Delivery State"


@dataclass
class HsnRow:
    """One HSN-summary line (Table 12), platform-level before merging."""
    hsn: str
    uqc: str
    qty: float
    txval: float
    iamt: float
    camt: float
    samt: float
    csamt: float
    rate: float  # percent, e.g. 5.0
    desc: str = ""


def reconcile_hsn_tax(rows: List["HsnRow"], source: str) -> Tuple[List["HsnRow"], List[str]]:
    """Enforce the portal rule: for one HSN+rate row, tax == taxable x rate.

    Platform HSN sheets sometimes report GROSS tax against NET taxable value.
    Real case (Amazon, July 2026): two 100%-promotional 'free' shipments had
    `Item Promo Tax -8.52` fully offsetting their tax, so Amazon's B2CS sheet
    correctly showed them at Rs 0 taxable — but its HSN Summary still added
    their 8.52 IGST each, giving taxable 170.48 with tax 25.56. The GST portal
    rejects that arithmetic, and the tax actually payable is the netted one.

    The taxable value (which drives the liability and must tally with B2CS) is
    trusted; the tax is recomputed from it, preserving the IGST vs CGST/SGST
    split. Every correction is reported — never silent.
    """
    warnings: List[str] = []
    for r in rows:
        expected = round(r.txval * r.rate / 100.0, 2)
        got = round(r.iamt + r.camt + r.samt, 2)
        if abs(expected - got) <= 0.02:
            continue
        if r.rate <= 0:
            warnings.append(
                f"{source} HSN {r.hsn}: tax {got} reported but the rate is "
                f"{r.rate}% — cannot verify. NOT corrected; check before filing.")
            continue
        if got == 0:
            warnings.append(
                f"{source} HSN {r.hsn}: taxable {r.txval} at {r.rate}% should "
                f"carry {expected} tax but the report shows zero — NOT corrected "
                "(cannot tell IGST from CGST/SGST). Check before filing.")
            continue
        before = f"IGST {r.iamt}/CGST {r.camt}/SGST {r.samt}"
        if r.camt == 0 and r.samt == 0:                 # pure inter-state
            r.iamt = expected
        elif r.iamt == 0:                               # pure intra-state
            half = round(expected / 2.0, 2)
            r.camt, r.samt = half, round(expected - half, 2)
        else:                                           # mixed — scale in place
            factor = expected / got
            r.iamt, r.camt, r.samt = (round(r.iamt * factor, 2),
                                      round(r.camt * factor, 2),
                                      round(r.samt * factor, 2))
            residue = round(expected - (r.iamt + r.camt + r.samt), 2)
            if residue:
                biggest = max(("iamt", "camt", "samt"),
                              key=lambda a: abs(getattr(r, a)))
                setattr(r, biggest, round(getattr(r, biggest) + residue, 2))
        warnings.append(
            f"{source} HSN {r.hsn}: the report's own HSN sheet showed tax {got} "
            f"against taxable {r.txval} at {r.rate}% (arithmetically impossible — "
            f"usually gross tax on zero-value promo/replacement shipments). "
            f"CORRECTED to {expected} to match the taxable value and your B2CS "
            f"[{before} -> IGST {r.iamt}/CGST {r.camt}/SGST {r.samt}].")
    return rows, warnings


@dataclass
class DocEntry:
    """One issued document (invoice or credit note) for Table 13."""
    doc_id: str
    kind: str            # 'invoice' | 'credit'
    cancelled: bool = False


@dataclass
class SeriesRow:
    """One Table 13 series row (from/to/counts)."""
    kind: str            # 'invoice' | 'credit'
    frm: str
    to: str
    totnum: int
    cancel: int

    @property
    def net(self) -> int:
        return self.totnum - self.cancel


@dataclass
class B2clInvoice:
    """One B2C Large invoice (Table 5): inter-state, value > Rs 1,00,000."""
    pos: str
    inum: str
    idt: str             # DD-MM-YYYY
    val: float
    rate: float          # percent
    txval: float
    iamt: float
    csamt: float = 0.0
    etin: str = ""


def _num(v: Any) -> float:
    try:
        f = float(v)
        return 0.0 if pd.isna(f) else f
    except (TypeError, ValueError):
        return 0.0


def _fmt_idt(v: Any) -> str:
    """Normalize any date-ish value to portal DD-MM-YYYY."""
    ts = pd.to_datetime(v, errors="coerce")
    return "" if pd.isna(ts) else ts.strftime("%d-%m-%Y")


def _clean_hsn(v: Any) -> str:
    s = str(v).strip()
    if s.endswith(".0"):
        s = s[:-2]
    return "" if s.lower() == "nan" else s


def _round_hsn_rows(hsn_map: Dict[Tuple[str, float], HsnRow]) -> List[HsnRow]:
    rows: List[HsnRow] = []
    for h in hsn_map.values():
        h.qty = round(h.qty, 2)
        h.txval = round(h.txval, 2)
        h.iamt = round(h.iamt, 2)
        h.camt = round(h.camt, 2)
        h.samt = round(h.samt, 2)
        h.csamt = round(h.csamt, 2)
        if h.txval != 0.0 or h.qty != 0.0:
            rows.append(h)
    return rows


# ---------------------------------------------------------------------------
# Flipkart — Sales Report (per-invoice): B2CS + HSN + docs + cashback CNs
# ---------------------------------------------------------------------------

def parse_flipkart_sales_report(path: Path) -> Dict[str, Any]:
    """Parses Flipkart's per-invoice Sales Report (sheets: Sales Report,
    Cash Back Report). Returns B2CS records net of returns AND cashback
    credit notes, HSN aggregation, and every document for Table 13."""
    xl = pd.ExcelFile(path)
    sr = pd.read_excel(xl, sheet_name="Sales Report")
    cb = (pd.read_excel(xl, sheet_name="Cash Back Report")
          if "Cash Back Report" in xl.sheet_names else pd.DataFrame())
    warnings: List[str] = []

    tv_col = "Taxable Value (Final Invoice Amount -Taxes)"
    if tv_col not in sr.columns:
        raise ValueError("Flipkart Sales Report: taxable value column not found.")
    sg_cols = [c for c in sr.columns if c.startswith("SGST Amount")]
    sgr_cols = [c for c in sr.columns if c.startswith("SGST Rate")]
    if not sg_cols or not sgr_cols:
        raise ValueError("Flipkart Sales Report: SGST columns not found.")
    sg_col, sgr_col = sg_cols[0], sgr_cols[0]

    gstins = (sr["Seller GSTIN"].dropna()
              if "Seller GSTIN" in sr.columns else pd.Series(dtype=str))
    seller_gstin = str(gstins.iloc[0]).strip().upper() if not gstins.empty else ""
    if len(seller_gstin) != 15:
        raise ValueError("Seller GSTIN not found in Flipkart Sales Report.")
    seller_pos = seller_gstin[:2]

    groups: Dict[Tuple[str, float], Dict[str, float]] = {}
    hsn_map: Dict[Tuple[str, float], HsnRow] = {}
    order_hsn: Dict[str, str] = {}
    docs: List[DocEntry] = []
    b2cl: List[B2clInvoice] = []
    unknown_events: set = set()

    def bump(pos: str, rate: float, txval: float, ig: float, cg: float, sg: float) -> None:
        g = groups.setdefault((pos, rate),
                              {"txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0})
        g["txval"] += txval
        g["iamt"] += ig
        g["camt"] += cg
        g["samt"] += sg

    def bump_hsn(hsn: str, rate: float, qty: float, txval: float,
                 ig: float, cg: float, sg: float) -> None:
        row = hsn_map.setdefault((hsn, rate), HsnRow(hsn, "PCS", 0, 0, 0, 0, 0, 0, rate))
        row.qty += qty
        row.txval += txval
        row.iamt += ig
        row.camt += cg
        row.samt += sg

    for _, r in sr.iterrows():
        event = str(r.get("Event Type", "")).strip()
        if event not in ("Sale", "Return"):
            if event and event.lower() != "nan":
                unknown_events.add(event)
            continue
        state_raw = r.get(DELIVERY_STATE_COL)
        pos = normalize_state_to_code(state_raw)
        if not pos:
            raise ValueError(
                f"Flipkart Sales Report: cannot resolve state {state_raw!r}. "
                "Add it to STATE_NORMALIZED_MAP."
            )
        rate = _num(r.get("IGST Rate")) or (_num(r.get("CGST Rate")) + _num(r.get(sgr_col)))
        txval = _num(r.get(tv_col))
        ig, cg, sg = _num(r.get("IGST Amount")), _num(r.get("CGST Amount")), _num(r.get(sg_col))
        hsn = _clean_hsn(r.get("HSN Code"))
        qty = _num(r.get("Item Quantity")) * (1 if event == "Sale" else -1)
        inv_id = str(r.get("Buyer Invoice ID", "")).strip()
        order_id = str(r.get("Order ID", "")).strip()
        if order_id and hsn:
            order_hsn.setdefault(order_id, hsn)

        inv_amount = _num(r.get("Final Invoice Amount (Price after discount+Shipping Charges)"))
        if event == "Return" and pos != seller_pos and abs(inv_amount) > B2CL_THRESHOLD:
            warnings.append(
                f"Flipkart RETURN of a B2C Large invoice detected (order {order_id}, "
                f"₹{abs(inv_amount):,.2f}): the correct GSTR-1 treatment is a CDNUR "
                "credit note, which this tool does not auto-fill. The amount has been "
                "netted into B2CS — REVIEW WITH YOUR TAX CONSULTANT before filing."
            )
        is_b2cl = (event == "Sale" and pos != seller_pos and inv_amount > B2CL_THRESHOLD)
        if is_b2cl:
            b2cl.append(B2clInvoice(
                pos=pos, inum=inv_id, idt=_fmt_idt(r.get("Buyer Invoice Date")),
                val=round(inv_amount, 2), rate=rate, txval=round(txval, 2),
                iamt=round(ig, 2), etin=FLIPKART_ECO_GSTIN_DEFAULT,
            ))
            warnings.append(
                f"Flipkart invoice {inv_id} is B2C Large (>Rs 1 lakh, inter-state) — "
                "reported in Table 5 (B2CL), not B2CS. Verify before filing."
            )
        else:
            bump(pos, rate, txval, ig, cg, sg)
        if hsn:
            bump_hsn(hsn, rate, qty, txval, ig, cg, sg)

        if inv_id and inv_id.lower() != "nan":
            docs.append(DocEntry(inv_id, "invoice" if event == "Sale" else "credit"))

    if unknown_events:
        warnings.append(
            f"Flipkart Sales Report: unhandled event types skipped: {sorted(unknown_events)}. "
            "Review whether they carry GST values."
        )

    # Cash Back Report rows are GST credit/debit notes: net them into B2CS + HSN
    # with the correct sign per Document Type.
    if not cb.empty:
        cb_sgr = [c for c in cb.columns if c.startswith("SGST Rate")]
        cb_sg = [c for c in cb.columns if c.startswith("SGST Amount")]
        for _, r in cb.iterrows():
            state_raw = r.get(DELIVERY_STATE_COL)
            pos = normalize_state_to_code(state_raw)
            if not pos:
                raise ValueError(
                    f"Flipkart Cash Back Report: cannot resolve state {state_raw!r}."
                )
            doc_type = str(r.get("Document Type", "Credit Note")).strip().title()
            if doc_type in ("Credit Note", "Nan", ""):
                sign, doc_kind = -1, "credit"     # reduces liability
            elif doc_type == "Debit Note":
                sign, doc_kind = +1, "debit"      # increases liability
            else:
                raise ValueError(
                    f"Flipkart Cash Back Report: unknown Document Type {doc_type!r} — "
                    "cannot process safely."
                )
            rate = _num(r.get("IGST Rate")) or (
                _num(r.get("CGST Rate")) + (_num(r.get(cb_sgr[0])) if cb_sgr else 0.0)
            )
            txval = _num(r.get("Taxable Value"))
            ig = _num(r.get("IGST Amount"))
            cg = _num(r.get("CGST Amount"))
            sg = _num(r.get(cb_sg[0])) if cb_sg else 0.0
            bump(pos, rate, sign * txval, sign * ig, sign * cg, sign * sg)
            order_id = str(r.get("Order ID", "")).strip()
            hsn = order_hsn.get(order_id, "")
            if not hsn and hsn_map:
                hsn = max(hsn_map.values(), key=lambda h: h.txval).hsn
                warnings.append(
                    f"Cashback {doc_kind} note for order {order_id or '?'} could not be "
                    f"matched to an HSN — attributed to {hsn}."
                )
            if hsn:
                bump_hsn(hsn, rate, 0.0, sign * txval, sign * ig, sign * cg, sign * sg)
            cn_id = str(r.get("Credit Note ID/ Debit Note ID", "")).strip()
            if cn_id and cn_id.lower() != "nan":
                docs.append(DocEntry(cn_id, doc_kind))

    records = [
        TaxRecord("Flipkart", pos, rate, g["txval"], g["iamt"], g["camt"], g["samt"], 0.0,
                  FLIPKART_ECO_GSTIN_DEFAULT)
        for (pos, rate), g in groups.items() if round(g["txval"], 2) != 0.0
    ]

    return {
        "seller_gstin": seller_gstin,
        "eco_gstin": FLIPKART_ECO_GSTIN_DEFAULT,
        "records": records,
        "hsn": _round_hsn_rows(hsn_map),
        "docs": docs,
        "b2cl": b2cl,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Flipkart — GSTR return report extras: Section 12 (HSN), 13 (series), 5B (B2CL)
# ---------------------------------------------------------------------------

def parse_flipkart_gstr_extras(path: Path) -> Dict[str, Any]:
    """Reads Sections 12/13/5B from Flipkart's official GSTR return report."""
    xl = pd.ExcelFile(path)
    warnings: List[str] = []
    hsn_rows: List[HsnRow] = []
    series: List[SeriesRow] = []
    b2cl: List[B2clInvoice] = []

    if "Section 12 in GSTR-1" in xl.sheet_names:
        df = _normalize_headers(pd.read_excel(xl, sheet_name="Section 12 in GSTR-1"))
        for _, r in df.iterrows():
            txval = _num(r.get("Total Taxable Value Rs."))
            if round(txval, 2) == 0.0:
                continue
            if not _clean_hsn(r.get("HSN Number")):
                raise ValueError(
                    "Flipkart Section 12: a row has taxable value "
                    f"{txval} but no HSN Number — cannot build Table 12 safely."
                )
            ig = _num(r.get("IGST Amount Rs."))
            cg = _num(r.get("CGST Amount Rs."))
            sg = _num(r.get("SGST Amount Rs."))
            cess = _num(r.get("Cess Rs."))
            total_tax = ig + cg + sg
            raw_rate = (total_tax / txval * 100.0) if txval else 0.0
            rate = min(KNOWN_GST_RATES, key=lambda k: abs(k - raw_rate))
            if abs(rate - raw_rate) > 0.3:
                warnings.append(
                    f"Flipkart Section 12 HSN {r.get('HSN Number')}: derived rate "
                    f"{raw_rate:.2f}% is not a standard GST rate — using {rate}%. Verify."
                )
            hsn_rows.append(HsnRow(
                hsn=_clean_hsn(r.get("HSN Number")), uqc="PCS",
                qty=_num(r.get("Total Quantity in Nos.")),
                txval=round(txval, 2), iamt=round(ig, 2), camt=round(cg, 2),
                samt=round(sg, 2), csamt=round(cess, 2), rate=rate,
            ))

    if "Section 13 in GSTR-1" in xl.sheet_names:
        df = _normalize_headers(pd.read_excel(xl, sheet_name="Section 13 in GSTR-1"))
        for _, r in df.iterrows():
            frm = str(r.get("Invoice Series From", "")).strip()
            to = str(r.get("Invoice Series To", "")).strip()
            tot = int(_num(r.get("Total Number of Invoices")))
            cancel = int(_num(r.get("Cancelled if any")))
            if frm and frm.lower() != "nan" and tot > 0:
                series.append(SeriesRow("invoice", frm, to, tot, cancel))

    if "Section 5B in GSTR-1" in xl.sheet_names:
        df = _normalize_headers(pd.read_excel(xl, sheet_name="Section 5B in GSTR-1"))
        cess_cols = [c for c in df.columns if "CESS" in str(c).upper() and "AMOUNT" in str(c).upper()]
        for _, r in df.iterrows():
            txval = _num(r.get("Taxable Value Rs."))
            inum = str(r.get("Invoice Number", "")).strip()
            if round(txval, 2) == 0.0 or not inum or inum.lower() == "nan":
                continue
            pos = normalize_state_to_code(r.get("Delivered State (PoS)"))
            if not pos:
                warnings.append(
                    f"Flipkart Section 5B invoice {inum}: unresolvable state — SKIPPED. Verify manually."
                )
                continue
            b2cl.append(B2clInvoice(
                pos=pos, inum=inum, idt=_fmt_idt(r.get("Invoice Date")),
                val=round(_num(r.get("Invoice Amount Rs.")), 2),
                rate=_num(r.get("IGST %")),
                txval=round(txval, 2),
                iamt=round(_num(r.get("IGST Amount Rs.")), 2),
                csamt=round(_num(r.get(cess_cols[0])) if cess_cols else 0.0, 2),
                etin=FLIPKART_ECO_GSTIN_DEFAULT,
            ))

    return {"hsn": hsn_rows, "series": series, "b2cl": b2cl, "warnings": warnings}


# ---------------------------------------------------------------------------
# Amazon — MTR extras (docs + HSN fallback) and RTF extras (HSN + B2CL)
# ---------------------------------------------------------------------------

def parse_amazon_mtr_extras(csv_path: Path) -> Dict[str, Any]:
    """Extracts Table 13 documents and (fallback) HSN aggregation from MTR."""
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        df = pd.read_csv(csv_path, encoding="utf-8")
    warnings: List[str] = []
    docs: List[DocEntry] = []
    hsn_map: Dict[Tuple[str, float], HsnRow] = {}
    refunds = 0

    seller_gstin = ""
    if "Seller Gstin" in df.columns:
        s = df["Seller Gstin"].dropna()
        if not s.empty:
            seller_gstin = str(s.iloc[0]).strip().upper()

    unknown_types: Dict[str, int] = {}
    for _, r in df.iterrows():
        t = str(r.get("Transaction Type", "")).strip()
        inum = str(r.get("Invoice Number", "")).strip()
        has_inum = inum and inum.lower() != "nan"
        if t == "Shipment" and has_inum:
            docs.append(DocEntry(inum, "invoice"))
        elif t == "Cancel" and has_inum:
            docs.append(DocEntry(inum, "invoice", cancelled=True))
        elif t == "Refund":
            refunds += 1  # MTR reuses the original invoice number; no CN series exists
        elif t and t.lower() != "nan":
            unknown_types[t] = unknown_types.get(t, 0) + 1

        if t in ("Shipment", "Refund"):
            txval = _num(r.get("Tax Exclusive Gross"))
            if round(txval, 2) == 0.0:
                continue
            hsn = _clean_hsn(r.get("Hsn/sac"))
            if not hsn:
                warnings.append(
                    f"Amazon MTR: invoice {inum} has taxable value {txval} but no "
                    "HSN code — its amount is missing from the HSN summary. Verify."
                )
                continue
            # Round: 0.28*100 -> 28.000000000000004 would break HSN merge keys.
            rate = round((_num(r.get("Igst Rate")) + _num(r.get("Cgst Rate"))
                          + _num(r.get("Sgst Rate"))) * 100.0, 2)
            qty = _num(r.get("Quantity")) * (1 if txval > 0 else -1)
            row = hsn_map.setdefault((hsn, rate), HsnRow(hsn, "PCS", 0, 0, 0, 0, 0, 0, rate))
            row.qty += qty
            row.txval += txval
            row.iamt += _num(r.get("Igst Tax"))
            row.camt += _num(r.get("Cgst Tax"))
            row.samt += _num(r.get("Sgst Tax"))
            row.csamt += _num(r.get("Compensatory Cess Tax"))

    if refunds:
        warnings.append(
            f"Amazon MTR: {refunds} refund row(s) reuse the original invoice number — Amazon "
            "issues no separate credit-note series, so none is reported in Table 13 for Amazon."
        )
    if unknown_types:
        warnings.append(
            f"Amazon MTR: unhandled transaction types skipped: {unknown_types}. "
            "Review whether they carry GST values or documents."
        )
    return {"docs": docs, "hsn": _round_hsn_rows(hsn_map),
            "seller_gstin": seller_gstin, "warnings": warnings}


def parse_amazon_rtf_extras(excel_path: Path) -> Dict[str, Any]:
    """Extracts the HSN Summary sheet and B2C Large invoices from Ready-to-File."""
    xl = pd.ExcelFile(excel_path)
    warnings: List[str] = []
    hsn_rows: List[HsnRow] = []
    b2cl: List[B2clInvoice] = []

    def find_header(sheet: str, *needles: str) -> int:
        """Locate the header row; FAIL LOUDLY if the layout changed —
        silently skipping a sheet means silently under-reporting tax."""
        probe = pd.read_excel(xl, sheet_name=sheet, header=None, nrows=30)
        for i in range(len(probe)):
            vals = [str(v).strip() for v in probe.iloc[i].tolist()]
            if all(n in vals for n in needles):
                return i
        raise ValueError(
            f"Amazon Ready-to-File: could not find the header row "
            f"({', '.join(needles)}) in sheet {sheet!r} — the report layout may "
            "have changed. Do NOT file until this is resolved."
        )

    if "HSN Summary" in xl.sheet_names:
        hdr = find_header("HSN Summary", "HSN", "Taxable Value")
        if hdr is not None:
            df = pd.read_excel(xl, sheet_name="HSN Summary", header=hdr)
            for _, r in df.iterrows():
                txval = _num(r.get("Taxable Value"))
                hsn = _clean_hsn(r.get("HSN"))
                if round(txval, 2) == 0.0 or not hsn:
                    continue
                uqc = str(r.get("UQC", "PCS")).strip() or "PCS"
                desc = str(r.get("Description", "")).strip()
                hsn_rows.append(HsnRow(
                    hsn=hsn, uqc="PCS" if uqc.lower() == "nan" else uqc,
                    qty=_num(r.get("Total Quantity")),
                    txval=round(txval, 2),
                    iamt=round(_num(r.get("Integrated Tax Amount")), 2),
                    camt=round(_num(r.get("Central Tax Amount")), 2),
                    samt=round(_num(r.get("State/UT Tax Amount")), 2),
                    csamt=round(_num(r.get("Cess Amount")), 2),
                    rate=rate_to_percent(r.get("Rate")),
                    desc="" if desc.lower() == "nan" else desc,
                ))

    if "B2C Large" in xl.sheet_names:
        hdr = find_header("B2C Large", "Place Of Supply", "Invoice Number")
        if hdr is not None:
            df = pd.read_excel(xl, sheet_name="B2C Large", header=hdr)
            for _, r in df.iterrows():
                txval = _num(r.get("Taxable Value"))
                inum = str(r.get("Invoice Number", "")).strip()
                if round(txval, 2) == 0.0 or not inum or inum.lower() == "nan":
                    continue
                pos = normalize_state_to_code(r.get("Place Of Supply"))
                if not pos:
                    warnings.append(
                        f"Amazon B2C Large invoice {inum}: unresolvable state — SKIPPED. Verify."
                    )
                    continue
                rate = rate_to_percent(r.get("Rate"))
                eco = str(r.get("E-Commerce GSTIN", "")).strip().upper()
                b2cl.append(B2clInvoice(
                    pos=pos, inum=inum, idt=_fmt_idt(r.get("Invoice date")),
                    val=round(_num(r.get("Invoice Value")), 2),
                    rate=rate, txval=round(txval, 2),
                    iamt=round(txval * rate / 100.0, 2),
                    csamt=round(_num(r.get("Cess Amount")), 2),
                    etin=eco if len(eco) == 15 else "",
                ))
    return {"hsn": hsn_rows, "b2cl": b2cl, "warnings": warnings}


# ---------------------------------------------------------------------------
# Meesho — invoice-details docs and HSN aggregation
# ---------------------------------------------------------------------------

def parse_meesho_invoice_details(path: Path) -> Dict[str, Any]:
    """Parses Tax_invoice_details.xlsx (Invoice_Info sheet)."""
    df = pd.read_excel(path, sheet_name="Invoice_Info")
    docs: List[DocEntry] = []
    warnings: List[str] = []
    for _, r in df.iterrows():
        t = str(r.get("Type", "")).strip().upper()
        inv = str(r.get("Invoice No.", "")).strip()
        if not inv or inv.lower() == "nan":
            continue
        if t == "INVOICE":
            docs.append(DocEntry(inv, "invoice"))
        elif t in ("CREDIT NOTE", "CREDIT_DISCOUNT", "CREDIT DISCOUNT"):
            docs.append(DocEntry(inv, "credit"))
        else:
            warnings.append(
                f"Meesho invoice details: unknown document type {t!r} for {inv} — skipped."
            )
    return {"docs": docs, "warnings": warnings}


def parse_meesho_hsn(sales_path: Path, returns_path: Optional[Path]) -> List[HsnRow]:
    """Aggregates Meesho HSN summary (net of returns) with intra/inter tax split."""
    hsn_map: Dict[Tuple[str, float], HsnRow] = {}
    seller_pos = ""

    def ingest(path: Path, sign: int) -> None:
        nonlocal seller_pos
        xl = pd.ExcelFile(path)
        sheet = [s for s in xl.sheet_names if s != "Help"][0]
        df = pd.read_excel(xl, sheet_name=sheet)
        if not seller_pos and "gstin" in df.columns:
            g = df["gstin"].dropna()
            if not g.empty:
                seller_pos = str(g.iloc[0]).strip()[:2]
        for _, r in df.iterrows():
            state_raw = r.get("end_customer_state_new")
            pos = normalize_state_to_code(state_raw)
            if not pos:
                raise ValueError(f"Meesho HSN: cannot resolve state {state_raw!r}.")
            hsn = _clean_hsn(r.get("hsn_code"))
            if not hsn:
                raise ValueError(
                    f"Meesho HSN: row for sub-order {r.get('sub_order_num')!r} has no "
                    "HSN code — cannot build Table 12 safely."
                )
            rate = _num(r.get("gst_rate"))
            txval = _num(r.get("total_taxable_sale_value")) * sign
            tax = _num(r.get("tax_amount")) * sign
            qty = _num(r.get("quantity")) * sign
            row = hsn_map.setdefault((hsn, rate), HsnRow(hsn, "PCS", 0, 0, 0, 0, 0, 0, rate))
            row.qty += qty
            row.txval += txval
            if pos == seller_pos:
                row.camt += tax / 2.0
                row.samt += tax / 2.0
            else:
                row.iamt += tax

    ingest(sales_path, 1)
    if returns_path is not None:
        ingest(returns_path, -1)
    return _round_hsn_rows(hsn_map)
