# -*- coding: utf-8 -*-
"""截圖朗讀小幫手 (Screen OCR Reader)

一個永遠浮在最上層的小視窗：
  1. 按「框選截圖」或全域快捷鍵（預設 Ctrl+Alt+R，可自訂），框選螢幕上任何地方
  2. 用離線 OCR（RapidOCR / PP-OCR，支援繁體中文、簡體中文、英文）辨識文字
  3. 用 Windows 內建語音或 Google 國語（台灣）語音朗讀，念到哪、字就亮到哪
  4. 可縮成一個浮動小圖案，右鍵有功能選單
  5. 可自訂多音字／破音字在不同語詞中的唸法

執行 `screen_ocr_reader.exe --selftest 結果.txt` 會做一次自我檢查（打包測試用）。
"""

import ctypes
import json
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import time
import traceback

APP_NAME = "截圖朗讀小幫手"
APP_VERSION = "1.6.1"

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
UI_FONT = "PingFang TC" if IS_MAC else "Microsoft JhengHei UI"
CLICK_READ_LABEL = "⌘ 或 Ctrl＋點字就從那裡開始念" if IS_MAC else "Ctrl＋點字就從那裡開始念"
# Tk 在 Mac 上：右鍵是 Button-2（有的版本是 3），兩個都接
RIGHT_BUTTONS = (2, 3) if IS_MAC else (3,)

GOOGLE_VOICE = "Google 國語（台灣）〔需要網路〕"


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


def data_dir():
    if IS_MAC:
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    folder = os.path.join(base, "ScreenOCRReader")
    os.makedirs(folder, exist_ok=True)
    return folder


def settings_path():
    return os.path.join(data_dir(), "settings.json")


def pron_path():
    return os.path.join(data_dir(), "pronunciations.json")


# 修飾鍵（和 Windows RegisterHotKey 的定義相同）
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8

HK_CAPTURE, HK_REPEAT, HK_STOP, HK_FASTER, HK_SLOWER, HK_FONT_UP, HK_FONT_DOWN, HK_PAUSE = 1, 2, 3, 4, 5, 6, 7, 8
HOTKEY_NAMES = {HK_CAPTURE: "框選截圖", HK_REPEAT: "同一區重截", HK_PAUSE: "暫停／播放", HK_STOP: "停止朗讀",
                HK_FASTER: "語速加快", HK_SLOWER: "語速放慢", HK_FONT_UP: "文字放大", HK_FONT_DOWN: "文字縮小"}
DEFAULT_HOTKEYS = {
    HK_CAPTURE: [MOD_CONTROL | MOD_ALT, 0x52],  # Ctrl+Alt+R
    HK_REPEAT: [MOD_CONTROL | MOD_ALT, 0x45],   # Ctrl+Alt+E
    HK_STOP: [MOD_CONTROL | MOD_ALT, 0x53],     # Ctrl+Alt+S
    HK_PAUSE: [MOD_CONTROL | MOD_ALT, 0x50],    # Ctrl+Alt+P（Ctrl+Alt+空白鍵常被其他程式占用）
    HK_FASTER: [MOD_CONTROL | MOD_ALT, 0x27],   # Ctrl+Alt+→
    HK_SLOWER: [MOD_CONTROL | MOD_ALT, 0x25],   # Ctrl+Alt+←
    HK_FONT_UP: [MOD_CONTROL | MOD_ALT, 0x26],  # Ctrl+Alt+↑
    HK_FONT_DOWN: [MOD_CONTROL | MOD_ALT, 0x28],  # Ctrl+Alt+↓
}

KEY_CHOICES = ([(chr(c), c) for c in range(0x41, 0x5B)]
               + [(str(d), 0x30 + d) for d in range(10)]
               + [(f"F{n}", 0x6F + n) for n in range(1, 13)]
               + [("空白鍵", 0x20), ("↑", 0x26), ("↓", 0x28), ("←", 0x25), ("→", 0x27), ("PageUp", 0x21), ("PageDown", 0x22),
                  ("-", 0xBD), ("=", 0xBB), (",", 0xBC), (".", 0xBE)])
VK_NAME = {vk: name for name, vk in KEY_CHOICES}


def hotkey_text(combo):
    if not combo:
        return "（不使用）"
    mods, vk = combo
    parts = []
    if mods & MOD_CONTROL:
        parts.append("Ctrl")
    if mods & MOD_ALT:
        parts.append("Option" if IS_MAC else "Alt")
    if mods & MOD_SHIFT:
        parts.append("Shift")
    if mods & MOD_WIN:
        parts.append("Cmd" if IS_MAC else "Win")
    parts.append(VK_NAME.get(vk, f"#{vk}"))
    return "+".join(parts)


DEFAULT_SETTINGS = {
    "voice": "",
    "rate": 0,
    "volume": 100,
    "auto_read": True,
    "show_text": True,
    "follow": True,
    "read_dir": "h",
    "alpha": 1.0,
    "x": None,
    "y": None,
    "mini_x": None,
    "mini_y": None,
    "mini_size": 64,
    "font_size": 12,
    "show_options": True,
    "hotkeys": {},
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


def load_hotkeys(settings):
    hk = {k: list(v) for k, v in DEFAULT_HOTKEYS.items()}
    saved = dict(settings.get("hotkeys") or {})
    # 1.3 版的語速預設是 Ctrl+Alt+↑↓，現在上下鍵改給文字大小，舊的預設值就不沿用
    if saved.get("4") == [MOD_CONTROL | MOD_ALT, 0x26] and saved.get("5") == [MOD_CONTROL | MOD_ALT, 0x28]:
        saved.pop("4")
        saved.pop("5")
    for k, v in saved.items():
        try:
            k = int(k)
        except ValueError:
            continue
        if k in hk:
            hk[k] = list(v) if v else None
    return hk


def log_error(text):
    try:
        with open(os.path.join(data_dir(), "error.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S]\n") + text + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# 多音字／破音字唸法
# ---------------------------------------------------------------------------

class PronBook:
    """使用者自訂的唸法：「語詞」→「唸成」（用同音字寫出正確讀音）。"""

    def __init__(self, path=None):
        self.path = path or pron_path()
        self.items = []  # [{"word":..., "say":..., "note":...}]
        self.load()

    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self.items = [d for d in data if d.get("word") and d.get("say")]
        except Exception:
            self.items = []

    def save(self):
        try:
            with open(self.path, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def set(self, word, say, note=""):
        for d in self.items:
            if d["word"] == word:
                d.update(say=say, note=note)
                break
        else:
            self.items.append({"word": word, "say": say, "note": note})
        self.save()

    def remove(self, word):
        self.items = [d for d in self.items if d["word"] != word]
        self.save()

    def rules(self):
        return [(d["word"], d["say"]) for d in self.items]


# ---------------------------------------------------------------------------
# OCR：RapidOCR (PP-OCR 模型，離線、內建於程式中)
# ---------------------------------------------------------------------------

_CJK = r"\u2e80-\u9fff\uf900-\ufaff\u3000-\u303f\uff00-\uffef"
_CJK_RE = re.compile(f"[{_CJK}]")


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
        """回傳 [(top, left, height, text, width), ...]（還沒排順序，排序交給 to_text）。"""
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
            items.append((min(ys), min(xs), max(ys) - min(ys), text, max(xs) - min(xs)))
        return items

    @staticmethod
    def _join(parts):
        joined = ""
        for p in parts:
            if joined and not (re.search(f"[{_CJK}]$", joined) or re.match(f"^[{_CJK}]", p)):
                joined += " "
            joined += p
        return joined

    @staticmethod
    def to_text(items, vertical=False):
        """把辨識出的文字框整理成文字。
        橫式：一行一行由上到下、每行由左到右。
        直式：一欄一欄由右到左、每欄由上到下。"""
        if not items:
            return ""
        if vertical:
            items = sorted(items, key=lambda t: -(t[1] + t[4]))  # 右邊的先
            cols = []
            for top, left, height, text, width in items:
                cx = left + width / 2
                for c in cols:
                    if c["l"] <= cx <= c["r"]:
                        c["parts"].append((top, text))
                        break
                else:
                    cols.append({"l": left, "r": left + width, "parts": [(top, text)]})
            cols.sort(key=lambda c: -c["r"])
            return "\n".join(OcrEngine._join([t for _, t in sorted(c["parts"])]) for c in cols)
        items = sorted(items, key=lambda t: (t[0], t[1]))
        lines = []
        for top, left, height, text, *_ in items:
            if lines:
                last = lines[-1]
                mid = top + height / 2
                if last["top"] <= mid <= last["top"] + last["height"]:
                    last["parts"].append((left, text))
                    continue
            lines.append({"top": top, "height": height, "parts": [(left, text)]})
        return "\n".join(OcrEngine._join([t for _, t in sorted(line["parts"])]) for line in lines)


def prepare_speech(text, rules=()):
    """整理要念的文字，並記下「念出的每個字」對應到原文的第幾個字。

    1. 中文行與行之間直接相連（避免一行一行斷開念），其他換行改成句號停頓。
    2. 套用多音字唸法（把語詞換成同音字）。
    回傳 (要念的文字, 對照表)；對照表第 k 項 = 念出的第 k 個字在原文的位置（-1 表示是補上的標點）。
    """
    chars, idx = [], []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "\r":
            i += 1
            continue
        if c == "\n":
            j = i
            while j < n and text[j] in "\r\n":
                j += 1
            prev = chars[-1] if chars else ""
            nxt = text[j] if j < n else ""
            single = text[i:j].count("\n") == 1
            if not (single and _CJK_RE.match(prev or " ") and _CJK_RE.match(nxt or " ")):
                if chars and prev not in "。！？!?.；;，,、：:":
                    chars.append("。")
                    idx.append(-1)
                chars.append("\n")
                idx.append(-1)
            i = j
            continue
        chars.append(c)
        idx.append(i)
        i += 1
    s = "".join(chars)

    by_first = {}
    for word, say in sorted((r for r in rules if r[0] and r[1]), key=lambda r: -len(r[0])):
        by_first.setdefault(word[0], []).append((word, say))
    out, omap = [], []
    k = 0
    while k < len(s):
        hit = None
        for word, say in by_first.get(s[k], ()):
            if s.startswith(word, k):
                hit = (word, say)
                break
        if hit:
            word, say = hit
            lw, ls = len(word), len(say)
            for t, ch in enumerate(say):
                out.append(ch)
                omap.append(idx[k + min(lw - 1, t * lw // ls)])
            k += lw
        else:
            out.append(s[k])
            omap.append(idx[k])
            k += 1
    while out and out[0].isspace():
        out.pop(0)
        omap.pop(0)
    while out and out[-1].isspace():
        out.pop()
        omap.pop()
    return "".join(out), omap


# ---------------------------------------------------------------------------
# 語音 1：Windows SAPI（含 Windows 10/11 新版的 OneCore 語音，如 Yating、Hanhan、Zhiwei）
# ---------------------------------------------------------------------------

class SapiSpeaker:
    SVSF_ASYNC = 1
    SVSF_PURGE = 2
    SVSF_NOT_XML = 16

    def __init__(self):
        self.voice = None
        self.tokens = {}  # 顯示名稱 -> token
        self.error = None
        self.active = False
        self.paused = False
        self.seen_speaking = False
        self.started = 0.0
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
        self.resume()
        token = self.tokens.get(voice_name) or self.tokens.get(self.default_voice())
        if token is not None:
            try:
                self.voice.Voice = token
            except Exception:
                pass
        self.voice.Rate = int(max(-10, min(10, rate)))
        self.voice.Volume = int(max(0, min(100, volume)))
        self.voice.Speak(text, self.SVSF_ASYNC | self.SVSF_PURGE | self.SVSF_NOT_XML)
        self.active = True
        self.seen_speaking = False
        self.started = time.time()

    def stop(self):
        self.active = False
        if self.voice:
            try:
                self.resume()
                self.voice.Speak("", self.SVSF_ASYNC | self.SVSF_PURGE)
            except Exception:
                pass

    def pause(self):
        if self.active and not self.paused and self.voice:
            try:
                self.voice.Pause()
                self.paused = True
            except Exception:
                pass

    def resume(self):
        if self.paused and self.voice:
            try:
                self.voice.Resume()
            except Exception:
                pass
        self.paused = False

    def poll(self):
        """回傳 (狀態, 念到第幾個字, 長度, 錯誤訊息)。"""
        if not self.active or not self.voice:
            return ("done", 0, 0, None)
        try:
            st = self.voice.Status
            state = st.RunningState
            if state == 2:
                self.seen_speaking = True
            if state == 1 and (self.seen_speaking or time.time() - self.started > 1.5):
                self.active = False
                return ("done", 0, 0, None)
            return ("paused" if self.paused else "speaking", st.InputWordPosition, st.InputWordLength, None)
        except Exception as e:
            self.active = False
            return ("error", 0, 0, str(e))


# ---------------------------------------------------------------------------
# 語音 1（Mac 版）：macOS 內建語音（NSSpeechSynthesizer），會回報念到哪個字
# ---------------------------------------------------------------------------

_MAC_DELEGATE = None


def _mac_delegate_class():
    """建立（只建一次）接收「念到哪個字」的 Objective-C 物件。"""
    global _MAC_DELEGATE
    if _MAC_DELEGATE is None:
        from Foundation import NSObject
        import objc

        class SpeechDelegate(NSObject):
            def initWithOwner_gen_(self, owner, gen):
                self = objc.super(type(self), self).init()
                if self is None:
                    return None
                self.owner, self.gen = owner, gen
                return self

            def speechSynthesizer_willSpeakWord_ofString_(self, sender, rng, string):
                o = self.owner
                if o is not None and self.gen == o.gen:
                    o.pos, o.length = int(rng.location), int(rng.length)

            def speechSynthesizer_didFinishSpeaking_(self, sender, ok):
                o = self.owner
                if o is not None and self.gen == o.gen:
                    o.finished = True

        _MAC_DELEGATE = SpeechDelegate
    return _MAC_DELEGATE


class MacSpeaker:
    BASE_WPM = 175.0   # NSSpeechSynthesizer 的標準語速

    def __init__(self):
        self.tokens = {}   # 顯示名稱 -> 語音代號
        self.locale = {}   # 顯示名稱 -> 語言（zh_TW…）
        self.error = None
        self.active = False
        self.paused = False
        self.gen = 0
        self.pos = self.length = 0
        self.finished = False
        self.synth = None
        self.delegate = None
        self.voice = self   # 讓 App 取用 voice.Rate / voice.Volume 時不出錯
        self.Rate = 0
        self.Volume = 100
        try:
            from AppKit import NSSpeechSynthesizer

            self._NS = NSSpeechSynthesizer
            for ident in NSSpeechSynthesizer.availableVoices():
                a = NSSpeechSynthesizer.attributesForVoice_(ident)
                loc = str(a.get("VoiceLocaleIdentifier", ""))
                # 中文語音全部列出；英文只列正常的語音（不列 Bad News、Zarvox 這類趣味語音）
                if not (loc.startswith("zh") or (loc.startswith("en") and str(ident).startswith("com.apple.voice"))):
                    continue
                name = str(a.get("VoiceName", ident))
                if name not in self.tokens:
                    self.tokens[name] = str(ident)
                    self.locale[name] = loc
        except Exception as e:  # pragma: no cover
            self.error = str(e)

    def voice_names(self):
        def rank(n):
            loc = self.locale[n]
            if loc == "zh_TW":
                return 0
            if loc.startswith("zh"):
                return 1
            return 2

        def pref(n):
            tid = self.tokens[n].lower()
            return 0 if ("meijia" in tid or "sinji" in tid) else 1

        return sorted(self.tokens, key=lambda n: (rank(n), pref(n), n))

    def has_chinese_voice(self):
        return any(l.startswith("zh") for l in self.locale.values())

    def default_voice(self):
        names = self.voice_names()
        for prefer in ("meijia", "美佳", "sinji", "善怡"):
            for n in names:
                if prefer in n.lower() or prefer in self.tokens[n].lower():
                    return n
        return names[0] if names else ""

    def speak(self, text, voice_name="", rate=0, volume=100):
        if self.error or not self.tokens:
            raise RuntimeError(self.error or "找不到語音引擎")
        self.stop()
        ident = self.tokens.get(voice_name) or self.tokens.get(self.default_voice())
        self.gen += 1
        self.pos = self.length = 0
        self.finished = False
        self.paused = False
        self.Rate, self.Volume = rate, volume
        synth = self._NS.alloc().initWithVoice_(ident)
        self.delegate = _mac_delegate_class().alloc().initWithOwner_gen_(self, self.gen)
        synth.setDelegate_(self.delegate)
        synth.setRate_(self.BASE_WPM * 2.0 ** (max(-10, min(10, int(rate))) / 10.0))
        synth.setVolume_(max(0, min(100, int(volume))) / 100.0)
        self.synth = synth
        self.active = True
        synth.startSpeakingString_(text)

    def stop(self):
        self.active = False
        self.paused = False
        self.gen += 1
        if self.synth is not None:
            try:
                self.synth.setDelegate_(None)
                self.synth.stopSpeaking()
            except Exception:
                pass
            self.synth = None

    def pause(self):
        if self.active and not self.paused and self.synth is not None:
            self.synth.pauseSpeakingAtBoundary_(0)
            self.paused = True

    def resume(self):
        if self.paused and self.synth is not None:
            self.synth.continueSpeaking()
        self.paused = False

    def set_live(self, rate, volume):
        """朗讀中改音量馬上生效；語速由 App 用「從目前的字重新念」處理。"""
        self.Rate, self.Volume = rate, volume
        if self.synth is not None:
            try:
                self.synth.setVolume_(max(0, min(100, int(volume))) / 100.0)
            except Exception:
                pass

    def poll(self):
        """回傳 (狀態, 念到第幾個字, 長度, 錯誤訊息)。"""
        if not self.active or self.synth is None:
            return ("done", 0, 0, None)
        if self.finished and not self.paused:
            self.active = False
            return ("done", 0, 0, None)
        return ("paused" if self.paused else "speaking", self.pos, self.length, None)


# ---------------------------------------------------------------------------
# 語音 2：Google 國語（台灣）——線上語音
# ---------------------------------------------------------------------------

def split_chunks(text, maxlen=90):
    """把文字切成一句一句（Google 語音一次念一句），回傳 [(起始位置, 句子), ...]。"""
    out = []
    for m in re.finditer(r"[^。！？!?\n]+[。！？!?]*|\n+", text):
        start, s = m.start(), m.group()
        while len(s) > maxlen:
            cut = max(s.rfind(p, 0, maxlen) for p in "，,、；;：: ")
            cut = cut + 1 if cut > 10 else maxlen
            out.append((start, s[:cut]))
            start, s = start + cut, s[cut:]
        out.append((start, s))
    return [(st, s) for st, s in out if any(c.isalnum() for c in s)]


class GoogleSpeaker:
    """Google 線上語音。

    用 miniaudio 解碼 mp3 並自己播放，變速用 WSOLA（只改快慢、不改音高，聲音還是同一個人），
    播放中拉語速、音量都會立刻生效。
    """

    SR = 24000          # gTTS 的取樣率
    N = 960             # 每個分析框 40 ms
    HS = 480            # 輸出步距
    TOL = 240           # 對齊搜尋範圍 ±10 ms
    LATENCY = 0.12      # 播放裝置延遲（秒），讓亮字和聲音對齊

    def __init__(self):
        self.gen = 0
        self.active = False
        self.paused = False
        self.device = None
        self.err = None
        self.played_any = False
        self.speed = 1.0
        self.vol = 1.0
        self._tmp = None
        self._lock = threading.Lock()

    @staticmethod
    def rate_to_speed(rate):
        return 2.0 ** (max(-10, min(10, int(rate))) / 10.0)  # -10 → 0.5 倍，+10 → 2 倍

    def set_rate(self, rate):
        self.speed = self.rate_to_speed(rate)

    def set_volume(self, volume):
        self.vol = max(0, min(100, int(volume))) / 100.0

    def _tmpdir(self):
        if not self._tmp or not os.path.isdir(self._tmp):
            self._tmp = tempfile.mkdtemp(prefix="ScreenOCRReader_")
        return self._tmp

    def cleanup(self):
        self.stop()
        if self._tmp:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._tmp = None

    def speak(self, text, rate=0, volume=100):
        import numpy as np

        self.stop()
        self.gen += 1
        self.chunks = split_chunks(text)
        self.speakable = [[k for k, c in enumerate(s) if c.isalnum()] for _, s in self.chunks]
        n = len(self.chunks)
        self.pcm = [None] * n
        self.real_len = [0] * n
        self.voiced = [None] * n
        self.err = None
        self.paused = False
        self.played_any = False
        self.finished_at = None
        self.set_rate(rate)
        self.set_volume(volume)
        self._cur_vol = self.vol
        # 播放狀態（在音效執行緒中使用）
        self.i = 0
        self.ana = 0.0
        self.natural = None
        self.flush = 0
        self.ola = np.zeros(self.N, dtype=np.float32)
        self.win = (0.5 - 0.5 * np.cos(2 * np.pi * np.arange(self.N) / self.N)).astype(np.float32)
        self.outbuf = np.zeros(0, dtype=np.float32)
        self.active = True
        threading.Thread(target=self._synth, args=(self.gen, list(self.chunks), self._tmpdir()), daemon=True).start()
        self._open_device()

    # ---- 背景：下載語音並解碼 ----
    def _synth(self, gen, chunks, folder):
        import numpy as np

        try:
            import miniaudio
            from gtts import gTTS
        except Exception as e:
            self.err = f"Google 語音元件載入失敗：{e}"
            return
        for i, (_, s) in enumerate(chunks):
            if gen != self.gen:
                return
            path = os.path.join(folder, f"g{gen}_{i}.mp3")
            try:
                try:
                    tts = gTTS(s, lang="zh-TW", lang_check=False, timeout=10)
                except TypeError:
                    tts = gTTS(s, lang="zh-TW", lang_check=False)
                tts.save(path)
                dec = miniaudio.decode_file(path, output_format=miniaudio.SampleFormat.FLOAT32,
                                            nchannels=1, sample_rate=self.SR)
                x = np.asarray(dec.samples, dtype=np.float32)
            except Exception as e:
                if gen == self.gen:
                    self.err = "Google 語音連不上（需要網路）"
                    log_error(f"gTTS: {e!r}")
                return
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
            if gen != self.gen:
                return
            # 每 10 ms 判斷有沒有聲音，用來把亮字對準「有在講話」的時間（跳過停頓）
            fl = self.SR // 100
            frames = len(x) // fl
            if frames:
                rms = np.sqrt((x[:frames * fl].reshape(frames, fl) ** 2).mean(axis=1))
                voiced = rms > max(0.01, rms.max() * 0.08)
                cum = np.concatenate([[0], np.cumsum(voiced)])
            else:
                cum = np.array([0, 1])
            self.voiced[i] = cum
            self.real_len[i] = len(x)
            self.pcm[i] = np.concatenate([x, np.zeros(self.N + 2 * self.TOL, dtype=np.float32)])

    # ---- 音效執行緒：產生聲音 ----
    def _open_device(self):
        import miniaudio

        def stream():
            req = yield b""
            while True:
                try:
                    out = self._render(req)
                except Exception:
                    log_error(traceback.format_exc())
                    import numpy as np
                    out = np.zeros(req, dtype=np.float32)
                req = yield out.tobytes()

        self.device = miniaudio.PlaybackDevice(output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1,
                                               sample_rate=self.SR, buffersize_msec=60, app_name=APP_NAME)
        s = stream()
        next(s)
        self.device.start(s)

    def _step(self):
        """WSOLA：產生下一段 HS 個樣本。"""
        import numpy as np

        N, HS, TOL = self.N, self.HS, self.TOL
        x = self.pcm[self.i]
        tgt = int(self.ana)
        if tgt >= self.real_len[self.i]:
            out = self.ola[:HS].copy()
            self.ola = np.concatenate([self.ola[HS:], np.zeros(HS, dtype=np.float32)])
            self.flush += 1
            if self.flush >= 2:
                self.i += 1
                self.ana, self.natural, self.flush = 0.0, None, 0
            return out
        L = len(x)
        lo, hi = max(0, tgt - TOL), min(L - N, tgt + TOL)
        if self.natural is None or self.natural + N > L or hi <= lo:
            best = min(tgt, L - N)
        else:
            ref = x[self.natural:self.natural + N]
            seg = x[lo:hi + N]
            corr = np.correlate(seg, ref, mode="valid")
            best = lo + int(np.argmax(corr))
        self.ola += x[best:best + N] * self.win
        out = self.ola[:HS].copy()
        self.ola = np.concatenate([self.ola[HS:], np.zeros(HS, dtype=np.float32)])
        self.natural = best + HS
        self.ana += HS * self.speed
        self.played_any = True
        return out

    def _render(self, nframes):
        import numpy as np

        parts = [self.outbuf]
        have = len(self.outbuf)
        while have < nframes:
            if not self.active or self.paused or self.i >= len(self.pcm) or self.pcm[self.i] is None:
                if self.active and self.i >= len(self.pcm) and self.finished_at is None:
                    self.finished_at = time.time()
                if self.active and not self.paused and self.i < len(self.pcm) and self.err:
                    self.finished_at = self.finished_at or time.time()
                parts.append(np.zeros(nframes - have, dtype=np.float32))
                have = nframes
                break
            seg = self._step()
            parts.append(seg)
            have += len(seg)
        buf = np.concatenate(parts)
        out, self.outbuf = buf[:nframes], buf[nframes:]
        # 音量平滑變化，避免爆音
        target = self.vol
        ramp = np.linspace(self._cur_vol, target, len(out), dtype=np.float32) if len(out) else out
        self._cur_vol = target
        return (out * ramp).astype(np.float32)

    # ---- 主執行緒 ----
    def stop(self):
        self.gen += 1
        self.active = False
        self.paused = False
        dev, self.device = self.device, None
        if dev is not None:
            try:
                dev.close()
            except Exception:
                pass

    def pause(self):
        if self.active:
            self.paused = True

    def resume(self):
        self.paused = False

    def poll(self):
        if not self.active:
            return ("done", 0, 0, None)
        if self.finished_at and time.time() - self.finished_at > self.LATENCY + 0.15:
            err = self.err if self.i < len(self.pcm) else None
            self.stop()
            return ("error", 0, 0, err) if err else ("done", 0, 0, None)
        i = self.i
        if i >= len(self.chunks):
            return ("speaking", 0, 0, None)
        state = "paused" if self.paused else "speaking"
        if self.pcm[i] is None:
            if self.err and not self.played_any:
                self.stop()
                return ("error", 0, 0, self.err)
            return ("loading" if not self.paused else "paused", self.chunks[i][0], 0, None)
        start, _ = self.chunks[i]
        sp = self.speakable[i]
        if not sp:
            return (state, start, 0, None)
        heard = max(0.0, self.ana - self.speed * self.LATENCY * self.SR)
        cum = self.voiced[i]
        f = min(len(cum) - 1, int(heard / (self.SR // 100)))
        frac = cum[f] / max(1, cum[-1])
        k = sp[min(len(sp) - 1, int(frac * len(sp)))]
        return (state, start + k, 1, None)


# ---------------------------------------------------------------------------
# 全域快捷鍵（不管目前在哪個程式都有效），可隨時重設、可檢查某組按鍵是否已被占用
# ---------------------------------------------------------------------------

class WinGlobalHotkeys(threading.Thread):
    MOD_NOREPEAT = 0x4000
    WM_HOTKEY = 0x0312
    WM_APP_CMD = 0x8001
    TEST_ID = 0xBF00

    def __init__(self, on_hotkey):
        super().__init__(daemon=True)
        self.on_hotkey = on_hotkey
        self.ready = threading.Event()
        self.cmds = queue.Queue()
        self.tid = None
        self.registered = {}  # id -> (mods, vk)

    def run(self):
        if not IS_WINDOWS:
            self.ready.set()
            return
        from ctypes import wintypes

        user32 = self.user32 = ctypes.windll.user32
        user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        msg = wintypes.MSG()
        user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)  # 建立訊息佇列
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self.ready.set()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == self.WM_HOTKEY:
                self.on_hotkey(int(msg.wParam))
            elif msg.message == self.WM_APP_CMD:
                self._drain()

    def _drain(self):
        while True:
            try:
                fn, args, box = self.cmds.get_nowait()
            except queue.Empty:
                return
            try:
                box["r"] = fn(*args)
            except Exception as e:
                box["e"] = e
            box["ev"].set()

    def _call(self, fn, *args):
        if not self.tid:
            return None
        box = {"ev": threading.Event()}
        self.cmds.put((fn, args, box))
        self.user32.PostThreadMessageW(self.tid, self.WM_APP_CMD, 0, 0)
        box["ev"].wait(5)
        return box.get("r")

    def apply(self, bindings):
        """bindings: {id: [mods, vk] 或 None}。回傳註冊失敗的 id 清單。"""
        r = self._call(self._apply, dict(bindings))
        return r if r is not None else list(bindings)

    def _apply(self, bindings):
        for hid in list(self.registered):
            self.user32.UnregisterHotKey(None, hid)
        self.registered.clear()
        failed = []
        for hid, combo in bindings.items():
            if not combo:
                continue
            mods, vk = combo
            if self.user32.RegisterHotKey(None, hid, mods | self.MOD_NOREPEAT, vk):
                self.registered[hid] = (mods, vk)
            else:
                failed.append(hid)
        return failed

    def scan(self, mods, vks):
        """檢查每個按鍵（搭配 mods）：'free' 可用、'taken' 已被系統或其他程式占用、'own' 本程式正在用。"""
        return self._call(self._scan, mods, list(vks)) or {}

    def _scan(self, mods, vks):
        res = {}
        own = set(self.registered.values())
        for vk in vks:
            if (mods, vk) in own:
                res[vk] = "own"
            elif self.user32.RegisterHotKey(None, self.TEST_ID, mods | self.MOD_NOREPEAT, vk):
                self.user32.UnregisterHotKey(None, self.TEST_ID)
                res[vk] = "free"
            else:
                res[vk] = "taken"
        return res


# Mac 版：用 Carbon 的 RegisterEventHotKey 註冊全域快捷鍵（不需要「輔助使用」權限）。
# 如果該組合已被系統或其他程式占用，註冊會失敗，所以可以偵測「已被占用」。

# Windows 虛擬鍵碼（程式內部沿用）→ macOS 鍵碼
MAC_KEYCODES = {
    **{ord(c): k for c, k in zip("ASDFHGZXCVBQWERYT", (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 13, 14, 15, 16, 17))},
    **{ord(c): k for c, k in zip("OUIPLJKNM", (31, 32, 34, 35, 37, 38, 40, 45, 46))},
    **{ord(c): k for c, k in zip("123465", (18, 19, 20, 21, 22, 23))},
    ord("9"): 25, ord("7"): 26, ord("8"): 28, ord("0"): 29,
    0x20: 49, 0x25: 123, 0x27: 124, 0x28: 125, 0x26: 126, 0x21: 116, 0x22: 121,
    0xBD: 27, 0xBB: 24, 0xBC: 43, 0xBE: 47,
    **{0x6F + n: k for n, k in zip(range(1, 13), (122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111))},
}
MAC_MODS = ((MOD_CONTROL, 0x1000), (MOD_ALT, 0x0800), (MOD_SHIFT, 0x0200), (MOD_WIN, 0x0100))


class MacGlobalHotkeys:
    TEST_ID = 0xBF00
    SIGNATURE = 0x534F5248  # 'SORH'

    def __init__(self, on_hotkey):
        self.on_hotkey = on_hotkey
        self.ready = threading.Event()
        self.registered = {}  # id -> (mods, vk)
        self.refs = {}        # id -> EventHotKeyRef
        self.error = None
        self._handler = None

    def start(self):
        """必須在主執行緒呼叫（事件會從主執行緒的事件迴圈送來）。"""
        import ctypes as C

        try:
            self.C = C
            carbon = self.carbon = C.CDLL("/System/Library/Frameworks/Carbon.framework/Carbon")

            class EventTypeSpec(C.Structure):
                _fields_ = [("eventClass", C.c_uint32), ("eventKind", C.c_uint32)]

            class EventHotKeyID(C.Structure):
                _fields_ = [("signature", C.c_uint32), ("id", C.c_uint32)]

            self.HotKeyID = EventHotKeyID
            upp = C.CFUNCTYPE(C.c_int32, C.c_void_p, C.c_void_p, C.c_void_p)
            carbon.GetApplicationEventTarget.restype = C.c_void_p
            carbon.InstallEventHandler.argtypes = [C.c_void_p, upp, C.c_uint32, C.POINTER(EventTypeSpec),
                                                   C.c_void_p, C.c_void_p]
            carbon.InstallEventHandler.restype = C.c_int32
            carbon.RegisterEventHotKey.argtypes = [C.c_uint32, C.c_uint32, EventHotKeyID, C.c_void_p,
                                                   C.c_uint32, C.POINTER(C.c_void_p)]
            carbon.RegisterEventHotKey.restype = C.c_int32
            carbon.UnregisterEventHotKey.argtypes = [C.c_void_p]
            carbon.GetEventParameter.argtypes = [C.c_void_p, C.c_uint32, C.c_uint32, C.c_void_p, C.c_uint32,
                                                 C.c_void_p, C.c_void_p]
            carbon.GetEventParameter.restype = C.c_int32

            def handler(call_ref, event, user_data):
                try:
                    hk = EventHotKeyID()
                    st = carbon.GetEventParameter(event, 0x2D2D2D2D, 0x68606964, None, C.sizeof(hk), None, C.byref(hk))
                    if st == 0 and hk.signature == self.SIGNATURE and hk.id != self.TEST_ID:
                        self.on_hotkey(int(hk.id))
                except Exception:
                    log_error(traceback.format_exc())
                return 0

            self._handler = upp(handler)
            spec = EventTypeSpec(0x6B657962, 5)  # kEventClassKeyboard, kEventHotKeyPressed
            st = carbon.InstallEventHandler(carbon.GetApplicationEventTarget(), self._handler, 1,
                                            C.byref(spec), None, None)
            if st != 0:
                self.error = f"InstallEventHandler 失敗（{st}）"
        except Exception as e:
            self.error = str(e)
            log_error(traceback.format_exc())
        self.ready.set()

    def _register(self, hid, mods, vk):
        """成功回傳 EventHotKeyRef，失敗（已被占用、不支援的鍵）回傳 None。"""
        C = self.C
        code = MAC_KEYCODES.get(vk)
        if code is None:
            return None
        cm = 0
        for mine, theirs in MAC_MODS:
            if mods & mine:
                cm |= theirs
        ref = C.c_void_p()
        st = self.carbon.RegisterEventHotKey(code, cm, self.HotKeyID(self.SIGNATURE, hid), self.carbon.GetApplicationEventTarget(),
                                             0, C.byref(ref))
        return ref if st == 0 and ref.value else None

    def apply(self, bindings):
        """bindings: {id: [mods, vk] 或 None}。回傳註冊失敗的 id 清單。"""
        if self.error:
            return list(bindings)
        for ref in self.refs.values():
            self.carbon.UnregisterEventHotKey(ref)
        self.refs.clear()
        self.registered.clear()
        failed = []
        for hid, combo in bindings.items():
            if not combo:
                continue
            mods, vk = combo
            ref = self._register(hid, mods, vk)
            if ref is not None:
                self.refs[hid] = ref
                self.registered[hid] = (mods, vk)
            else:
                failed.append(hid)
        return failed

    def scan(self, mods, vks):
        """檢查每個按鍵（搭配 mods）：'free' 可用、'taken' 已被系統或其他程式占用、'own' 本程式正在用。"""
        res = {}
        if self.error:
            return res
        own = set(self.registered.values())
        for vk in vks:
            if (mods, vk) in own:
                res[vk] = "own"
                continue
            ref = self._register(self.TEST_ID, mods, vk)
            if ref is not None:
                self.carbon.UnregisterEventHotKey(ref)
                res[vk] = "free"
            else:
                res[vk] = "taken"
        return res


GlobalHotkeys = MacGlobalHotkeys if IS_MAC else WinGlobalHotkeys


# ---------------------------------------------------------------------------
# 截圖：先拍下整個（多螢幕）桌面，再讓使用者框選範圍
# ---------------------------------------------------------------------------

def make_borderless(win, transparent=False, activates=True):
    """無邊框視窗。Mac 用 Tk 的 MacWindowStyle（overrideredirect 的視窗在 Mac 收不到鍵盤）。"""
    if IS_MAC:
        try:
            attrs = [] if activates else ["noActivates"]
            win.tk.call("::tk::unsupported::MacWindowStyle", "style", win._w, "plain", attrs)
        except Exception:
            win.overrideredirect(True)
        if transparent:
            try:
                win.attributes("-transparent", True)
                win.configure(bg="systemTransparent")
            except Exception:
                pass
    else:
        win.overrideredirect(True)


def mac_float(win, title, level=25, clear=False):
    """Mac：讓視窗浮在所有視窗之上，也能浮在全螢幕的程式上面、出現在每個桌面空間。"""
    if not IS_MAC:
        return
    try:
        from AppKit import NSApp

        win.title(title)
        win.update_idletasks()
        for w in NSApp.windows():
            if str(w.title()) == title:
                w.setLevel_(level)
                w.setCollectionBehavior_(1 | 256)  # CanJoinAllSpaces | FullScreenAuxiliary
                w.setHidesOnDeactivate_(False)
                if clear:  # 圓形圖案的四個角要透明
                    from AppKit import NSColor

                    w.setOpaque_(False)
                    w.setBackgroundColor_(NSColor.clearColor())
                    w.setHasShadow_(False)
    except Exception:
        log_error("mac_float: " + traceback.format_exc())


def mac_screen_access(request=False):
    """Mac：是否已允許「螢幕錄製」（沒允許時截到的圖只有桌面背景）。"""
    if not IS_MAC:
        return True
    try:
        from Quartz import CGPreflightScreenCaptureAccess, CGRequestScreenCaptureAccess

        ok = bool(CGPreflightScreenCaptureAccess())
        if not ok and request:
            CGRequestScreenCaptureAccess()
        return ok
    except Exception:
        return True


def _mss():
    import mss

    return mss.MSS() if hasattr(mss, "MSS") else mss.mss()


def grab_virtual_screen():
    from PIL import Image

    with _mss() as sct:
        mon = sct.monitors[0]  # 所有螢幕合併的虛擬桌面
        shot = sct.grab(mon)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        # Mac 的 Retina 螢幕拍出來的像素比座標點數多（通常 2 倍）
        scale = img.size[0] / max(1, mon["width"])
        return img, mon["left"], mon["top"], scale


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
        self.full, self.ox, self.oy, self.sc = grab_virtual_screen()
        w, h = round(self.full.size[0] / self.sc), round(self.full.size[1] / self.sc)  # 畫面上的大小（點數）
        # 顯示用的圖：Retina 時縮到和畫面座標一樣大；真正辨識用的是原本的高解析度圖
        self.view = self.full if self.sc == 1 else self.full.resize((w, h))

        self.win = tk.Toplevel(root)
        make_borderless(self.win)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{w}x{h}+{self.ox}+{self.oy}")
        self.win.configure(cursor="crosshair")
        mac_float(self.win, "sor-selector", level=1000)

        dim = ImageEnhance.Brightness(self.view).enhance(0.55)
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
        self.canvas.create_text(tx, ty, text=tip, fill="white", font=(UI_FONT, 14, "bold"))

        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._drag)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        for b in RIGHT_BUTTONS:
            self.canvas.bind(f"<ButtonPress-{b}>", lambda e: self._finish(None))
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
        self.crop_photo = ImageTk.PhotoImage(self.view.crop((l, t, r, b)))
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
        k = self.sc
        img = self.full.crop((round(l * k), round(t * k), round(r * k), round(b * k)))
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
# 浮動小圖案
# ---------------------------------------------------------------------------

class MiniIcon:
    KEY = "#ff00fe"  # 這個顏色會變透明，讓圖案是圓的
    COLORS = {"idle": "#1e6fd9", "busy": "#8a8a8a", "speaking": "#f08c00",
              "paused": "#2e9e4f", "error": "#d93025"}

    def __init__(self, app):
        tk = app.tk
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        make_borderless(self.win, transparent=True, activates=False)
        self.win.attributes("-topmost", True)
        if IS_MAC:
            self.KEY = "systemTransparent"
        else:
            self.win.configure(bg=self.KEY)
            try:
                self.win.attributes("-transparentcolor", self.KEY)
            except Exception:
                pass
        mac_float(self.win, "sor-mini", clear=True)
        self.canvas = tk.Canvas(self.win, bg=self.KEY, highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack()
        self.state = "idle"
        self.pulse = False
        self.drag = None
        self.tip = None
        self.tip_job = None
        self.toast_win = None
        self.visible = False
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        if not IS_MAC:  # Mac 的右鍵由 App._install_mac_rightclick 用原生事件處理（兩邊都接會同時開兩個選單而當掉）
            for b in RIGHT_BUTTONS:
                self.canvas.bind(f"<ButtonPress-{b}>", self._menu)
        self.canvas.bind("<Enter>", self._enter)
        self.canvas.bind("<Leave>", lambda e: self._hide_tip())
        self._redraw()
        self._animate()

    @property
    def size(self):
        scale = 1.0 if IS_MAC else self.app.root.winfo_fpixels("1i") / 96.0
        return int(int(self.app.settings.get("mini_size", 64)) * scale)

    def _redraw(self):
        s = self.size
        pad = max(3, s // 12)
        c = self.canvas
        c.configure(width=s + pad * 2, height=s + pad * 2)
        c.delete("all")
        color = self.COLORS.get(self.state, self.COLORS["idle"])
        if self.pulse and self.state == "speaking":
            c.create_oval(1, 1, s + pad * 2 - 1, s + pad * 2 - 1, outline="#ffd54f", width=pad)
        c.create_oval(pad, pad, pad + s, pad + s, fill=color, outline="white", width=max(2, s // 24))
        label = {"busy": "⋯", "paused": "❚❚"}.get(self.state, "讀")
        c.create_text(pad + s / 2, pad + s / 2, text=label, fill="white",
                      font=(UI_FONT, -int(s * 0.46), "bold"))

    def _animate(self):
        if self.state == "speaking" and self.visible:
            self.pulse = not self.pulse
            self._redraw()
        elif self.pulse:
            self.pulse = False
            self._redraw()
        self.win.after(450, self._animate)

    def set_state(self, state):
        if state != self.state:
            self.state = state
            self._redraw()

    def apply_alpha(self, a):
        try:
            self.win.attributes("-alpha", max(0.01, a))
        except Exception:
            pass

    def show(self):
        st = self.app.settings
        self._redraw()
        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        vx, vy = self.win.winfo_vrootx(), self.win.winfo_vrooty()
        sw, sh = self.win.winfo_screenwidth(), self.win.winfo_screenheight()
        x, y = st.get("mini_x"), st.get("mini_y")
        if x is None or y is None:
            x, y = sw - w - 40, sh // 3
        # 不要跑出螢幕外（考慮多螢幕時用虛擬桌面範圍）
        if IS_MAC:
            vy += 28  # 不要蓋到最上面的選單列
            sh -= 28
        else:
            try:
                from ctypes import windll
                vx, vy = windll.user32.GetSystemMetrics(76), windll.user32.GetSystemMetrics(77)
                sw, sh = windll.user32.GetSystemMetrics(78), windll.user32.GetSystemMetrics(79)
            except Exception:
                pass
        x = max(vx, min(int(x), vx + sw - w))
        y = max(vy, min(int(y), vy + sh - w))
        self.win.geometry(f"+{x}+{y}")
        self.win.deiconify()
        self.win.attributes("-topmost", True)
        self.win.lift()
        self.visible = True

    def hide(self):
        self._hide_tip()
        self.win.withdraw()
        self.visible = False

    # ---- 滑鼠 ----
    def _press(self, e):
        self._hide_tip()
        self.drag = (e.x_root, e.y_root, self.win.winfo_x(), self.win.winfo_y(), False)

    def _motion(self, e):
        if not self.drag:
            return
        x0, y0, wx, wy, moved = self.drag
        dx, dy = e.x_root - x0, e.y_root - y0
        if moved or abs(dx) > 4 or abs(dy) > 4:
            self.drag = (x0, y0, wx, wy, True)
            self.win.geometry(f"+{wx + dx}+{wy + dy}")

    def _release(self, e):
        d, self.drag = self.drag, None
        if d and d[4]:
            self.app.settings.update(mini_x=self.win.winfo_x(), mini_y=self.win.winfo_y())
        else:
            self.app.leave_mini()

    def _menu(self, e):
        self._hide_tip()
        menu = self.app.build_menu(self.win)
        try:
            menu.tk_popup(e.x_root, e.y_root)
        finally:
            menu.grab_release()

    # ---- 提示 ----
    def _enter(self, e):
        self._hide_tip()
        self.tip_job = self.win.after(700, self._show_tip)

    def _show_tip(self):
        self.tip_job = None
        if self.drag:
            return
        hk = hotkey_text(self.app.hotkeys_cfg.get(HK_CAPTURE))
        self.tip = self._bubble(f"左鍵點一下：打開面板\n右鍵：功能選單　拖曳：移動\n{hk}：框選截圖")

    def _hide_tip(self):
        if self.tip_job:
            self.win.after_cancel(self.tip_job)
            self.tip_job = None
        if self.tip:
            self.tip.destroy()
            self.tip = None

    def _bubble(self, text):
        tk = self.app.tk
        b = tk.Toplevel(self.win)
        make_borderless(b, activates=False)
        b.attributes("-topmost", True)
        tk.Label(b, text=text, bg="#333333", fg="white", justify="left",
                 font=(UI_FONT, 11), padx=10, pady=6).pack()
        b.update_idletasks()
        bw, bh = b.winfo_reqwidth(), b.winfo_reqheight()
        x, y = self.win.winfo_x(), self.win.winfo_y()
        w = self.win.winfo_width()
        bx = x - bw - 6 if x - bw - 6 > self.win.winfo_vrootx() else x + w + 6
        b.geometry(f"+{bx}+{y + max(0, (w - bh) // 2)}")
        mac_float(b, f"sor-bubble-{id(b)}", level=101)
        return b

    def toast(self, text, ms=2500):
        if not self.visible:
            return
        if self.toast_win:
            try:
                self.toast_win.destroy()
            except Exception:
                pass
        self.toast_win = t = self._bubble(text)
        self.win.after(ms, lambda: t.winfo_exists() and t.destroy())


# ---------------------------------------------------------------------------
# 快捷鍵設定視窗
# ---------------------------------------------------------------------------

class HotkeyDialog:
    NONE = "（不使用）"

    def __init__(self, app):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.cache = {}
        self.win = w = tk.Toplevel(app.root)
        w.title("快捷鍵設定")
        w.attributes("-topmost", True)
        w.resizable(False, False)
        frm = ttk.Frame(w, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="勾選要一起按的鍵，再選一個字母或數字。\n"
                            "清單中標「✖ 已被占用」的組合，是" + ("系統" if IS_MAC else "Windows") + "或其他程式已經在用的，不能選。",
                  justify="left").grid(row=0, column=0, columnspan=8, sticky="w", pady=(0, 8))
        self.rows = {}
        cfg = app.hotkeys_cfg
        for r, hid in enumerate(HOTKEY_NAMES, start=1):
            combo = cfg.get(hid)
            mods = combo[0] if combo else MOD_CONTROL | MOD_ALT
            row = {
                "ctrl": tk.BooleanVar(value=bool(mods & MOD_CONTROL)),
                "alt": tk.BooleanVar(value=bool(mods & MOD_ALT)),
                "shift": tk.BooleanVar(value=bool(mods & MOD_SHIFT)),
                "win": tk.BooleanVar(value=bool(mods & MOD_WIN)),
                "key": tk.StringVar(value=VK_NAME.get(combo[1], self.NONE) if combo else self.NONE),
            }
            ttk.Label(frm, text=HOTKEY_NAMES[hid], width=9).grid(row=r, column=0, sticky="w", pady=4)
            for c, (k, label) in enumerate((("ctrl", "Ctrl"), ("alt", "Option" if IS_MAC else "Alt"), ("shift", "Shift"), ("win", "Cmd" if IS_MAC else "Win")), start=1):
                ttk.Checkbutton(frm, text=label, variable=row[k],
                                command=lambda h=hid: self.refresh(h)).grid(row=r, column=c, padx=2)
            ttk.Label(frm, text="＋").grid(row=r, column=5)
            box = ttk.Combobox(frm, textvariable=row["key"], state="readonly", width=16, height=20)
            box.grid(row=r, column=6, padx=4)
            box.bind("<<ComboboxSelected>>", lambda e, h=hid: self._picked(h))
            row["box"] = box
            row["status"] = ttk.Label(frm, width=30)
            row["status"].grid(row=r, column=7, sticky="w")
            self.rows[hid] = row
        ttk.Label(frm, foreground="#666", justify="left",
                  text="說明：只在某個程式裡才有作用的快捷鍵（例如 Word 的 Ctrl+B）無法偵測；\n"
                       "如果選到一樣的，會以本程式為優先。").grid(row=10, column=0, columnspan=8, sticky="w", pady=(8, 0))
        btns = ttk.Frame(frm)
        btns.grid(row=11, column=0, columnspan=8, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="恢復預設", command=self.defaults).pack(side="left", padx=4)
        self.ok_btn = ttk.Button(btns, text="✔ 套用", command=self.apply)
        self.ok_btn.pack(side="left", padx=4)
        ttk.Button(btns, text="取消", command=w.destroy).pack(side="left", padx=4)
        for hid in self.rows:
            self.refresh(hid)
        app.center_on_screen(w)

    def _mods(self, row):
        return ((MOD_CONTROL if row["ctrl"].get() else 0) | (MOD_ALT if row["alt"].get() else 0)
                | (MOD_SHIFT if row["shift"].get() else 0) | (MOD_WIN if row["win"].get() else 0))

    def _scan(self, mods):
        if mods not in self.cache:
            self.cache[mods] = self.app.hotkeys.scan(mods, [vk for _, vk in KEY_CHOICES])
        return self.cache[mods]

    @staticmethod
    def _key_of(value):
        return value.split()[0] if value and not value.startswith("（") else None

    def _combo(self, hid):
        row = self.rows[hid]
        name = self._key_of(row["key"].get())
        if not name:
            return None
        return [self._mods(row), dict(KEY_CHOICES)[name]]

    def _picked(self, hid):
        row = self.rows[hid]
        name = self._key_of(row["key"].get())
        row["key"].set(name or self.NONE)
        self.refresh(hid)

    def refresh(self, hid):
        row = self.rows[hid]
        mods = self._mods(row)
        name = self._key_of(row["key"].get())
        res = self._scan(mods) if mods & (MOD_CONTROL | MOD_ALT | MOD_WIN) else {}
        values = [self.NONE]
        for kname, vk in KEY_CHOICES:
            mark = {"taken": "  ✖ 已被占用", "own": "  ★ 目前使用中"}.get(res.get(vk), "")
            values.append(kname + mark)
        row["box"].configure(values=values)
        row["ok"] = True
        if not name:
            row["status"].configure(text="（不使用這個快捷鍵）", foreground="#666")
        elif not mods & (MOD_CONTROL | MOD_ALT | MOD_WIN):
            row["status"].configure(text="⚠ 至少要勾 Ctrl、" + ("Option 或 Cmd" if IS_MAC else "Alt 或 Win"), foreground="#c62828")
            row["ok"] = False
        elif res.get(dict(KEY_CHOICES)[name]) == "taken":
            row["status"].configure(text="❌ 已被占用，請換一個", foreground="#c62828")
            row["ok"] = False
        else:
            row["status"].configure(text="✅ 可以使用", foreground="#2e7d32")
        # 檢查三個快捷鍵彼此有沒有重複
        combos = {h: self._combo(h) for h in self.rows}
        for h, c in combos.items():
            if c and any(c == c2 for h2, c2 in combos.items() if h2 != h):
                self.rows[h]["status"].configure(text="❌ 和其他快捷鍵重複", foreground="#c62828")
                self.rows[h]["ok"] = False
        self.ok_btn.configure(state="normal" if all(r.get("ok", True) for r in self.rows.values()) else "disabled")

    def defaults(self):
        for hid, (mods, vk) in DEFAULT_HOTKEYS.items():
            row = self.rows[hid]
            row["ctrl"].set(bool(mods & MOD_CONTROL))
            row["alt"].set(bool(mods & MOD_ALT))
            row["shift"].set(bool(mods & MOD_SHIFT))
            row["win"].set(bool(mods & MOD_WIN))
            row["key"].set(VK_NAME[vk])
        for hid in self.rows:
            self.refresh(hid)

    def apply(self):
        new = {hid: self._combo(hid) for hid in self.rows}
        failed = self.app.apply_hotkeys(new)
        if failed:
            self.app.messagebox.showwarning(
                "快捷鍵設定", "這些快捷鍵設定失敗（可能剛好被占用）：\n" +
                "\n".join(f"{HOTKEY_NAMES[h]}：{hotkey_text(new[h])}" for h in failed), parent=self.win)
            self.cache.clear()
            for hid in self.rows:
                self.refresh(hid)
            return
        self.win.destroy()


# ---------------------------------------------------------------------------
# 多音字唸法設定視窗
# ---------------------------------------------------------------------------

class PronDialog:
    def __init__(self, app, word=""):
        tk, ttk = app.tk, app.ttk
        self.app = app
        self.win = w = tk.Toplevel(app.root)
        w.title("多音字／破音字唸法設定")
        w.attributes("-topmost", True)
        w.minsize(520, 420)
        frm = ttk.Frame(w, padding=12)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, justify="left", wraplength=560, text=(
            "電腦有時會把多音字唸錯。在這裡輸入「語詞」，再用「同音字」寫出正確的唸法，"
            "以後朗讀遇到這個語詞，就會照你設定的念。\n"
            "例：語詞「銀行」→ 唸成「銀杭」；語詞「快樂」→ 唸成「快勒」。"
            "（語詞寫長一點比較準，只會改到這個語詞，不影響其他地方）")).pack(anchor="w")

        form = ttk.Frame(frm)
        form.pack(fill="x", pady=(10, 4))
        self.word_var = tk.StringVar(value=word)
        self.say_var = tk.StringVar()
        self.note_var = tk.StringVar()
        ttk.Label(form, text="語詞").grid(row=0, column=0, sticky="w")
        ttk.Entry(form, textvariable=self.word_var, width=14, font=(UI_FONT, 12)).grid(row=1, column=0, padx=(0, 6))
        ttk.Label(form, text="唸成（同音字）").grid(row=0, column=1, sticky="w")
        say_entry = ttk.Entry(form, textvariable=self.say_var, width=14, font=(UI_FONT, 12))
        say_entry.grid(row=1, column=1, padx=(0, 6))
        ttk.Label(form, text="備註（可寫注音，不會念出來）").grid(row=0, column=2, sticky="w")
        ttk.Entry(form, textvariable=self.note_var, width=20, font=(UI_FONT, 12)).grid(row=1, column=2, padx=(0, 6))
        ttk.Button(form, text="➕ 加入／更新", command=self.save_item).grid(row=1, column=3)

        test = ttk.Frame(frm)
        test.pack(fill="x", pady=(2, 8))
        ttk.Button(test, text="🔊 聽「語詞」原本怎麼念", command=self.test_original).pack(side="left")
        ttk.Button(test, text="🔊 聽「唸成」", command=self.test_fixed).pack(side="left", padx=6)

        cols = ("word", "say", "note")
        self.tree = ttk.Treeview(frm, columns=cols, show="headings", height=9)
        for c, title, wd in zip(cols, ("語詞", "唸成", "備註"), (140, 140, 220)):
            self.tree.heading(c, text=title)
            self.tree.column(c, width=wd)
        self.tree.pack(fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self._select)

        bottom = ttk.Frame(frm)
        bottom.pack(fill="x", pady=(8, 0))
        ttk.Button(bottom, text="🗑 刪除選取的", command=self.delete).pack(side="left")
        ttk.Label(bottom, text="整句試聽：").pack(side="left", padx=(16, 2))
        self.sentence_var = tk.StringVar()
        ttk.Entry(bottom, textvariable=self.sentence_var, width=22, font=(UI_FONT, 11)).pack(side="left")
        ttk.Button(bottom, text="🔊", width=3, command=self.test_sentence).pack(side="left", padx=2)
        ttk.Button(bottom, text="關閉", command=w.destroy).pack(side="right")
        self.reload()
        if word:
            for d in app.pron.items:
                if d["word"] == word:
                    self.say_var.set(d["say"])
                    self.note_var.set(d.get("note", ""))
            say_entry.focus_set()
        app.center_on_screen(w)

    def reload(self):
        self.tree.delete(*self.tree.get_children())
        for d in sorted(self.app.pron.items, key=lambda d: d["word"]):
            self.tree.insert("", "end", iid=d["word"], values=(d["word"], d["say"], d.get("note", "")))

    def _select(self, e=None):
        sel = self.tree.selection()
        if sel:
            word, say, note = self.tree.item(sel[0], "values")
            self.word_var.set(word)
            self.say_var.set(say)
            self.note_var.set(note)

    def save_item(self):
        word, say = self.word_var.get().strip(), self.say_var.get().strip()
        if not word or not say:
            self.app.messagebox.showinfo("唸法設定", "「語詞」和「唸成」都要填。", parent=self.win)
            return
        self.app.pron.set(word, say, self.note_var.get().strip())
        self.reload()
        self.tree.selection_set(word)
        self.tree.see(word)

    def delete(self):
        for iid in self.tree.selection():
            self.app.pron.remove(iid)
        self.reload()

    def test_original(self):
        self.app.speak_preview(self.word_var.get(), use_rules=False)

    def test_fixed(self):
        self.app.speak_preview(self.say_var.get() or self.word_var.get(), use_rules=False)

    def test_sentence(self):
        self.app.speak_preview(self.sentence_var.get(), use_rules=True)


# ---------------------------------------------------------------------------
# 主視窗
# ---------------------------------------------------------------------------

class WinTrayIcon:
    """工作列右下角（系統匣）的小圖示。左鍵：打開完整面板；右鍵：選單（含完全關閉程式）。"""

    def __init__(self, app):
        self.icon = None
        try:
            import pystray
            from PIL import Image

            img = Image.open(resource_path("icon.ico"))
            post = lambda kind: (lambda icon, item: app.events.put(("tray", kind)))  # noqa: E731
            menu = pystray.Menu(
                pystray.MenuItem("🗔 打開完整面板", post("panel"), default=True),
                pystray.MenuItem("⚪ 顯示浮動圖案", post("mini")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("📷 框選截圖", post("capture")),
                pystray.MenuItem("↻ 同一區再截一次", post("repeat")),
                pystray.MenuItem("⏯ 暫停／播放", post("pause")),
                pystray.MenuItem("⏹ 停止朗讀", post("stop")),
                pystray.MenuItem("語速加快 ＋1", post("faster")),
                pystray.MenuItem("語速放慢 −1", post("slower")),
                pystray.MenuItem("文字放大", post("font_up")),
                pystray.MenuItem("文字縮小", post("font_down")),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("橫式閱讀（左→右）", post("dir_h"), radio=True,
                                 checked=lambda item: getattr(app, "read_dir", "h") != "v"),
                pystray.MenuItem("直式閱讀（上→下、右→左）", post("dir_v"), radio=True,
                                 checked=lambda item: getattr(app, "read_dir", "h") == "v"),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("✖ 完全關閉程式", post("quit")),
            )
            self.icon = pystray.Icon("ScreenOCRReader", img, APP_NAME, menu)
            threading.Thread(target=self.icon.run, daemon=True).start()
        except Exception:
            log_error("tray: " + traceback.format_exc())

    def notify(self, msg):
        if self.icon:
            try:
                self.icon.notify(msg, APP_NAME)
            except Exception:
                pass

    def stop(self):
        if self.icon:
            try:
                self.icon.stop()
            except Exception:
                pass


_MAC_TRAY_TARGET = None


def _mac_tray_target_class():
    global _MAC_TRAY_TARGET
    if _MAC_TRAY_TARGET is None:
        from Foundation import NSObject
        import objc

        class TrayTarget(NSObject):
            def initWithTray_(self, tray):
                self = objc.super(type(self), self).init()
                if self is None:
                    return None
                self.tray = tray
                return self

            def statusClick_(self, sender):
                self.tray.on_click()

            def menuPick_(self, sender):
                self.tray.app.events.put(("tray", str(sender.representedObject())))

        _MAC_TRAY_TARGET = TrayTarget
    return _MAC_TRAY_TARGET


class MacTrayIcon:
    """選單列（螢幕最上方右側）的小圖示。左鍵：打開完整面板；右鍵：選單（含完全關閉程式）。"""

    def __init__(self, app):
        self.app = app
        self.item = None
        try:
            import io

            from AppKit import NSImage, NSStatusBar
            from Foundation import NSData
            from PIL import Image

            img = Image.open(resource_path("icon.ico")).convert("RGBA").resize((44, 44), Image.LANCZOS)
            buf = io.BytesIO()
            img.save(buf, "PNG")
            data = NSData.dataWithBytes_length_(buf.getvalue(), len(buf.getvalue()))
            nsimg = NSImage.alloc().initWithData_(data)
            nsimg.setSize_((20, 20))
            self.target = _mac_tray_target_class().alloc().initWithTray_(self)
            self.item = NSStatusBar.systemStatusBar().statusItemWithLength_(-1)  # NSVariableStatusItemLength
            btn = self.item.button()
            btn.setImage_(nsimg)
            btn.setToolTip_(APP_NAME)
            btn.setTarget_(self.target)
            btn.setAction_("statusClick:")
            btn.sendActionOn_(4 | 16)  # 左鍵放開、右鍵放開
        except Exception:
            log_error("tray: " + traceback.format_exc())

    def on_click(self):
        from AppKit import NSApp

        ev = NSApp.currentEvent()
        right = ev is not None and (ev.type() == 4 or (int(ev.modifierFlags()) & 0x40000))  # 右鍵或 Control＋點
        if right:
            self._show_menu()
        else:
            self.app.events.put(("tray", "panel"))

    def _menu(self):
        from AppKit import NSMenu, NSMenuItem

        app = self.app
        m = NSMenu.alloc().init()
        m.setAutoenablesItems_(False)

        def add(title, kind, checked=False):
            it = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, "menuPick:", "")
            it.setTarget_(self.target)
            it.setRepresentedObject_(kind)
            if checked:
                it.setState_(1)
            m.addItem_(it)

        def sep():
            m.addItem_(NSMenuItem.separatorItem())

        add("🗔 打開完整面板", "panel")
        add("⚪ 顯示浮動圖案", "mini")
        sep()
        add("📷 框選截圖", "capture")
        add("↻ 同一區再截一次", "repeat")
        add("⏯ 暫停／播放", "pause")
        add("⏹ 停止朗讀", "stop")
        add("語速加快 ＋1", "faster")
        add("語速放慢 −1", "slower")
        add("文字放大", "font_up")
        add("文字縮小", "font_down")
        sep()
        vertical = getattr(app, "read_dir", "h") == "v"
        add("橫式閱讀（左→右）", "dir_h", checked=not vertical)
        add("直式閱讀（上→下、右→左）", "dir_v", checked=vertical)
        sep()
        add("✖ 完全關閉程式", "quit")
        return m

    def _show_menu(self):
        if not self.item:
            return
        self.item.setMenu_(self._menu())
        self.item.button().performClick_(None)
        self.item.setMenu_(None)

    def notify(self, msg):
        try:
            self.app.toast(msg)
        except Exception:
            pass

    def stop(self):
        if self.item:
            try:
                from AppKit import NSStatusBar

                NSStatusBar.systemStatusBar().removeStatusItem_(self.item)
            except Exception:
                pass
            self.item = None


TrayIcon = MacTrayIcon if IS_MAC else WinTrayIcon


class RedDot:
    """透明度低於 40% 時顯示的不透明小紅點（呼吸燈），讓使用者找得到視窗。"""

    KEY = "#ff00fe"

    def __init__(self, app, target):
        tk = app.tk
        self.app = app
        self.target = target  # "panel" 或 "mini"
        self.win = tk.Toplevel(app.root)
        self.win.withdraw()
        make_borderless(self.win, transparent=True, activates=False)
        self.win.attributes("-topmost", True)
        if IS_MAC:
            self.KEY = "systemTransparent"
        else:
            self.win.configure(bg=self.KEY)
            try:
                self.win.attributes("-transparentcolor", self.KEY)
            except Exception:
                pass
        mac_float(self.win, f"sor-dot-{target}", level=26, clear=True)
        scale = 1.0 if IS_MAC else app.root.winfo_fpixels("1i") / 96.0
        self.core = int(12 * scale)
        self.size = self.core * 2 + 4
        self.canvas = tk.Canvas(self.win, width=self.size, height=self.size, bg=self.KEY,
                                highlightthickness=0, bd=0, cursor="hand2")
        self.canvas.pack()
        self.shown = False
        c = self.canvas
        if target == "mini":
            mini = app.mini
            c.bind("<ButtonPress-1>", mini._press)
            c.bind("<B1-Motion>", mini._motion)
            c.bind("<ButtonRelease-1>", mini._release)
            if not IS_MAC:
                for b in RIGHT_BUTTONS:
                    c.bind(f"<ButtonPress-{b}>", mini._menu)
        else:
            c.bind("<ButtonPress-1>", lambda e: (app.reveal("panel"), app.root.lift()))
        c.bind("<Enter>", lambda e: app.reveal(target))

    def update(self, want, x=0, y=0):
        if not want:
            if self.shown:
                self.win.withdraw()
                self.shown = False
            return
        import math

        # 呼吸燈：紅點本身不變，整顆「一顯一透」慢慢浮現又慢慢消失（約 2.4 秒一次）
        b = 0.5 - 0.5 * math.cos(2 * math.pi * ((time.time() % 2.4) / 2.4))
        if not self.canvas.find_all():
            s, r, m = self.size, self.core, self.size / 2
            self.canvas.create_oval(m - r + 1, m - r + 1, m + r - 1, m + r - 1, fill="#ff1f1f",
                                    outline="white", width=max(1, r // 6))
        self.win.attributes("-alpha", 0.03 + 0.97 * b)
        s = self.size
        self.win.geometry(f"+{int(x - s / 2)}+{int(y - s / 2)}")
        if not self.shown:
            self.win.deiconify()
            self.shown = True
        self.win.lift()


class App:
    def __init__(self):
        import tkinter as tk
        from tkinter import messagebox, ttk

        self.tk, self.ttk, self.messagebox = tk, ttk, messagebox
        self.settings = load_settings()
        self.hotkeys_cfg = load_hotkeys(self.settings)
        self.pron = PronBook()
        self.ocr = OcrEngine()
        self.sapi = MacSpeaker() if IS_MAC else SapiSpeaker()
        self.google = GoogleSpeaker()
        self.cur = None          # 正在念的語音引擎
        self.reading = None      # {"base", "map", "spoken", "voice"}
        self.hl = None
        self._last_pos = 0
        self._live_job = None
        self.revealed = None
        self.hidden = False
        self.events = queue.Queue()
        self.last_rect = None
        self.busy = False
        self.last_items = None
        self.mode = "tray"       # "panel" 完整面板、"mini" 浮動小圖案、"tray" 只剩右下角圖示
        self._tray_tip_shown = False

        # 每次開程式都用這些預設：浮動圖案、透明度 100%、音量 100%、語速 0、Google 國語（台灣）
        self.settings.update(voice=GOOGLE_VOICE, rate=0, volume=100, alpha=1.0)

        root = self.root = tk.Tk()
        if IS_MAC:
            # 選單列小程式：不佔 Dock、不出現在 Cmd+Tab 清單
            try:
                from AppKit import NSApp

                NSApp.setActivationPolicy_(1)  # NSApplicationActivationPolicyAccessory
            except Exception:
                log_error("policy: " + traceback.format_exc())
        root.withdraw()
        root.title(APP_NAME)
        root.attributes("-topmost", True)
        root.minsize(380, 200)
        root.report_callback_exception = self._on_tk_error
        try:
            root.iconbitmap(default=resource_path("icon.ico"))
        except Exception:
            pass

        style = ttk.Style()
        try:
            style.theme_use("vista")
        except Exception:
            pass
        font = (UI_FONT, 11)
        big = (UI_FONT, 12, "bold")
        style.configure("Big.TButton", font=big, padding=(8, 6))
        style.configure("TButton", font=font)
        style.configure("TLabel", font=font)
        style.configure("TCheckbutton", font=font)
        style.configure("TRadiobutton", font=font)
        style.configure("TLabelframe.Label", font=(UI_FONT, 10))
        style.configure("Treeview", font=(UI_FONT, 11), rowheight=int((1.0 if IS_MAC else root.winfo_fpixels("1i") / 96) * 26))
        style.configure("Treeview.Heading", font=(UI_FONT, 11, "bold"))

        # --- 上方按鈕列 ---
        bar = self.bar = ttk.Frame(root, padding=(6, 6, 6, 2))
        bar.pack(side="top", fill="x")
        self.btn_capture = ttk.Button(bar, text="📷 框選截圖", style="Big.TButton", command=self.start_capture)
        self.btn_capture.pack(side="left")
        self.btn_repeat = ttk.Button(bar, text="↻ 同一區", command=self.repeat_capture, state="disabled")
        self.btn_repeat.pack(side="left", padx=(4, 0))
        self.btn_read = ttk.Button(bar, text="🔊 朗讀", command=self.read_text)
        self.btn_read.pack(side="left", padx=(4, 0))
        self.btn_pause = ttk.Button(bar, text="⏸ 暫停", command=self.toggle_pause, width=7)
        self.btn_pause.pack(side="left", padx=(4, 0))
        self.btn_stop = ttk.Button(bar, text="⏹ 停止", command=self.stop_speech)
        self.btn_stop.pack(side="left", padx=(4, 0))
        self.btn_mini = ttk.Button(bar, text="⊖ 縮小", command=self.enter_mini)
        self.btn_mini.pack(side="right")
        self.btn_more = ttk.Button(bar, text="⚙", width=3, command=self.toggle_options)
        self.btn_more.pack(side="right", padx=(0, 4))

        # --- 最下方：狀態列、快捷鍵說明（一直顯示）---
        self.status = ttk.Label(root, text="", foreground="#555", padding=(8, 0, 8, 4))
        self.status.pack(side="bottom", fill="x")
        self.info_label = ttk.Label(root, foreground="#555", justify="left", padding=(8, 2, 8, 2),
                                    wraplength=620 if IS_MAC else 0)
        self.info_label.pack(side="bottom", fill="x")

        # --- 設定區（在文字框下方，按 ⚙ 可收合）---
        self.options = opt = ttk.Frame(root, padding=(6, 2, 6, 2))
        opt.pack(side="bottom", fill="x", after=self.info_label)
        r = 0
        ttk.Label(opt, text="語音").grid(row=r, column=0, sticky="w")
        names = self.voice_names()
        self.voice_var = tk.StringVar(value=self.settings.get("voice") or self.sapi.default_voice())
        if names and self.voice_var.get() not in names:
            self.voice_var.set(self.sapi.default_voice() or names[0])
        self.voice_box = ttk.Combobox(opt, textvariable=self.voice_var, values=names, state="readonly", width=36)
        self.voice_box.grid(row=r, column=1, columnspan=2, sticky="we", padx=4, pady=2)
        r += 1

        def slider(row, label, var, lo, hi, res, cmd):
            ttk.Label(opt, text=label).grid(row=row, column=0, sticky="w")
            if IS_MAC:
                # Mac 上用 ttk 的滑桿（外觀跟著系統深色／淺色）；自己處理「一格一格」的刻度
                def on_move(v, var=var, res=res, cmd=cmd):
                    x = round(float(v) / res) * res
                    x = int(x) if res >= 1 else round(x, 2)
                    if var.get() != x:
                        var.set(x)
                    cmd()
                ttk.Scale(opt, from_=lo, to=hi, orient="horizontal", variable=var,
                          command=on_move).grid(row=row, column=1, sticky="we", padx=4)
            else:
                tk.Scale(opt, from_=lo, to=hi, resolution=res, orient="horizontal", showvalue=False,
                         variable=var, command=lambda v: cmd()).grid(row=row, column=1, sticky="we", padx=4)
            lab = ttk.Label(opt, width=5)
            lab.grid(row=row, column=2, sticky="w")
            return lab

        self.rate_var = tk.IntVar(value=int(self.settings.get("rate", 0)))
        self.rate_label = slider(r, "語速", self.rate_var, -10, 10, 1, self._on_rate_change)
        r += 1
        self.vol_var = tk.IntVar(value=int(self.settings.get("volume", 100)))
        self.vol_label = slider(r, "音量", self.vol_var, 0, 100, 1, self._on_volume_change)
        r += 1
        self.alpha_var = tk.DoubleVar(value=max(0.01, min(1.0, float(self.settings.get("alpha", 1.0)))))
        self.alpha_label = slider(r, "透明度", self.alpha_var, 0.01, 1.0, 0.01, self.apply_alpha)
        r += 1
        self.font_var = tk.IntVar(value=int(self.settings.get("font_size", 12)))
        self.font_label = slider(r, "文字大小", self.font_var, 9, 36, 1, self.apply_font)
        r += 1
        ttk.Label(opt, text="文字方向").grid(row=r, column=0, sticky="w")
        dirs = ttk.Frame(opt)
        dirs.grid(row=r, column=1, columnspan=2, sticky="w", padx=4)
        self.dir_var = tk.StringVar(value="v" if self.settings.get("read_dir") == "v" else "h")
        self.read_dir = self.dir_var.get()
        for label, v in (("橫式（左→右、上→下）", "h"), ("直式（上→下、右→左）", "v")):
            ttk.Radiobutton(dirs, text=label, value=v, variable=self.dir_var,
                            command=self._on_dir_change).pack(side="left", padx=(0, 10))
        r += 1
        ttk.Label(opt, text="小圖案").grid(row=r, column=0, sticky="w")
        sizes = ttk.Frame(opt)
        sizes.grid(row=r, column=1, columnspan=2, sticky="w", padx=4)
        self.size_var = tk.IntVar(value=int(self.settings.get("mini_size", 64)))
        for label, v in (("小", 48), ("中", 64), ("大", 84)):
            ttk.Radiobutton(sizes, text=label, value=v, variable=self.size_var,
                            command=self._resize_mini).pack(side="left", padx=(0, 10))
        r += 1
        checks = ttk.Frame(opt)
        checks.grid(row=r, column=0, columnspan=3, sticky="w", pady=(2, 0))
        self.auto_var = tk.BooleanVar(value=bool(self.settings.get("auto_read", True)))
        ttk.Checkbutton(checks, text="截圖後自動朗讀", variable=self.auto_var).pack(side="left")
        self.follow_var = tk.BooleanVar(value=bool(self.settings.get("follow", True)))
        ttk.Checkbutton(checks, text="跟讀標示", variable=self.follow_var,
                        command=self._clear_highlight).pack(side="left", padx=(12, 0))
        self.show_var = tk.BooleanVar(value=bool(self.settings.get("show_text", True)))
        ttk.Checkbutton(checks, text="顯示文字框", variable=self.show_var,
                        command=self.toggle_text).pack(side="left", padx=(12, 0))
        self.clickread_var = tk.BooleanVar(value=bool(self.settings.get("click_read", True)))
        ttk.Checkbutton(checks, text=CLICK_READ_LABEL, variable=self.clickread_var).pack(side="left", padx=(12, 0))
        r += 1
        tools = ttk.Frame(opt)
        tools.grid(row=r, column=0, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Button(tools, text="📋 複製文字", command=self.copy_text).pack(side="left")
        ttk.Button(tools, text="📖 多音字唸法設定…", command=self.open_pron).pack(side="left", padx=4)
        ttk.Button(tools, text="⌨ 快捷鍵設定…", command=self.open_hotkeys).pack(side="left")
        ttk.Button(tools, text="✖ 完全關閉程式", command=self.quit).pack(side="left", padx=4)
        opt.columnconfigure(1, weight=1)
        if not self.settings.get("show_options", True):
            opt.pack_forget()

        # --- 文字框（中間，會跟著視窗變大）---
        self.text_frame = ttk.Frame(root, padding=(6, 2, 6, 4))
        self.text = tk.Text(self.text_frame, wrap="char", height=6, width=42, undo=True, spacing1=2, spacing3=2)
        sb = ttk.Scrollbar(self.text_frame, command=self.text.yview)
        self.text.configure(yscrollcommand=sb.set)
        self.text.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        for b in RIGHT_BUTTONS:
            self.text.bind(f"<Button-{b}>", self._text_menu)
        self.text.bind("<ButtonPress-1>", self._text_press, add="+")
        self.text.bind("<ButtonRelease-1>", self._text_release, add="+")
        self._press_at = None
        self.apply_font()
        self.toggle_text()
        self._update_labels()

        if self.settings.get("x") is not None and self.settings.get("y") is not None:
            root.geometry(f"+{int(self.settings['x'])}+{int(self.settings['y'])}")
        # Windows：× 收到右下角；Mac：× 就是直接關掉程式（Cmd+Q 也一樣）
        root.protocol("WM_DELETE_WINDOW", self.quit if IS_MAC else self.hide_to_tray)
        if IS_MAC:
            try:
                root.createcommand("::tk::mac::Quit", self.quit)
            except Exception:
                pass

        if IS_MAC:
            self._install_mac_rightclick()
            try:
                import signal

                # 再次點開 app 時（第二個程式送來 SIGUSR1）→ 打開完整面板
                signal.signal(signal.SIGUSR1, lambda *a: self.events.put(("tray", "panel")))
            except Exception:
                pass
        self.mini = MiniIcon(self)
        self.dots = {"panel": RedDot(self, "panel"), "mini": RedDot(self, "mini")}
        self.apply_alpha()

        # 全域快捷鍵
        self.hotkeys = GlobalHotkeys(lambda hid: self.events.put(("hotkey", hid)))
        self.hotkeys.start()
        self.hotkeys.ready.wait(2)
        failed = self.hotkeys.apply(self.hotkeys_cfg)
        self._update_info()
        msg = f"按「框選截圖」或 {hotkey_text(self.hotkeys_cfg.get(HK_CAPTURE))} 開始"
        if failed:
            msg = ("快捷鍵被占用：" + "、".join(f"{HOTKEY_NAMES[h]} {hotkey_text(self.hotkeys_cfg[h])}" for h in failed)
                   + "（可按「⌨ 快捷鍵設定」更換）")
        if self.sapi.error:
            msg = "找不到語音引擎：" + self.sapi.error
        elif not self.sapi.has_chinese_voice():
            msg = ("電腦沒有中文語音：請到 系統設定→輔助使用→朗讀內容→系統語音→管理語音，下載「中文（台灣）」"
                   if IS_MAC else "電腦沒有中文語音：請到 設定→時間與語言→語音 新增「中文(台灣)」")
        self.set_status(msg)

        self.tray = TrayIcon(self)
        self.enter_mini()

        # 背景先載入 OCR 模型，第一次截圖會比較快
        threading.Thread(target=self._preload, daemon=True).start()
        root.after(40, self._poll)

    def _install_mac_rightclick(self):
        """Mac：Tk 不一定收得到浮動圖案上的右鍵，所以用 macOS 原生事件接住：
        在浮動小圖案（或它的小紅點）上按右鍵、或 Ctrl＋點，就彈出功能選單。其他地方交還給 Tk 照原本處理。"""
        try:
            from AppKit import NSEvent

            def handler(ev):
                try:
                    t = int(ev.type())
                    ctrl = bool(int(ev.modifierFlags()) & 0x40000)
                    if t == 3 or (t == 1 and ctrl):  # 右鍵按下，或 Ctrl＋左鍵按下
                        w = ev.window()
                        title = str(w.title()) if w is not None else ""
                        if title in ("sor-mini", "sor-dot-mini"):
                            # 注意：這裡是在 macOS 的事件回呼裡，不能呼叫任何 Tk 的東西（會讓 Tk 當掉）；
                            # 只把「要開選單」放進佇列，交給 Tk 自己的計時器去處理
                            self.events.put(("mini_menu", None))
                            return None  # 這個事件我們處理掉
                except Exception:
                    log_error("rightclick: " + traceback.format_exc())
                return ev

            self._rc_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_((1 << 3) | (1 << 1), handler)
        except Exception:
            log_error("rightclick monitor: " + traceback.format_exc())

    def _mini_menu_at_pointer(self):
        if getattr(self, "_menu_open", False):
            return  # 選單已經開著，不要再開一個（選單裡面再開選單會當掉）
        self._menu_open = True
        self.mini._hide_tip()
        x, y = self.root.winfo_pointerxy()
        menu = self.build_menu(self.mini.win)
        try:
            menu.tk_popup(x, y)
        finally:
            menu.grab_release()
            self._menu_open = False

    @property
    def mini_mode(self):
        """完整面板沒有打開（浮動圖案或只剩右下角圖示）。"""
        return self.mode != "panel"

    # ---- 介面小工具 ----
    def voice_names(self):
        names = self.sapi.voice_names()
        tw = [n for n in names if "taiwan" in n.lower() or "台灣" in n]
        pos = names.index(tw[-1]) + 1 if tw else 0
        return names[:pos] + [GOOGLE_VOICE] + names[pos:]

    def _update_labels(self):
        self.rate_label.configure(text=f"{self.rate_var.get():+d}" if self.rate_var.get() else "0")
        self.vol_label.configure(text=f"{self.vol_var.get()}%")
        self.alpha_label.configure(text=f"{round(float(self.alpha_var.get()) * 100)}%")
        self.font_label.configure(text=str(self.font_var.get()))

    def _update_info(self):
        hk = self.hotkeys_cfg
        self.info_label.configure(text=(
            f"⌨ 快捷鍵：{hotkey_text(hk.get(HK_CAPTURE))} 框選截圖　{hotkey_text(hk.get(HK_REPEAT))} 同一區重念　"
            f"{hotkey_text(hk.get(HK_PAUSE))} 暫停／播放　{hotkey_text(hk.get(HK_STOP))} 停止朗讀\n"
            f"　　　　　{hotkey_text(hk.get(HK_FASTER))} 語速加快　{hotkey_text(hk.get(HK_SLOWER))} 語速放慢　"
            f"{hotkey_text(hk.get(HK_FONT_UP))} 文字放大　{hotkey_text(hk.get(HK_FONT_DOWN))} 文字縮小\n"
            f"小技巧：按住 {'⌘ 或 Ctrl' if IS_MAC else 'Ctrl'} 點文字框裡的某個字，就從那個字開始念（一般點一下可以直接改字）；反白一段再按「朗讀」只念那一段；"
            f"文字上按右鍵可設定唸法。　版本 {APP_VERSION}"))

    def set_status(self, msg):
        self.status.configure(text=msg)

    def notify(self, msg):
        """狀態列顯示；縮成小圖案時改用小提示泡泡。"""
        self.set_status(msg)
        if self.mode == "mini":
            self.mini.toast(msg)
        elif self.mode == "tray":
            self.tray.notify(msg)

    def toast(self, msg, ms=3500):
        """螢幕右上角的小提示（面板收起來、沒有狀態列可看的時候用）。"""
        tk = self.tk
        old = getattr(self, "_toast", None)
        if old is not None:
            try:
                old.destroy()
            except Exception:
                pass
        t = self._toast = tk.Toplevel(self.root)
        make_borderless(t, activates=False)
        t.attributes("-topmost", True)
        tk.Label(t, text=msg, bg="#333333", fg="white", justify="left", font=(UI_FONT, 12), padx=14, pady=10).pack()
        t.update_idletasks()
        sw = t.winfo_screenwidth()
        t.geometry(f"+{max(0, sw - t.winfo_reqwidth() - 20)}+{40 if IS_MAC else 20}")
        mac_float(t, f"sor-toast-{id(t)}", level=101)
        t.after(ms, lambda: t.winfo_exists() and t.destroy())

    def center_on_screen(self, win):
        win.update_idletasks()
        if self.mode == "mini":
            x, y = self.mini.win.winfo_x(), self.mini.win.winfo_y()
        elif self.mode == "panel":
            x, y = self.root.winfo_x(), self.root.winfo_y()
        else:
            x, y = win.winfo_screenwidth() // 2, win.winfo_screenheight() // 4
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
        x = max(0, min(x + 30 if IS_MAC else x - w // 3, sw - w))
        y = max(0, min(y + 40, sh - h - 40))
        win.geometry(f"+{x}+{y}")
        win.lift()
        mac_float(win, win.title(), level=27)  # Mac：要浮在面板（層級 25）上面
        win.focus_force()

    def toggle_options(self):
        if self.options.winfo_ismapped():
            self.options.pack_forget()
        else:
            self.options.pack(side="bottom", fill="x", after=self.info_label)
        self.root.geometry("")

    def toggle_text(self):
        if self.show_var.get():
            self.text_frame.pack(side="top", fill="both", expand=True, after=self.bar)
        else:
            self.text_frame.pack_forget()
            self.root.geometry("")

    def apply_font(self):
        size = int(self.font_var.get())
        self.text.configure(font=(UI_FONT, size))
        # 跟讀時正在念的字：黃底、粗體、稍微放大
        self.text.tag_configure("reading", background="#ffd54f", foreground="#000000",
                                font=(UI_FONT, size + max(2, size // 4), "bold"))
        self.text.tag_raise("reading")
        if hasattr(self, "font_label"):
            self.font_label.configure(text=str(size))

    def apply_alpha(self):
        a = max(0.01, min(1.0, float(self.alpha_var.get())))
        self.root.attributes("-alpha", 1.0 if self.revealed == "panel" else a)
        self.mini.apply_alpha(1.0 if self.revealed == "mini" else a)
        if hasattr(self, "alpha_label"):
            self.alpha_label.configure(text=f"{round(a * 100)}%")

    def reveal(self, target):
        """滑鼠移到小紅點上：暫時把視窗變清楚，滑鼠離開後恢復透明。"""
        if float(self.alpha_var.get()) >= 0.4:
            return
        self.revealed = target
        self.apply_alpha()

    def _check_reveal(self):
        if not self.revealed:
            return
        px, py = self.root.winfo_pointerxy()
        win = self.mini.win if self.revealed == "mini" else self.root
        dot = self.dots[self.revealed].win

        def inside(w, pad=8):
            try:
                x, y = w.winfo_rootx(), w.winfo_rooty()
                if w is self.root:
                    y = w.winfo_y()
                return x - pad <= px <= x + w.winfo_width() + pad and y - pad <= py <= w.winfo_rooty() + w.winfo_height() + pad
            except Exception:
                return False

        if not (inside(win) or inside(dot)):
            self.revealed = None
            self.apply_alpha()

    def _update_dots(self):
        low = float(self.alpha_var.get()) < 0.4 and not self.hidden
        panel_on = low and not self.mini_mode and self.root.state() == "normal"
        if panel_on:
            x = self.root.winfo_rootx() + self.root.winfo_width() / 2
            y = (self.root.winfo_y() + self.root.winfo_rooty()) / 2
            if self.root.winfo_rooty() - self.root.winfo_y() < 10:
                y = self.root.winfo_rooty() + 10
        self.dots["panel"].update(panel_on, *((x, y) if panel_on else ()))
        mini_on = low and self.mini_mode and self.mini.visible
        if mini_on:
            w = self.mini.win
            self.dots["mini"].update(True, w.winfo_x() + w.winfo_width() / 2, w.winfo_y() + w.winfo_height() / 2)
        else:
            self.dots["mini"].update(False)

    def _on_tk_error(self, exc, val, tb):
        err = "".join(traceback.format_exception(exc, val, tb))
        log_error(err)
        self.notify(f"發生錯誤：{val}")

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
                    if payload == HK_CAPTURE:
                        self.start_capture()
                    elif payload == HK_REPEAT:
                        self.repeat_capture()
                    elif payload == HK_STOP:
                        self.stop_speech()
                    elif payload == HK_PAUSE:
                        self.play_pause()
                    elif payload == HK_FASTER:
                        self._nudge_rate(1, show=True)
                    elif payload == HK_SLOWER:
                        self._nudge_rate(-1, show=True)
                    elif payload == HK_FONT_UP:
                        self._nudge_font(1, show=True)
                    elif payload == HK_FONT_DOWN:
                        self._nudge_font(-1, show=True)
                elif kind == "tray":
                    {"panel": self.leave_mini, "mini": self.enter_mini, "capture": self.start_capture,
                     "repeat": self.repeat_capture, "stop": self.stop_speech, "quit": self.quit,
                     "pause": self.play_pause,
                     "dir_h": lambda: self._set_dir("h"), "dir_v": lambda: self._set_dir("v"),
                     "faster": lambda: self._nudge_rate(1, show=True),
                     "slower": lambda: self._nudge_rate(-1, show=True),
                     "font_up": lambda: self._nudge_font(1, show=True),
                     "font_down": lambda: self._nudge_font(-1, show=True)}[payload]()
                elif kind == "mini_menu":
                    self._mini_menu_at_pointer()
                elif kind == "ocr_done":
                    self._ocr_done(payload)
                elif kind == "error":
                    self.busy = False
                    self.mini.set_state("idle")
                    self.notify(payload)
        except queue.Empty:
            pass
        for fn in (self._tick, self._update_dots, self._check_reveal):
            try:
                fn()
            except Exception:
                log_error(traceback.format_exc())
        self.root.after(40, self._poll)

    # ---- 縮成小圖案 ----
    def enter_mini(self):
        if self.mode == "panel":
            self.settings.update(x=self.root.winfo_x(), y=self.root.winfo_y())
        self.mode = "mini"
        self.revealed = None
        self.root.withdraw()
        self.mini.show()
        self.apply_alpha()

    def hide_to_tray(self):
        """按完整面板的 ×：不關掉程式，收到工作列右下角的小圖示。"""
        if self.mode == "panel":
            self.settings.update(x=self.root.winfo_x(), y=self.root.winfo_y())
        elif self.mode == "mini":
            self.settings.update(mini_x=self.mini.win.winfo_x(), mini_y=self.mini.win.winfo_y())
        self.mode = "tray"
        self.revealed = None
        self.root.withdraw()
        self.mini.hide()
        if not self._tray_tip_shown:
            self._tray_tip_shown = True
            self.tray.notify("程式還在執行，收到右下角了。\n點圖示打開面板；按右鍵可「完全關閉程式」。")

    def leave_mini(self):
        self.mode = "panel"
        self.revealed = None
        self.mini.hide()
        self.root.deiconify()
        self.root.attributes("-topmost", True)
        self.root.lift()
        mac_float(self.root, APP_NAME)
        if IS_MAC:
            try:
                from AppKit import NSApp

                NSApp.activateIgnoringOtherApps_(True)  # 面板要能打字
            except Exception:
                pass
        self.apply_alpha()

    def build_menu(self, parent):
        """浮動小圖案的右鍵選單（完整面板上也都有這些功能）。"""
        tk = self.tk
        f = (UI_FONT, 11)
        m = tk.Menu(parent, tearoff=0, font=f)
        hk = self.hotkeys_cfg
        speaking = self.cur is not None
        m.add_command(label="📷 框選截圖", accelerator=hotkey_text(hk.get(HK_CAPTURE)) if hk.get(HK_CAPTURE) else "",
                      command=self.start_capture)
        m.add_command(label="↻ 同一區再截一次", accelerator=hotkey_text(hk.get(HK_REPEAT)) if hk.get(HK_REPEAT) else "",
                      command=self.repeat_capture, state="normal" if self.last_rect else "disabled")
        m.add_command(label="🔊 重新朗讀", command=self.read_text)
        paused = speaking and self.cur.paused
        m.add_command(label="▶ 繼續" if paused else "⏸ 暫停",
                      accelerator=hotkey_text(hk.get(HK_PAUSE)) if hk.get(HK_PAUSE) else "",
                      command=self.toggle_pause, state="normal" if speaking else "disabled")
        m.add_command(label="⏹ 停止", accelerator=hotkey_text(hk.get(HK_STOP)) if hk.get(HK_STOP) else "",
                      command=self.stop_speech, state="normal" if speaking else "disabled")
        m.add_separator()

        vm = tk.Menu(m, tearoff=0, font=f)
        for name in self.voice_names():
            vm.add_radiobutton(label=name, variable=self.voice_var, value=name)
        m.add_cascade(label="🗣 語音", menu=vm)

        rm = tk.Menu(m, tearoff=0, font=f)
        for label, v in (("很慢", -6), ("稍慢", -3), ("正常", 0), ("稍快", 3), ("很快", 6)):
            rm.add_radiobutton(label=f"{label}（{v:+d}）" if v else label, variable=self.rate_var, value=v,
                               command=self._on_rate_change)
        rm.add_separator()
        rm.add_command(label="慢一點 −1", command=lambda: self._nudge_rate(-1))
        rm.add_command(label="快一點 ＋1", command=lambda: self._nudge_rate(1))
        m.add_cascade(label=f"🐢 語速（目前 {self.rate_var.get():+d}）", menu=rm)

        vol = tk.Menu(m, tearoff=0, font=f)
        for v in (100, 80, 60, 40, 20):
            vol.add_radiobutton(label=f"{v}%", variable=self.vol_var, value=v, command=self._on_volume_change)
        m.add_cascade(label=f"🔉 音量（目前 {self.vol_var.get()}%）", menu=vol)

        am = tk.Menu(m, tearoff=0, font=f)
        for v in (1.0, 0.8, 0.6, 0.4, 0.2, 0.1, 0.01):
            am.add_radiobutton(label=f"{round(v * 100)}%" + ("（會出現小紅點）" if v < 0.4 else ""),
                               variable=self.alpha_var, value=v, command=self.apply_alpha)
        m.add_cascade(label=f"🌫 透明度（目前 {round(float(self.alpha_var.get()) * 100)}%）", menu=am)

        sm = tk.Menu(m, tearoff=0, font=f)
        for label, v in (("小", 48), ("中", 64), ("大", 84)):
            sm.add_radiobutton(label=label, variable=self.size_var, value=v, command=self._resize_mini)
        m.add_cascade(label="⚪ 圖案大小", menu=sm)

        dm = tk.Menu(m, tearoff=0, font=f)
        dm.add_radiobutton(label="橫式（左→右、上→下）", variable=self.dir_var, value="h", command=self._on_dir_change)
        dm.add_radiobutton(label="直式（上→下、右→左）", variable=self.dir_var, value="v", command=self._on_dir_change)
        m.add_cascade(label="📐 文字方向（目前 " + ("直式" if self.dir_var.get() == "v" else "橫式") + "）", menu=dm)

        m.add_checkbutton(label="截圖後自動朗讀", variable=self.auto_var)
        m.add_checkbutton(label="跟讀標示", variable=self.follow_var, command=self._clear_highlight)
        m.add_checkbutton(label=CLICK_READ_LABEL, variable=self.clickread_var)
        m.add_separator()
        m.add_command(label="📋 複製辨識出的文字", command=self.copy_text)
        m.add_command(label="📖 多音字唸法設定…", command=self.open_pron)
        m.add_command(label="⌨ 快捷鍵設定…", command=self.open_hotkeys)
        m.add_separator()
        m.add_command(label="🗔 打開完整面板", command=self.leave_mini)
        if not IS_MAC:
            m.add_command(label="⤓ 收到右下角圖示", command=self.hide_to_tray)
        m.add_command(label="✖ 完全關閉程式", command=self.quit)
        return m

    def _nudge_font(self, d, show=False):
        self.font_var.set(max(9, min(36, int(self.font_var.get()) + d)))
        self.apply_font()
        if show:
            msg = f"文字大小 {self.font_var.get()}"
            self.set_status(msg)
            if self.mode == "mini":
                self.mini.toast(msg, ms=1000)

    def _nudge_rate(self, d, show=False):
        self.rate_var.set(max(-10, min(10, self.rate_var.get() + d)))
        self._on_rate_change()
        if show:
            v = self.rate_var.get()
            msg = f"語速 {v:+d}" if v else "語速 0（正常）"
            self.set_status(msg)
            if self.mode == "mini":
                self.mini.toast(msg, ms=1000)

    def _resize_mini(self):
        self.settings["mini_size"] = self.size_var.get()
        self.mini._redraw()
        if self.mini.visible:
            self.mini.show()

    def copy_text(self):
        text = self.text.get("1.0", "end-1c").strip()
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.notify("已複製文字" if text else "沒有文字可以複製")

    def _text_menu(self, e):
        tk = self.tk
        if IS_MAC and (e.state & 0x4) and self.clickread_var.get():
            # Mac 上 Control＋點一下會被系統當成右鍵，這裡把它當成「從這個字開始念」
            self._click_read_at(e)
            return
        m = tk.Menu(self.root, tearoff=0, font=(UI_FONT, 11))
        try:
            sel = self.text.get("sel.first", "sel.last").strip()
        except Exception:
            sel = ""
        if sel and len(sel) <= 12 and "\n" not in sel:
            m.add_command(label=f"📖 設定「{sel}」的唸法…", command=lambda: self.open_pron(sel))
        else:
            m.add_command(label="📖 設定唸法…（先反白要設定的語詞）", command=self.open_pron)
        m.add_command(label="🔊 朗讀" + ("反白的部分" if sel else "全部"), command=self.read_text)
        m.add_separator()
        m.add_command(label="複製", command=lambda: self.text.event_generate("<<Copy>>"))
        m.add_command(label="貼上", command=lambda: self.text.event_generate("<<Paste>>"))
        m.add_command(label="全選", command=lambda: self.text.tag_add("sel", "1.0", "end-1c"))
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    def open_pron(self, word=""):
        if self.mini_mode:
            self.leave_mini()
        PronDialog(self, word)

    def open_hotkeys(self):
        if self.mini_mode:
            self.leave_mini()
        HotkeyDialog(self)

    def apply_hotkeys(self, new):
        failed = self.hotkeys.apply(new)
        if failed:
            self.hotkeys.apply(self.hotkeys_cfg)  # 失敗就恢復原本的
            return failed
        self.hotkeys_cfg = {k: (list(v) if v else None) for k, v in new.items()}
        self.settings["hotkeys"] = {str(k): v for k, v in self.hotkeys_cfg.items()}
        save_settings(self.settings)
        self._update_info()
        self.set_status("快捷鍵已更新")
        return []

    # ---- 截圖流程 ----
    def _hide_self(self):
        self.hidden = True
        for d in self.dots.values():
            d.update(False)
        if self.mode == "mini":
            self.mini.hide()
        elif self.mode == "panel":
            self.root.withdraw()

    def _show_self(self):
        self.hidden = False
        if self.mode == "mini":
            self.mini.show()
        elif self.mode == "panel":
            self.root.deiconify()
            self.root.attributes("-topmost", True)

    def _need_screen_access(self):
        """Mac：沒允許「螢幕錄製」就截不到其他視窗的內容，先提醒。"""
        if mac_screen_access(request=True):
            return False
        self.notify("要先允許「螢幕錄製」：系統設定→隱私權與安全性→螢幕與系統錄音，打開本程式，再重開程式")
        return True

    def start_capture(self):
        if self.busy:
            return
        if self._need_screen_access():
            return
        self.busy = True
        self.stop_speech()
        self._hide_self()  # 先把自己藏起來，免得被截進去
        self.root.after(200, self._open_selector)

    def _open_selector(self):
        try:
            RegionSelector(self.root, self._selected)
        except Exception as e:
            self._show_self()
            self.busy = False
            self.notify(f"截圖失敗：{e}")

    def _selected(self, result):
        self._show_self()
        if result is None:
            self.busy = False
            self.set_status("已取消")
            return
        img, rect = result
        self.last_rect = rect
        self.btn_repeat.configure(state="normal")
        self._run_ocr(img)

    def repeat_capture(self):
        if self.busy or not self.last_rect or self._need_screen_access():
            return
        self.busy = True
        self.stop_speech()
        self._hide_self()
        self.root.after(150, self._grab_last)

    def _grab_last(self):
        try:
            img = grab_rect(self.last_rect)
        except Exception as e:
            img = None
            self.set_status(f"截圖失敗：{e}")
        self._show_self()
        if img is None:
            self.busy = False
            return
        self._run_ocr(img)

    def _run_ocr(self, img):
        self.set_status("辨識文字中…")
        self.mini.set_state("busy")

        def work():
            try:
                t = time.time()
                items = self.ocr.recognize(img)
                self.events.put(("ocr_done", (items, time.time() - t)))
            except Exception as e:
                self.events.put(("error", f"辨識失敗：{e}"))

        threading.Thread(target=work, daemon=True).start()

    def _set_dir(self, v):
        self.dir_var.set(v)
        self._on_dir_change()

    def _on_dir_change(self):
        """改文字方向：如果文字框是剛辨識出來、沒被改過的，就馬上用新方向重排。"""
        v = self.dir_var.get()
        if v == self.read_dir:
            return
        old_text = OcrEngine.to_text(self.last_items, self.read_dir == "v") if self.last_items else None
        self.read_dir = v
        msg = "文字方向：" + ("直式（上→下、右→左）" if v == "v" else "橫式（左→右、上→下）")
        if old_text is not None and self.text.get("1.0", "end-1c") == old_text:
            was_speaking = self.cur is not None
            self.stop_speech()
            self.text.delete("1.0", "end")
            self.text.insert("1.0", OcrEngine.to_text(self.last_items, v == "v"))
            msg += "，已重新排列"
            if was_speaking:
                self.read_text()
        self.set_status(msg)
        if self.mode == "mini":
            self.mini.toast(msg, ms=1200)

    def _ocr_done(self, payload):
        items, secs = payload
        self.last_items = items
        text = OcrEngine.to_text(items, self.dir_var.get() == "v")
        self.busy = False
        self.mini.set_state("idle")
        self.text.delete("1.0", "end")
        self.text.insert("1.0", text)
        if not text.strip():
            self.notify("沒有辨識到文字，請框大一點或換個範圍")
            return
        self.set_status(f"辨識完成（{secs:.1f} 秒），共 {len(text)} 字")
        if self.auto_var.get() or self.mini_mode:
            self.read_text()

    # ---- 朗讀 ----
    def _speaker_for(self, voice):
        return self.google if voice == GOOGLE_VOICE else self.sapi

    def _start(self, spoken, voice, base=None, idxmap=None):
        self.stop_speech()
        sp = self._speaker_for(voice)
        if sp is self.google:
            sp.speak(spoken, self.rate_var.get(), self.vol_var.get())
        else:
            sp.speak(spoken, voice, self.rate_var.get(), self.vol_var.get())
        self.cur = sp
        self._last_pos = 0
        self.reading = {"base": base, "map": idxmap, "spoken": spoken, "voice": voice}
        self.btn_pause.configure(text="⏸ 暫停")
        self.mini.set_state("speaking")

    def _text_press(self, e):
        self._press_at = (e.x, e.y, e.state)

    def _text_release(self, e):
        """按住 Ctrl 在文字框點一下某個字，就從那個字開始往後念。
        （一般點一下只是放游標，方便編輯文字。）"""
        p, self._press_at = self._press_at, None
        if not p or not self.clickread_var.get():
            return
        x0, y0, state = p
        if abs(e.x - x0) > 4 or abs(e.y - y0) > 4:  # 拖曳反白
            return
        want = 0x4 | (0x8 if IS_MAC else 0)  # Mac 也可以按 ⌘（Command）
        if not (state & want) or state & 0x1:  # 要按著 Ctrl（Mac：或 ⌘）、沒按 Shift
            return
        self._click_read_at(e)

    def _click_read_at(self, e):
        idx = self.text.index(f"@{e.x},{e.y}")
        box = self.text.bbox(idx)
        if not box or not (box[0] <= e.x <= box[0] + box[2] and box[1] <= e.y <= box[1] + box[3]):
            return  # 點在行尾後面的空白處
        self.root.after(30, lambda: self._read_from_click(idx))

    def _read_from_click(self, idx):
        self.text.tag_remove("sel", "1.0", "end")
        self.read_text(start=idx)

    def read_text(self, start=None):
        if start is not None:
            base = start
            content = self.text.get(start, "end-1c")
        else:
            try:
                base = self.text.index("sel.first")
                content = self.text.get("sel.first", "sel.last")
            except Exception:
                base = "1.0"
                content = self.text.get("1.0", "end-1c")
        spoken, idxmap = prepare_speech(content, self.pron.rules())
        if not spoken:
            self.notify("沒有可以朗讀的文字")
            return
        try:
            self._start(spoken, self.voice_var.get(), base, idxmap)
        except Exception as e:
            self.notify(f"朗讀失敗：{e}")

    def speak_preview(self, text, use_rules=True):
        """多音字設定視窗的試聽（不標示文字）。"""
        spoken, _ = prepare_speech(text, self.pron.rules() if use_rules else ())
        if not spoken:
            return
        try:
            self._start(spoken, self.voice_var.get())
        except Exception as e:
            self.set_status(f"朗讀失敗：{e}")

    # ---- 朗讀中即時調整語速、音量 ----
    def _on_rate_change(self):
        self._update_labels()
        self._schedule_live()

    def _on_volume_change(self):
        self._update_labels()
        self._schedule_live()

    def _schedule_live(self):
        if self.cur is self.google:
            self.google.set_rate(self.rate_var.get())
            self.google.set_volume(self.vol_var.get())
            return
        if hasattr(self.sapi, "set_live") and self.cur is self.sapi:
            self.sapi.set_live(int(self.rate_var.get()), int(self.vol_var.get()))  # 音量馬上生效
        if self._live_job:
            self.root.after_cancel(self._live_job)
        self._live_job = self.root.after(250, self._apply_live_sapi)

    def _apply_live_sapi(self):
        """內建語音要到下一句才會換速度，所以從目前念到的詞重新開始念，馬上就聽得到效果。"""
        self._live_job = None
        sp, r = self.sapi, self.reading
        if self.cur is not sp or not r:
            return
        if sp.paused:
            try:
                if hasattr(sp, "set_live"):
                    sp.set_live(int(self.rate_var.get()), int(self.vol_var.get()))
                else:
                    sp.voice.Rate = int(self.rate_var.get())
                    sp.voice.Volume = int(self.vol_var.get())
            except Exception:
                pass
            return
        pos = max(0, min(self._last_pos, len(r["spoken"])))
        rest = r["spoken"][pos:]
        if not rest.strip():
            return
        try:
            sp.speak(rest, r["voice"], self.rate_var.get(), self.vol_var.get())
        except Exception as e:
            self.set_status(f"朗讀失敗：{e}")
            return
        self._last_pos = 0
        self.reading = dict(r, spoken=rest, map=r["map"][pos:] if r["map"] else None)

    def play_pause(self):
        """暫停／播放快捷鍵：念到一半就暫停或繼續；沒在念就開始念。"""
        if self.cur:
            self.toggle_pause()
            msg = "⏸ 暫停" if self.cur.paused else "▶ 繼續"
        else:
            self.read_text()
            msg = "▶ 開始朗讀" if self.cur else ""
        if msg:
            self.set_status(msg)
            if self.mode == "mini":
                self.mini.toast(msg, ms=900)

    def toggle_pause(self):
        sp = self.cur
        if not sp:
            return
        if sp.paused:
            sp.resume()
            self.btn_pause.configure(text="⏸ 暫停")
            self.mini.set_state("speaking")
            if sp is self.sapi and self._live_job is None:
                pass
        else:
            sp.pause()
            self.btn_pause.configure(text="▶ 繼續")
            self.mini.set_state("paused")

    def stop_speech(self):
        if self.cur:
            self.cur.stop()
        self._speech_ended()

    def _speech_ended(self):
        self.cur = None
        self.reading = None
        self._clear_highlight()
        self.btn_pause.configure(text="⏸ 暫停")
        if not self.busy:
            self.mini.set_state("idle")

    def _clear_highlight(self):
        self.text.tag_remove("reading", "1.0", "end")
        self.hl = None

    def _tick(self):
        sp = self.cur
        if not sp:
            return
        state, pos, length, err = sp.poll()
        if state == "done":
            self._speech_ended()
            return
        if state == "error":
            r = self.reading
            if sp is self.google and not sp.played_any and r and not self.sapi.error:
                # Google 連不上：改用電腦內建語音
                self.notify((err or "Google 語音失敗") + "，先改用電腦內建語音")
                self._start(r["spoken"], self.sapi.default_voice(), r["base"], r["map"])
            else:
                self.notify(err or "朗讀失敗")
                self._speech_ended()
            return
        if state == "loading" and sp is self.google and not sp.played_any:
            self.set_status("正在取得 Google 語音…")
        if length > 0:
            self._last_pos = pos
        r = self.reading
        if r and r["map"] and self.follow_var.get() and length > 0:
            self._highlight(pos, length)

    def _highlight(self, pos, length):
        m = self.reading["map"]
        rng = [m[k] for k in range(pos, min(len(m), pos + length)) if m[k] >= 0]
        if not rng:
            return
        a, b = min(rng), max(rng) + 1
        if self.hl == (a, b):
            return
        self.hl = (a, b)
        base = self.reading["base"]
        self.text.tag_remove("reading", "1.0", "end")
        self.text.tag_add("reading", f"{base}+{a}c", f"{base}+{b}c")
        self.text.see(f"{base}+{b}c")
        self.text.see(f"{base}+{a}c")

    def quit(self):
        try:
            self.stop_speech()
            self.google.cleanup()
        except Exception:
            pass
        if self.mode == "panel":
            self.settings.update(x=self.root.winfo_x(), y=self.root.winfo_y())
        elif self.mode == "mini":
            self.settings.update(mini_x=self.mini.win.winfo_x(), mini_y=self.mini.win.winfo_y())
        self.settings.update(
            voice=self.voice_var.get(),
            rate=self.rate_var.get(),
            volume=self.vol_var.get(),
            auto_read=self.auto_var.get(),
            show_text=self.show_var.get(),
            show_options=bool(self.options.winfo_ismapped()) if not self.mini_mode else self.settings.get("show_options", True),
            follow=self.follow_var.get(),
            read_dir=self.dir_var.get(),
            click_read=self.clickread_var.get(),
            alpha=float(self.alpha_var.get()),
            font_size=int(self.font_var.get()),
            mini_size=int(self.size_var.get()),
            hotkeys={str(k): v for k, v in self.hotkeys_cfg.items()},
        )
        save_settings(self.settings)
        self.tray.stop()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


def resource_path(name):
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (getattr(sys, "_MEIPASS", here), here, os.path.join(here, "..", "assets")):
        path = os.path.join(base, name)
        if os.path.exists(path):
            return path
    return os.path.join(here, name)


# ---------------------------------------------------------------------------
# 自我檢查（給打包流程驗證 exe 用）
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
        for f in ("/System/Library/Fonts/PingFang.ttc", "/System/Library/Fonts/STHeiti Medium.ttc",
                  "/System/Library/Fonts/Hiragino Sans GB.ttc", r"C:\Windows\Fonts\msjh.ttc", r"C:\Windows\Fonts\mingliu.ttc", r"C:\Windows\Fonts\msyh.ttc",
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
        # 三欄直書（由右到左），中間那欄被切成上下兩段
        fake = [(10, 100, 80, "一二三", 20), (10, 60, 40, "四五", 20), (60, 60, 40, "六", 20), (10, 20, 80, "七八", 20)]
        vt = OcrEngine.to_text(fake, vertical=True)
        ht = OcrEngine.to_text(fake)
        lines.append(f"Vertical order: {vt!r}  horizontal: {ht!r}")
        if vt != "一二三\n四五六\n七八":
            ok = False
            lines.append("FAIL: vertical order")
    except Exception:
        ok = False
        lines.append("VERT ERROR:\n" + traceback.format_exc())

    try:
        spoken, m = prepare_speech("我去銀行\n排行列。\n\nEnd", [("銀行", "銀杭")])
        lines.append(f"Pronunciation: {spoken!r}")
        if not spoken.startswith("我去銀杭排行列") or len(m) != len(spoken):
            ok = False
            lines.append("FAIL: prepare_speech")
    except Exception:
        ok = False
        lines.append("PRON ERROR:\n" + traceback.format_exc())

    try:
        import mss  # noqa: F401
        img, ox, oy, sc = grab_virtual_screen()
        lines.append(f"Screen grab: {img.size} at ({ox},{oy}) scale={sc} screen-access={mac_screen_access()}")
    except Exception:
        lines.append("SCREEN WARN:\n" + traceback.format_exc())

    try:
        sp = MacSpeaker() if IS_MAC else SapiSpeaker()
        lines.append(f"Voices: {sp.voice_names()} default={sp.default_voice()!r} error={sp.error!r}")
    except Exception:
        lines.append("TTS WARN:\n" + traceback.format_exc())

    try:
        from gtts import gTTS
        import miniaudio  # noqa: F401
        lines.append("gTTS + miniaudio import OK")
        if IS_MAC:
            from AppKit import NSStatusBar  # noqa: F401
            lines.append("Menu bar (AppKit) OK; icon file: " + str(os.path.exists(resource_path("icon.ico"))))
        else:
            import pystray
            from PIL import Image as _Img
            pystray.Icon("selftest", _Img.open(resource_path("icon.ico")), "t")
            lines.append(f"Tray icon OK ({pystray.Icon.__module__})")
        try:
            p = os.path.join(tempfile.gettempdir(), "sor_selftest.mp3")
            gTTS("測試", lang="zh-TW", lang_check=False).save(p)
            import miniaudio
            dec = miniaudio.decode_file(p, output_format=miniaudio.SampleFormat.FLOAT32, nchannels=1, sample_rate=24000)
            lines.append(f"Google TTS online OK ({os.path.getsize(p)} bytes, {len(dec.samples) / 24000:.2f}s decoded)")
            os.remove(p)
        except Exception as e:
            lines.append(f"WARN: Google TTS online failed (no network?): {e!r}")
    except Exception:
        ok = False
        lines.append("GTTS ERROR:\n" + traceback.format_exc())

    try:
        hk = GlobalHotkeys(lambda h: None)
        hk.start()
        hk.ready.wait(2)
        res = hk.scan(MOD_CONTROL | MOD_ALT, [0x52, 0x45, 0x53])
        lines.append(f"Hotkey scan Ctrl+Alt+R/E/S: {res}")
        if (IS_WINDOWS or IS_MAC) and len(res) != 3:
            ok = False
            lines.append("FAIL: hotkey scan")
    except Exception:
        ok = False
        lines.append("HOTKEY ERROR:\n" + traceback.format_exc())

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
    if IS_MAC:
        import fcntl

        global _LOCK_FILE
        _LOCK_FILE = open(os.path.join(data_dir(), "app.lock"), "w")
        try:
            fcntl.flock(_LOCK_FILE, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            # 已經有一個在執行：請它把面板打開，自己就結束
            try:
                import signal

                with open(os.path.join(data_dir(), "app.pid")) as f:
                    os.kill(int(f.read().strip()), signal.SIGUSR1)
            except Exception:
                os.system("osascript -e 'display dialog \"截圖朗讀小幫手已經在執行了（看螢幕最上方選單列右側的圖示）。\" "
                          "buttons {\"好\"} default button 1 with icon note' >/dev/null 2>&1")
            return
        with open(os.path.join(data_dir(), "app.pid"), "w") as f:
            f.write(str(os.getpid()))
    if IS_WINDOWS:
        ctypes.windll.kernel32.CreateMutexW(None, False, "ScreenOCRReader_SingleInstance")
        if ctypes.windll.kernel32.GetLastError() == 183:
            ctypes.windll.user32.MessageBoxW(None, "截圖朗讀小幫手已經在執行了。", APP_NAME, 0x40)
            return
    try:
        App().run()
    except Exception:
        err = traceback.format_exc()
        log_error(err)
        log = os.path.join(data_dir(), "error.log")
        if IS_MAC:
            msg = ("程式發生錯誤，詳細記錄在：" + log).replace('"', "'")
            os.system(f"osascript -e 'display dialog \"{msg}\" buttons {{\"好\"}} with icon stop' >/dev/null 2>&1")
        if IS_WINDOWS:
            ctypes.windll.user32.MessageBoxW(None, f"程式發生錯誤：\n{err[-800:]}\n\n詳細記錄：{log}", APP_NAME, 0x10)
        raise


if __name__ == "__main__":
    main()
