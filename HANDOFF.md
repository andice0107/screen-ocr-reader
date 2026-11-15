# 交接說明：截圖朗讀小幫手（給之後接手的 Claude）

> 最後更新：2026-11-16，v1.6.1。
> 使用者 andice 是國小老師，不寫程式。請用簡單的中文、一步一步說明；**先等他說要改什麼再動工**。

## 現況與規則

- **GitHub 只放原始碼與記錄檔，不放軟體本體（exe）**。舊的 v1.0.x Releases 與雲端打包流程（GitHub Actions）已刪除，請不要再加回來。
- 成品 exe 只放在使用者的專案資料夾：
  `G:\我的雲端硬碟\claude\雜務專區\MSI Claw 8 AI+\生成電腦版軟體-截圖文字辨識報讀程式\`
  （裡面有 `ScreenOCRReader.exe`、`使用說明.md`、`原始碼\` 備份）
- 本機開發資料夾：`C:\dev\screen-ocr-reader`（含 `.venv`；不要在 G: 雲端硬碟裡打包，檔案太多同步很慢）
- 使用者**平常執行的是 `C:\Program Files\ScreenOCRReader.exe`**（他自己放的；要管理員權限才能覆蓋，所以由他自己複製取代）。每次出新版要提醒他換。
- 使用者桌面 `C:\Users\s9040\OneDrive\桌面\ScreenOCRReader.exe` 是舊的 v1.0，使用者沒要求覆蓋。
- 「上傳到兩端」＝ GitHub ＋ 專案資料夾（`使用說明.md`、`原始碼\` 要和 git 追蹤的檔案一致）。
- 覆蓋 exe 前，先請使用者「完全關閉程式」（右下角圖示右鍵 →「✖ 完全關閉程式」），不要強制結束他的程式。

## 這是什麼

Windows 桌面小工具：框選螢幕任何地方 → 離線 OCR（繁中為主）→ 念出來，念到哪字亮到哪。功能細節見 `README.md`（給使用者看的說明）。

## 檔案

```
src\screen_ocr_reader.py   ← 全部程式（約 2400 行，APP_VERSION 在檔頭）
assets\icon.ico            ← 圖示（藍底白字「讀」）
requirements.txt           ← Python 套件
run_dev.bat                ← 不打包直接執行（測試用）
build_local.bat            ← 本機打包（最後有 pause；Claude 可直接跑裡面的 pyinstaller 指令）
README.md                  ← 使用說明（複製到專案資料夾時改名「使用說明.md」）
HANDOFF.md                 ← 本文件
```

## 打包與檢查

1. Python 3.12（不要用 3.13+）。`.venv` 在 `C:\dev\screen-ocr-reader\.venv`。
2. 跑 `build_local.bat` 的 pyinstaller 參數 → `dist\ScreenOCRReader.exe`（約 120 MB）。
3. `dist\ScreenOCRReader.exe --selftest selftest.txt`，最後一行要是 `RESULT: PASS`（會測 OCR、多音字、截圖、語音、Google 語音下載解碼、系統匣、快捷鍵、tkinter）。
4. 複製到專案資料夾，並同步 `使用說明.md` 與 `原始碼\`。

## 技術重點（都驗證過，別輕易換）

- **介面**：Python 3.12 + tkinter/ttk；`SetProcessDpiAwareness(2)`；單一執行個體（Mutex）。
- **OCR**：`rapidocr==3.10.0`（PP-OCRv6，模型內含）+ onnxruntime。舊版 PP-OCRv4 繁體會漏字，不要退回。
- **截圖**：mss 拍整個虛擬桌面，再用全螢幕 Toplevel 框選。
- **文字方向**：`OcrEngine.recognize()` 回傳 `(top, left, height, text, width)`，排序在 `to_text(items, vertical)`：橫式依行；直式依欄（框中心 x 落在同一欄就合併），欄由右到左、欄內由上到下。RapidOCR 會自動把直長框轉 90° 辨識，直書辨識率實測良好。`App.last_items` 記住上次結果，切換方向時若文字框沒被改過就直接重排。設定 `read_dir`（h/v）。
- **內建語音**：pywin32 `SAPI.SpVoice`，含 OneCore 語音（Hanhan、Yating、Zhiwei）。跟讀標示靠輪詢 `Status.InputWordPosition/Length`（`SVSF_NOT_XML`）。SAPI 語速只在下一句生效，所以即時改語速是「從目前的字重新開始念」（250 ms 去抖動）。
- **Google 國語（台灣）**：gTTS（`lang="zh-TW"`）取 mp3 → miniaudio 解碼成 24 kHz float32 → `miniaudio.PlaybackDevice` 用 generator 播放。
  - **變速用 WSOLA**（N=960、HS=480、TOL=240），音高不變（以前用 MCI 調速會變「花栗鼠聲」，使用者不接受）。速度倍率 `2**(rate/10)`。
  - 音量平滑漸變、可即時調整；跟讀標示依每段語音能量累積估算到字。
  - 沒網路自動退回內建語音。
- **多音字**：`PronBook`（`pronunciations.json`），用同音字替換。`prepare_speech()` 會產生要念的文字和對照回原文的索引表，供跟讀標示使用。
- **全域快捷鍵**：`RegisterHotKey` 在自己的執行緒跑訊息迴圈，用 `PostThreadMessageW`（WM_APP 0x8001）+ queue 傳指令；`scan()` 試註冊來偵測已被占用的組合。
  - 預設快捷鍵（Ctrl+Alt+…）：

    | 鍵 | 功能 |
    |---|---|
    | R | 截圖 |
    | E | 同一區 |
    | S | 停止 |
    | P | 暫停／播放 |
    | ←／→ | 語速 |
    | ↑／↓ | 文字大小 |

  - Ctrl+Alt+空白鍵在使用者電腦被不明程式占用（不是 PowerToys），所以暫停用 P。
- **模式**：`App.mode` = `"panel"`（完整面板）／`"mini"`（浮動圖案 `MiniIcon`）／`"tray"`（只剩系統匣，pystray win32）。面板 × 是收到系統匣，「完全關閉程式」才真的結束。
- **透明度**：可到 1%。40% 以下顯示小紅點 `RedDot`：alpha 在 0.03～1 之間「一顯一透」呼吸（2.4 秒一輪），滑鼠移上去視窗暫時變清楚。
- **每次啟動固定預設**：浮動圖案、透明度 100%、音量 100%、語速 0、Google 國語（台灣）。其餘設定（快捷鍵、文字大小、位置、多音字…）會記住。
- 資料：`%APPDATA%\ScreenOCRReader\` 裡的 `settings.json`、`pronunciations.json`、`error.log`。

## 版本紀錄

| 版本 | 內容 |
|---|---|
| v1.0.x | 雲端初版：框選截圖、離線 OCR、SAPI 朗讀 |
| v1.1 | 跟讀標示、Google 國語、可自訂快捷鍵（偵測占用）、浮動小圖案與右鍵選單、多音字唸法 |
| v1.2 | 透明度到 1% + 呼吸小紅點、面板常駐快捷鍵說明、設定條移到文字框下方、文字大小、WSOLA 不變調變速、語速音量即時生效、右鍵功能全進面板 |
| v1.3 | 小紅點改一顯一透、× 收到系統匣、「完全關閉程式」、啟動固定預設、語速快捷鍵 |
| v1.4 | 點字就從那個字開始念、語速改 Ctrl+Alt+←→、文字大小 Ctrl+Alt+↑↓ |
| v1.5 | 暫停／播放快捷鍵 Ctrl+Alt+P、快捷鍵可選空白鍵、系統匣選單加暫停／播放 |
| v1.6 | 文字方向可選橫式／直式（直式：上→下、右→左），面板、浮動圖案右鍵、系統匣都能切換 |
| v1.6.1 | 點字開始念改成要按住 Ctrl（一般點一下只放游標，方便編輯文字） |

## 已知事項

- exe 沒有數位簽章，第一次開啟要按「其他資訊」→「仍要執行」。onefile 啟動要等幾秒。
- 使用者問過 Mac 版，約定之後再說（需改截圖權限、快捷鍵、語音）。
