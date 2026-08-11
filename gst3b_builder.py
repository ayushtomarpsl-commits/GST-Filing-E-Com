"""GSTR-3B builder.

GSTR-3B is filed AFTER GSTR-1 and must agree with it:
  Table 3.1(a)  = net outward taxable supplies  -> derived from B2CS + B2CL
  Table 3.2     = inter-state slice of 3.1(a) to unregistered persons, state-wise
  Table 4       = Input Tax Credit             -> derived from the GSTR-2B JSON
                  (auto-drafted ITC statement downloaded from the GST portal)
  Tables 3.1(b)-(e), 3.1.1, 5, 5.1 are normally zero for a goods seller on
  marketplaces; they are shown explicitly so nothing is filled blindly.

Design rule (project-wide): NEVER drop data silently. Every 2B section this
module does not compute (amendments, imports, ISD...) raises a visible error
message instead of being skipped.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

TAX_HEADS = ("iamt", "camt", "samt", "csamt")


def _r2(x: Any) -> float:
    return round(float(x or 0), 2)


def _num(v: float) -> Any:
    rv = round(v, 2)
    return int(rv) if rv == int(rv) else rv


def _zero_heads() -> Dict[str, float]:
    return {"txval": 0.0, "iamt": 0.0, "camt": 0.0, "samt": 0.0, "csamt": 0.0}


# ---------------------------------------------------------------------------
# GSTR-2B (auto-drafted ITC statement) JSON parsing
# ---------------------------------------------------------------------------

@dataclass
class Gstr2bResult:
    gstin: str = ""
    rtnprd: str = ""                 # MMYYYY
    gen_date: str = ""
    itc_oth: Dict[str, float] = field(default_factory=_zero_heads)      # 4(A)(5)
    itc_isrc: Dict[str, float] = field(default_factory=_zero_heads)     # 4(A)(3)
    rcm: Dict[str, float] = field(default_factory=_zero_heads)          # 3.1(d)
    itc_unavail: Dict[str, float] = field(default_factory=_zero_heads)  # 2B "ITC not available"
    doc_count: int = 0
    supplier_count: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def _item_amounts(item: Dict[str, Any]) -> Tuple[float, float, float, float, float]:
    """2B document items use igst/cgst/sgst/cess key names."""
    return (
        _r2(item.get("txval")),
        _r2(item.get("igst", item.get("iamt"))),
        _r2(item.get("cgst", item.get("camt"))),
        _r2(item.get("sgst", item.get("samt"))),
        _r2(item.get("cess", item.get("csamt"))),
    )


def _add(bucket: Dict[str, float], sign: int,
         txval: float, ig: float, cg: float, sg: float, cs: float) -> None:
    bucket["txval"] += sign * txval
    bucket["iamt"] += sign * ig
    bucket["camt"] += sign * cg
    bucket["samt"] += sign * sg
    bucket["csamt"] += sign * cs


# docdata sections this parser computes vs. sections that need manual action
_KNOWN_2B_SECTIONS = {"b2b", "cdnr"}
_MANUAL_2B_SECTIONS = {
    "b2ba": "amended B2B invoices (B2BA)",
    "cdnra": "amended credit/debit notes (CDNRA)",
    "impg": "import of goods (IMPG) — belongs in Table 4(A)(1)",
    "impgsez": "import of goods from SEZ (IMPGSEZ) — belongs in Table 4(A)(1)",
    "isd": "Input Service Distributor documents (ISD) — belongs in Table 4(A)(4)",
    "isda": "amended ISD documents (ISDA)",
    "eco": "documents reported by e-commerce operators u/s 9(5) (ECO)",
}


def parse_gstr2b_json(data: bytes) -> Gstr2bResult:
    """Parse the GSTR-2B JSON downloaded from the GST portal.

    Portal path: Returns Dashboard -> select month -> GSTR-2B -> Download ->
    'GENERATE JSON FILE TO DOWNLOAD'.

    ITC is computed from the document data (B2B invoices minus credit notes),
    honouring the per-document ITC-availability and reverse-charge flags.
    """
    res = Gstr2bResult()
    try:
        root = json.loads(data.decode("utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"GSTR-2B JSON could not be parsed: {exc}") from exc
    if not isinstance(root, dict):
        raise ValueError("GSTR-2B JSON has an unexpected structure (root is not an object).")

    d = root.get("data") if isinstance(root.get("data"), dict) else root
    res.gstin = str(d.get("gstin", ""))
    res.rtnprd = str(d.get("rtnprd", "") or d.get("ret_period", ""))
    res.gen_date = str(d.get("gendt", ""))

    docdata = d.get("docdata")
    if not isinstance(docdata, dict):
        raise ValueError(
            "GSTR-2B JSON contains no 'docdata' section — download the full "
            "GSTR-2B JSON from the portal (not the summary/Excel)."
        )

    missing_flag = 0
    suppliers: set = set()

    def consume(doc: Dict[str, Any], base_sign: int, doc_label: str) -> None:
        nonlocal missing_flag
        res.doc_count += 1
        rev = str(doc.get("rev", "N")).upper()
        itcavl = doc.get("itcavl")
        if itcavl is None:
            missing_flag += 1
            itcavl = "Y"
        itcavl = str(itcavl).upper()
        items = doc.get("items") or []
        if not items:
            res.errors.append(
                f"GSTR-2B document {doc_label} has no line items — cannot compute "
                "its ITC. Verify Table 4 on the portal."
            )
            return
        for item in items:
            txval, ig, cg, sg, cs = _item_amounts(item)
            if rev == "Y":
                _add(res.rcm, base_sign, txval, ig, cg, sg, cs)
                _add(res.itc_isrc, base_sign, txval, ig, cg, sg, cs)
            elif itcavl == "N":
                _add(res.itc_unavail, base_sign, txval, ig, cg, sg, cs)
            else:
                _add(res.itc_oth, base_sign, txval, ig, cg, sg, cs)

    for sup in docdata.get("b2b", []) or []:
        suppliers.add(str(sup.get("ctin", "")))
        for inv in sup.get("inv", []) or []:
            consume(inv, +1, f"invoice {inv.get('inum', '?')} of {sup.get('ctin', '?')}")

    for sup in docdata.get("cdnr", []) or []:
        suppliers.add(str(sup.get("ctin", "")))
        for nt in sup.get("nt", []) or []:
            typ = str(nt.get("typ", nt.get("ntty", ""))).upper()
            if typ == "C":
                sign = -1
            elif typ == "D":
                sign = +1
            else:
                res.errors.append(
                    f"GSTR-2B note {nt.get('ntnum', '?')} of {sup.get('ctin', '?')} has "
                    f"unknown type {typ!r} (expected C or D) — NOT included. "
                    "Verify Table 4 on the portal."
                )
                continue
            consume(nt, sign, f"note {nt.get('ntnum', '?')} of {sup.get('ctin', '?')}")

    # Sections we do not compute must be loud, never silent.
    for key, value in docdata.items():
        if key in _KNOWN_2B_SECTIONS or not value:
            continue
        label = _MANUAL_2B_SECTIONS.get(key, f"unrecognized section {key!r}")
        res.errors.append(
            f"GSTR-2B contains {label}. This tool does not compute that section — "
            "fill/verify the affected Table 4 field manually on the portal."
        )

    res.supplier_count = len(suppliers)
    if missing_flag:
        res.warnings.append(
            f"{missing_flag} GSTR-2B document(s) had no ITC-availability flag — "
            "treated as available. Verify Table 4 against the portal's auto-draft."
        )
    if res.doc_count == 0:
        res.warnings.append(
            "GSTR-2B has zero B2B documents — no ITC this month "
            "(platform commission invoices usually appear here; check the period)."
        )
    for head, amount in res.itc_oth.items():
        if amount < -0.005:
            res.warnings.append(
                f"Net 'All other ITC' {head} is NEGATIVE ({_r2(amount)}) — credit "
                "notes exceeded invoices this month. The portal accepts negative "
                "Table 4 values; verify before filing."
            )
            break
    for k in res.itc_oth:
        res.itc_oth[k] = _r2(res.itc_oth[k])
        res.itc_isrc[k] = _r2(res.itc_isrc[k])
        res.rcm[k] = _r2(res.rcm[k])
        res.itc_unavail[k] = _r2(res.itc_unavail[k])
    return res


# ---------------------------------------------------------------------------
# GSTR-3B computation from GSTR-1 data
# ---------------------------------------------------------------------------

def compute_31a_and_32(b2cs_rows: List[Dict[str, Any]],
                       b2cl_invoices: List[Any]) -> Tuple[Dict[str, float],
                                                          List[Dict[str, float]]]:
    """Derive Table 3.1(a) totals and Table 3.2 state-wise inter-state rows.

    b2cs_rows: the portal-JSON b2cs rows (pos/sply_ty/rt/txval/iamt/camt/samt).
    b2cl_invoices: B2clInvoice objects (inter-state > 1 lakh, IGST only).
    All B2C supplies are to unregistered persons, so every inter-state row
    belongs in Table 3.2 'unregistered' bucket.
    """
    t31a = _zero_heads()
    inter: Dict[str, Dict[str, float]] = {}

    for r in b2cs_rows:
        txval = _r2(r.get("txval"))
        ig, cg, sg = _r2(r.get("iamt")), _r2(r.get("camt")), _r2(r.get("samt"))
        cs = _r2(r.get("csamt"))
        t31a["txval"] += txval
        t31a["iamt"] += ig
        t31a["camt"] += cg
        t31a["samt"] += sg
        t31a["csamt"] += cs
        if str(r.get("sply_ty", "")).upper() == "INTER":
            slot = inter.setdefault(str(r.get("pos")), {"txval": 0.0, "iamt": 0.0})
            slot["txval"] += txval
            slot["iamt"] += ig

    for inv in b2cl_invoices:
        t31a["txval"] += _r2(inv.txval)
        t31a["iamt"] += _r2(inv.iamt)
        t31a["csamt"] += _r2(inv.csamt)
        slot = inter.setdefault(str(inv.pos), {"txval": 0.0, "iamt": 0.0})
        slot["txval"] += _r2(inv.txval)
        slot["iamt"] += _r2(inv.iamt)

    for k in t31a:
        t31a[k] = _r2(t31a[k])
    inter_rows = [{"pos": pos, "txval": _r2(v["txval"]), "iamt": _r2(v["iamt"])}
                  for pos, v in sorted(inter.items())]
    return t31a, inter_rows


def build_gstr3b_payload(gstin: str, period: str,
                         t31a: Dict[str, float],
                         inter_rows: List[Dict[str, float]],
                         itc: Optional[Gstr2bResult]) -> Dict[str, Any]:
    """Assemble the GSTR-3B offline-utility JSON.

    When no GSTR-2B was provided, the itc_elg section is OMITTED entirely so
    that uploading this JSON can never overwrite the portal's auto-drafted
    Table 4 with zeros (which would make the seller overpay tax).
    """
    rcm = itc.rcm if itc else _zero_heads()
    payload: Dict[str, Any] = {
        "gstin": gstin,
        "ret_period": period,
        "sup_details": {
            "osup_det": {
                "txval": _num(t31a["txval"]), "iamt": _num(t31a["iamt"]),
                "camt": _num(t31a["camt"]), "samt": _num(t31a["samt"]),
                "csamt": _num(t31a["csamt"]),
            },
            "osup_zero": {"txval": 0, "iamt": 0, "csamt": 0},
            "osup_nil_exmp": {"txval": 0},
            "isup_rev": {
                "txval": _num(rcm["txval"]), "iamt": _num(rcm["iamt"]),
                "camt": _num(rcm["camt"]), "samt": _num(rcm["samt"]),
                "csamt": _num(rcm["csamt"]),
            },
            "osup_nongst": {"txval": 0},
        },
    }
    if inter_rows:
        payload["inter_sup"] = {
            "unreg_details": [
                {"pos": r["pos"], "txval": _num(r["txval"]), "iamt": _num(r["iamt"])}
                for r in inter_rows
            ]
        }
    if itc is not None:
        avl = [
            {"ty": "IMPG", "iamt": 0, "csamt": 0},
            {"ty": "IMPS", "iamt": 0, "csamt": 0},
            {"ty": "ISRC", "iamt": _num(itc.itc_isrc["iamt"]),
             "camt": _num(itc.itc_isrc["camt"]), "samt": _num(itc.itc_isrc["samt"]),
             "csamt": _num(itc.itc_isrc["csamt"])},
            {"ty": "ISD", "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            {"ty": "OTH", "iamt": _num(itc.itc_oth["iamt"]),
             "camt": _num(itc.itc_oth["camt"]), "samt": _num(itc.itc_oth["samt"]),
             "csamt": _num(itc.itc_oth["csamt"])},
        ]
        net = {h: _r2(itc.itc_isrc[h] + itc.itc_oth[h]) for h in TAX_HEADS}
        payload["itc_elg"] = {
            "itc_avl": avl,
            "itc_rev": [
                {"ty": "RUL", "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
                {"ty": "OTH", "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
            ],
            "itc_net": {h: _num(net[h]) for h in TAX_HEADS},
            "itc_inelg": [
                {"ty": "RUL", "iamt": 0, "camt": 0, "samt": 0, "csamt": 0},
                {"ty": "OTH", "iamt": _num(itc.itc_unavail["iamt"]),
                 "camt": _num(itc.itc_unavail["camt"]),
                 "samt": _num(itc.itc_unavail["samt"]),
                 "csamt": _num(itc.itc_unavail["csamt"])},
            ],
        }
    # Table 5 — inward exempt/nil/non-GST: zero for a typical marketplace
    # goods seller (shown on the card so it is a conscious zero, not a blind one)
    payload["inward_sup"] = {
        "isup_details": [
            {"ty": "GST", "inter": 0, "intra": 0},
            {"ty": "NONGST", "inter": 0, "intra": 0},
        ]
    }
    return payload


def validate_gstr3b(payload: Dict[str, Any],
                    gstr1_taxable: float, gstr1_tax: float) -> Tuple[List[str], List[str]]:
    """Internal consistency + GSTR-1 agreement checks. Returns (errors, warnings)."""
    errors: List[str] = []
    warnings: List[str] = []

    det = payload["sup_details"]["osup_det"]
    out_tax = _r2(det["iamt"]) + _r2(det["camt"]) + _r2(det["samt"]) + _r2(det["csamt"])
    if abs(_r2(det["txval"]) - _r2(gstr1_taxable)) > 0.02:
        errors.append(
            f"GSTR-3B 3.1(a) taxable {det['txval']} does not equal the GSTR-1 "
            f"total {gstr1_taxable} — the portal cross-checks these."
        )
    if abs(out_tax - _r2(gstr1_tax)) > 0.05:
        errors.append(
            f"GSTR-3B 3.1(a) tax {out_tax} does not equal the GSTR-1 tax {gstr1_tax}."
        )
    if _r2(det["txval"]) < 0:
        warnings.append(
            "GSTR-3B 3.1(a) is NEGATIVE (returns exceeded sales this month). "
            "The portal permits negative values here since Jan-2025, but verify "
            "with your tax advisor."
        )

    inter = payload.get("inter_sup", {}).get("unreg_details", [])
    inter_txval = sum(_r2(r["txval"]) for r in inter)
    inter_igst = sum(_r2(r["iamt"]) for r in inter)
    if inter and (inter_txval - _r2(det["txval"]) > 0.02
                  or inter_igst - _r2(det["iamt"]) > 0.02):
        errors.append(
            "Table 3.2 totals exceed Table 3.1(a) — impossible; investigate."
        )
    for r in inter:
        if _r2(r["txval"]) < 0:
            warnings.append(
                f"Table 3.2 state {r['pos']} is negative ({r['txval']}) — the "
                "portal may reject negative state rows; usually adjusted in the "
                "next month. Consult your advisor."
            )

    itc = payload.get("itc_elg")
    if itc:
        for h in TAX_HEADS:
            avl = sum(_r2(row.get(h, 0)) for row in itc["itc_avl"])
            rev = sum(_r2(row.get(h, 0)) for row in itc["itc_rev"])
            if abs(_r2(avl - rev) - _r2(itc["itc_net"][h])) > 0.02:
                errors.append(
                    f"Table 4 net ITC ({h}) must equal available minus reversed."
                )
    return errors, warnings
