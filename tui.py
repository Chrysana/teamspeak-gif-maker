# SPDX-License-Identifier: GPL-3.0-or-later
"""Interactive text interface for the TeamSpeak GIF maker (Windows console).

Layout and keys follow the GOG Downloader interface: a menu on the left, the
selected page on the right, a title bar on top, hints and a status bar below.
Run "python tui.py [files...]"; "Make GIF.bat" starts it.
"""

import ctypes
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import unicodedata
from ctypes import wintypes as wt
from datetime import datetime
from pathlib import Path

# The embeddable Python in the release zip doesn't put the script's folder on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_gif as mg  # noqa: E402

TITLE = "TeamSpeak GIF Maker · Avatar GIF Converter"
SETTINGS_FILE = Path(os.environ.get("APPDATA", Path.home())) / "teamspeak-gif-maker" / "settings.json"


class Theme:
    """SGR styles; only the basic 16 colours so it looks the same in every console."""
    BASE = "37;40"
    TITLE_BAR = "1;91;40"
    STATUS_BAR = "31;40"
    STATUS_BAR_OK = "32;40"
    STATUS_BAR_WARN = "33;40"
    MENU = "97;44"
    MENU_SELECTED = "30;42"
    MENU_SELECTED_UNFOCUSED = "30;46"
    HEADING = "1;97;40"
    LABEL = "37;40"
    TEXT = "37;40"
    DIM = "90;40"
    VALUE = "97;40"
    GOOD = "92;40"
    BAD = "91;40"
    WARN = "93;40"
    INFO = "96;40"
    BUTTON = "97;44"
    BUTTON_FOCUSED = "97;45"
    BUTTON_DISABLED = "90;44"
    INPUT = "30;46"
    INPUT_FOCUSED = "30;106"
    INPUT_PLACEHOLDER = "3;30;46"
    FOCUSED = "30;47"
    LIST_CURSOR = "30;46"
    LIST_CURSOR_UNFOCUSED = "30;47"
    LIST_HEADER = "1;93;40"
    MODAL = "97;44"
    MODAL_BORDER = "1;97;44"


# ---- console ------------------------------------------------------------------------------------------------

class Key:
    """A key or mouse event. Printable characters have name 'char'."""
    MOUSE = ("click", "doubleclick", "rightclick", "scrollup", "scrolldown")

    def __init__(self, name: str, char: str = "", x: int = -1, y: int = -1):
        self.name, self.char, self.x, self.y = name, char, x, y

    def is_(self, *names: str) -> bool:
        return self.name in names

    def is_char(self, char: str | None = None) -> bool:
        return self.name == "char" and (char is None or self.char.lower() == char.lower())

    def is_mouse(self) -> bool:
        return self.name in self.MOUSE


class COORD(ctypes.Structure):
    _fields_ = [("X", wt.SHORT), ("Y", wt.SHORT)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", wt.SHORT), ("Top", wt.SHORT), ("Right", wt.SHORT), ("Bottom", wt.SHORT)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD), ("wAttributes", wt.WORD),
                ("srWindow", SMALL_RECT), ("dwMaximumWindowSize", COORD)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD), ("wVirtualKeyCode", wt.WORD),
                ("wVirtualScanCode", wt.WORD), ("UnicodeChar", wt.WCHAR),
                ("dwControlKeyState", wt.DWORD)]


class MOUSE_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("dwMousePosition", COORD), ("dwButtonState", wt.DWORD),
                ("dwControlKeyState", wt.DWORD), ("dwEventFlags", wt.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("MouseEvent", MOUSE_EVENT_RECORD),
                ("padding", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", _EVENT)]


class WindowsConsole:
    """Raw console input (keys + mouse) and VT output through the Win32 console API."""
    ENABLE_PROCESSED_OUTPUT, ENABLE_WRAP_AT_EOL_OUTPUT, ENABLE_VT = 0x1, 0x2, 0x4
    ENABLE_WINDOW_INPUT, ENABLE_MOUSE_INPUT, ENABLE_EXTENDED_FLAGS = 0x8, 0x10, 0x80
    KEY_EVENT, MOUSE_EVENT = 0x1, 0x2
    MOUSE_MOVED, MOUSE_WHEELED, MOUSE_HWHEELED = 0x1, 0x4, 0x8
    VIRTUAL_KEYS = {0x08: "backspace", 0x09: "tab", 0x0D: "enter", 0x1B: "esc", 0x21: "pageup",
                    0x22: "pagedown", 0x23: "end", 0x24: "home", 0x25: "left", 0x26: "up",
                    0x27: "right", 0x28: "down", 0x2D: "insert", 0x2E: "delete",
                    **{0x70 + i: f"f{i + 1}" for i in range(12)}}

    def __init__(self):
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        self.k = k
        k.GetStdHandle.restype = wt.HANDLE
        k.GetConsoleMode.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        k.SetConsoleMode.argtypes = [wt.HANDLE, wt.DWORD]
        k.GetNumberOfConsoleInputEvents.argtypes = [wt.HANDLE, ctypes.POINTER(wt.DWORD)]
        k.ReadConsoleInputW.argtypes = [wt.HANDLE, ctypes.POINTER(INPUT_RECORD), wt.DWORD,
                                        ctypes.POINTER(wt.DWORD)]
        k.GetConsoleScreenBufferInfo.argtypes = [wt.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
        k.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
        k.WaitForSingleObject.restype = wt.DWORD
        self.input = k.GetStdHandle(wt.DWORD(-10 & 0xFFFFFFFF))
        self.output = k.GetStdHandle(wt.DWORD(-11 & 0xFFFFFFFF))
        self.orig_in = self.orig_out = self.orig_cp = self.orig_icp = None
        self.want_in = self.want_out = None
        self.prev_buttons = 0
        self.high_surrogate = None

    def _mode(self, handle) -> int | None:
        m = wt.DWORD()
        return m.value if self.k.GetConsoleMode(handle, ctypes.byref(m)) else None

    def enable(self) -> None:
        self.orig_in = self._mode(self.input)
        if self.orig_in is None:
            raise RuntimeError("the interface must run in a console window")
        # no line input/echo/ctrl+c handling; quick edit off so mouse clicks reach us
        self.want_in = self.ENABLE_WINDOW_INPUT | self.ENABLE_MOUSE_INPUT | self.ENABLE_EXTENDED_FLAGS
        self.k.SetConsoleMode(self.input, self.want_in)
        self.orig_out = self._mode(self.output)
        if self.orig_out is not None:
            self.want_out = (self.ENABLE_PROCESSED_OUTPUT | self.ENABLE_VT
                             | (self.orig_out & self.ENABLE_WRAP_AT_EOL_OUTPUT))
            self.k.SetConsoleMode(self.output, self.want_out)
        self.orig_cp, self.orig_icp = self.k.GetConsoleOutputCP(), self.k.GetConsoleCP()
        self.k.SetConsoleOutputCP(65001)
        self.k.SetConsoleCP(65001)

    def restore(self) -> None:
        if self.orig_in is not None:
            self.k.SetConsoleMode(self.input, self.orig_in | self.ENABLE_EXTENDED_FLAGS)
        if self.orig_out is not None:
            self.k.SetConsoleMode(self.output, self.orig_out)
        if self.orig_cp:
            self.k.SetConsoleOutputCP(self.orig_cp)
            self.k.SetConsoleCP(self.orig_icp)
        self.want_in = self.want_out = None

    def _ensure_modes(self) -> None:
        """Child processes sharing the console can reset its modes; put ours back."""
        if self.want_in is not None:
            cur = self._mode(self.input)
            if cur is not None and (cur | self.ENABLE_EXTENDED_FLAGS) != self.want_in:
                self.k.SetConsoleMode(self.input, self.want_in)
                self.prev_buttons = 0
        if self.want_out is not None and self._mode(self.output) != self.want_out:
            self.k.SetConsoleMode(self.output, self.want_out)

    def _info(self):
        info = CONSOLE_SCREEN_BUFFER_INFO()
        return info if self.k.GetConsoleScreenBufferInfo(self.output, ctypes.byref(info)) else None

    def size(self) -> tuple[int, int]:
        info = self._info()
        if info is None:
            return 80, 25
        w = info.srWindow
        return max(1, w.Right - w.Left + 1), max(1, w.Bottom - w.Top + 1)

    def write(self, data: str) -> None:
        sys.stdout.buffer.write(data.encode("utf-8"))
        sys.stdout.buffer.flush()

    def read(self, timeout_ms: int) -> list[Key]:
        self._ensure_modes()
        if self.k.WaitForSingleObject(self.input, timeout_ms) != 0:
            return []
        count = wt.DWORD()
        if not self.k.GetNumberOfConsoleInputEvents(self.input, ctypes.byref(count)) or not count.value:
            return []
        n = min(256, count.value)
        records = (INPUT_RECORD * n)()
        read = wt.DWORD()
        if not self.k.ReadConsoleInputW(self.input, records, n, ctypes.byref(read)):
            return []
        keys = []
        for rec in records[:read.value]:
            if rec.EventType == self.MOUSE_EVENT:
                key = self._mouse(rec.Event.MouseEvent)
                if key:
                    keys.append(key)
            elif rec.EventType == self.KEY_EVENT and rec.Event.KeyEvent.bKeyDown:
                ev = rec.Event.KeyEvent
                key = self._key(ev.wVirtualKeyCode, ev.UnicodeChar, ev.dwControlKeyState)
                if key:
                    keys.extend([key] * max(1, ev.wRepeatCount))
        return keys

    def _mouse(self, ev) -> Key | None:
        info = self._info()
        x = ev.dwMousePosition.X - (info.srWindow.Left if info else 0)
        y = ev.dwMousePosition.Y - (info.srWindow.Top if info else 0)
        flags, buttons = ev.dwEventFlags, ev.dwButtonState
        if flags & self.MOUSE_WHEELED:
            delta = (buttons >> 16) & 0xFFFF
            return Key("scrollup" if delta and delta < 0x8000 else "scrolldown", x=x, y=y)
        if flags & (self.MOUSE_HWHEELED | self.MOUSE_MOVED):
            return None
        pressed = buttons & ~self.prev_buttons
        self.prev_buttons = buttons
        if pressed & 0x1:
            return Key("click", x=x, y=y)
        if pressed & 0x2:
            return Key("rightclick", x=x, y=y)
        return None

    def _key(self, vk: int, char: str, state: int) -> Key | None:
        ctrl, alt, shift = state & 0xC, state & 0x3, state & 0x10
        if vk in self.VIRTUAL_KEYS:
            name = self.VIRTUAL_KEYS[vk]
            return Key("backtab" if name == "tab" and shift else name)
        if ctrl and not alt and 0x41 <= vk <= 0x5A:
            return Key("ctrl+" + chr(vk).lower())
        code = ord(char) if char else 0
        if not code:
            return None
        if 0xD800 <= code <= 0xDBFF:
            self.high_surrogate = code
            return None
        if 0xDC00 <= code <= 0xDFFF and self.high_surrogate:
            code = 0x10000 + ((self.high_surrogate - 0xD800) << 10) + (code - 0xDC00)
            self.high_surrogate = None
        if code < 32:
            return {13: Key("enter"), 9: Key("tab"), 8: Key("backspace"), 27: Key("esc")}.get(code)
        return Key("char", chr(code))


# ---- drawing ------------------------------------------------------------------------------------------------

def char_width(ch: str) -> int:
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in "WF" else 1


def text_width(text: str) -> int:
    return sum(char_width(c) for c in text)


def fit(text: str, width: int) -> str:
    """Truncate with an ellipsis to at most width columns, then pad."""
    if text_width(text) > width:
        out, used = "", 0
        for c in text:
            if used + char_width(c) > width - 1:
                break
            out += c
            used += char_width(c)
        text = out + "…"
    return text + " " * max(0, width - text_width(text))


class Canvas:
    def __init__(self, width: int, height: int):
        self.width, self.height = width, height
        self.cells = [[[" ", Theme.BASE] for _ in range(width)] for _ in range(height)]
        self.cursor = None

    def fill(self, x, y, w, h, style, char=" "):
        for row in range(max(0, y), min(self.height, y + h)):
            for col in range(max(0, x), min(self.width, x + w)):
                self.cells[row][col] = [char, style]

    def text(self, x, y, text, style, max_width=None) -> int:
        if not 0 <= y < self.height:
            return 0
        written = 0
        for c in text:
            cw = char_width(c)
            if cw == 0:
                continue
            if max_width is not None and written + cw > max_width:
                break
            if 0 <= x + written and x + written + cw - 1 < self.width:
                self.cells[y][x + written] = [c, style]
                if cw == 2:
                    self.cells[y][x + written + 1] = ["", style]
            written += cw
        return written

    def centered(self, x, y, width, text, style):
        self.text(x + max(0, (width - text_width(text)) // 2), y, text, style, width)

    def box(self, x, y, w, h, style, title=""):
        self.text(x, y, "┌" + "─" * (w - 2) + "┐", style)
        for row in range(y + 1, y + h - 1):
            self.text(x, row, "│", style)
            self.text(x + w - 1, row, "│", style)
        self.text(x, y + h - 1, "└" + "─" * (w - 2) + "┘", style)
        if title:
            self.text(x + 2, y, f" {title} ", style, w - 4)

    def rows(self) -> list[str]:
        out = []
        for row in self.cells:
            line, style = [], None
            for ch, st in row:
                if ch == "":
                    continue
                if st != style:
                    line.append(f"\x1b[0;{st}m")
                    style = st
                line.append(ch)
            out.append("".join(line) + "\x1b[0m")
        return out


# ---- widgets ------------------------------------------------------------------------------------------------

class Widget:
    focusable = True
    captures_vertical = False

    def __init__(self):
        self.x = self.y = 0
        self.width = 0
        self.visible = True
        self.enabled = True

    def height(self) -> int:
        return 1

    def contains(self, x, y) -> bool:
        return self.visible and self.y <= y < self.y + self.height() and self.x <= x < self.x + self.width

    def render(self, c: Canvas, focused: bool):
        pass

    def handle_key(self, key: Key) -> bool:
        return False

    def handle_mouse(self, key: Key) -> bool:
        return False


class TextInput(Widget):
    def __init__(self, value="", width=20, placeholder="", on_change=None, on_enter=None):
        super().__init__()
        self.value, self.width, self.placeholder = value, width, placeholder
        self.on_change, self.on_enter = on_change, on_enter
        self.pos = len(value)
        self.invalid = False

    def set(self, value: str):
        self.value, self.pos = value, len(value)

    def render(self, c, focused):
        style = Theme.INPUT_FOCUSED if focused else Theme.INPUT
        inner = self.width - 2
        start = max(0, self.pos - inner + 1)
        shown = self.value[start:start + inner]
        c.text(self.x, self.y, "[", Theme.BAD if self.invalid else Theme.DIM)
        if not self.value and self.placeholder and not focused:
            c.text(self.x + 1, self.y, fit(self.placeholder, inner), Theme.INPUT_PLACEHOLDER)
        else:
            c.text(self.x + 1, self.y, fit(shown, inner), style)
        c.text(self.x + self.width - 1, self.y, "]", Theme.BAD if self.invalid else Theme.DIM)
        if focused:
            c.cursor = (self.x + 1 + text_width(self.value[start:self.pos]), self.y)

    def _changed(self):
        if self.on_change:
            self.on_change(self.value)

    def handle_key(self, key):
        if key.is_char():
            self.value = self.value[:self.pos] + key.char + self.value[self.pos:]
            self.pos += 1
            self._changed()
        elif key.is_("backspace"):
            if self.pos:
                self.value = self.value[:self.pos - 1] + self.value[self.pos:]
                self.pos -= 1
                self._changed()
        elif key.is_("delete"):
            self.value = self.value[:self.pos] + self.value[self.pos + 1:]
            self._changed()
        elif key.is_("left"):
            self.pos = max(0, self.pos - 1)
        elif key.is_("right"):
            self.pos = min(len(self.value), self.pos + 1)
        elif key.is_("home"):
            self.pos = 0
        elif key.is_("end"):
            self.pos = len(self.value)
        elif key.is_("ctrl+u"):
            self.set("")
            self._changed()
        elif key.is_("enter") and self.on_enter:
            self.on_enter(self.value)
        else:
            return False
        return True

    def handle_mouse(self, key):
        if key.is_("click", "doubleclick"):
            self.pos = min(len(self.value), max(0, key.x - self.x - 1))
            return True
        return False


class Checkbox(Widget):
    def __init__(self, label, value=False, on_change=None):
        super().__init__()
        self.label, self.value, self.on_change = label, value, on_change
        self.width = 4 + text_width(label)

    def render(self, c, focused):
        c.text(self.x, self.y, "[X]" if self.value else "[ ]", Theme.FOCUSED if focused else Theme.VALUE)
        c.text(self.x + 4, self.y, self.label, Theme.LABEL)

    def toggle(self):
        self.value = not self.value
        if self.on_change:
            self.on_change(self.value)

    def handle_key(self, key):
        if key.is_char(" ") or key.is_("enter"):
            self.toggle()
            return True
        return False

    def handle_mouse(self, key):
        if key.is_("click", "doubleclick"):
            self.toggle()
            return True
        return False


class RadioGroup(Widget):
    def __init__(self, options: dict, value: str, on_change=None):
        super().__init__()
        self.options, self.value, self.on_change = options, value, on_change
        self.areas = []
        self.width = sum(4 + text_width(l) + 3 for l in options.values())

    def render(self, c, focused):
        x = self.x
        self.areas = []
        for value, label in self.options.items():
            sel = value == self.value
            w = c.text(x, self.y, "(X)" if sel else "( )", Theme.FOCUSED if focused and sel else Theme.VALUE)
            w += 1 + c.text(x + w + 1, self.y, label, Theme.LABEL)
            self.areas.append((value, x, w))
            x += w + 3

    def _set(self, value):
        if value != self.value:
            self.value = value
            if self.on_change:
                self.on_change(value)

    def handle_key(self, key):
        values = list(self.options)
        i = values.index(self.value) if self.value in values else 0
        if key.is_("left"):
            self._set(values[max(0, i - 1)])
        elif key.is_("right"):
            self._set(values[min(len(values) - 1, i + 1)])
        elif key.is_char(" ") or key.is_("enter"):
            self._set(values[(i + 1) % len(values)])
        else:
            return False
        return True

    def handle_mouse(self, key):
        if key.is_("click", "doubleclick"):
            for value, x, w in self.areas:
                if x <= key.x < x + w:
                    self._set(value)
                    return True
        return False


class Button(Widget):
    def __init__(self, label, on_press):
        super().__init__()
        self.label, self.on_press = label, on_press
        self.width = text_width(label) + 4

    def render(self, c, focused):
        style = Theme.BUTTON_DISABLED if not self.enabled else (Theme.BUTTON_FOCUSED if focused else Theme.BUTTON)
        c.text(self.x, self.y, f"  {self.label}  ", style)

    def handle_key(self, key):
        if (key.is_("enter") or key.is_char(" ")) and self.enabled:
            self.on_press()
            return True
        return False

    def handle_mouse(self, key):
        if key.is_("click", "doubleclick") and self.enabled:
            self.on_press()
            return True
        return False


class Form:
    """Focus handling for a list of widgets."""

    def __init__(self):
        self.widgets: list[Widget] = []
        self.focus = 0

    def add(self, widget):
        self.widgets.append(widget)
        return widget

    def focusable(self):
        return [w for w in self.widgets if w.visible and w.focusable and w.enabled]

    def focused(self) -> Widget | None:
        items = self.focusable()
        if not items:
            return None
        self.focus = min(self.focus, len(items) - 1)
        return items[self.focus]

    def focus_widget(self, widget):
        items = self.focusable()
        if widget in items:
            self.focus = items.index(widget)

    def move(self, step) -> bool:
        items = self.focusable()
        if not items:
            return False
        new = self.focus + step
        if not 0 <= new < len(items):
            return False
        self.focus = new
        return True

    def render(self, c, focused):
        current = self.focused()
        for w in self.widgets:
            if w.visible:
                w.render(c, focused and w is current)

    def handle_key(self, key) -> bool:
        current = self.focused()
        if current and current.handle_key(key):
            return True
        if key.is_("tab"):
            if not self.move(1):
                self.focus = 0
            return True
        if key.is_("backtab"):
            if not self.move(-1):
                self.focus = len(self.focusable()) - 1
            return True
        if key.is_("down"):
            return self.move(1)
        if key.is_("up"):
            return self.move(-1)
        return False

    def handle_mouse(self, key) -> bool:
        for w in self.widgets:
            if w.contains(key.x, key.y) and w.enabled:
                self.focus_widget(w)
                w.handle_mouse(key)
                return True
        return False


class Modal:
    def __init__(self, app, title, lines, buttons=None, on_escape=None):
        self.app, self.title, self.lines = app, title, lines
        self.on_escape = on_escape
        self.form = Form()
        for label, callback in (buttons or {"OK": None}).items():
            self.form.add(Button(label, self._closer(callback)))

    def _closer(self, callback):
        def press():
            self.app.close_modal(self)
            if callback:
                callback()
        return press

    def render(self, c: Canvas):
        texts = [l[0] if isinstance(l, tuple) else l for l in self.lines]
        buttons_w = sum(b.width + 2 for b in self.form.widgets)
        w = min(c.width - 4, max([30, buttons_w + 4, text_width(self.title) + 8] + [text_width(t) + 4 for t in texts]))
        h = min(c.height - 2, len(self.lines) + 5)
        x, y = (c.width - w) // 2, (c.height - h) // 2
        c.fill(x + 1, y + 1, w, h, "30;40")
        c.fill(x, y, w, h, Theme.MODAL)
        c.box(x, y, w, h, Theme.MODAL_BORDER, self.title)
        for i, line in enumerate(self.lines[:h - 5]):
            text, style = line if isinstance(line, tuple) else (line, Theme.MODAL)
            c.text(x + 2, y + 2 + i, text, style, w - 4)
        bx = x + (w - buttons_w + 2) // 2
        for b in self.form.widgets:
            b.x, b.y = bx, y + h - 2
            bx += b.width + 2
        c.cursor = None
        self.form.render(c, True)

    def handle_key(self, key):
        if key.is_("esc"):
            self.app.close_modal(self)
            if self.on_escape:
                self.on_escape()
        elif key.is_("left"):
            self.form.handle_key(Key("backtab"))
        elif key.is_("right"):
            self.form.handle_key(Key("tab"))
        else:
            self.form.handle_key(key)

    def handle_mouse(self, key):
        self.form.handle_mouse(key)


# ---- data ---------------------------------------------------------------------------------------------------

class Entry:
    """A file in the list, with its analysis and conversion state."""

    def __init__(self, path: Path):
        self.path = path
        self.info = None
        self.loop = None
        self.analysis = "pending"   # pending, done, error
        self.error = ""
        self.status = ""            # "", queued, a stage name, done, failed, cancelled
        self.result = None

    def describe(self) -> tuple[str, str]:
        if self.status == "done" and self.result:
            r = self.result
            return f"✓ {r['width']}x{r['height']}, {r['duration']:.1f}s, {mg.format_size(r['bytes'])}", Theme.GOOD
        if self.status == "failed":
            return f"✗ {self.error}", Theme.BAD
        if self.status == "cancelled":
            return "cancelled", Theme.WARN
        if self.status and self.status != "queued":
            return f"{self.status}…", Theme.WARN
        if self.analysis == "error":
            return f"✗ {self.error}", Theme.BAD
        if self.info is None:
            return "reading…", Theme.DIM
        i = self.info
        text = f"{i['width']}x{i['height']} · {i['fps']:g}fps · {i['duration']:.1f}s"
        if self.analysis == "pending":
            text += " · checking for loop…"
        elif self.loop:
            text += f" · loops every {self.loop['seconds']:.1f}s (x{self.loop['cycles']:.1f})"
        return text, Theme.TEXT


class FileList(Widget):
    captures_vertical = True

    def __init__(self, app):
        super().__init__()
        self.app = app
        self.cursor = 0
        self.scroll = 0
        self.rows = 4

    def height(self):
        return self.rows

    def render(self, c, focused):
        entries = self.app.entries
        self.cursor = max(0, min(self.cursor, len(entries) - 1))
        if self.cursor < self.scroll:
            self.scroll = self.cursor
        if self.cursor >= self.scroll + self.rows:
            self.scroll = self.cursor - self.rows + 1
        if not entries:
            c.fill(self.x, self.y, self.width, self.rows, Theme.BASE)
            c.text(self.x + 2, self.y + 1, "No files yet. Drag videos or GIFs onto this window.", Theme.DIM, self.width - 2)
            return
        name_w = max(16, min(36, self.width // 3))
        for row in range(self.rows):
            i = self.scroll + row
            y = self.y + row
            if i >= len(entries):
                break
            e = entries[i]
            sel = i == self.cursor
            style = (Theme.LIST_CURSOR if focused else Theme.LIST_CURSOR_UNFOCUSED) if sel else Theme.VALUE
            c.text(self.x, y, fit(" " + e.path.name, name_w), style)
            desc, dstyle = e.describe()
            c.text(self.x + name_w + 1, y, fit(desc, self.width - name_w - 1), style if sel else dstyle)
        if len(entries) > self.rows:
            c.text(self.x + self.width - 8, self.y + self.rows - 1,
                   f" {self.scroll + 1}-{min(len(entries), self.scroll + self.rows)}/{len(entries)} ", Theme.DIM)

    def handle_key(self, key):
        n = len(self.app.entries)
        if key.is_("up") and self.cursor > 0:
            self.cursor -= 1
        elif key.is_("down") and self.cursor < n - 1:
            self.cursor += 1
        elif key.is_("delete", "backspace") and n:
            self.app.remove_entry(self.cursor)
        else:
            return False
        return True

    def handle_mouse(self, key):
        if key.is_("scrollup"):
            self.cursor = max(0, self.cursor - 1)
        elif key.is_("scrolldown"):
            self.cursor = min(len(self.app.entries) - 1, self.cursor + 1)
        elif key.is_("click", "doubleclick"):
            self.cursor = min(len(self.app.entries) - 1, self.scroll + key.y - self.y)
        return True


# ---- pages --------------------------------------------------------------------------------------------------

PRESET_NOTES = {
    "standard": "256 colours, no dithering. Most accurate per byte.",
    "high": "256 colours, light dithering. Softens banding in smooth gradients.",
    "small": "128 colours + light compression. ~20-35% smaller, some banding.",
    "smallest": "Like Small, plus at most 30fps. Much smaller for 60fps clips.",
}


class ConvertPage:
    label = "Convert"
    hints = "Tab/↑↓ move · Space select · ←→ change · Del remove file · Esc menu · F1 help · Ctrl+Q quit"

    def __init__(self, app):
        self.app = app
        s = app.settings
        f = self.form = Form()
        self.add_input = f.add(TextInput(placeholder="drag files here, or type a path and press Enter",
                                         on_enter=lambda v: app.add_from_text(v, report=True)))
        self.list = f.add(FileList(app))
        self.preset = f.add(RadioGroup({"standard": "Standard", "high": "High", "small": "Small",
                                        "smallest": "Smallest"}, s.get("preset", "standard")))
        self.speed = f.add(RadioGroup({"real": "Correct speed (50fps)", "faux60": "Smooth (all frames, slower)"},
                                      "faux60" if s.get("faux60") else "real"))
        self.start = f.add(TextInput(width=10, placeholder="0:00"))
        self.length = f.add(TextInput(width=10, placeholder="all"))
        self.loop = f.add(Checkbox("Keep one loop if a clip repeats", s.get("loop", True)))
        self.square = f.add(Checkbox("Crop to square", s.get("square", False)))
        self.out_dir = f.add(TextInput(s.get("output_dir", ""), placeholder="next to each source file"))
        self.convert_btn = f.add(Button("Convert", app.start_conversion))
        self.clear_btn = f.add(Button("Clear List", app.clear_entries))
        self.open_btn = f.add(Button("Open Output Folder", app.open_output))

    def layout(self, x, y, w, h):
        lx = x + 12
        self.add_input.x, self.add_input.y, self.add_input.width = lx, y + 1, w - 12
        self.list.x, self.list.y, self.list.width = x, y + 3, w
        self.list.rows = max(3, min(8, h - 19))
        r = self.list.y + self.list.rows + 1
        self.rows = {"preset": r, "note": r + 1, "speed": r + 3, "trim": r + 5, "options": r + 7,
                     "save": r + 9, "buttons": r + 11, "status": r + 13}
        self.preset.x, self.preset.y = lx, r
        self.speed.x, self.speed.y = lx, r + 3
        self.start.x, self.start.y = lx + 6, r + 5
        self.length.x, self.length.y = lx + 26, r + 5
        self.loop.x, self.loop.y = lx, r + 7
        self.square.x, self.square.y = lx + self.loop.width + 4, r + 7
        self.out_dir.x, self.out_dir.y, self.out_dir.width = lx, r + 9, w - 12
        bx = x
        for b in (self.convert_btn, self.clear_btn, self.open_btn):
            b.x, b.y = bx, r + 11
            bx += b.width + 2

    def render(self, c, x, y, w, h, focused):
        self.layout(x, y, w, h)
        app = self.app
        busy = app.job_running()
        self.convert_btn.label = "Cancel" if busy else "Convert"
        self.convert_btn.width = text_width(self.convert_btn.label) + 4
        self.clear_btn.enabled = not busy
        self.open_btn.enabled = app.last_output is not None
        count = len(app.entries)
        c.text(x, y, "Files", Theme.HEADING)
        c.text(x + 6, y, f"({count})" if count else "", Theme.DIM)
        c.text(x, y + 1, "Add", Theme.LABEL)
        R = self.rows
        c.text(x, R["preset"], "Preset", Theme.LABEL)
        c.text(x + 12, R["note"], PRESET_NOTES[self.preset.value], Theme.DIM, w - 12)
        c.text(x, R["speed"], "Over 50fps", Theme.LABEL)
        c.text(x + 12, R["speed"] + 1,
               "For 60fps clips. Smooth keeps every frame, plays at ~83% speed."
               if self.speed.value == "real" else "Every frame kept: perfectly smooth, plays slower (~83% at 60fps).",
               Theme.DIM, w - 12)
        c.text(x, R["trim"], "Trim", Theme.LABEL)
        c.text(x + 12, R["trim"], "Start", Theme.LABEL)
        c.text(x + 30, R["trim"], "Length", Theme.LABEL)
        c.text(x + 49, R["trim"], "e.g. 12.5 or 1:05", Theme.DIM, max(0, w - 49))
        c.text(x, R["options"], "Options", Theme.LABEL)
        c.text(x, R["save"], "Save to", Theme.LABEL)
        self.form.render(c, focused)
        self.render_status(c, x, R["status"], w)

    def render_status(self, c, x, y, w):
        app = self.app
        if app.job_running():
            spinner = "|/-\\"[int(time.time() * 4) % 4]
            e = app.job_entry
            done = sum(1 for en in app.job_entries if en.status in ("done", "failed", "cancelled"))
            elapsed = time.time() - app.job_started
            line = f"{spinner} Converting {done + 1}/{len(app.job_entries)}: {e.path.name if e else ''}"
            c.text(x, y, line, Theme.WARN, w - 12)
            c.text(x + w - 10, y, f"{int(elapsed // 60)}:{int(elapsed % 60):02d}", Theme.DIM)
            if app.cancel.is_set():
                c.text(x, y + 1, "Cancelling…", Theme.WARN)
        elif app.last_summary:
            text, style = app.last_summary
            c.text(x, y, text, style, w)

    def options(self) -> dict:
        return {"preset": self.preset.value, "faux60": self.speed.value == "faux60",
                "loop": self.loop.value, "square": self.square.value,
                "start": self.start.value.strip() or None, "duration": self.length.value.strip() or None,
                "output_dir": self.out_dir.value.strip().strip('"') or None}

    def handle_key(self, key) -> bool:
        return self.form.handle_key(key)

    def handle_mouse(self, key) -> bool:
        return self.form.handle_mouse(key)


class LogPage:
    label = "Activity Log"
    hints = "↑↓ PgUp PgDn scroll · c clear · Esc menu · F1 help"

    def __init__(self, app):
        self.app = app
        self.scroll = None  # None = follow the end

    def render(self, c, x, y, w, h, focused):
        c.text(x, y, "Activity Log", Theme.HEADING)
        lines = self.app.log_lines
        rows = h - 2
        end = len(lines) if self.scroll is None else min(len(lines), self.scroll + rows)
        start = max(0, end - rows)
        for i, (stamp, text, style) in enumerate(lines[start:end]):
            c.text(x, y + 2 + i, stamp, Theme.DIM)
            c.text(x + 9, y + 2 + i, text, style, w - 9)
        if not lines:
            c.text(x, y + 2, "Nothing yet.", Theme.DIM)

    def _scroll(self, delta):
        lines = len(self.app.log_lines)
        top = (self.scroll if self.scroll is not None else max(0, lines - 20)) + delta
        self.scroll = None if top >= lines - 20 else max(0, top)

    def handle_key(self, key):
        if key.is_("up"):
            self._scroll(-1)
        elif key.is_("down"):
            self._scroll(1)
        elif key.is_("pageup"):
            self._scroll(-15)
        elif key.is_("pagedown"):
            self._scroll(15)
        elif key.is_char("c"):
            self.app.log_lines.clear()
            self.scroll = None
        else:
            return False
        return True

    def handle_mouse(self, key):
        if key.is_("scrollup", "scrolldown"):
            self._scroll(-3 if key.is_("scrollup") else 3)
            return True
        return False


# ---- app ----------------------------------------------------------------------------------------------------

PATH_RE = re.compile(r'"([^"]+)"|(\S+)')


def split_paths(text: str) -> list[Path]:
    """Paths as typed or dropped onto the console: "C:\\a b.mp4" C:\\c.mp4"""
    return [Path(q or u) for q, u in PATH_RE.findall(text.strip())]


class App:
    MENU_WIDTH, MENU_X, MENU_Y = 20, 1, 2
    MIN_W, MIN_H = 100, 26

    def __init__(self, console: WindowsConsole, files: list[str]):
        self.console = console
        self.settings = self.load_settings()
        self.entries: list[Entry] = []
        self.log_lines: list[tuple[str, str, str]] = []
        self.events = queue.Queue()
        self.analysis_queue = queue.Queue()
        self.modals: list[Modal] = []
        self.pages = [ConvertPage(self), LogPage(self)]
        self.selected = 0
        self.menu_focused = False
        self.running = True
        self.prev_rows, self.prev_size = [], None
        self.job_thread = None
        self.job_entries, self.job_entry, self.job_started = [], None, 0.0
        self.cancel = threading.Event()
        self.last_output = None
        self.last_summary = None
        self.last_click = None
        threading.Thread(target=self.analysis_worker, daemon=True).start()
        for f in files:
            self.add_path(Path(f))
        self.log(f"TeamSpeak GIF Maker {mg.__version__} started")
        missing = mg.missing_tools()
        if missing:
            self.show_error("Tools missing", f"Can't find {', '.join(missing)}. Put them in {mg.BIN}"
                            " or on your PATH (see README).")

    # -- settings
    @staticmethod
    def load_settings() -> dict:
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_settings(self):
        o = self.pages[0].options()
        data = {k: o[k] for k in ("preset", "faux60", "loop", "square")}
        data["output_dir"] = o["output_dir"] or ""
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except OSError:
            pass

    # -- files
    def add_path(self, path: Path) -> bool:
        path = path.expanduser()
        if not path.is_file():
            return False
        if any(e.path.resolve() == path.resolve() for e in self.entries):
            return True
        entry = Entry(path)
        self.entries.append(entry)
        self.analysis_queue.put(entry)
        return True

    def add_from_text(self, text: str, report=False) -> bool:
        whole = Path(text.strip().strip('"'))
        paths = [whole] if text.strip() and whole.is_file() else split_paths(text)
        if not paths:
            return False
        if all(p.is_file() for p in paths):
            for p in paths:
                self.add_path(p)
            self.pages[0].add_input.set("")
            self.pages[0].list.cursor = len(self.entries) - 1
            return True
        if report:
            bad = next(p for p in paths if not p.is_file())
            self.show_error("File not found", str(bad))
        return False

    def remove_entry(self, index: int):
        if self.job_running():
            return
        del self.entries[index]

    def clear_entries(self):
        if not self.job_running():
            self.entries.clear()
            self.last_summary = None

    def analysis_worker(self):
        while True:
            entry = self.analysis_queue.get()
            try:
                info = mg.probe(entry.path)
                self.events.put(("info", entry, info))
                loop = mg.detect_loop(entry.path, info)
                self.events.put(("analysed", entry, loop))
            except Exception as e:
                self.events.put(("analysis_error", entry, str(e)))

    # -- conversion
    def job_running(self) -> bool:
        return self.job_thread is not None and self.job_thread.is_alive()

    def start_conversion(self):
        if self.job_running():
            self.cancel.set()
            return
        page = self.pages[0]
        o = page.options()
        problems = []
        for field, value in (("Start", o["start"]), ("Length", o["duration"])):
            if value and not mg.TIME_RE.match(value):
                problems.append(f'{field} "{value}" is not a time (use 12.5 or 1:05).')
        if o["output_dir"] and not Path(o["output_dir"]).is_dir():
            problems.append(f'Save to: "{o["output_dir"]}" is not a folder.')
        page.start.invalid = bool(o["start"] and not mg.TIME_RE.match(o["start"]))
        page.length.invalid = bool(o["duration"] and not mg.TIME_RE.match(o["duration"]))
        ready = [e for e in self.entries if e.analysis != "error"]
        if not ready:
            problems.append("Add at least one video or GIF first.")
        if mg.missing_tools():
            problems.append(f"Missing tools: {', '.join(mg.missing_tools())}.")
        if problems:
            self.show_message("Can't convert yet", problems)
            return
        opts = mg.options(**o)
        self.cancel.clear()
        for e in ready:
            e.status, e.result, e.error = "queued", None, ""
        self.job_entries, self.job_started = ready, time.time()
        self.last_summary = None
        self.save_settings()
        self.log(f"Converting {len(ready)} file(s), preset {opts.preset}"
                 + (", smooth (faux 60)" if opts.faux60 else "") + (", one loop" if opts.loop else ""))
        self.job_thread = threading.Thread(target=self.job_worker, args=(ready, opts), daemon=True)
        self.job_thread.start()

    def job_worker(self, entries, opts):
        ok = failed = 0
        for e in entries:
            if self.cancel.is_set():
                self.events.put(("status", e, "cancelled", None))
                continue
            self.events.put(("current", e))
            try:
                result = mg.convert(e.path, opts, log=lambda t: self.events.put(("log", t)),
                                    cancel=self.cancel,
                                    stage=lambda t, e=e: self.events.put(("status", e, t, None)),
                                    known_loop=e.loop if e.analysis == "done" else ...)
                ok += 1
                self.events.put(("status", e, "done", result))
            except mg.Cancelled:
                self.events.put(("status", e, "cancelled", None))
            except Exception as ex:
                failed += 1
                self.events.put(("status", e, "failed", str(ex)))
        self.events.put(("finished", ok, failed))

    def open_output(self):
        if self.last_output and self.last_output.exists():
            subprocess.Popen(["explorer", "/select,", str(self.last_output)])
        elif self.last_output:
            os.startfile(self.last_output.parent)

    # -- events from worker threads
    def process_events(self) -> bool:
        changed = False
        while True:
            try:
                ev = self.events.get_nowait()
            except queue.Empty:
                return changed
            changed = True
            kind = ev[0]
            if kind == "info":
                ev[1].info = ev[2]
            elif kind == "analysed":
                ev[1].loop, ev[1].analysis = ev[2], "done"
            elif kind == "analysis_error":
                ev[1].analysis, ev[1].error = "error", ev[2]
                self.log(f"{ev[1].path.name}: {ev[2]}", Theme.BAD)
            elif kind == "log":
                text = ev[1]
                self.log(text, Theme.WARN if text.lstrip().startswith("note") else Theme.TEXT)
            elif kind == "current":
                self.job_entry = ev[1]
            elif kind == "status":
                entry, status, extra = ev[1], ev[2], ev[3]
                entry.status = status
                if status == "done":
                    entry.result = extra
                    self.last_output = extra["path"]
                elif status == "failed":
                    entry.error = extra
                    self.log(f"{entry.path.name} failed: {extra}", Theme.BAD)
            elif kind == "finished":
                ok, failed = ev[1], ev[2]
                cancelled = sum(1 for e in self.job_entries if e.status == "cancelled")
                parts = [f"{ok} converted"] + ([f"{failed} failed"] if failed else []) \
                    + ([f"{cancelled} cancelled"] if cancelled else [])
                style = Theme.BAD if failed else (Theme.WARN if cancelled else Theme.GOOD)
                self.last_summary = ("Finished: " + ", ".join(parts)
                                     + (f". Saved to {self.last_output.parent}" if ok else ""), style)
                self.log(self.last_summary[0], style)
                self.job_entry = None

    # -- log & modals
    def log(self, text: str, style: str = Theme.TEXT):
        for line in text.splitlines() or [""]:
            self.log_lines.append((datetime.now().strftime("%H:%M:%S"), line, style))
        del self.log_lines[:-2000]

    def show_message(self, title, lines, buttons=None):
        self.modals.append(Modal(self, title, lines if isinstance(lines, list) else [lines], buttons))

    def show_error(self, title, message):
        self.log(f"Error: {message}", Theme.BAD)
        self.show_message(title, [(message, "1;97;44")])

    def close_modal(self, modal):
        if modal in self.modals:
            self.modals.remove(modal)

    def show_help(self):
        self.show_message("Help", [
            ("Adding files", Theme.MODAL_BORDER),
            "  Drag videos or GIFs onto this window, or onto Make GIF.bat.",
            "  You can also type or paste a path into Add and press Enter.",
            "",
            ("Keys", Theme.MODAL_BORDER),
            "  Tab / ↑↓     move between fields      Space / Enter  toggle, press",
            "  ← →          change a choice          Del            remove a file",
            "  Esc          back to the menu         Ctrl+Q         quit",
            "  While converting, the Convert button becomes Cancel.",
            "",
            ("Tips", Theme.MODAL_BORDER),
            "  Length matters most for file size. Keep one loop if a clip repeats.",
            "  GIFs are saved as <name>_<date>_<time>.gif, nothing is overwritten.",
        ])

    def quit(self):
        if self.job_running():
            def stop():
                self.cancel.set()
                self.running = False
            self.show_message("Quit", ["A conversion is running. Cancel it and quit?"],
                              {"Quit": stop, "Keep Going": None})
        else:
            self.running = False

    # -- input
    def handle_batch(self, keys: list[Key]):
        chars = [k for k in keys if k.is_char()]
        page = self.pages[self.selected]
        focused = page.form.focused() if hasattr(page, "form") and not self.menu_focused else None
        # A burst of characters is a file dropped onto the window (the console types its path)
        # or a paste. Send it to the Add field unless a text field already has focus.
        if len(chars) >= 4 and not self.modals and not isinstance(focused, TextInput):
            convert = self.pages[0]
            self.selected, self.menu_focused = 0, False
            convert.form.focus_widget(convert.add_input)
        for key in keys:
            self.handle_key(key)
        if self.pages[0].add_input.value:
            self.add_from_text(self.pages[0].add_input.value)

    def handle_key(self, key: Key):
        if key.is_mouse():
            return self.handle_mouse(key)
        if key.is_("ctrl+c", "ctrl+q"):
            return self.quit()
        if self.modals:
            return self.modals[-1].handle_key(key)
        if key.is_("f1"):
            return self.show_help()
        menu_count = len(self.pages) + 1
        if self.menu_focused:
            if key.is_("up"):
                self.selected = (self.selected - 1) % menu_count
            elif key.is_("down"):
                self.selected = (self.selected + 1) % menu_count
            elif key.is_("enter", "right", "tab"):
                if self.selected == len(self.pages):
                    self.quit()
                else:
                    self.menu_focused = False
            elif key.is_char("q"):
                self.quit()
            return
        page = self.pages[self.selected]
        if key.is_("esc"):
            self.menu_focused = True
            return
        if not page.handle_key(key) and key.is_("left"):
            self.menu_focused = True

    def handle_mouse(self, key: Key):
        if key.is_("click"):
            now = time.time()
            if self.last_click and now - self.last_click[0] < 0.4 and self.last_click[1:] == (key.x, key.y):
                key = Key("doubleclick", x=key.x, y=key.y)
            self.last_click = (now, key.x, key.y)
        if self.modals:
            return self.modals[-1].handle_mouse(key)
        if key.x < self.MENU_X + self.MENU_WIDTH and key.is_("click", "doubleclick"):
            index = (key.y - self.MENU_Y) // 2
            if (key.y - self.MENU_Y) % 2 == 0 and 0 <= index <= len(self.pages):
                if index == len(self.pages):
                    return self.quit()
                self.selected, self.menu_focused = index, False
            return
        page = self.pages[self.selected] if self.selected < len(self.pages) else None
        if page and page.handle_mouse(key):
            self.menu_focused = False

    # -- drawing
    def render(self):
        w, h = self.console.size()
        if (w, h) != self.prev_size:
            self.prev_rows, self.prev_size = [], (w, h)
            self.console.write("\x1b[0m\x1b[2J")
        c = Canvas(w, h)
        if w < self.MIN_W or h < self.MIN_H:
            c.centered(0, h // 2, w, f"Please enlarge the window (at least {self.MIN_W}x{self.MIN_H})", Theme.WARN)
            return self.flush(c)
        c.centered(0, 0, w, TITLE, Theme.TITLE_BAR)
        labels = [p.label for p in self.pages] + ["Exit"]
        for i, label in enumerate(labels):
            style = (Theme.MENU_SELECTED if self.menu_focused else Theme.MENU_SELECTED_UNFOCUSED) \
                if i == self.selected else Theme.MENU
            c.text(self.MENU_X, self.MENU_Y + i * 2, fit(" " + label, self.MENU_WIDTH), style)
        cx = self.MENU_X + self.MENU_WIDTH + 2
        cw, ch = w - cx - 1, h - 4
        if self.selected < len(self.pages):
            page = self.pages[self.selected]
            page.render(c, cx, 2, cw, ch, not self.menu_focused and not self.modals)
            hints = page.hints
        else:
            c.centered(cx, 4, cw, "Exit TeamSpeak GIF Maker", Theme.HEADING)
            c.centered(cx, 6, cw, "Press Enter to exit.", Theme.TEXT)
            hints = ""
        if self.menu_focused:
            hints = "↑↓ choose · Enter open · F1 help · q exit"
        c.text(1, h - 2, hints, Theme.DIM, w - 2)
        if self.job_running():
            status, sstyle = "● Converting", Theme.STATUS_BAR_WARN
        else:
            status, sstyle = (f"● Ready · {len(self.entries)} file(s)", Theme.STATUS_BAR_OK)
        c.text(1, h - 1, status, sstyle)
        right = f"v{mg.__version__}  " + datetime.now().strftime("%a %d %b %Y %H:%M")
        c.text(w - text_width(right) - 1, h - 1, right, Theme.STATUS_BAR)
        if self.menu_focused or self.selected >= len(self.pages):
            c.cursor = None  # otherwise the focused text input placed it
        for m in self.modals:
            m.render(c)
        self.flush(c)

    def flush(self, c: Canvas):
        out = []
        for i, row in enumerate(c.rows()):
            if i >= len(self.prev_rows) or self.prev_rows[i] != row:
                out.append(f"\x1b[{i + 1};1H{row}")
        self.prev_rows = c.rows()
        if c.cursor:
            out.append(f"\x1b[{c.cursor[1] + 1};{c.cursor[0] + 1}H\x1b[?25h")
        else:
            out.append("\x1b[?25l")
        self.console.write("".join(out))

    # -- main loop
    def run(self):
        self.console.enable()
        try:
            self.console.write("\x1b[?1049h\x1b[?25l\x1b[2J")
            last_clock, dirty = "", True
            while self.running:
                keys = self.console.read(100 if self.job_running() else 250)
                if keys:
                    self.handle_batch(keys)
                    dirty = True
                if self.process_events():
                    dirty = True
                if self.job_running():
                    dirty = True  # spinner and timer
                clock = datetime.now().strftime("%H:%M")
                if clock != last_clock:
                    last_clock, dirty = clock, True
                if dirty or self.console.size() != self.prev_size:
                    self.render()
                    dirty = False
        finally:
            self.cancel.set()
            self.save_settings()
            self.console.write("\x1b[0m\x1b[?25h\x1b[?1049l")
            self.console.restore()


def main() -> int:
    files = sys.argv[1:]
    if sys.platform != "win32":
        print("The interactive interface currently needs a Windows console."
              " Use make_gif.py from the command line instead (see --help).")
        return 1
    try:
        console = WindowsConsole()
        app = App(console, files)
        app.run()
    except RuntimeError as e:
        print(f"Can't start the interface: {e}. Use make_gif.py from the command line instead.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
