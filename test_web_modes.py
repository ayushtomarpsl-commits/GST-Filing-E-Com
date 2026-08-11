"""Web app mode tests (local vs PUBLIC_MODE) via the Flask test client.

Verifies:
  1. LOCAL mode  — folder-path panel present, POST /process with a server
     folder path works end-to-end and produces GSTR-3B output.
  2. PUBLIC mode — folder-path panel gone (upload UI stays), server refuses
     to scan disk paths, folder-relative uploads work end-to-end.
  3. /help       — Prepare Offline guide with inline SVG illustrations.
  4. Onboarding  — help nav link, guide button, tour assets, render.yaml.

Run:  py -W ignore test_web_modes.py [sample_folder]   (default: ./tax report)
"""
from __future__ import annotations

import io
import sys
from pathlib import Path

import web_app

FAILURES: list[str] = []


def check(label: str, got, want) -> None:
    if got != want:
        FAILURES.append(f"{label}: got {got!r}, want {want!r}")
        print(f"  X  {label}: got {got!r}, want {want!r}")
    else:
        print(f"  OK {label} = {want!r}")


def check_true(label: str, cond, detail: str = "") -> None:
    if not cond:
        FAILURES.append(f"{label}{' — ' + detail if detail else ''}")
        print(f"  X  {label}{' — ' + detail if detail else ''}")
    else:
        print(f"  OK {label}")


def html(resp) -> str:
    return resp.get_data(as_text=True)


def upload_tuples(folder: Path, prefix: str = "myfolder"):
    """(BytesIO, folder-relative-filename) pairs from the real sample files,
    mimicking a webkitdirectory browser upload."""
    tuples = []
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            rel = path.relative_to(folder).as_posix()
            tuples.append((io.BytesIO(path.read_bytes()), f"{prefix}/{rel}"))
    return tuples


def main(folder: Path) -> int:
    root = Path(web_app.__file__).resolve().parent
    client = web_app.app.test_client()

    # ---------------- 1. LOCAL mode ----------------
    print("== 1. LOCAL mode (PUBLIC_MODE=False) ==")
    web_app.PUBLIC_MODE = False

    r = client.get("/")
    page = html(r)
    check("GET / status", r.status_code, 200)
    check_true('index has id="folder_path"', 'id="folder_path"' in page)
    check_true('index has id="folderInput"', 'id="folderInput"' in page)

    r = client.post("/process", data={"folder_path": str(folder), "period": ""})
    page = html(r)
    check("POST /process (folder path) status", r.status_code, 200)
    check_true("results mention GSTR-3B", "GSTR-3B" in page,
               f"status={r.status_code}, first 200 chars: {page[:200]!r}")

    # ---------------- 2. PUBLIC mode ----------------
    print("\n== 2. PUBLIC mode (PUBLIC_MODE=True) ==")
    web_app.PUBLIC_MODE = True
    try:
        r = client.get("/")
        page = html(r)
        check("GET / status", r.status_code, 200)
        check_true('index hides id="folder_path"', 'id="folder_path"' not in page)
        check_true('index keeps id="folderInput"', 'id="folderInput"' in page)

        # Path-only POST: the server must NOT scan its own disk when hosted.
        r = client.post("/process",
                        data={"folder_path": str(folder), "period": ""})
        page = html(r)
        check("path-only POST status (path scan refused)", r.status_code, 400)
        check_true('error page says "No files found"', "No files found" in page,
                   f"status={r.status_code}, first 200 chars: {page[:200]!r}")
        # index.html's hero copy mentions "GSTR-3B", so instead assert no
        # results artifacts (download links) were produced from the path.
        check_true("no downloads produced from server path",
                   "/download_file/" not in page)

        # Folder upload: files carry folder-relative names (webkitdirectory).
        files = upload_tuples(folder)
        check_true("sample folder has files to upload", len(files) >= 3,
                   f"found {len(files)} files in {folder}")
        r = client.post(
            "/process",
            data={"folder_path": "", "period": "", "files": files},
            content_type="multipart/form-data",
        )
        page = html(r)
        check("upload POST status", r.status_code, 200)
        check_true("uploaded folder produces GSTR-3B", "GSTR-3B" in page,
                   f"status={r.status_code}, first 200 chars: {page[:200]!r}")
    finally:
        web_app.PUBLIC_MODE = False

    # ---------------- 3. Help page ----------------
    print("\n== 3. /help (Prepare Offline guide) ==")
    r = client.get("/help")
    page = html(r)
    check("GET /help status", r.status_code, 200)
    check_true('help mentions "Prepare"', "Prepare" in page)
    svg_count = page.count("<svg")
    check_true("help has >= 3 inline <svg>", svg_count >= 3,
               f"found {svg_count}")

    # ---------------- 4. Onboarding assets ----------------
    print("\n== 4. Onboarding assets (nav, tour, render.yaml) ==")
    r = client.get("/")
    page = html(r)
    check_true("index has navHelp", "navHelp" in page)
    check_true("index has showGuideBtn", "showGuideBtn" in page)
    check_true("index includes tour.js", "tour.js" in page)

    tour_js = root / "static" / "js" / "tour.js"
    tour_css = root / "static" / "css" / "tour.css"
    check_true("static/js/tour.js exists", tour_js.is_file())
    check_true("static/css/tour.css exists", tour_css.is_file())
    if tour_js.is_file():
        check_true("tour.js non-trivial (>1000 bytes)",
                   tour_js.stat().st_size > 1000,
                   f"{tour_js.stat().st_size} bytes")
    if tour_css.is_file():
        check_true("tour.css non-trivial (>1000 bytes)",
                   tour_css.stat().st_size > 1000,
                   f"{tour_css.stat().st_size} bytes")

    render_yaml = root / "render.yaml"
    check_true("render.yaml exists", render_yaml.is_file())
    if render_yaml.is_file():
        text = render_yaml.read_text(encoding="utf-8")
        check_true("render.yaml uses waitress-serve", "waitress-serve" in text)
        check_true("render.yaml sets PUBLIC_MODE", "PUBLIC_MODE" in text)

    print()
    if FAILURES:
        print(f"FAILED — {len(FAILURES)} mismatch(es)")
        return 1
    print("ALL WEB MODE CHECKS PASSED")
    return 0


if __name__ == "__main__":
    folder = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).parent / "tax report"
    sys.exit(main(folder))
