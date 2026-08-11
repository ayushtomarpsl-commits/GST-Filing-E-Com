"""
GST Filing E-Commerce — upload platform tax reports and generate GSTR-1 B2CS files.

Run: streamlit run app.py
"""
from __future__ import annotations

import streamlit as st

from gst_processor import (
    build_json_payload,
    csv_rows_for_export,
    generate_excel_bytes,
    process_reports,
    write_csv_bytes,
    write_json_bytes,
)

st.set_page_config(
    page_title="GST Filing E-Commerce",
    page_icon="📊",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    .main-header { font-size: 1.75rem; font-weight: 700; color: #1B365D; margin-bottom: 0.25rem; }
    .sub-header { color: #64748B; margin-bottom: 1.5rem; }
    div[data-testid="stMetric"] {
        background: #F8FAFC;
        border: 1px solid #E2E8F0;
        border-radius: 8px;
        padding: 0.75rem;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

st.markdown('<p class="main-header">GST Filing — E-Commerce Consolidation</p>', unsafe_allow_html=True)
st.markdown(
    '<p class="sub-header">Upload Flipkart, Amazon, and/or Meesho tax reports to generate '
    "consolidated GSTR-1 B2CS files for the GST portal and offline tool.</p>",
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("Filing details")
    period = st.text_input(
        "Return period (MMYYYY)",
        value="052026",
        help="Example: 042026 for April 2026",
        max_chars=6,
    )
    st.divider()
    st.markdown("**Expected report formats**")
    st.markdown(
        "- **Flipkart:** GSTR return report `.xlsx`\n"
        "- **Amazon:** Tax report `.csv`\n"
        "- **Meesho:** TCS sales `.xlsx` (+ optional returns `.xlsx`)"
    )

col1, col2, col3 = st.columns(3)

with col1:
    st.subheader("Flipkart")
    flipkart_file = st.file_uploader(
        "GSTR return report",
        type=["xlsx", "xls"],
        key="flipkart",
        help="Flipkart GSTR return report Excel file",
    )

with col2:
    st.subheader("Amazon")
    amazon_file = st.file_uploader(
        "Tax report CSV",
        type=["csv"],
        key="amazon",
        help="Amazon tax report CSV export",
    )

with col3:
    st.subheader("Meesho")
    meesho_sales = st.file_uploader(
        "TCS sales report",
        type=["xlsx", "xls"],
        key="meesho_sales",
    )
    meesho_returns = st.file_uploader(
        "TCS sales return (optional)",
        type=["xlsx", "xls"],
        key="meesho_returns",
    )

uploaded_count = sum(
    1 for f in (flipkart_file, amazon_file, meesho_sales) if f is not None
)
st.caption(f"{uploaded_count} platform report(s) attached")

process_clicked = st.button(
    "Process reports",
    type="primary",
    disabled=uploaded_count == 0,
    use_container_width=False,
)

if process_clicked:
    if not period.strip():
        st.error("Enter a return period (MMYYYY).")
        st.stop()

    with st.spinner("Parsing and consolidating reports…"):
        try:
            result = process_reports(
                period=period,
                flipkart_bytes=flipkart_file.getvalue() if flipkart_file else None,
                amazon_bytes=amazon_file.getvalue() if amazon_file else None,
                meesho_sales_bytes=meesho_sales.getvalue() if meesho_sales else None,
                meesho_returns_bytes=meesho_returns.getvalue() if meesho_returns else None,
            )
            st.session_state["result"] = result
        except ValueError as exc:
            st.error(str(exc))
            st.stop()
        except Exception as exc:
            st.error(f"Processing failed: {exc}")
            st.stop()

if "result" in st.session_state:
    result = st.session_state["result"]
    stats = result.breakdown_stats["Consolidated"]

    st.success("Reports processed successfully.")

    if result.gstin_warnings:
        for warning in result.gstin_warnings:
            st.warning(warning)

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Seller GSTIN", result.seller_gstin)
    m2.metric("Return period", result.period)
    m3.metric("B2CS rows", len(result.csv_rows))
    m4.metric("Total taxable (₹)", f"{stats['taxable']:,.2f}")

    with st.expander("Platform parse status", expanded=False):
        for plat in result.platforms:
            if plat.error:
                st.error(f"**{plat.platform}:** {plat.error}")
            elif plat.records:
                st.info(
                    f"**{plat.platform}:** {len(plat.records)} rows · "
                    f"GSTIN {plat.seller_gstin} · E-commerce {plat.eco_gstin}"
                )
            else:
                st.warning(f"**{plat.platform}:** No data rows (skipped or empty).")

    st.subheader("Platform summary")
    summary_rows = []
    for name in ("Flipkart", "Amazon", "Meesho", "Consolidated"):
        s = result.breakdown_stats[name]
        summary_rows.append(
            {
                "Platform": name,
                "Taxable (₹)": s["taxable"],
                "IGST (₹)": s["igst"],
                "CGST (₹)": s["cgst"],
                "SGST (₹)": s["sgst"],
                "Cess (₹)": s["cess"],
                "Total tax (₹)": s["tax"],
            }
        )
    st.dataframe(summary_rows, use_container_width=True, hide_index=True)

    export_csv = csv_rows_for_export(result.csv_rows)
    payload = build_json_payload(result.seller_gstin, result.period, result.json_rows)
    csv_bytes = write_csv_bytes(export_csv)
    json_bytes = write_json_bytes(payload, pretty=False)
    excel_bytes = generate_excel_bytes(result)

    st.subheader("Download outputs")
    d1, d2, d3 = st.columns(3)
    base_name = f"gstr1_b2cs_{result.period}"

    with d1:
        st.download_button(
            label="B2CS CSV (offline tool)",
            data=csv_bytes,
            file_name=f"{base_name}_upload.csv",
            mime="text/csv",
            use_container_width=True,
        )
    with d2:
        st.download_button(
            label="GSTR-1 JSON (portal)",
            data=json_bytes,
            file_name=f"{base_name}.json",
            mime="application/json",
            use_container_width=True,
        )
    with d3:
        st.download_button(
            label="Excel workbook",
            data=excel_bytes,
            file_name=f"{base_name}_consolidated.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            use_container_width=True,
        )

    if result.validation_errors:
        st.error("Portal JSON validation issues:")
        for err in result.validation_errors:
            st.markdown(f"- {err}")
    else:
        st.caption("Portal JSON structure validated successfully.")

    with st.expander("Preview — consolidated B2CS (CSV)", expanded=False):
        st.dataframe(export_csv, use_container_width=True, hide_index=True)

    with st.expander("Preview — portal JSON B2CS", expanded=False):
        st.json(result.json_rows)

else:
    st.info("Attach one or more platform reports, set the return period, then click **Process reports**.")
