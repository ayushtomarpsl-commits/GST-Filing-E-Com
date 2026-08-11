"""
GST Helper for E-Commerce — browser web app (Flask).

Point it at a folder of downloaded Amazon/Flipkart/Meesho reports (zips fine);
it auto-detects every file, consolidates GSTR-1 Tables 5/7/12/13, and produces
ready-to-upload portal JSONs — with guidance when files are missing or wrong.

Run: python web_app.py
Then open http://127.0.0.1:5000 in your browser.
"""
from __future__ import annotations

import os
import re
import secrets
import shutil
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path

from flask import Flask, abort, render_template, request, send_file
from werkzeug.utils import secure_filename

from gst_autodetect import scan_files, scan_folder
from gst_builder import build
from gst_processor import csv_rows_for_export, write_csv_bytes
from gst_parsers import GSTConsolidator

# Public/hosted mode: the server cannot (and must not) read folder paths on a
# visitor's computer, so the path-scan feature is disabled server-side and the
# UI switches to the browse-folder upload flow.
PUBLIC_MODE = (os.environ.get("PUBLIC_MODE", "").strip().lower()
               in ("1", "true", "yes")) or "--public" in sys.argv[1:]

app = Flask(__name__)
app.secret_key = secrets.token_hex(16)
# Generous cap for a month of reports (zips included); prevents abuse when hosted.
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024

JOBS_DIR = Path(__file__).resolve().parent / ".jobs"
JOB_TTL_SECONDS = 3600
JOB_ID_RE = re.compile(r"^[0-9a-f]{16}$")


@app.context_processor
def _inject_mode():
    return {"public_mode": PUBLIC_MODE}


def _cleanup_old_jobs() -> None:
    if not JOBS_DIR.exists():
        return
    cutoff = time.time() - JOB_TTL_SECONDS
    for folder in JOBS_DIR.iterdir():
        if folder.is_dir() and folder.stat().st_mtime < cutoff:
            shutil.rmtree(folder, ignore_errors=True)


@app.route("/")
def index():
    _cleanup_old_jobs()
    return render_template("index.html")


@app.route("/help")
def help_page():
    return render_template("help.html")


@app.errorhandler(413)
def too_large(_exc):
    # A friendly page instead of the bare 413 — the XHR upload path renders
    # whatever this returns, so the visitor must get a usable form back.
    return render_template(
        "index.html",
        error="Your upload is larger than the 200 MB limit. Remove big files "
              "that are not reports (videos, photos, backups) from the folder, "
              "or upload fewer files at a time, and try again.",
    ), 413


@app.route("/process", methods=["POST"])
def process():
    _cleanup_old_jobs()
    period = (request.form.get("period") or "").strip()
    folder_path = (request.form.get("folder_path") or "").strip().strip('"').strip("'")
    if PUBLIC_MODE:
        # Server-side enforcement, independent of the UI: never scan server
        # disk paths when hosted publicly.
        folder_path = ""

    try:
        blobs = []
        # Any uploaded files (multi-input and/or legacy per-platform fields).
        # Every upload is passed to detection — never silently dropped by
        # extension; unrecognized files surface in the scan table instead.
        for storage in request.files.getlist("files") + [
            request.files.get(k) for k in ("flipkart", "amazon", "meesho_sales", "meesho_returns")
        ]:
            if storage and storage.filename:
                blobs.append((storage.filename, storage.read()))

        detected = []
        if folder_path:
            folder = Path(folder_path)
            if not folder.is_dir():
                return render_template(
                    "index.html",
                    error=f"Folder not found: {folder_path}",
                    folder_path=folder_path, period=period,
                ), 400
            detected.extend(scan_folder(folder))
        if blobs:
            detected.extend(scan_files(blobs))

        if not detected:
            return render_template(
                "index.html",
                error="No files found — enter your reports folder path or upload files.",
                folder_path=folder_path, period=period,
            ), 400

        result = build(detected, period)

        # Persist downloadable artifacts for this job (hex id: filesystem-safe
        # and survives the download route's validation untouched)
        job_id = secrets.token_hex(8)
        job_dir = JOBS_DIR / job_id
        job_dir.mkdir(parents=True, exist_ok=True)
        for card in result.cards:
            (job_dir / secure_filename(card.filename)).write_text(
                card.json_str, encoding="utf-8"
            )
        export_csv = csv_rows_for_export(result.csv_rows)
        (job_dir / f"b2cs_offline_tool_{result.period}.csv").write_bytes(
            write_csv_bytes(export_csv)
        )
        xlsx_path = job_dir / f"consolidated_{result.period}.xlsx"
        GSTConsolidator.generate_excel(
            seller_gstin=result.seller_gstin,
            period=result.period,
            csv_rows=result.csv_rows,
            stats=result.breakdown_stats,
            flipkart_records=result.records_by_platform["Flipkart"],
            amazon_records=result.records_by_platform["Amazon"],
            meesho_records=result.records_by_platform["Meesho"],
            excel_path=xlsx_path,
        )

        return render_template(
            "results.html",
            period=result.period,
            seller_gstin=result.seller_gstin,
            job_id=job_id,
            scan={"source": folder_path or "uploaded files",
                  "entries": result.scan_entries},
            guidance=[asdict(g) for g in result.guidance],
            platforms=[asdict(p) for p in result.platforms],
            totals=result.totals,
            cards=[asdict(c) for c in result.cards],
            extra_downloads=[
                {"filename": f"b2cs_offline_tool_{result.period}.csv",
                 "label": "B2CS CSV (offline tool)"},
                {"filename": f"consolidated_{result.period}.xlsx",
                 "label": "Excel workbook (reconciliation)"},
            ],
        )
    except ValueError as exc:
        return render_template("index.html", error=str(exc),
                               folder_path=folder_path, period=period), 400
    except Exception as exc:  # pragma: no cover — safety net
        return render_template("index.html",
                               error=f"Processing failed: {exc}",
                               folder_path=folder_path, period=period), 500


@app.route("/download_file/<job_id>/<path:filename>")
def download_file(job_id: str, filename: str):
    if not JOB_ID_RE.fullmatch(job_id):
        abort(404)
    job_dir = (JOBS_DIR / job_id).resolve()
    target = (job_dir / secure_filename(Path(filename).name)).resolve()
    if not target.is_file() or target.parent != job_dir \
            or job_dir.parent != JOBS_DIR.resolve():
        abort(404)
    return send_file(target, as_attachment=True, download_name=target.name)


if __name__ == "__main__":
    JOBS_DIR.mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", "5000"))
    for arg in sys.argv[1:]:
        if arg.startswith("--port="):
            port = int(arg.split("=", 1)[1])
    if PUBLIC_MODE:
        host = "0.0.0.0"
        print(f"GST Helper (PUBLIC mode) listening on port {port}")
    else:
        host = "127.0.0.1"
        if "--no-browser" not in sys.argv[1:]:
            threading.Timer(
                1.2, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
        print(f"GST Helper for E-Commerce running at http://127.0.0.1:{port}")
        print("Press Ctrl+C to stop.")
    app.run(host=host, port=port, debug=False, use_reloader=False)
