@echo off
chcp 65001 >nul
REM 在本機把程式打包成單一 exe（需要先安裝 Python 3.12，安裝時勾選 Add python.exe to PATH）
cd /d "%~dp0"
if not exist .venv (
  py -3.12 -m venv .venv || python -m venv .venv
)
call .venv\Scripts\activate.bat
python -m pip install --upgrade pip
pip install -r requirements.txt pyinstaller
pyinstaller --noconfirm --clean --onefile --windowed ^
  --name ScreenOCRReader ^
  --icon assets/icon.ico ^
  --add-data "assets/icon.ico;." ^
  --collect-data rapidocr ^
  --collect-submodules rapidocr ^
  --hidden-import win32com.client ^
  --hidden-import pythoncom ^
  --exclude-module torch --exclude-module paddle --exclude-module openvino --exclude-module tensorrt ^
  --add-binary "%WINDIR%\System32\msvcp140.dll;." ^
  --add-binary "%WINDIR%\System32\msvcp140_1.dll;." ^
  --add-binary "%WINDIR%\System32\vcruntime140.dll;." ^
  --add-binary "%WINDIR%\System32\vcruntime140_1.dll;." ^
  src/screen_ocr_reader.py
dist\ScreenOCRReader.exe --selftest selftest.txt
type selftest.txt
echo.
echo 完成：dist\ScreenOCRReader.exe
pause
