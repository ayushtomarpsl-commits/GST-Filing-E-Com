"""GSTR-3B feature tests.

Runs the real sample folder ('tax report') end-to-end and verifies the
GSTR-3B payload agrees with GSTR-1, then exercises the GSTR-2B ITC path with
synthetic files covering: credit notes, ITC-unavailable docs, reverse charge,
sections needing manual action, wrong GSTIN, wrong month, own-output JSONs
and invalid JSON — all of which must be VISIBLE, never silent.

Run:  python test_gstr3b.py [sample_folder]   (default: ./tax report)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from gst_autodetect import scan_files, scan_folder
from gst_builder import build

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  X  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  OK {label} = {want!r}")


def approx(label: str, got: float, want: float, tol: float = 0.02) -> None:
    if abs(got - want) > tol:
        FAILURES.append(f"{label}: got {got}, want {want} (+/-{tol})")
        print(f"  X  {label}: got {got}, want {want}")
    else:
        print(f"  OK {label} = {want}")


def make_2b(gstin: str, period: str, extra_docdata: dict | None = None) -> bytes:
    """Synthetic GSTR-2B JSON in the portal download shape."""
    docdata = {
        "b2b": [
            {"ctin": "07AAACA1111A1Z5", "trdnm": "Marketplace Fees Pvt Ltd",
             "supprd": period, "inv": [
                 {"inum": "FEE-001", "typ": "R", "dt": "15-06-2026", "val": 1180,
                  "pos": "09", "rev": "N", "itcavl": "Y", "items": [
                      {"num": 1, "rt": 18, "txval": 1000,
                       "igst": 0, "cgst": 90, "sgst": 90, "cess": 0}]},
                 {"inum": "FEE-002", "typ": "R", "dt": "20-06-2026", "val": 590,
                  "pos": "29", "rev": "N", "itcavl": "N", "rsn": "P", "items": [
                      {"num": 1, "rt": 10, "txval": 500,
                       "igst": 50, "cgst": 0, "sgst": 0, "cess": 0}]},
                 {"inum": "GTA-01", "typ": "R", "dt": "22-06-2026", "val": 118,
                  "pos": "09", "rev": "Y", "itcavl": "Y", "items": [
                      {"num": 1, "rt": 18, "txval": 100,
                       "igst": 18, "cgst": 0, "sgst": 0, "cess": 0}]},
             ]},
        ],
        "cdnr": [
            {"ctin": "27BBBCB2222B1Z6", "trdnm": "Other Fees Ltd",
             "nt": [
                 {"ntnum": "CN-9", "typ": "C", "dt": "25-06-2026", "val": 118,
                  "rev": "N", "itcavl": "Y", "items": [
                      {"num": 1, "rt": 18, "txval": 100,
                       "igst": 0, "cgst": 9, "sgst": 9, "cess": 0}]},
             ]},
        ],
    }
    if extra_docdata:
        docdata.update(extra_docdata)
    return json.dumps({
        "chksum": "test", "data": {
            "gstin": gstin, "rtnprd": period, "version": "1.0",
            "gendt": "14-07-2026", "docdata": docdata,
        }
    }).encode()


def main(folder: Path) -> int:
    # ---------------- 1. Build without GSTR-2B ----------------
    print("== 1. Marketplace folder only (no GSTR-2B) ==")
    detected = scan_folder(folder)
    result = build(detected, "")
    period, gstin = result.period, result.seller_gstin
    print(f"   period={period} gstin={gstin}")

    p3b = result.payload_3b
    check("payload_3b produced", p3b is not None, True)
    check("3B ret_period", p3b["ret_period"], period)
    check("3B gstin", p3b["gstin"], gstin)
    check("no itc_elg without 2B (portal auto-draft protected)",
          "itc_elg" not in p3b, True)

    b2cs = result.payload_full["b2cs"]
    b2cl_groups = result.payload_full.get("b2cl", [])
    b2cl_items = [itm["itm_det"] for g in b2cl_groups
                  for inv in g.get("inv", []) for itm in inv["itms"]]
    want_txval = round(sum(float(r["txval"]) for r in b2cs)
                       + sum(float(i["txval"]) for i in b2cl_items), 2)
    want_tax = round(sum(float(r["iamt"]) + float(r["camt"]) + float(r["samt"])
                         for r in b2cs)
                     + sum(float(i["iamt"]) + float(i.get("csamt", 0))
                           for i in b2cl_items), 2)
    det = p3b["sup_details"]["osup_det"]
    got_tax = round(float(det["iamt"]) + float(det["camt"])
                    + float(det["samt"]) + float(det["csamt"]), 2)
    approx("3.1(a) taxable == GSTR-1 (B2CS+B2CL)", float(det["txval"]), want_txval)
    approx("3.1(a) tax == GSTR-1 tax", got_tax, want_tax)

    inter = p3b.get("inter_sup", {}).get("unreg_details", [])
    seller_state = gstin[:2]
    check("3.2 excludes home state", all(r["pos"] != seller_state for r in inter), True)
    inter_want = round(sum(float(r["txval"]) for r in b2cs
                           if r["sply_ty"] == "INTER")
                       + sum(float(i["txval"]) for i in b2cl_items), 2)
    approx("3.2 taxable == inter-state slice",
           round(sum(float(r["txval"]) for r in inter), 2), inter_want)
    check("Table 5 zeros present", p3b["inward_sup"]["isup_details"][0],
          {"ty": "GST", "inter": 0, "intra": 0})
    check("3B card exists", any(c.id == "gstr3b" for c in result.cards), True)
    check("2B missing tile shown",
          any(p.name == "GSTR-2B (ITC)" and p.status == "missing"
              for p in result.platforms), True)
    check("2B download guidance shown",
          any("GSTR-2B" in g.title for g in result.guidance
              if g.level == "info"), True)

    # ---------------- 2. With a good GSTR-2B ----------------
    print("\n== 2. With GSTR-2B JSON (ITC computed) ==")
    blobs = [("GSTR2B_test.json", make_2b(gstin, period))]
    result2 = build(scan_folder(folder) + scan_files(blobs), "")
    p3b2 = result2.payload_3b
    check("itc_elg present", "itc_elg" in p3b2, True)
    avl = {row["ty"]: row for row in p3b2["itc_elg"]["itc_avl"]}
    # OTH = invoice(1000, 90/90) - credit note(100, 9/9); unavailable excluded
    check("OTH cgst", avl["OTH"]["camt"], 81)
    check("OTH sgst", avl["OTH"]["samt"], 81)
    check("OTH igst", avl["OTH"]["iamt"], 0)
    check("ISRC igst (reverse charge)", avl["ISRC"]["iamt"], 18)
    check("net ITC igst", p3b2["itc_elg"]["itc_net"]["iamt"], 18)
    check("net ITC cgst", p3b2["itc_elg"]["itc_net"]["camt"], 81)
    inelg = {row["ty"]: row for row in p3b2["itc_elg"]["itc_inelg"]}
    check("unavailable ITC surfaced in 4(D)(2)", inelg["OTH"]["iamt"], 50)
    rcm = p3b2["sup_details"]["isup_rev"]
    check("3.1(d) RCM taxable", rcm["txval"], 100)
    check("3.1(d) RCM igst", rcm["iamt"], 18)
    check("2B tile ok", any(p.name == "GSTR-2B (ITC)" and p.status == "ok"
                            for p in result2.platforms), True)
    check("2B doc count on tile",
          next(p.records for p in result2.platforms if p.name == "GSTR-2B (ITC)"), 4)
    check("no 3B validation errors",
          not any("GSTR-3B" in g.title and g.level == "error"
                  for g in result2.guidance), True)

    # ---------------- 3. 2B with sections needing manual action ----------------
    print("\n== 3. GSTR-2B with IMPG/amendments (must be loud) ==")
    blobs = [("GSTR2B_test.json",
              make_2b(gstin, period,
                      {"impg": [{"boe": [{"num": 1}]}], "b2ba": [{"ctin": "x"}]}))]
    result3 = build(scan_folder(folder) + scan_files(blobs), "")
    manual = [g for g in result3.guidance
              if g.level == "error" and "manual attention" in g.title]
    check("IMPG flagged", any("IMPG" in g.detail for g in manual), True)
    check("B2BA amendments flagged", any("B2BA" in g.detail for g in manual), True)

    # ---------------- 4. Wrong GSTIN / wrong month / junk ----------------
    print("\n== 4. Wrong GSTIN, wrong month, own JSONs, junk ==")
    blobs = [("GSTR2B_other.json", make_2b("29ZZZZZ9999Z9Z9", period))]
    result4 = build(scan_folder(folder) + scan_files(blobs), "")
    check("different-GSTIN 2B rejected loudly",
          any("different GSTIN" in g.title for g in result4.guidance
              if g.level == "error"), True)
    check("no ITC taken from foreign 2B", "itc_elg" not in result4.payload_3b, True)

    # (a) valid-format wrong month -> excluded up front by period detection
    wrong_month = "032026" if period != "032026" else "052026"
    blobs = [("GSTR2B_old.json", make_2b(gstin, wrong_month))]
    result5 = build(scan_folder(folder) + scan_files(blobs), "")
    check("wrong-month 2B excluded", "itc_elg" not in result5.payload_3b, True)
    check("wrong-month 2B visible in scan",
          any("GSTR2B_old" in e["name"] and e["status"] == "error"
              for e in result5.scan_entries), True)

    # (b) malformed period string -> caught by the builder-level cross-check
    blobs = [("GSTR2B_weird.json", make_2b(gstin, "011999"))]
    result5b = build(scan_folder(folder) + scan_files(blobs), "")
    check("malformed-period 2B rejected", "itc_elg" not in result5b.payload_3b, True)
    check("malformed-period 2B error shown",
          any("not for this filing period" in g.title for g in result5b.guidance
              if g.level == "error"), True)

    own1 = json.dumps({"gstin": gstin, "fp": period, "version": "GST3.2.4",
                       "hash": "hash", "b2cs": []}).encode()
    own3b = json.dumps({"gstin": gstin, "ret_period": period,
                        "sup_details": {}}).encode()
    junk = b"{not json"
    result6 = build(scan_folder(folder) + scan_files(
        [("gstr1_full_old.json", own1), ("gstr3b_old.json", own3b),
         ("weird.json", junk)]), "")
    entries = {e["name"]: e for e in result6.scan_entries}
    check("own GSTR-1 JSON ignored", entries["gstr1_full_old.json"]["status"], "ignored")
    check("own GSTR-3B JSON ignored", entries["gstr3b_old.json"]["status"], "ignored")
    check("junk JSON flagged unknown", entries["weird.json"]["status"], "warning")

    # ---------------- 5. HSN tax reconciliation ----------------
    # Real Amazon defect (July 2026): gross tax counted on zero-value promo
    # shipments while the taxable value was correctly netted.
    print("\n== 5. HSN tax reconciliation (portal rule: tax == taxable x rate) ==")
    from gst_ext_parsers import HsnRow, reconcile_hsn_tax

    rows, msgs = reconcile_hsn_tax(
        [HsnRow("17011310", "PCS", 3, 170.48, 25.56, 0, 0, 0, 5.0)], "Amazon")
    check("inter-state tax corrected", rows[0].iamt, 8.52)
    check("taxable untouched", rows[0].txval, 170.48)
    check("quantity untouched", rows[0].qty, 3)
    check("correction reported", len(msgs), 1)
    check("message names the platform", msgs[0].startswith("Amazon HSN 17011310"), True)

    rows, msgs = reconcile_hsn_tax(
        [HsnRow("170114", "PCS", 2, 200.0, 0, 15.0, 15.0, 0, 5.0)], "Meesho")
    check("intra-state split stays equal", (rows[0].camt, rows[0].samt), (5.0, 5.0))
    check("intra-state correction reported", len(msgs), 1)

    rows, msgs = reconcile_hsn_tax(
        [HsnRow("170114", "PCS", 1, 100.0, 5.0, 0, 0, 0, 5.0)], "Flipkart")
    check("correct row left alone", (rows[0].iamt, msgs), (5.0, []))

    rows, msgs = reconcile_hsn_tax(
        [HsnRow("170114", "PCS", 1, 100.0, 0, 0, 0, 0, 0.0)], "X")
    check("zero-rate row not silently zeroed", len(msgs), 0)
    rows, msgs = reconcile_hsn_tax(
        [HsnRow("170114", "PCS", 1, 100.0, 9.0, 0, 0, 0, 0.0)], "X")
    check("tax at 0% rate warns, no correction", (rows[0].iamt, len(msgs)), (9.0, 1))
    rows, msgs = reconcile_hsn_tax(
        [HsnRow("170114", "PCS", 1, 100.0, 0, 0, 0, 0, 5.0)], "X")
    check("missing tax warns, not guessed", (rows[0].iamt, len(msgs)), (0, 1))

    print()
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} mismatch(es)")
        return 1
    print("ALL GSTR-3B CHECKS PASSED")
    return 0


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "tax report"
    sys.exit(main(folder))
