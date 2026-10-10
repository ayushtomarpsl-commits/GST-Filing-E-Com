"""Portal-rule tests: every check here mirrors a real GST portal upload rejection.

Run:  python test_portal_rules.py
"""
from __future__ import annotations

import json
import sys

from gst_builder import review_portal_error_report, validate_full_payload
from gst_parsers import normalize_state_to_code
from gst_portal_rules import check_gstr1_rules, check_gstr3b_pos, gstin_checksum_ok

FAILURES: list[str] = []
SELLER = "09BXDPT0789G1ZZ"


def check(label: str, got, want) -> None:
    ok = got == want
    print(f"  {'OK ' if ok else 'BAD'} {label} = {got!r}" + ("" if ok else f" (want {want!r})"))
    if not ok:
        FAILURES.append(label)


def has(msgs: list[str], needle: str) -> bool:
    return any(needle in m for m in msgs)


def b2cs(pos: str, sply: str, **kw) -> dict:
    row = {"sply_ty": sply, "pos": pos, "typ": "OE", "txval": 100, "rt": 5,
           "iamt": 5 if sply == "INTER" else 0,
           "camt": 2.5 if sply == "INTRA" else 0,
           "samt": 2.5 if sply == "INTRA" else 0, "csamt": 0}
    row.update(kw)
    return row


def payload(**sections) -> dict:
    return {"gstin": SELLER, "fp": "092026", "version": "GST3.2.4", "hash": "hash", **sections}


def main() -> int:
    print("== 1. State names / codes -> portal POS ==")
    for raw, want in [
        ("MADHYA PRADESH", "23"), ("DAMAN & DIU", "26"), ("25-Daman & Diu", "26"),
        ("25", "26"), ("28-Andhra Pradesh", "37"), ("28", "37"), ("09", "09"),
        (9, "09"), (9.0, "09"), ("09-Uttar Pradesh", "09"), ("IN-TS", "36"),
        ("IN-OD", "21"), ("IN-DH", "26"), ("IN-UK", "05"),
        ("The Dadra and Nagar Haveli and Daman and Diu", "26"),
        ("Dadra & Nagar Haveli & Daman & Diu", "26"), ("NCT OF DELHI", "07"),
        ("Telengana", "36"), ("Other Territory", "97"), ("Narnia", None),
        ("99-Narnia", None),
    ]:
        check(f"{raw!r}", normalize_state_to_code(raw), want)

    print("\n== 2. GSTIN check digit ==")
    check("seller GSTIN valid", gstin_checksum_ok(SELLER), True)
    check("one-char typo caught", gstin_checksum_ok("09BXDPT0789G1ZA"), False)

    print("\n== 3. GSTR-1 rules ==")
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("26", "INTER"), b2cs("09", "INTRA")]))
    check("clean payload has no errors", errs, [])
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("25", "INTER")]))
    check("POS 25 rejected (RET191240)", has(errs, "RET191240"), True)
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("99", "INTER")]))
    check("unknown POS rejected", has(errs, "not a valid 2-digit"), True)
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("10", "INTRA")]))
    check("INTRA for another state rejected", has(errs, "must be INTER"), True)
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("10", "INTER", camt=1, samt=1)]))
    check("CGST on inter-state rejected", has(errs, "only IGST"), True)
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("10", "INTER", rt=13)]))
    check("13% rate rejected", has(errs, "rate 13%"), True)
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("10", "INTER", rt=40, iamt=40)]))
    check("40% GST 2.0 slab accepted", errs, [])
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("10", "INTER", txval=100.123)]))
    check(">2 decimals rejected", has(errs, "more than 2 decimals"), True)
    errs, _ = check_gstr1_rules(payload(hsn={"hsn_b2c": [
        {"num": 1, "hsn_sc": "1701", "uqc": "PIECES", "qty": 1, "txval": 100,
         "iamt": 5, "camt": 0, "samt": 0, "csamt": 0, "rt": 5}]}))
    check("bad UQC rejected", has(errs, "not a portal UQC"), True)
    errs, _ = check_gstr1_rules(payload(doc_issue={"doc_det": [{"doc_num": 1, "docs": [
        {"num": 1, "from": "INV#2026/000000001", "to": "INV#2026/000000009",
         "totnum": 9, "cancel": 0, "net_issue": 9}]}]}))
    check("doc number >16 chars / bad chars rejected", has(errs, "1-16 characters"), True)
    errs, _ = check_gstr1_rules(payload(b2cl=[{"pos": "09", "inv": [
        {"inum": "A1", "idt": "01-09-2026", "val": 200000,
         "itms": [{"num": 1, "itm_det": {"rt": 5, "txval": 190476.19,
                                          "iamt": 9523.81, "csamt": 0}}]}]}]))
    check("B2CL to own state rejected", has(errs, "only for INTER-state"), True)
    errs, _ = check_gstr1_rules(payload(b2cl=[{"pos": "10", "inv": [
        {"inum": "A1", "idt": "01-09-2026", "val": 50000,
         "itms": [{"num": 1, "itm_det": {"rt": 5, "txval": 47619.05,
                                          "iamt": 2380.95, "csamt": 0}}]}]}]))
    check("B2CL under Rs 1 lakh rejected", has(errs, "belongs in B2CS"), True)
    errs, warns = check_gstr1_rules(payload(b2cl=[{"pos": "10", "inv": [
        {"inum": "A1", "idt": "01-09-2026", "val": 200000, "etin": "09AARCM9332R1CM",
         "itms": [{"num": 1, "itm_det": {"rt": 5, "txval": 190476.19,
                                          "iamt": 9523.81, "csamt": 0}}]}]}]))
    check("Meesho TCS GSTIN (14th char C) accepted", (errs, warns), ([], []))
    errs, _ = check_gstr1_rules(payload(b2cs=[b2cs("10", "INTER", typ="E", etin="MEESHO")]))
    check("garbage e-com GSTIN rejected", has(errs, "not a valid GSTIN format"), True)
    errs, _ = validate_full_payload(payload(b2cs=[b2cs("25", "INTER")]))
    check("validate_full_payload runs portal rules", has(errs, "RET191240"), True)

    print("\n== 4. GSTR-3B Table 3.2 ==")
    p3b = {"gstin": SELLER, "inter_sup": {"unreg_details": [
        {"pos": "25", "txval": 1, "iamt": 0.05}, {"pos": "09", "txval": 1, "iamt": 0.05}]}}
    e = check_gstr3b_pos(p3b)
    check("3.2 retired POS + own state both flagged", (has(e, "retired"), has(e, "own state")),
          (True, True))

    print("\n== 5. Portal error report (real Sep 2026 RET191240) ==")
    report = json.dumps({"form_typ": "R1", "status_cd": "PE", "fp": "092026",
                         "gstin": SELLER, "error_report": {"b2cs": [
                             {"samt": 0, "csamt": 0, "rt": 5, "error_msg": "Invalid POS selected",
                              "pos": "25", "txval": 183.81, "typ": "OE", "camt": 0,
                              "iamt": 9.19, "sply_ty": "INTER", "error_cd": "RET191240"}]}}).encode()
    g = review_portal_error_report(report, payload(b2cs=[b2cs("26", "INTER")]))
    check("fixed row reported as FIXED", (g[0].level, "FIXED" in g[0].detail), ("info", True))
    g = review_portal_error_report(report, payload(b2cs=[b2cs("25", "INTER")]))
    check("unfixed row reported as error", (g[0].level, "STILL PRESENT" in g[0].detail),
          ("error", True))
    check("hint explains retired POS", "26 / 37" in g[0].detail, True)

    print()
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("ALL PORTAL RULE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
