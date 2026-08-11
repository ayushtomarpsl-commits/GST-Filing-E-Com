# GST Helper for E-Commerce

Point it at a folder of downloaded Amazon / Flipkart / Meesho seller tax
reports — it auto-detects every file (zips included), consolidates them, and
produces **ready-to-upload GSTR-1 and GSTR-3B portal JSONs** covering:

| GSTR-1 table | Section | Source |
|---|---|---|
| Table 7 — B2CS | `b2cs` | All platforms, state-wise net (sales − returns − cashback CNs) |
| Table 5 — B2CL | `b2cl` | Inter-state invoices > ₹1,00,000 (when present) |
| Table 12 — HSN (B2C) | `hsn.hsn_b2c` | Platform HSN summaries, merged on HSN+UQC+rate |
| Table 13 — Documents | `doc_issue` | Invoice & credit-note series (from/to/cancelled) |

| GSTR-3B table | Section | Source |
|---|---|---|
| 3.1(a) — Outward taxable | `sup_details.osup_det` | Derived from the GSTR-1 build (B2CS + B2CL) |
| 3.1(d) — Inward reverse charge | `sup_details.isup_rev` | GSTR-2B documents flagged `rev=Y` |
| 3.2 — Inter-state to unregistered | `inter_sup.unreg_details` | State-wise inter-state slice of 3.1(a) |
| 4 — Eligible ITC | `itc_elg` | **Your GSTR-2B JSON** (see below) — omitted if not provided, so the portal's auto-draft is never overwritten with zeros |
| 5 — Exempt/nil inward | `inward_sup` | Explicit zeros (typical marketplace goods seller) |

Each table gets its own **card** in the dashboard with a data preview,
**Copy JSON**, and **Download** — plus one combined GSTR-1 JSON that fills
every table in a single *Prepare Offline* upload, and a GSTR-3B card listing
**every portal field with the exact value to enter/verify**.

## Run it

Double-click **`run.bat`** or:

```bash
pip install -r requirements.txt
python web_app.py
```

Browser opens at **http://127.0.0.1:5000**. Paste your reports-folder path
(e.g. `C:\Users\you\Downloads\gst june`), leave the period blank
(auto-detected from the files), click **Process folder**.

## Host it publicly (free)

See **[DEPLOY_GUIDE.md](DEPLOY_GUIDE.md)** for click-by-click steps
(Render.com recommended; PythonAnywhere as an alternative).

When hosted, the app runs with the environment variable `PUBLIC_MODE=1`:
the server binds to `0.0.0.0`/`$PORT`, the "paste a folder path" feature is
disabled (a public server cannot read visitors' disks), visitors instead
**browse and upload** their report files, and each visitor's generated
files are auto-deleted after 1 hour.

## What to download each month

| Platform | Where | File | Fills |
|----------|-------|------|-------|
| Amazon | Seller Central → GST Reports → *GST Ready-to-File* | `GSTR1-<MONTH>-….xlsx` | B2CS, B2CL, HSN |
| Amazon | Seller Central → Reports → Tax Document Library | MTR B2C `.csv` | Table 13 invoice numbers |
| Flipkart | Seller Hub → Reports → GST Reports | *GSTR return report* `.xlsx` | B2CS, HSN, invoice series |
| Flipkart | Seller Hub → Reports | *Sales Report* `.xlsx` | Credit-note series (+ everything if GSTR report absent) |
| Meesho | Supplier Panel → Download → *GST Report* | `gst_…zip` (tcs_sales + returns) | B2CS, HSN |
| Meesho | Supplier Panel → Download → *Supplier Tax Invoice* | `…TAX_INVOICE.zip` | Table 13 series |
| **GST portal** | Returns Dashboard → month → **GSTR-2B** → Download → *GENERATE JSON FILE TO DOWNLOAD* | `GSTR2B_….json` | **GSTR-3B Table 4 (ITC)** — the GST you paid on marketplace commissions/fees |

Drop everything (zips unextracted are fine) into one folder. The tool tells
you exactly which file is missing, wrong-month, or unrecognized.

### GSTR-3B — how it works here

- **Nothing extra is needed from Amazon/Flipkart/Meesho** — 3.1(a) and 3.2
  are derived from the same sales reports as GSTR-1, so 3B always matches
  the GSTR-1 you just filed (the portal cross-checks exactly this).
- The **only new file** is the **GSTR-2B JSON** from the GST portal itself —
  it carries your Input Tax Credit (Table 4). Without it the 3B JSON simply
  omits Table 4 (never zeros it), and the dashboard tells you so.
- 2B safety: wrong-GSTIN and wrong-month 2B files are rejected loudly;
  sections the tool does not compute (imports IMPG/IMPS, ISD, amendments
  B2BA/CDNRA) raise visible "fill manually" errors instead of being skipped.
- **TCS** deducted by the marketplaces (0.5% u/s 52) is *not* part of
  GSTR-3B — the dashboard shows the approximate amount and reminds you to
  accept it under *Services → Returns → TDS and TCS credit received*.
- File GSTR-1 first, then GSTR-3B. On the portal, 3B Tables 3.1/3.2 are
  auto-drafted from GSTR-1 and Table 4 from GSTR-2B — use the card to
  **verify field-by-field**, or upload `gstr3b_<period>.json` via the
  GSTR-3B offline utility path.

## Safety rails (learned from real filings)

- **No silent data loss** — unmappable states (e.g. Meesho's `CHATTISGARH`
  misspelling) raise visible errors instead of dropping rows.
- **Wrong-month files are excluded automatically** with a warning (an old
  April file in the folder cannot poison a June filing).
- **Portal-rule validation** before you file: duplicate POS+rate rows, tax
  math per row, HSN↔B2CS cross-total, Table 13 net counts, negative B2CS rows.
- **Regression-tested** against a real filed month:
  `python test_ground_truth.py <june-folder>`.

## Project layout

- `web_app.py` — Flask web server (main UI)
- `gst_autodetect.py` — folder/zip scanner + content fingerprinting
- `gst_parsers.py` — platform B2CS parsers (+ state normalization)
- `gst_ext_parsers.py` — HSN / document-series / B2CL parsers
- `gst_builder.py` — consolidation, payload assembly, validation, guidance
- `gst3b_builder.py` — GSTR-3B derivation + GSTR-2B (ITC) JSON parser
- `gst_processor.py` — legacy single-file pipeline (CSV/Excel writers reused)
- `templates/`, `static/` — dashboard UI
- `test_ground_truth.py` — regression test vs a real filed month
- `test_gstr3b.py` — GSTR-3B/2B tests (`py test_gstr3b.py`, uses `tax report/`)
- `app.py` — legacy Streamlit UI (`streamlit run app.py`)

## CLI (Flipkart only, legacy)

```bash
python flipkart_gstr1_b2cs.py "GSTR return report.xlsx" --period 042026
```
