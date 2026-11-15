# 交接說明：截圖朗讀小幫手（給本機端的 Claude）

> 寫於 2026-11-15，由雲端專案「雜務專區」的 Claude 交接。使用者 andice 是國小老師，不寫程式、只會按按鍵，請用簡單的中文和他溝通，需要他做的事要給「一步一步、點哪裡」的說明。

## 1. 這是什麼

一個 Windows 桌面小工具：一個**永遠浮在螢幕最上層**的小視窗，框選螢幕上任何地方 → 離線 OCR 辨識文字（繁體中文為主，也支援簡中、英文）→ 用 Windows 內建語音念出來。使用者要邊看其他程式邊截圖朗讀。

使用者已試用 v1.0.2，回饋：「還滿適合我說的方向的」。目前沒有已知錯誤、沒有待辦的修改要求，下一步等使用者提出想調整的地方。

## 2. 檔案在哪裡

| 東西 | 位置 |
|---|---|
| **原始碼（這個資料夾）** | `H:\我的雲端硬碟\claude\雜務專區\截圖朗讀小幫手\`（Google 雲端硬碟同步資料夾） |
| 使用者已下載的 exe | 檔名 `ScreenOCRReader.exe`（沒改名），放在**桌面**。使用者不確定是 OneDrive 的桌面（`%USERPROFILE%\OneDrive\桌面` 或 `...\OneDrive\Desktop`）還是一般桌面（`C:\Users\<使用者>\Desktop`），請自己找，例如 PowerShell：`Get-ChildItem $env:USERPROFILE -Recurse -Filter ScreenOCRReader.exe -ErrorAction SilentlyContinue -Depth 3` |
| GitHub repo | https://github.com/andice0107/screen-ocr-reader （和這個資料夾內容相同，commit `dce5221`） |
| 使用者設定檔 | `%APPDATA%\ScreenOCRReader\settings.json`；當機記錄 `%APPDATA%\ScreenOCRReader\error.log` |

注意：桌面上的 exe 只是打包好的成品，**沒辦法拿來改**；要修改請改這個資料夾裡的 `src\screen_ocr_reader.py`，再重新打包。

使用者**不想再從 GitHub 下載**（太慢），之後請在本機改、本機打包、直接把新 exe 給他（可以覆蓋桌面上那個，覆蓋前先確認程式已關閉）。GitHub 上的雲端打包流程保留著，但不需要用。

## 3. 資料夾內容

```
截圖朗讀小幫手\
├─ src\screen_ocr_reader.py   ← 全部程式都在這一個檔案（約 830 行）
├─ assets\icon.ico           ← 圖示（藍底白字「讀」）
├─ requirements.txt          ← Python 套件
├─ run_dev.bat               ← 不打包、直接執行原始碼（改完測試最快）
├─ build_local.bat           ← 本機打包成 dist\ScreenOCRReader.exe，並跑自我檢查
├─ .github\workflows\build-windows.yml  ← 雲端打包（GitHub Actions，可不用）
├─ README.md                 ← 給使用者的使用說明
└─ HANDOFF.md                ← 本文件
```

## 4. 技術選型（已經驗證過，別輕易換）

- **語言/介面**：Python 3.12 + tkinter（ttk）。
- **截圖**：`mss`，先拍整個虛擬桌面（支援多螢幕），再用全螢幕半透明 Toplevel 讓使用者拖曳框選；框內顯示原亮度。程式啟動時呼叫 `SetProcessDpiAwareness(2)`，高 DPI 螢幕座標才不會偏。
- **OCR**：`rapidocr==3.10.0`（PP-OCRv6 模型，**模型已內含在 wheel 裡，不用連網下載**）+ onnxruntime。實測繁體中文非常準（「國語課本第三課：龜兔賽跑，烏龜慢慢爬」「與眾不同」「鬱悶」全對）。
  - 曾試過 `rapidocr_onnxruntime 1.4.4`（PP-OCRv4），繁體字會漏字（龜、眾），**不要退回舊版**。
  - 沒用 Windows 內建 OCR（winrt），因為使用者電腦不一定裝了中文 OCR 語言套件。
  - 小圖會自動放大 2 倍再辨識；辨識結果依座標排成行，中文字之間不加空格。
- **朗讀**：pywin32 呼叫 `SAPI.SpVoice`（非同步 + PURGE 可隨時停止）。另外用 `SpObjectTokenCategory.SetId("HKLM\SOFTWARE\Microsoft\Speech_OneCore\Voices")` 把 Windows 10/11 的新版語音（Hanhan、Yating、Zhiwei）也列進選單。預設優先選中文（台灣）語音；若電腦沒有中文語音，狀態列會提示去「設定→時間與語言→語音」新增。
  - 朗讀前把中文行與行之間的換行接起來，避免一行一行斷著念。
- **全域快捷鍵**：ctypes `RegisterHotKey`（背景執行緒跑訊息迴圈）。`Ctrl+Alt+R` 框選截圖、`Ctrl+Alt+E` 同一區重截。被占用時狀態列會提示。
- **其他**：單一執行個體（Mutex）；設定自動存 `%APPDATA%\ScreenOCRReader\settings.json`（語音、語速、音量、透明度、自動朗讀、顯示文字、視窗位置）；OCR 在背景執行緒，結果用 queue + `after()` 回到主執行緒。

## 5. 介面

小視窗按鈕：`📷 框選截圖`、`↻ 同一區`（重截上次範圍，翻頁用）、`🔊 朗讀`（文字框有反白就只念反白那段）、`⏹ 停止`、`⚙`（展開選項：語音、語速、音量、透明度、截圖後自動朗讀、顯示辨識文字）。下方是可編輯的文字框和狀態列。

## 6. 怎麼在本機改與打包

1. 電腦需要 **Python 3.12**（https://www.python.org/downloads/ ，安裝時勾「Add python.exe to PATH」）。先用 `py -3.12 --version` 檢查；沒有就請使用者安裝或你用 winget：`winget install Python.Python.3.12`。
   - 不要用 3.13 以上，部分套件（onnxruntime 等）相容性較不保險；目前已驗證的是 3.12。
2. **改完程式快速測試**：雙擊 `run_dev.bat`（第一次會建 `.venv` 並安裝套件，約幾分鐘；之後秒開）。
3. **打包成 exe**：雙擊 `build_local.bat` → 產出 `dist\ScreenOCRReader.exe`（約 120 MB），並自動跑 `--selftest`，`selftest.txt` 最後一行應是 `RESULT: PASS`。
   - 建議把 `.venv`、`build`、`dist` 放在本機磁碟而不是 Google 雲端硬碟（H:）同步資料夾內，否則同步會很慢；必要時把整個資料夾複製到 `C:\dev\screen-ocr-reader` 再打包。
4. 把新的 exe 複製到使用者桌面（覆蓋舊的前先關掉程式）。

自我檢查指令：`ScreenOCRReader.exe --selftest 結果.txt`（會測 OCR、截圖、語音清單、tkinter）。

## 7. 已知事項 / 可能的下一步

- exe 沒有數位簽章，第一次開啟 Windows 會跳「已保護您的電腦」→「其他資訊」→「仍要執行」。
- onefile 啟動要先解壓，第一次開會等幾秒。若使用者嫌慢，可改用 `--onedir`（資料夾版，啟動較快）。
- 使用者曾問過 **Mac 版**：可做（.app 免安裝，但第一次要右鍵「打開」並允許「螢幕錄製」權限），已約定「先用 Windows 版，之後再說」。Mac 版需改：截圖權限、快捷鍵（不能用 RegisterHotKey）、語音改用 macOS `say` / NSSpeechSynthesizer。
- 可能的加強方向（使用者尚未要求）：線上自然語音（edge-tts）、朗讀時逐句標示、念完自動截下一頁。
