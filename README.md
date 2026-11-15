# 截圖朗讀小幫手（Screen OCR Reader）

一個**永遠浮在螢幕最上層**的小視窗。框選螢幕上任何地方（任何程式、網頁、PDF、影片字幕都可以），它會辨識裡面的文字，並用 Windows 內建語音念給你聽。

- 免安裝：下載 `ScreenOCRReader.exe`，雙擊就能用
- 離線辨識：不用網路，支援**繁體中文**、簡體中文、英文
- 適用 Windows 10 / 11（64 位元）

## 下載

到 [Releases](../../releases/latest) 下載最新的 **ScreenOCRReader.exe**。

第一次開啟若出現「Windows 已保護您的電腦」，請按「其他資訊」→「仍要執行」。（因為這個程式沒有購買數位簽章，不是病毒。）

第一次啟動需要幾秒鐘解壓縮，請稍等。

## 使用方式

| 動作 | 按鈕 | 快捷鍵（在任何程式中都能按） |
|---|---|---|
| 框選螢幕範圍並朗讀 | 📷 框選截圖 | **Ctrl + Alt + R** |
| 同一個範圍再截一次並朗讀（例如翻頁後） | ↻ 同一區 | **Ctrl + Alt + E** |
| 重新朗讀文字框裡的文字 | 🔊 朗讀 | |
| 停止朗讀 | ⏹ 停止 | |

- 框選時：按住滑鼠左鍵拖曳；按 **Esc** 或滑鼠右鍵取消。
- 辨識出的文字會顯示在文字框裡，可以直接修改；**反白一段文字再按「朗讀」**就只念那一段。
- 按 ⚙ 可以選擇語音、調整語速、音量、視窗透明度、是否自動朗讀。設定會自動記住。

## 語音

使用 Windows 內建的中文語音（如 Hanhan、Yating、Zhiwei）。如果下拉選單裡沒有中文語音，請到
「設定 → 時間與語言 → 語音 → 新增語音」，加入「中文（台灣）」。

## 給開發者

- 程式：`src/screen_ocr_reader.py`（Python + tkinter；OCR 用 [RapidOCR](https://github.com/RapidAI/RapidOCR) / PP-OCR 模型；截圖用 mss；朗讀用 Windows SAPI）
- 每次推到 `main`，GitHub Actions 會在 Windows 上用 PyInstaller 打包成單一 exe，跑自我檢查後發佈到 Releases。
- 自我檢查：`ScreenOCRReader.exe --selftest result.txt`
