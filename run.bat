@echo off
cd /d "%~dp0"
where python >nul 2>nul && (
    pip install -r requirements.txt -q
    python web_app.py
) || (
    py -m pip install -r requirements.txt -q
    py web_app.py
)
