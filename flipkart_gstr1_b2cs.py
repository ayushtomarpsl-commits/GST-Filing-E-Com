#!/usr/bin/env python3
"""
Flipkart GSTR-1 B2CS generator.

Reads Flipkart "GSTR return report.xlsx" and produces:
  - b2cs_upload.csv  (for GST Returns Offline Tool import)
  - gstr1_b2cs.json  (for GST portal upload; no etin field)

Usage:
  python flipkart_gstr1_b2cs.py
  python flipkart_gstr1_b2cs.py "GSTR return report.xlsx"
  python flipkart_gstr1_b2cs.py --report report.xlsx --period 042026
  python flipkart_gstr1_b2cs.py --report report.xlsx --sales "Sales Report.xlsx"
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import pandas as pd

GSTR_VERSION = "GST3.2.4"
PORTAL_JSON_TYP = "OE"
PORTAL_JSON_HASH = "hash"
PORTAL_B2CS_KEYS = (
    "sply_ty",
    "pos",
    "typ",
    "txval",
    "rt",
    "iamt",
    "camt",
    "samt",
    "csamt",
)
PORTAL_ROOT_KEYS = ("gstin", "fp", "version", "hash", "b2cs")
DEFAULT_REPORT_NAME = "GSTR return report.xlsx"
DEFAULT_SALES_NAME = "Sales Report.xlsx"
CSV_NAME = "b2cs_upload.csv"
JSON_NAME = "gstr1_b2cs.json"

STATE_NAMES = {
    "01": "Jammu & Kashmir",
    "02": "Himachal Pradesh",
    "03": "Punjab",
    "04": "Chandigarh",
    "05": "Uttarakhand",
    "06": "Haryana",
    "07": "Delhi",
    "08": "Rajasthan",
    "09": "Uttar Pradesh",
    "10": "Bihar",
    "11": "Sikkim",
    "12": "Arunachal Pradesh",
    "13": "Nagaland",
    "14": "Manipur",
    "15": "Mizoram",
    "16": "Tripura",
    "17": "Meghalaya",
    "18": "Assam",
    "19": "West Bengal",
    "20": "Jharkhand",
    "21": "Odisha",
    "22": "Chhattisgarh",
    "23": "Madhya Pradesh",
    "24": "Gujarat",
    "25": "Daman & Diu",
    "26": "Dadra & Nagar Haveli & Daman & Diu",
    "27": "Maharashtra",
    "29": "Karnataka",
    "30": "Goa",
    "31": "Lakshadweep",
    "32": "Kerala",
    "33": "Tamil Nadu",
    "34": "Puducherry",
    "35": "Andaman & Nicobar Islands",
    "36": "Telangana",
    "37": "Andhra Pradesh",
    "38": "Ladakh",
}

FLIPKART_STATE_CODES = {
    "IN-AP": "37",
    "IN-AR": "12",
    "IN-AS": "18",
    "IN-BR": "10",
    "IN-CT": "22",
    "IN-CH": "04",
    "IN-DL": "07",
    "IN-GA": "30",
    "IN-GJ": "24",
    "IN-HR": "06",
    "IN-HP": "02",
    "IN-JK": "01",
    "IN-JH": "20",
    "IN-KA": "29",
    "IN-KL": "32",
    "IN-LA": "38",
    "IN-LD": "31",
    "IN-MP": "23",
    "IN-MH": "27",
    "IN-MN": "14",
    "IN-ML": "17",
    "IN-MZ": "15",
    "IN-NL": "13",
    "IN-OR": "21",
    "IN-PY": "34",
    "IN-PB": "03",
    "IN-RJ": "08",
    "IN-SK": "11",
    "IN-TN": "33",
    "IN-TG": "36",
    "IN-TR": "16",
    "IN-UP": "09",
    "IN-UT": "05",
    "IN-WB": "19",
}

CSV_HEADERS = [
    "Type",
    "Place Of Supply",
    "Rate",
    "Applicable % of Tax Rate",
    "Taxable Value",
    "Cess Amount",
    "E-Commerce GSTIN",
]

POS_PATTERN = re.compile(r"^(\d{2})")


def round2(value: float) -> float:
    return float(
        Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    )


def portal_number(value: float) -> int | float:
    """Match GST portal JSON: integers when whole, else up to 2 decimals."""
    rounded = round2(value)
    if rounded == int(rounded):
        return int(rounded)
    return rounded


def format_pos(state_code: str) -> str:
    return f"{state_code}-{STATE_NAMES[state_code]}"


def format_rate(value: float) -> str:
    return str(int(value)) if value == int(value) else str(value)


def format_taxable(value: float) -> str:
    rounded = round2(value)
    if rounded == int(rounded):
        return str(int(rounded))
    text = f"{rounded:.2f}".rstrip("0").rstrip(".")
    return text


def detect_seller_gstin(gstr: pd.ExcelFile) -> str:
    for sheet in ("Section 7(A)(2) in GSTR-1", "Section 7(B)(2) in GSTR-1"):
        frame = pd.read_excel(gstr, sheet_name=sheet)
        if not frame.empty and "GSTIN" in frame.columns:
            gstin = str(frame["GSTIN"].iloc[0]).strip().upper()
            if len(gstin) == 15:
                return gstin
    raise ValueError("Could not read seller GSTIN from the Flipkart GSTR report.")


def detect_flipkart_gstin(gstr: pd.ExcelFile) -> str:
    frame = pd.read_excel(gstr, sheet_name="Section 3 in GSTR-8")
    gstin = str(frame["GSTIN of Flipkart.Com"].iloc[0]).strip().upper()
    if len(gstin) != 15:
        raise ValueError("Could not read Flipkart GSTIN from Section 3 in GSTR-8.")
    return gstin


def detect_return_period(sales_path: Path | None) -> str | None:
    if sales_path is None or not sales_path.exists():
        return None
    frame = pd.read_excel(sales_path, sheet_name="Sales Report")
    for column in ("Buyer Invoice Date", "Order Date"):
        if column not in frame.columns:
            continue
        dates = pd.to_datetime(frame[column], errors="coerce").dropna()
        if dates.empty:
            continue
        anchor = dates.max()
        return f"{anchor.month:02d}{anchor.year}"
    return None


def read_tax_amounts(
    gstr: pd.ExcelFile, seller_pos: str
) -> dict[tuple[str, str], dict[str, float]]:
    taxes: dict[tuple[str, str], dict[str, float]] = {}

    intra = pd.read_excel(gstr, sheet_name="Section 7(A)(2) in GSTR-1")
    for _, row in intra.iterrows():
        taxable = round2(float(row["Aggregate Taxable Value Rs."]))
        if taxable == 0:
            continue
        taxes[(seller_pos, "INTRA")] = {
            "txval": taxable,
            "iamt": 0.0,
            "camt": round2(float(row["CGST Amount Rs."])),
            "samt": round2(float(row["SGST /UT Amount Rs."])),
            "csamt": round2(float(row["CESS Amount Rs."])),
        }

    inter = pd.read_excel(gstr, sheet_name="Section 7(B)(2) in GSTR-1")
    for _, row in inter.iterrows():
        taxable = round2(float(row["Aggregate Taxable Value Rs."]))
        if taxable == 0:
            continue
        state_key = str(row["Delivered State Code"]).strip().upper()
        pos = FLIPKART_STATE_CODES.get(state_key)
        if not pos:
            state_name = str(row.get("Delivered State (PoS)", state_key))
            raise ValueError(f"Unknown delivery state code: {state_key} ({state_name})")
        taxes[(pos, "INTER")] = {
            "txval": taxable,
            "iamt": round2(float(row["IGST Amount Rs."])),
            "camt": 0.0,
            "samt": 0.0,
            "csamt": round2(float(row["CESS Amount Rs."])),
        }
    return taxes


def build_csv_rows(
    gstr: pd.ExcelFile, seller_pos: str, flipkart_gstin: str
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []

    intra = pd.read_excel(gstr, sheet_name="Section 7(A)(2) in GSTR-1")
    for _, record in intra.iterrows():
        taxable = float(record["Aggregate Taxable Value Rs."])
        if round2(taxable) == 0:
            continue
        rate = float(record["CGST %"]) + float(record["SGST/UT %"])
        rows.append(
            {
                "Type": "E",
                "Place Of Supply": format_pos(seller_pos),
                "Rate": format_rate(rate),
                "Applicable % of Tax Rate": "",
                "Taxable Value": format_taxable(taxable),
                "Cess Amount": "0",
                "E-Commerce GSTIN": flipkart_gstin,
            }
        )

    inter = pd.read_excel(gstr, sheet_name="Section 7(B)(2) in GSTR-1")
    for _, record in inter.iterrows():
        taxable = float(record["Aggregate Taxable Value Rs."])
        if round2(taxable) == 0:
            continue
        state_key = str(record["Delivered State Code"]).strip().upper()
        state_code = FLIPKART_STATE_CODES.get(state_key)
        if not state_code:
            raise ValueError(f"Unknown state code: {state_key}")
        rate = float(record["IGST %"])
        rows.append(
            {
                "Type": "E",
                "Place Of Supply": format_pos(state_code),
                "Rate": format_rate(rate),
                "Applicable % of Tax Rate": "",
                "Taxable Value": format_taxable(taxable),
                "Cess Amount": "0",
                "E-Commerce GSTIN": flipkart_gstin,
            }
        )

    rows.sort(key=lambda row: row["Place Of Supply"])
    return rows


def write_csv(rows: list[dict[str, str]], path: Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle, lineterminator="\r\n")
        writer.writerow(CSV_HEADERS)
        for row in rows:
            writer.writerow([row[column] for column in CSV_HEADERS])


def csv_rows_to_json(
    csv_rows: list[dict[str, str]],
    taxes: dict[tuple[str, str], dict[str, float]],
    seller_pos: str,
) -> list[dict[str, object]]:
    """Build portal B2CS rows (typ OE, no etin/diff_percent)."""
    json_rows: list[dict[str, object]] = []
    for row in csv_rows:
        match = POS_PATTERN.match(row["Place Of Supply"])
        if not match:
            raise ValueError(f"Invalid place of supply: {row['Place Of Supply']}")
        pos = match.group(1)
        supply_kind = "INTRA" if pos == seller_pos else "INTER"
        rate = float(row["Rate"])
        key = (pos, supply_kind)
        amounts = taxes.get(key)
        if amounts is None:
            taxable = round2(float(row["Taxable Value"]))
            total_tax = round2(taxable * rate / 100)
            if supply_kind == "INTRA":
                camt = round2(total_tax / 2)
                amounts = {
                    "txval": taxable,
                    "iamt": 0.0,
                    "camt": camt,
                    "samt": round2(total_tax - camt),
                    "csamt": round2(float(row["Cess Amount"])),
                }
            else:
                amounts = {
                    "txval": taxable,
                    "iamt": total_tax,
                    "camt": 0.0,
                    "samt": 0.0,
                    "csamt": round2(float(row["Cess Amount"])),
                }
        json_rows.append(
            {
                "sply_ty": supply_kind,
                "pos": pos,
                "typ": PORTAL_JSON_TYP,
                "txval": portal_number(amounts["txval"]),
                "rt": portal_number(rate),
                "iamt": portal_number(amounts["iamt"]),
                "camt": portal_number(amounts["camt"]),
                "samt": portal_number(amounts["samt"]),
                "csamt": portal_number(amounts["csamt"]),
            }
        )
    json_rows.sort(key=lambda item: (item["sply_ty"], item["pos"], item["rt"]))
    return json_rows


def build_json_payload(
    seller_gstin: str,
    return_period: str,
    b2cs_rows: list[dict[str, object]],
) -> dict[str, object]:
    """Portal format: gstin, fp, version, hash, b2cs only."""
    return {
        "gstin": seller_gstin,
        "fp": return_period,
        "version": GSTR_VERSION,
        "hash": PORTAL_JSON_HASH,
        "b2cs": b2cs_rows,
    }


def validate_portal_payload(payload: dict[str, object]) -> list[str]:
    """Validate JSON matches the GST portal sample structure."""
    errors: list[str] = []
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


def run(
    report_path: Path,
    output_dir: Path,
    return_period: str | None,
    sales_path: Path | None,
    pretty_json: bool = False,
) -> None:
    if not report_path.exists():
        raise FileNotFoundError(f"Flipkart report not found: {report_path}")

    gstr = pd.ExcelFile(report_path)
    seller_gstin = detect_seller_gstin(gstr)
    seller_pos = seller_gstin[:2]
    flipkart_gstin = detect_flipkart_gstin(gstr)

    period = return_period or detect_return_period(sales_path)
    if not period:
        raise ValueError(
            "Return period not found. Pass --period MMYYYY (example: 042026 for Apr 2026) "
            "or place Sales Report.xlsx in the same folder."
        )

    taxes = read_tax_amounts(gstr, seller_pos)
    csv_rows = build_csv_rows(gstr, seller_pos, flipkart_gstin)
    if not csv_rows:
        raise ValueError("No B2CS rows found in the Flipkart GSTR report.")

    json_rows = csv_rows_to_json(csv_rows, taxes, seller_pos)
    payload = build_json_payload(seller_gstin, period, json_rows)

    output_dir.mkdir(parents=True, exist_ok=True)
    csv_path = output_dir / CSV_NAME
    json_path = output_dir / JSON_NAME

    write_csv(csv_rows, csv_path)
    if pretty_json:
        json_text = json.dumps(payload, indent=2, ensure_ascii=False)
    else:
        json_text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    json_path.write_text(json_text + "\n", encoding="utf-8")

    validation_errors = validate_portal_payload(payload)
    total_taxable = sum(float(row["txval"]) for row in json_rows)
    print(f"Seller GSTIN : {seller_gstin}")
    print(f"Return period: {period}")
    print(f"Flipkart GSTIN (CSV only): {flipkart_gstin}")
    print(f"B2CS rows     : {len(json_rows)}")
    print(f"Total taxable : {total_taxable:.2f}")
    print(f"CSV saved     : {csv_path}")
    print(f"JSON saved    : {json_path}")
    print(f"JSON format   : typ={PORTAL_JSON_TYP}, hash={PORTAL_JSON_HASH!r}, compact")
    if validation_errors:
        print("Validation    : FAILED")
        for message in validation_errors:
            print(f"  - {message}")
    else:
        print("Validation    : OK (matches portal sample structure)")
    print()
    for row in json_rows:
        print(
            f"  {row['sply_ty']:5} pos={row['pos']}  "
            f"txval={row['txval']:>8}  "
            f"IGST={row['iamt']:>6}  CGST={row['camt']:>5}  SGST={row['samt']:>5}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate GSTR-1 B2CS CSV and portal JSON from Flipkart GSTR return report."
    )
    parser.add_argument(
        "report",
        nargs="?",
        default=DEFAULT_REPORT_NAME,
        help=f"Path to Flipkart GSTR return report (default: {DEFAULT_REPORT_NAME})",
    )
    parser.add_argument(
        "--report",
        dest="report_flag",
        help="Same as positional report path",
    )
    parser.add_argument(
        "--sales",
        default=DEFAULT_SALES_NAME,
        help=f"Sales report for auto return period (default: {DEFAULT_SALES_NAME})",
    )
    parser.add_argument(
        "--period",
        help="Return period as MMYYYY, e.g. 042026 for April 2026",
    )
    parser.add_argument(
        "--output",
        default=".",
        help="Output folder (default: current directory)",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Write indented JSON instead of compact portal format",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    report_path = Path(args.report_flag or args.report).expanduser().resolve()
    output_dir = Path(args.output).expanduser().resolve()
    sales_path = Path(args.sales).expanduser()
    if not sales_path.is_absolute():
        sales_path = (report_path.parent / sales_path).resolve()

    run(
        report_path=report_path,
        output_dir=output_dir,
        return_period=args.period,
        sales_path=sales_path,
        pretty_json=args.pretty,
    )


if __name__ == "__main__":
    main()
