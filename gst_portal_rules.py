"""GST portal master data and upload rules, checked BEFORE the JSON is uploaded.

Every rule here mirrors a rejection the GST portal makes on offline upload
(the "Processed with Error" status + error report). Catching them here means
the seller sees a plain-English fix instead of a portal error code.

Real case that prompted this module (Sep 2026): Meesho reported a customer
state as "DAMAN & DIU"; the old code 25 was retired on 26-Jan-2020 when the
UT merged into 26, and the portal rejected the row with RET191240
"Invalid POS selected".

Kept free of project imports so every other module can depend on it.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Place-of-supply codes the portal accepts today. 25 (Daman & Diu) and
# 28 (undivided Andhra Pradesh) are retired and rejected.
VALID_POS: Dict[str, str] = {
    "01": "Jammu & Kashmir", "02": "Himachal Pradesh", "03": "Punjab",
    "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana", "07": "Delhi",
    "08": "Rajasthan", "09": "Uttar Pradesh", "10": "Bihar", "11": "Sikkim",
    "12": "Arunachal Pradesh", "13": "Nagaland", "14": "Manipur",
    "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
    "19": "West Bengal", "20": "Jharkhand", "21": "Odisha",
    "22": "Chhattisgarh", "23": "Madhya Pradesh", "24": "Gujarat",
    "26": "Dadra & Nagar Haveli & Daman & Diu", "27": "Maharashtra",
    "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
    "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman & Nicobar Islands",
    "36": "Telangana", "37": "Andhra Pradesh", "38": "Ladakh",
    "97": "Other Territory",
}

# Retired codes -> the code the portal expects instead.
RETIRED_POS: Dict[str, str] = {"25": "26", "28": "37"}

# GST rates (percent) the portal offers in the rate dropdown. 40% is the
# GST 2.0 slab effective 22-Sep-2025.
VALID_RATES: Tuple[float, ...] = (0.0, 0.1, 0.25, 1.0, 1.5, 3.0, 5.0, 6.0,
                                  7.5, 12.0, 18.0, 28.0, 40.0)

# Unit Quantity Codes accepted in Table 12 (HSN summary).
VALID_UQC = frozenset((
    "BAG", "BAL", "BDL", "BKL", "BOU", "BOX", "BTL", "BUN", "CAN", "CBM",
    "CCM", "CMS", "CTN", "DOZ", "DRM", "GGK", "GMS", "GRS", "GYD", "KGS",
    "KLR", "KME", "LTR", "MLT", "MTR", "MTS", "NOS", "OTH", "PAC", "PCS",
    "PRS", "QTL", "ROL", "SET", "SQF", "SQM", "SQY", "TBS", "TGM", "THD",
    "TON", "TUB", "UGS", "UNT", "YDS", "NA",
))

# Invoice / credit-note / Table 13 serial numbers: max 16 chars, only
# letters, digits, '/' and '-'.
DOC_NUMBER_RE = re.compile(r"^[A-Za-z0-9/\-]{1,16}$")
# 14th char is 'Z' for normal taxpayers but 'C' for e-commerce TCS
# registrations (e.g. Meesho 09AARCM9332R1CM), 'D' for TDS — so not fixed.
GSTIN_RE = re.compile(r"^\d{2}[A-Z0-9]{10}[1-9A-Z][A-Z0-9][0-9A-Z]$")
_GSTIN_CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"

# Known portal error codes -> what to do about them.
PORTAL_ERROR_HINTS: Dict[str, str] = {
    "RET191240": "Invalid place of supply. Retired codes (25 Daman & Diu, "
                 "28 old Andhra Pradesh) must be 26 / 37 — re-generate the "
                 "JSON with this tool, it now maps them automatically.",
}


def pos_name(code: Any) -> str:
    return VALID_POS.get(str(code), "?")


def gstin_checksum_ok(gstin: str) -> bool:
    """GSTIN 15th character is a mod-36 check digit over the first 14."""
    g = str(gstin or "").strip().upper()
    if not GSTIN_RE.fullmatch(g):
        return False
    total = 0
    for i, ch in enumerate(g[:14]):
        prod = _GSTIN_CHARS.index(ch) * (2 if i % 2 else 1)
        total += prod // 36 + prod % 36
    return _GSTIN_CHARS[(36 - total % 36) % 36] == g[14]


def gstin_problem(gstin: str, label: str, checksum: bool = True) -> Optional[Tuple[str, str]]:
    """(level, message) or None. A bad check digit is only a warning, and is
    skipped for marketplace (ECO) GSTINs: their reports print operator GSTINs
    that fail the public algorithm (Meesho TCS GSTIN, 2026)."""
    g = str(gstin or "").strip().upper()
    if not GSTIN_RE.fullmatch(g):
        return "error", f"{label} {gstin!r} is not a valid GSTIN format."
    if g[:2] not in VALID_POS:
        return "error", f"{label} {g} starts with state code {g[:2]}, which the portal does not accept."
    if checksum and not gstin_checksum_ok(g):
        return "warning", (f"{label} {g} fails the GSTIN check-digit test — verify it "
                           "on the portal (Search Taxpayer) before filing.")
    return None


def rate_ok(rate: Any) -> bool:
    try:
        r = float(rate)
    except (TypeError, ValueError):
        return False
    return any(abs(r - v) < 1e-6 for v in VALID_RATES)


def _f(row: Dict[str, Any], key: str) -> float:
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _has_more_than_2_decimals(v: Any) -> bool:
    try:
        return abs(round(float(v), 2) - float(v)) > 1e-9
    except (TypeError, ValueError):
        return False


def check_gstr1_rules(payload: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Portal master-data rules for a GSTR-1 offline JSON. Returns (errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []
    gstin = str(payload.get("gstin", ""))
    def add(problem: Optional[Tuple[str, str]]) -> None:
        if problem:
            (errors if problem[0] == "error" else warnings).append(problem[1])

    add(gstin_problem(gstin, "Seller GSTIN"))
    seller_state = gstin[:2]

    def pos_check(where: str, pos: Any) -> bool:
        p = str(pos)
        if p in RETIRED_POS:
            errors.append(
                f"{where}: POS {p} is retired — the portal rejects it (RET191240 "
                f"'Invalid POS selected'). It must be {RETIRED_POS[p]}-"
                f"{pos_name(RETIRED_POS[p])}.")
            return False
        if p not in VALID_POS:
            errors.append(f"{where}: POS {p!r} is not a valid 2-digit state code "
                          "— the portal rejects it.")
            return False
        return True

    # ---- Table 7 (B2CS)
    for i, row in enumerate(payload.get("b2cs", [])):
        pos = str(row.get("pos"))
        where = f"B2CS row {i + 1} (POS {pos}-{pos_name(pos)})"
        pos_check(where, pos)
        if not rate_ok(row.get("rt")):
            errors.append(f"{where}: rate {row.get('rt')}% is not a GST rate the portal accepts.")
        sply = str(row.get("sply_ty", "")).upper()
        want = "INTRA" if pos == seller_state else "INTER"
        if sply != want:
            errors.append(
                f"{where}: supply type is {sply or 'missing'} but POS "
                f"{'equals' if want == 'INTRA' else 'differs from'} your state "
                f"{seller_state} — must be {want}.")
        if want == "INTER" and (_f(row, "camt") or _f(row, "samt")):
            errors.append(f"{where}: inter-state row carries CGST/SGST — only IGST is allowed.")
        if want == "INTRA":
            if _f(row, "iamt"):
                errors.append(f"{where}: intra-state row carries IGST — only CGST+SGST is allowed.")
            if abs(_f(row, "camt") - _f(row, "samt")) > 0.01:
                errors.append(f"{where}: CGST {row.get('camt')} and SGST {row.get('samt')} must be equal.")
        typ = str(row.get("typ", ""))
        if typ not in ("OE", "E"):
            errors.append(f"{where}: type {typ!r} must be OE or E.")
        if typ == "E":
            add(gstin_problem(row.get("etin", ""), f"{where}: e-commerce GSTIN", checksum=False))
        for k in ("txval", "iamt", "camt", "samt", "csamt"):
            if _has_more_than_2_decimals(row.get(k, 0)):
                errors.append(f"{where}: {k} {row.get(k)} has more than 2 decimals.")

    # ---- Table 5 (B2CL)
    for grp in payload.get("b2cl", []):
        pos = str(grp.get("pos"))
        where = f"B2CL POS {pos}-{pos_name(pos)}"
        if pos_check(where, pos) and pos == seller_state:
            errors.append(f"{where}: B2CL is only for INTER-state invoices — "
                          "an invoice to your own state belongs in B2CS.")
        for inv in grp.get("inv", []):
            inum = str(inv.get("inum", ""))
            if not DOC_NUMBER_RE.fullmatch(inum):
                errors.append(f"{where}: invoice number {inum!r} must be 1-16 characters "
                              "of letters, digits, '/' or '-'.")
            if _f(inv, "val") <= 100000:
                errors.append(f"{where} invoice {inum}: value {inv.get('val')} is not above "
                              "Rs 1,00,000 — it belongs in B2CS, not B2CL.")
            if not re.fullmatch(r"\d{2}-\d{2}-\d{4}", str(inv.get("idt", ""))):
                errors.append(f"{where} invoice {inum}: date {inv.get('idt')!r} must be DD-MM-YYYY.")
            if inv.get("etin"):
                add(gstin_problem(inv["etin"], f"{where} invoice {inum}: e-commerce GSTIN", checksum=False))
            for itm in inv.get("itms", []):
                det = itm.get("itm_det", {})
                if not rate_ok(det.get("rt")):
                    errors.append(f"{where} invoice {inum}: rate {det.get('rt')}% is not a valid GST rate.")

    # ---- Table 12 (HSN)
    for i, row in enumerate(payload.get("hsn", {}).get("hsn_b2c", [])):
        where = f"HSN row {i + 1} ({row.get('hsn_sc')})"
        if str(row.get("uqc", "")).upper() not in VALID_UQC:
            errors.append(f"{where}: unit {row.get('uqc')!r} is not a portal UQC code "
                          "(e.g. PCS, NOS, KGS, BOX, SET, PRS).")
        if not rate_ok(row.get("rt")):
            errors.append(f"{where}: rate {row.get('rt')}% is not a valid GST rate.")
        if len(str(row.get("desc", ""))) > 30:
            warnings.append(f"{where}: description longer than 30 characters — the portal may truncate it.")

    # ---- Table 13 (documents issued)
    for det in payload.get("doc_issue", {}).get("doc_det", []):
        seen_from: set = set()
        for row in det.get("docs", []):
            where = f"Table 13 series {row.get('from')}..{row.get('to')}"
            for k in ("from", "to"):
                if not DOC_NUMBER_RE.fullmatch(str(row.get(k, ""))):
                    errors.append(f"{where}: {k!r} serial must be 1-16 characters of "
                                  "letters, digits, '/' or '-'.")
            if row.get("from") in seen_from:
                errors.append(f"{where}: duplicate series start — the portal rejects repeats.")
            seen_from.add(row.get("from"))
            tot, can = int(row.get("totnum", 0)), int(row.get("cancel", 0))
            if tot < 0 or can < 0 or can > tot:
                errors.append(f"{where}: cancelled ({can}) cannot exceed total ({tot}) "
                              "and neither can be negative.")
    return errors, warnings


def check_gstr3b_pos(payload: Dict[str, Any]) -> List[str]:
    """Table 3.2 state rows: valid, non-retired, and never the seller's own state."""
    errors: List[str] = []
    seller_state = str(payload.get("gstin", ""))[:2]
    for r in payload.get("inter_sup", {}).get("unreg_details", []):
        p = str(r.get("pos"))
        if p in RETIRED_POS:
            errors.append(f"GSTR-3B Table 3.2: POS {p} is retired — must be {RETIRED_POS[p]}.")
        elif p not in VALID_POS:
            errors.append(f"GSTR-3B Table 3.2: POS {p!r} is not a valid state code.")
        elif p == seller_state:
            errors.append(f"GSTR-3B Table 3.2: your own state {p} cannot appear "
                          "(3.2 is inter-state supplies only).")
    return errors


def explain_portal_error(code: str, msg: str) -> str:
    hint = PORTAL_ERROR_HINTS.get(code)
    if hint:
        return hint
    m = (msg or "").lower()
    if "pos" in m or "place of supply" in m:
        return "Place-of-supply problem — check the state code of this row."
    if "rate" in m:
        return "Rate problem — the portal only accepts standard GST rates."
    if "hsn" in m or "uqc" in m:
        return "HSN summary problem — check the HSN code, unit and tax amounts."
    if "invoice" in m or "doc" in m:
        return "Document-number problem — max 16 characters, letters/digits/'/'/'-'."
    return "Not a known code — share this error report with the developer."
