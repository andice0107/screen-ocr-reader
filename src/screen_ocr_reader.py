# -*- coding: utf-8 -*-
"""截圖朗讀小幫手 (Screen OCR Reader)

一個永遠浮在最上層的小視窗：
  1. 按「框選截圖」或全域快捷鍵 Ctrl+Alt+R，框選螢幕上任何地方
  2. 用離線 OCR（RapidOCR / PP-OCR，支援繁體中文、簡體中文、英文）辨識文字
  3. 用 Windows 內建語音朗讀出來

執行 `screen_ocr_reader.exe --selftest 結果.txt` 會做一次自我檢查（打包測試用）。
"""

import ctypes
import json
import os
import queue
import re
import sys
import threading
import time
import traceback

APP_NAME = "截圖朗讀小幫手"
APP_VERSION = "1.0.0"
HOTKEY_TEXT = "Ctrl+Alt+R"
REPEAT_HOTKEY_TEXT = "Ctrl+Alt+E"

IS_WINDOWS = sys.platform == "win32"


# ---------------------------------------------------------------------------
# 基本設定
# ---------------------------------------------------------------------------

def set_dpi_aware():
    """讓座標以實際像素計算，高解析度螢幕截圖才不會偏移。"""
    if not IS_WINDOWS:
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def settings_path():
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "ScreenOCRReader")
    os.makedirs(folder, exist_ok=True)
    return os.path.join(folder, "settings.json")


DEFAULT_SETTINGS = {
    "voice": "",
    "rate": 0,
    "volume": 100,
    "auto_read": True,
    "show_text": True,
    "alpha": 1.0,
    "x": None,
    "y": None,
}


def load_settings():
    data = dict(DEFAULT_SETTINGS)
    try:
        with open(settings_path(), "r", encoding="utf-8") as f:
            data.update(json.load(f))
    except Exception:
        pass
    return data


def save_settings(data):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# OCR：RapidOCR (PP-OCR 模型，離線、內建於程式中)
# ---------------------------------------------------------------------------

_CJK = r"\u2e80-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef"


class OcrEngine:
    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()

    def load(self):
        with self._lock:
            if self._engine is None:
                import logging
                from rapidocr import RapidOCR

                self._engine = RapidOCR()
                for name in ("RapidOCR", "rapidocr"):
                    logging.getLogger(name).setLevel(logging.WARNING)
        return self._engine

    def recognize(self, pil_image):
        """回傳 [(top, left, height, text), ...]（已依閱讀順序排列）。"""
        import numpy as np
        from PIL import Image

        img = pil_image.convert("RGB")
        # 小字放大，辨識率會好很多
        w, h = img.size
        scale = 1
        if min(w, h) < 60 or max(w, h) < 400:
            scale = 2
        if scale != 1:
            img = img.resize((w * scale, h * scale), Image.LANCZOS)

        engine = self.load()
        result = engine(np.array(img))
        boxes = getattr(result, "boxes", None)
        txts = getattr(result, "txts", None)
        if boxes is None or txts is None:
            return []
        items = []
        for box, text in zip(boxes, txts):
            text = (text or "").strip()
            if not text:
                continue
            ys = [p[1] for p in box]
            xs = [p[0] for p in box]
            items.append((min(ys), min(xs), max(ys) - min(ys), text))
        return items

    @staticmethod
    def to_text(items):
        """把辨識出的文字框依「行」整理成文字。"""
        if not items:
            return ""
        items = sorted(items, key=lambda t: (t[0], t[1]))
        lines = []
        for top, left, height, text in items:
            if lines:
                last = lines[-1]
                mid = top + height / 2
                if last["top"] <= mid <= last["top"] + last["height"]:
                    last["parts"].append((left, text))
                    continue
            lines.append({"top": top, "height": height, "parts": [(left, text)]})
        out = []
        for line in lines:
            parts = [t for _, t in sorted(line["parts"])]
            joined = ""
            for p in parts:
                if joined and not (re.search(f"[{_CJK}]$", joined) or re.match(f"^[{_CJK}]", p)):
                    joined += " "
                joined += p
            out.append(joined)
        return "\n".join(out)


def text_for_speech(text):
    """朗讀前整理：中文行與行之間直接相連，避免一行一行斷開念。"""
    text = text.replace("\r", "")
    text = re.sub(f"(?<=[{_CJK}])\\n(?=[{_CJK}])", "", text)
    text = re.sub(r"\n+", "。\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 語音：Windows SAPI（含 Windows 10/11 新版的 OneCore 語音，如 Yating、Hanhan、Zhiwei）
# ---------------------------------------------------------------------------

class Speaker:
    SVSF_ASYNC = 1
    SVSF_PURGE = 2

    def __init__(self):
        self.voice = None
        self.tokens = {}  # 顯示名稱 -> token
        self.error = None
        if not IS_WINDOWS:
            self.error = "只支援 Windows"
            return
        try:
            import pythoncom  # noqa: F401
            import win32com.client

            self.voice = win32com.client.Dispatch("SAPI.SpVoice")
            self._collect_voices(win32com.client)
        except Exception as e:  # pragma: no cover
            self.error = str(e)

    def _collect_voices(self, wc):
        def add(token):
            try:
                name = token.GetDescription()
            except Exception:
                return
            if name and name not in self.tokens:
                self.tokens[name] = token

        # Windows 10/11 新版語音（OneCore），通常比較自然
        for path in (
            r"HKEY_LOCAL_MACHINE\SOFTWARE\Microsoft\Speech_OneCore\Voices",
            r"HKEY_LOCAL_MACHINE\SOFTWARE\WOW6432Node\Microsoft\Speech_OneCore\Voices",
        ):
            try:
                cat = wc.Dispatch("SAPI.SpObjectTokenCategory")
                cat.SetId(path, False)
                toks = cat.EnumerateTokens()
                for i in range(toks.Count):
                    add(toks.Item(i))
            except Exception:
                pass
        # 傳統 SAPI5 語音
        try:
            toks = self.voice.GetVoices()
            for i in range(toks.Count):
                add(toks.Item(i))
        except Exception:
            pass

    def voice_names(self):
        def lang(token):
            try:
                return token.GetAttribute("Language").lower()
            except Exception:
                return ""

        def rank(name):
            l = lang(self.tokens[name])
            if "404" in l.split(";"):
                return 0  # 中文（台灣）
            if "c04" in l or "804" in l:
                return 1  # 中文（香港/中國）
            return 2

        return sorted(self.tokens, key=lambda n: (rank(n), n))

    def has_chinese_voice(self):
        for token in self.tokens.values():
            try:
                langs = token.GetAttribute("Language").lower().split(";")
            except Exception:
                continue
            if any(l in ("404", "804", "c04", "1004", "1404") for l in langs):
                return True
        return False

    def default_voice(self):
        names = self.voice_names()
        for prefer in ("Yating", "Hanhan", "Zhiwei", "HsiaoChen", "雅婷", "涵涵", "志偉"):
            for n in names:
                if prefer.lower() in n.lower():
                    return n
        return names[0] if names else ""

    def speak(self, text, voice_name="", rate=0, volume=100):
        if not self.voice:
            raise RuntimeError(self.error or "找不到語音引擎")
        token = self.tokens.get(voice_name)
        if token is not None:
            try:
                self.voice.Voice = token
            except Exception:
                pass
        self.voice.Rate = int(max(-10, min(10, rate)))
        self.voice.Volume = int(max(0, min(100, volume)))
        self.voice.Speak(text, self.SVSF_ASYNC | self.SVSF_PURGE)

    def stop(self):
        if self.voice:
            try:
                self.voice.Speak("", self.SVSF_ASYNC | self.SVSF_PURGE)
            except Exception:
                pass

    def is_speaking(self):
        if not self.voice:
            return False
        try:
            return self.voice.Status.RunningState == 2
        except Exception:
            return False


# ---------------------------------------------------------------------------
# 全域快捷鍵（不管目前在哪個程式都有效）
# ---------------------------------------------------------------------------

class GlobalHotkeys(threading.Thread):
    MOD_ALT = 0x0001
    MOD_CONTROL = 0x0002
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312

    def __init__(self, bindings, on_hotkey):
        """bindings: {id: (vk_code)}，全部使用 Ctrl+Alt。"""
        super().__init__(daemon=True)
        self.bindings = bindings
        self.on_hotkey = on_hotkey
        self.failed = []
        self.ready = threading.Event()

    def run(self):
        if not IS_WINDOWS:
            self.ready.set()
            return
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        mods = self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT
        for hid, vk in self.bindings.items():
            if not user32.RegisterHotKey(None, hid, mods, vk):
                self.failed.append(hid)
        self.ready.set()
        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == self.WM_HOTKEY:
                self.on_hotkey(int(msg.wParam))


# ---------------------------------------------------------------------------
# 截圖：先拍下整個（多螢幕）桌面，再讓使用者框選範圍
# ---------------------------------------------------------------------------

def _mss():
    import mss

    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def grab_virtual_screen():
    from PIL import Image

    with _mss() as sct:
        mon = sct.monitors[0]  # 所有螢幕合併的虛擬桌面
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        return img, mon["left"], mon["top"]


def grab_rect(rect):
    from PIL import Image

    left, top, right, bottom = rect
    with _mss() as sct:
        shot = sct.grab({"left": left, "top": top, "width": right - left, "height": bottom - top})
        return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")


class RegionSelector:
    """全螢幕半透明遮罩，讓使用者拖曳框選範圍。"""

    def __init__(self, root, on_done):
        import tkinter as tk
        from PIL import ImageEnhance, ImageTk

        self.root = root
        self.on_done = on_done
        self.full, self.ox, self.oy = grab_virtual_screen()
        w, h = self.full.size

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{w}x{h}+{self.ox}+{self.oy}")
        self.win.configure(cursor="crosshair")

        dim = ImageEnhance.Brightness(self.full).enhance(0.55)
        self.bg = ImageTk.PhotoImage(dim)
        self.canvas = tk.Canvas(self.win, width=w, height=h, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.create_image(0, 0, anchor="nw", image=self.bg)
        self.crop_item = None
        self.crop_photo = None
        self.rect = None
        self.start = None

        # 在滑鼠所在的螢幕上顯示提示
        px, py = root.winfo_pointerxy()
        tip = "拖曳滑鼠框選要朗讀的範圍　（Esc 或右鍵 = 取消）"
        tx, ty = px - self.ox, max(30, py - self.oy - 60)
        self.canvas.create_rectangle(tx - 260, ty - 20, tx + 260, ty + 20, fill="#222", outline="#ffcc00")
        self.canvas.create_text(tx, ty, text=tip, fill="white", font=("Microsoft JhengHei UI", 14, "bold"))

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<ButtonPress-3>", lambda e: self._finish(None))
        self.win.bind("<Escape>", lambda e: self._finish(None))
        self.win.after(50, self._grab_focus)

    def _grab_focus(self):
        try:
            self.win.focus_force()
            self.win.grab_set()
        except Exception:
            pass

    def _press(self, e):
        self.start = (e.x, e.y)
        if self.rect:
            self.canvas.delete(self.rect)
        self.rect = self.canvas.create_rectangle(e.x, e.y, e.x, e.y, outline="#ff3b30", width=3)

    def _drag(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        self.canvas.coords(self.rect, x0, y0, e.x, e.y)
        self._show_crop(x0, y0, e.x, e.y)

    def _show_crop(self, x0, y0, x1, y1):
        """框選範圍內顯示原本亮度，方便看清楚。"""
        from PIL import ImageTk

        l, t, r, b = min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)
        if r - l < 2 or b - t < 2:
            return
        self.crop_photo = ImageTk.PhotoImage(self.full.crop((l, t, r, b)))
        if self.crop_item is None:
            self.crop_item = self.canvas.create_image(l, t, anchor="nw", image=self.crop_photo)
        else:
            self.canvas.coords(self.crop_item, l, t)
            self.canvas.itemconfigure(self.crop_item, image=self.crop_photo)
        self.canvas.tag_raise(self.rect)

    def _release(self, e):
        if not self.start:
            return
        x0, y0 = self.start
        l, t, r, b = min(x0, e.x), min(y0, e.y), max(x0, e.x), max(y0, e.y)
        if r - l < 5 or b - t < 5:
            self.start = None
            return
        img = self.full.crop((l, t, r, b))
        rect = (l + self.ox, t + self.oy, r + self.ox, b + self.oy)
        self._finish((img, rect))

    def _finish(self, result):
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()
        self.on_done(result)


# ---------------------------------------------------------------------------
# 主視窗
# ---------------------------------------------------------------------------

class App:
    HK_CAPTURE = 1
    HK_REPEAT = 2

    def __init__(self):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.settings = load_settings()
        self.ocr = OcrEngine()
        self.speaker = Speaker()
        self.events = queue.Queue()
        self.last_rect = None
        self.busy = False

        root = self.root = tk.Tk()
        root.title(APP_NAME)
        root.attributes("-topmost", True)
        root.minsize(300, 120)
        try:
            root.iconbitmap(default=resource_path("icon.ico"))
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        font = ("Microsoft JhengHei UI", 11)
        big = ("Microsoft JhengHei UI", 12, "bold")
        style.configure("Big.TButton", font=big, padding=(8, 6))
        style.configure("TButton", font=font)
        style.configure("TLabel", font=font)
        style.configure("TCheckbutton", font=font)

        # --- 按鈕列 ---
        bar = ttk.Frame(root, padding=(6, 6, 6, 2))
        bar.pack(fill="x")
        self.btn_capture = ttk.Button(bar, text="📷 框選截圖", style="Big.TButton", command=self.start_capture)
        self.btn_capture.pack(side="left")
        self.btn_repeat = ttk.Button(bar, text="↻ 同一區", command=self.repeat_capture, state="disabled")
        self.btn_repeat.pack(side="left", padx=(4, 0))
        self.btn_read = ttk.Button(bar, text="🔊 朗讀", command=self.read_text)
        self.btn_read.pack(side="left", padx=(4, 0))
        self.btn_stop = ttk.Button(bar, text="⏹ 停止", command=self.speaker.stop)
        self.btn_stop.pack(side="left", padx=(4, 0))
        self.btn_more = ttk.Button(bar, text="⚙", width=3, command=self.toggle_options)
        self.btn_more.pack(side="right")

        # --- 選項（可收合）---
        self.options = ttk.Frame(root, padding=(6, 2, 6, 2))
        r = 0
        ttk.Label(self.options, text="語音").grid(row=r, column=0, sticky="w")
        names = self.speaker.voice_names()
        self.voice_var = tk.StringVar(value=self.settings.get("voice") or self.speaker.default_voice())
        if names and self.voice_var.get() not in names:
            self.voice_var.set(self.speaker.default_voice())
        self.voice_box = ttk.Combobox(self.options, textvariable=self.voice_var, values=names, state="readonly", width=34)
        self.voice_box.grid(row=r, column=1, columnspan=2, sticky="we", padx=4, pady=2)
        r += 1
        ttk.Label(self.options, text="語速").grid(row=r, column=0, sticky="w")
        self.rate_var = tk.IntVar(value=int(self.settings.get("rate", 0)))
        self.rate_label = ttk.Label(self.options, width=4)
        tk.Scale(self.options, from_=-10, to=10, orient="horizontal", showvalue=False, variable=self.rate_var,
                 command=lambda v: self._update_labels()).grid(row=r, column=1, sticky="we", padx=4)
        self.rate_label.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.options, text="音量").grid(row=r, column=0, sticky="w")
        self.vol_var = tk.IntVar(value=int(self.settings.get("volume", 100)))
        self.vol_label = ttk.Label(self.options, width=4)
        tk.Scale(self.options, from_=0, to=100, orient="horizontal", showvalue=False, variable=self.vol_var,
                 command=lambda v: self._update_labels()).grid(row=r, column=1, sticky="we", padx=4)
        self.vol_label.grid(row=r, column=2, sticky="w")
        r += 1
        ttk.Label(self.options, text="透明度").grid(row=r, column=0, sticky="w")
        self.alpha_var = tk.DoubleVar(value=float(self.settings.get("alpha", 1.0)))
        tk.Scale(self.options, from_=0.4, to=1.0, resolution=0.05, orient="horizontal", showvalue=False,
                 variable=self.alpha_var, command=lambda v: root.attributes("-alpha", float(v))).grid(
            row=r, column=1, sticky="we", padx=4)
        r += 1
        self.auto_var = tk.BooleanVar(value=bool(self.settings.get("auto_read", True)))
        ttk.Checkbutton(self.options, text="截圖後自動朗讀", variable=self.auto_var).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        self.show_var = tk.BooleanVar(value=bool(self.settings.get("show_text", True)))
        ttk.Checkbutton(self.options, text="顯示辨識出的文字", variable=self.show_var,
                        command=self.toggle_text).grid(row=r, column=0, columnspan=2, sticky="w")
        r += 1
        ttk.Label(self.options, foreground="#666",
                  text=f"快捷鍵：{HOTKEY_TEXT} 框選截圖　{REPEAT_HOTKEY_TEXT} 同一區重念\n"
                       f"小技巧：在文字框中反白一段，再按「朗讀」只念那一段。\n版本 {APP_VERSION}").grid(
            row=r, column=0, columnspan=3, sticky="w", pady=(4, 0))
        self.options.columnconfigure(1, weight=1)
        self._update_labels()

        # --- 文字框 ---
        self.text_frame = ttk.Frame(root, padding=(6, 2, 6, 6))
        self.text = tk.Text(self.text_frame, wrap="word", height=6, width=40, font=("Microsoft JhengHei UI", 12), undo=True)
        sb = ttk.Scrollbar(self.text_frame, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        # --- 狀態列 ---
        self.status = ttk.Label(root, text="", foreground="#555", padding=(8, 0, 8, 4))
        self.status.pack(side="bottom", fill="x")
        self.toggle_text()

        root.attributes("-alpha", float(self.alpha_var.get()))
        if self.settings.get("x") is not None and self.settings.get("y") is not None:
            root.geometry(f"+{int(self.settings['x'])}+{int(self.settings['y'])}")
        root.protocol("WM_DELETE_WINDOW", self.quit)

        # 全域快捷鍵
        self.hotkeys = GlobalHotkeys({self.HK_CAPTURE: 0x52, self.HK_REPEAT: 0x45}, lambda hid: self.events.put(("hotkey", hid)))
        self.hotkeys.start()
        self.hotkeys.ready.wait(2)
        msg = f"按「框選截圖」或 {HOTKEY_TEXT} 開始"
        if self.hotkeys.failed:
            msg = "快捷鍵被其他程式占用，請用按鈕操作"
        if self.speaker.error:
            msg = "找不到語音引擎：" + self.speaker.error
        elif not self.speaker.has_chinese_voice():
            msg = "電腦沒有中文語音：請到 設定→時間與語言→語音 新增「中文(台灣)」"

        self.set_status(msg)

        # 背景先載入 OCR 模型，第一次截圖會比較快
        threading.Thread(target=self._preload, daemon=True).start()
        root.after(50, self._poll)

    # ---- 介面小工具 ----
    def _update_labels(self):
        self.rate_label.configure(text=str(self.rate_var.get()))
        self.vol_label.configure(text=str(self.vol_var.get()))

    def set_status(self, msg):
        self.status.configure(text=msg)

    def toggle_options(self):
        if self.options.winfo_ismapped():
            self.options.pack_forget()
        else:
            self.options.pack(fill="x", after=self.btn_capture.master)

    def toggle_text(self):
        if self.show_var.get():
            self.text_frame.pack(fill="both", expand=True, before=self.status)
        else:
            self.text_frame.pack_forget()
            self.root.geometry("")

    def _preload(self):
        try:
            self.ocr.load()
        except Exception as e:
            self.events.put(("error", f"OCR 載入失敗：{e}"))

    def _poll(self):
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "hotkey":
                    if payload == self.HK_CAPTURE:
                        self.start_capture()
                    elif payload == self.HK_REPEAT:
                        self.repeat_capture()
                elif kind == "ocr_done":
                    self._ocr_done(payload)
                elif kind == "error":
                    self.busy = False
                    self.set_status(payload)
        except queue.Empty:
            pass
        self.root.after(50, self._poll)

    # ---- 截圖流程 ----
    def start_capture(self):
        if self.busy:
            return
        self.busy = True
        self.speaker.stop()
        self.root.withdraw()  # 先把自己藏起來，免得被截進去
        self.root.after(200, self._open_selector)

    def _open_selector(self):
        try:
            RegionSelector(self.root, self._selected)
        except Exception as e:
            self.root.deiconify()
            self.busy = False
            self.set_status(f"截圖失敗：{e}")

    def _selected(self, result):
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        if result is None:
            self.busy = False
            self.set_status("已取消")
            return
        img, rect = result
        self.last_rect = rect
        self.btn_repeat.configure(state="normal")
        self._run_ocr(img)

    def repeat_capture(self):
        if self.busy or not self.last_rect:
            return
        self.busy = True
        self.speaker.stop()
        self.root.withdraw()
        self.root.after(150, self._grab_last)

    def _grab_last(self):
        try:
            img = grab_rect(self.last_rect)
        except Exception as e:
            img = None
            self.set_status(f"截圖失敗：{e}")
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        if img is None:
            self.busy = False
            return
        self._run_ocr(img)

    def _run_ocr(self, img):
        self.set_status("辨識文字中…")

        def work():
            try:
                t = time.time()
                text = OcrEngine.to_text(self.ocr.recognize(img))
                self.events.put(("ocr_done", (text, time.time() - t)))
            except Exception as e:
                self.events.put(("error", f"辨識失敗：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _ocr_done(self, payload):
        text, secs = payload
        self.busy = False
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        if not text.strip():
            self.set_status("沒有辨識到文字，請框大一點或換個範圍")
            return
        self.set_status(f"辨識完成（{secs:.1f} 秒），共 {len(text)} 字")
        if self.auto_var.get():
            self.read_text()

    # ---- 朗讀 ----
    def read_text(self):
        try:
            content = self.text.get("sel.first", "sel.last")
        except Exception:
            content = self.text.get("1.0", "end")
        content = text_for_speech(content)
        if not content:
            self.set_status("沒有可以朗讀的文字")
            return
        try:
            self.speaker.speak(content, self.voice_var.get(), self.rate_var.get(), self.vol_var.get())
        except Exception as e:
            self.set_status(f"朗讀失敗：{e}")

    def quit(self):
        self.speaker.stop()
        self.settings.update(
            voice=self.voice_var.get(),
            rate=self.rate_var.get(),
            volume=self.vol_var.get(),
            auto_read=self.auto_var.get(),
            show_text=self.show_var.get(),
            alpha=float(self.alpha_var.get()),
            x=self.root.winfo_x(),
            y=self.root.winfo_y(),
        )
        save_settings(self.settings)
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def resource_path(name):
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# ---------------------------------------------------------------------------
# 自我檢查（給雲端打包流程驗證 exe 用）
# ---------------------------------------------------------------------------

def selftest(out_path):
    lines = [f"{APP_NAME} {APP_VERSION} selftest"]
    ok = True
    try:
        from PIL import Image, ImageDraw, ImageFont

        sample_zh = "國語課本第三課：龜兔賽跑"
        sample_en = "Hello World 2026"
        img = Image.new("RGB", (700, 140), "white")
        d = ImageDraw.Draw(img)
        font = None
        for f in (r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\mingliu.ttc", r"C:\Windows\Fonts\msyh.ttc",
                  "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"):
            if os.path.exists(f):
                font = ImageFont.truetype(f, 32)
                break
        if font is None:
            font = ImageFont.load_default()
        d.text((10, 10), sample_zh, font=font, fill="black")
        d.text((10, 70), sample_en, font=font, fill="black")
        t = time.time()
        text = OcrEngine.to_text(OcrEngine().recognize(img))
        lines.append(f"OCR ({time.time() - t:.1f}s): {text!r}")
        if "Hello" not in text:
            ok = False
            lines.append("FAIL: english text not recognized")
        if "龜兔" not in text:
            lines.append("WARN: chinese text not recognized (font may be missing)")
    except Exception:
        ok = False
        lines.append("OCR ERROR:\n" + traceback.format_exc())

    try:
        import mss  # noqa: F401
        img, ox, oy = grab_virtual_screen()
        lines.append(f"Screen grab: {img.size} at ({ox},{oy})")
    except Exception:
        lines.append("SCREEN WARN:\n" + traceback.format_exc())

    try:
        sp = Speaker()
        lines.append(f"Voices: {sp.voice_names()} default={sp.default_voice()!r} error={sp.error!r}")
    except Exception:
        lines.append("TTS WARN:\n" + traceback.format_exc())

    try:
        import tkinter  # noqa: F401
        lines.append("tkinter OK")
    except Exception:
        ok = False
        lines.append("TK ERROR:\n" + traceback.format_exc())

    lines.append("RESULT: " + ("PASS" if ok else "FAIL"))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return 0 if ok else 1


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "--selftest":
        out = sys.argv[2] if len(sys.argv) >= 3 else "selftest.txt"
        sys.exit(selftest(out))

    set_dpi_aware()

    # 只允許開一個
    if IS_WINDOWS:
        ctypes.windll.kernel32.CreateMutexW(None, False, "ScreenOCRReader_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(None, "截圖朗讀小幫手已經在執行了。", APP_NAME, 0x40)
            return
    try:
        App().run()
    except Exception:
        err = traceback.format_exc()
        try:
            log = os.path.join(os.path.dirname(settings_path()), "error.log")
            with open(log, "w", encoding="utf-8") as f:
                f.write(err)
        except Exception:
            log = ""
        if IS_WINDOWS:
            ctypes.windll.user32.MessageBoxW(None, f"程式發生錯誤：\n{err[-800:]}\n\n詳細記錄：{log}", APP_NAME, 0x10)
        raise


if __name__ == "__main__":
    main()
