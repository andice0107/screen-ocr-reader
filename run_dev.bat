@echo off
chcp 65001 >nul
REM 不打包、直接從原始碼執行（改程式後測試最快的方法）
cd /d "%~dp0"
if not exist .venv (
  py -3.12 -m venv .venv || python -m venv .venv
  call .venv\Scripts\activate.bat
  python -m pip install --upgrade pip
  pip install -r requirements.txt pyinstaller
) else (
  call .venv\Scripts\activate.bat
)
python src\screen_ocr_reader.py
