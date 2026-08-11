"""Regression test: the auto-pipeline must reproduce the June 2026 filing exactly.

Ground truth = the numbers manually verified and filed for period 062026:
  * B2CS: 23 rows, taxable 9622.85, tax 481.15 (incl. the two recovered
    Meesho states: 22 -> 533.34, 35 -> 164.76)
  * HSN:  17011310 qty 5 / 599.05, 17011410 qty 1 / 160.96, 170114 qty 50 / 8862.86
  * Docs: invoices LWADTWQ...042-049 (8), IN-11 (1), LKO1-15 (1),
    cmr5w27107-27171 (65 total, 2 cancelled); credits MFADXIM...017-021 (5),
    LYADCW4...018-020 (3), cmr5w27C25-C34 (10), cmr5w27CM2 (1)

Run:  python test_ground_truth.py <test_folder>
"""
from __future__ import annotations

import sys
from pathlib import Path

from gst_autodetect import scan_folder
from gst_builder import build

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
    else:
        print(f"  OK {label} = {want!r}")


def approx(label: str, got: float, want: float, tol: float = 0.02) -> None:
    if abs(got - want) > tol:
        FAILURES.append(f"{label}: got {got}, want {want} (±{tol})")
    else:
        print(f"  OK {label} = {want}")


def main(folder: Path) -> int:
    detected = scan_folder(folder)
    print(f"Scanned {len(detected)} file entries")
    result = build(detected, "")

    print("\n-- Period & GSTIN --")
    check("period", result.period, "062026")
    check("seller_gstin", result.seller_gstin, "09BXDPT0789G1ZZ")

    print("\n-- Trap handling --")
    excluded = [e for e in result.scan_entries if e["status"] == "error"]
    check("wrong-month file excluded", any("amazon_april_old" in e["name"] for e in excluded), True)
    unknown = [e for e in result.scan_entries if e["status"] == "warning"]
    check("unrelated csv flagged unknown", any("tasks.csv" in e["name"] for e in unknown), True)

    print("\n-- B2CS (Table 7) --")
    b2cs = result.payload_full["b2cs"]
    check("b2cs row count", len(b2cs), 23)
    approx("b2cs taxable", sum(float(r["txval"]) for r in b2cs), 9622.85, 0.05)
    approx("b2cs tax", sum(float(r["iamt"]) + float(r["camt"]) + float(r["samt"])
                           for r in b2cs), 481.15, 0.05)
    by_pos = {r["pos"]: float(r["txval"]) for r in b2cs}
    approx("pos 22 (recovered CHATTISGARH)", by_pos.get("22", 0), 533.34)
    approx("pos 35 (recovered ANDAMAN)", by_pos.get("35", 0), 164.76)
    approx("pos 09 intra", by_pos.get("09", 0), 1669.54)

    print("\n-- HSN (Table 12) --")
    hsn = {r["hsn_sc"]: r for r in result.payload_full["hsn"]["hsn_b2c"]}
    check("hsn codes", sorted(hsn), ["17011310", "170114", "17011410"])
    check("17011310 qty", hsn["17011310"]["qty"], 5)
    approx("17011310 txval", float(hsn["17011310"]["txval"]), 599.05)
    check("17011410 qty", hsn["17011410"]["qty"], 1)
    approx("17011410 txval", float(hsn["17011410"]["txval"]), 160.96)
    check("170114 qty", hsn["170114"]["qty"], 50)
    approx("170114 txval", float(hsn["170114"]["txval"]), 8862.86)

    print("\n-- Documents (Table 13) --")
    doc_det = {d["doc_num"]: d["docs"] for d in result.payload_full["doc_issue"]["doc_det"]}
    invoices = {r["from"]: r for r in doc_det[1]}
    credits = {r["from"]: r for r in doc_det[5]}
    check("invoice series count", len(invoices), 4)
    check("credit series count", len(credits), 4)
    fk = invoices["LWADTWQ270000042"]
    check("flipkart to", fk["to"], "LWADTWQ270000049")
    check("flipkart totnum/cancel", (fk["totnum"], fk["cancel"]), (8, 0))
    check("amazon IN-11", (invoices["IN-11"]["totnum"], invoices["IN-11"]["cancel"]), (1, 0))
    check("amazon LKO1-15", invoices["LKO1-15"]["totnum"], 1)
    ms = invoices["cmr5w27107"]
    check("meesho to", ms["to"], "cmr5w27171")
    check("meesho totnum/cancel/net", (ms["totnum"], ms["cancel"], ms["net_issue"]), (65, 2, 63))
    check("flipkart returns CN", credits["MFADXIM270000017"]["totnum"], 5)
    check("flipkart cashback CN", credits["LYADCW4270000018"]["totnum"], 3)
    check("meesho CN", (credits["cmr5w27C25"]["to"], credits["cmr5w27C25"]["totnum"]),
          ("cmr5w27C34", 10))
    check("meesho credit-discount", credits["cmr5w27CM2"]["totnum"], 1)

    print("\n-- Payload shape --")
    check("root keys", list(result.payload_full.keys()),
          ["gstin", "fp", "version", "hash", "b2cs", "hsn", "doc_issue"])
    check("build ok (no validation errors)", result.ok, True)

    print("\n-- GSTR-3B (derived from GSTR-1) --")
    p3b = result.payload_3b
    check("3B period", p3b["ret_period"], "062026")
    det = p3b["sup_details"]["osup_det"]
    approx("3B 3.1(a) taxable", float(det["txval"]), 9622.85, 0.05)
    approx("3B 3.1(a) tax", float(det["iamt"]) + float(det["camt"])
           + float(det["samt"]) + float(det["csamt"]), 481.15, 0.05)
    inter = p3b["inter_sup"]["unreg_details"]
    check("3B 3.2 excludes home state 09", all(r["pos"] != "09" for r in inter), True)
    approx("3B 3.2 inter-state taxable",
           sum(float(r["txval"]) for r in inter), 9622.85 - 1669.54, 0.05)
    check("3B has no ITC section without GSTR-2B", "itc_elg" not in p3b, True)

    print()
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} mismatch(es):")
        for f in FAILURES:
            print("  X", f)
        return 1
    print("ALL GROUND-TRUTH CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
