#!/bin/bash
# 在 Mac 上把程式打包成「截圖朗讀小幫手.app」
# 用法：在這個資料夾打開終端機，執行  ./build_mac.sh
set -e
cd "$(dirname "$0")"
PY=/opt/homebrew/bin/python3.12     # 需要有 tkinter：brew install python@3.12 python-tk@3.12
[ -d .venv ] || $PY -m venv .venv
. .venv/bin/activate
pip -q install --upgrade pip
pip -q install -r requirements-mac.txt pyinstaller

# 圖示：icon.ico → icon.icns
rm -rf build_icon && mkdir -p build_icon/icon.iconset
python - <<'PYEOF'
from PIL import Image
im = Image.open("assets/icon.ico").convert("RGBA")
for s in (16, 32, 64, 128, 256, 512, 1024):
    im.resize((s, s), Image.LANCZOS).save(f"build_icon/icon.iconset/tmp_{s}.png")
import os, shutil
d = "build_icon/icon.iconset"
for name, s in (("icon_16x16",16),("icon_16x16@2x",32),("icon_32x32",32),("icon_32x32@2x",64),("icon_128x128",128),
                ("icon_128x128@2x",256),("icon_256x256",256),("icon_256x256@2x",512),("icon_512x512",512),("icon_512x512@2x",1024)):
    shutil.copy(f"{d}/tmp_{s}.png", f"{d}/{name}.png")
for f in os.listdir(d):
    if f.startswith("tmp_"): os.remove(f"{d}/{f}")
PYEOF
iconutil -c icns build_icon/icon.iconset -o build_icon/icon.icns

rm -rf build dist
pyinstaller --noconfirm --clean --windowed --name "截圖朗讀小幫手" \
  --icon build_icon/icon.icns \
  --osx-bundle-identifier tw.edu.local.screenocrreader \
  --add-data "assets/icon.ico:." \
  --collect-data rapidocr --collect-submodules rapidocr \
  --hidden-import gtts --collect-submodules gtts \
  --hidden-import miniaudio --hidden-import _miniaudio --hidden-import _cffi_backend \
  --hidden-import AppKit --hidden-import Foundation --hidden-import Quartz --hidden-import objc \
  --exclude-module torch --exclude-module paddle --exclude-module openvino --exclude-module tensorrt \
  src/screen_ocr_reader.py

# 設成「選單列小程式」（不在 Dock 顯示），並寫上權限說明
PLIST="dist/截圖朗讀小幫手.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Add :LSUIElement bool true" "$PLIST" 2>/dev/null || /usr/libexec/PlistBuddy -c "Set :LSUIElement true" "$PLIST"
/usr/libexec/PlistBuddy -c "Add :NSHighResolutionCapable bool true" "$PLIST" 2>/dev/null || true
/usr/libexec/PlistBuddy -c "Add :CFBundleDisplayName string 截圖朗讀小幫手" "$PLIST" 2>/dev/null || true
# 簽一個本機用的簽章（沒有 Apple 開發者帳號時用），讓「螢幕錄製」權限比較穩定
codesign --force --deep --sign - "dist/截圖朗讀小幫手.app" || true

"dist/截圖朗讀小幫手.app/Contents/MacOS/截圖朗讀小幫手" --selftest dist/selftest.txt || true
cat dist/selftest.txt
echo
echo "完成：dist/截圖朗讀小幫手.app"
