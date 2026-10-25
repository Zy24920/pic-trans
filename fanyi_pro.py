#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fanyi_pro.py — 桌面翻译（文本翻译 + 截图图片翻译）
=============================================================================
在 fanyi.py 基础上新增：
  1. 全局热键 Ctrl+Alt+D 启动截图翻译（Win32 RegisterHotKey，独立线程监听）
  2. 冻结屏幕框选组件（微信截图式：暗化背景 + 亮区选取，ESC/右键取消）
  3. 图片翻译结果悬浮窗（参考网易有道截图翻译 UI）：
       [译文图] 在原图上叠加翻译结果（paste=2 区块合成）
       [原图]   框选的原始截图
       [对比]   原图 / 译文 左右并排
       [文本]   OCR 原文 + 译文，可复制
     支持切换目标语言重译、复制译文、保存图片、置顶、拖动
  4. 百度 APPID / TOKEN 不上界面，直接在下方配置区填写，
     或设置环境变量 BAIDU_FANYI_APPID / BAIDU_FANYI_TOKEN（环境变量优先）

依赖：Pillow（截图与图像合成）
图片翻译核心：baidu_pic_translate.py（与本文件同目录）
"""

import base64
import ctypes
import io
import os
import queue
import sys
import threading
import time
import tkinter as tk
import traceback
from ctypes import wintypes
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

# ---------------------------------------------------------------------------
# DPI 感知：必须在创建任何窗口前设置，保证截图坐标与 tkinter 坐标一致
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

# ---------------------------------------------------------------------------
# 自定义文本翻译模块（与 fanyi.py 相同）
# ---------------------------------------------------------------------------
sys.path.append('J:/us/C/spt')
try:
    import textrans
except Exception:
    textrans = None

# ---------------------------------------------------------------------------
# 图片翻译核心模块（同目录）
# ---------------------------------------------------------------------------
sys.path.insert(0, str(Path(__file__).resolve().parent))
from baidu_pic_translate import (
    PictureTranslator, compose_blocks,
    PicTransError, APIError, LANG_NAMES, TARGET_LANGS,
)

from PIL import Image, ImageEnhance, ImageGrab, ImageTk

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
# 百度图片翻译凭证：直接填在下面（留空则读环境变量，环境变量优先）
BAIDU_APPID = ""
BAIDU_TOKEN = ""
BAIDU_APPID = os.getenv("BAIDU_FANYI_APPID") or BAIDU_APPID
BAIDU_TOKEN = os.getenv("BAIDU_FANYI_TOKEN") or BAIDU_TOKEN

# 图片翻译默认参数
PIC_PASTE = 2          # 2=区块贴合：一次调用同时支持 叠加图/对比/文本 三种视图
PIC_VIEW_TYPE = 1      # 1=高精擦除
PIC_NEED_INTERVENE = 0
PIC_DEFAULT_TARGET = "zh"

MAX_B64_SIZE = 5 * 1024 * 1024   # 接口 Base64 上限

# 结果窗：最小尺寸（避免小选区时窗口过小）
PIC_WIN_MIN_W = 820
PIC_WIN_MIN_H = 600
# 自动适应只缩小不放大：译文图已在送检前预放大，二次插值会重新变糊
PIC_MAX_UPSCALE = 1.0

# 热键
HOTKEY_ID = 0xB001
HOTKEY_VK = 0x44     # D
HOTKEY_MODS = 0x0002 | 0x0001 | 0x4000   # MOD_CONTROL | MOD_ALT | MOD_NOREPEAT
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

# 图片翻译目标语言（界面显示名 -> 语种代码）
IMG_LANGS = [(LANG_NAMES[c], c) for c in TARGET_LANGS]

# 文本翻译目标语言（界面显示名 -> 语种代码，百度文本翻译语种代码）
TEXT_LANGS = [
    ("中文", "zh"), ("英语", "en"),
    ("韩语", "kor"), ("日语", "jp"), ("俄语", "ru"),
]


# ---------------------------------------------------------------------------
# Win32 工具
# ---------------------------------------------------------------------------
class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND),
        ("message", wintypes.UINT),
        ("wParam", wintypes.WPARAM),
        ("lParam", wintypes.LPARAM),
        ("time", wintypes.DWORD),
        ("pt", wintypes.POINT),
    ]


_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def virtual_screen_rect():
    """返回虚拟屏幕（含所有显示器）的 (x, y, w, h)，原点可能为负。"""
    return (
        _user32.GetSystemMetrics(76),   # SM_XVIRTUALSCREEN
        _user32.GetSystemMetrics(77),   # SM_YVIRTUALSCREEN
        _user32.GetSystemMetrics(78),   # SM_CXVIRTUALSCREEN
        _user32.GetSystemMetrics(79),   # SM_CYVIRTUALSCREEN
    )


class HotkeyListener(threading.Thread):
    """独立线程注册并监听全局热键；事件通过队列交给主线程。"""

    def __init__(self, events):
        super().__init__(daemon=True, name="HotkeyListener")
        self.events = events
        self.thread_id = None

    def run(self):
        self.thread_id = _kernel32.GetCurrentThreadId()
        if not _user32.RegisterHotKey(None, HOTKEY_ID, HOTKEY_MODS, HOTKEY_VK):
            self.events.put(("hotkey_fail", None))
            return
        self.events.put(("hotkey_ok", None))
        msg = MSG()
        while True:
            ret = _user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret <= 0:            # 0=WM_QUIT, -1=错误
                break
            if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                self.events.put(("hotkey", None))
        _user32.UnregisterHotKey(None, HOTKEY_ID)

    def stop(self):
        if self.thread_id:
            _user32.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)


# ---------------------------------------------------------------------------
# 截图框选组件
# ---------------------------------------------------------------------------
class SnipOverlay:
    """冻结全屏 -> 暗化 -> 框选亮区。完成后回调 PNG 字节与虚拟屏幕坐标。"""

    MIN_SIZE = 8   # 小于该尺寸视为误触

    def __init__(self, root, on_done, on_cancel):
        self.on_done = on_done
        self.on_cancel = on_cancel
        self.start = None
        self.finished = False

        x0, y0, w, h = virtual_screen_rect()
        self.origin = (x0, y0)

        # 冻结屏幕（在多显示器上覆盖整个虚拟屏幕）
        shot = ImageGrab.grab(all_screens=True)
        if shot.size != (w, h):     # DPI/显示器缩放异常兜底
            shot = shot.resize((w, h), Image.LANCZOS)
        self.shot = shot.convert("RGB")

        dark = ImageEnhance.Brightness(self.shot).enhance(0.55)

        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.geometry(f"{w}x{h}+{x0}+{y0}")
        self.win.configure(cursor="crosshair")

        self.canvas = tk.Canvas(
            self.win, width=w, height=h,
            highlightthickness=0, cursor="crosshair",
        )
        self.canvas.pack(fill=tk.BOTH, expand=True)
        self._dark_photo = ImageTk.PhotoImage(dark)
        self.canvas.create_image(0, 0, anchor="nw", image=self._dark_photo)
        self._crop_photo = None

        hint = self.canvas.create_text(
            w // 2, 36,
            text="按住左键拖动框选翻译区域    ·    ESC / 右键 取消",
            fill="#FFFFFF", font=("Microsoft YaHei", 14, "bold"),
        )
        self.win.after(3000, lambda: self.canvas.delete(hint))

        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Button-3>", lambda e: self._cancel())
        self.win.bind("<Escape>", lambda e: self._cancel())

        self.win.focus_force()
        self.win.grab_set()

    # --------------------------------------------------------------
    def _clamp(self, x, y):
        w = self.canvas.winfo_width() or self.shot.width
        h = self.canvas.winfo_height() or self.shot.height
        return max(0, min(x, w)), max(0, min(y, h))

    def _on_press(self, event):
        self.start = self._clamp(event.x, event.y)
        self.canvas.delete("sel")

    def _on_motion(self, event):
        if not self.start:
            return
        x2, y2 = self._clamp(event.x, event.y)
        x1, y1 = self.start
        nx1, nx2 = sorted((x1, x2))
        ny1, ny2 = sorted((y1, y2))
        w, h = nx2 - nx1, ny2 - ny1

        self.canvas.delete("sel")
        if w >= 2 and h >= 2:
            crop = self.shot.crop((nx1, ny1, nx2, ny2))
            self._crop_photo = ImageTk.PhotoImage(crop)
            self.canvas.create_image(
                nx1, ny1, anchor="nw", image=self._crop_photo, tags="sel")
        self.canvas.create_rectangle(
            nx1, ny1, nx2, ny2, outline="#07C160", width=2, tags="sel")
        # 尺寸标签
        label = f"{w} × {h}"
        ty = ny1 - 22 if ny1 > 26 else ny2 + 8
        self.canvas.create_rectangle(
            nx1, ty, nx1 + 90, ty + 20, fill="#07C160", width=0, tags="sel")
        self.canvas.create_text(
            nx1 + 45, ty + 10, text=label, fill="#FFFFFF",
            font=("Microsoft YaHei", 9), tags="sel")

    def _on_release(self, event):
        if not self.start or self.finished:
            return
        x2, y2 = self._clamp(event.x, event.y)
        x1, y1 = self.start
        self.start = None
        nx1, nx2 = sorted((x1, x2))
        ny1, ny2 = sorted((y1, y2))
        if (nx2 - nx1) < self.MIN_SIZE or (ny2 - ny1) < self.MIN_SIZE:
            self.canvas.delete("sel")
            return
        self.finished = True

        crop = self.shot.crop((nx1, ny1, nx2, ny2))
        ox, oy = self.origin
        rect = (ox + nx1, oy + ny1, ox + nx2, oy + ny2)   # 虚拟屏幕坐标
        self._close()
        self.on_done(crop, rect)

    def _cancel(self):
        if self.finished:
            return
        self.finished = True
        self._close()
        self.on_cancel()

    def _close(self):
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()


# ---------------------------------------------------------------------------
# 图片翻译结果悬浮窗（参考有道截图翻译）
# ---------------------------------------------------------------------------
class PicResultWindow:
    """无边框悬浮窗：译文图 / 原图 / 对比 / 文本 四视图。"""

    def __init__(self, app, anchor_rect):
        self.app = app
        self.anchor_rect = anchor_rect
        self.closed = False          # 窗口是否已关闭，异步结果不再回填
        self.view = "trans"
        self.orig_pil = None
        self.trans_pil = None
        self.result = None
        self._photo = []          # 防止 PhotoImage 被 GC
        self._topmost = tk.BooleanVar(value=True)
        self.target_name = tk.StringVar()
        self.zoom = None          # None=自动适应窗口；float=手动缩放倍数
        self._last_scale = 1.0
        self._last_disp_size = (0, 0)

        win = tk.Toplevel(app.root)
        self.win = win
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg="#2B2B2B")

        self._build_title_bar()
        self._build_toolbar()
        self.content = tk.Frame(win, bg="#2B2B2B")
        self.content.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 4))

        bottom = tk.Frame(win, bg="#2B2B2B")
        bottom.pack(fill=tk.X, padx=10, pady=(0, 6))
        self.status = tk.Label(
            bottom, text="正在翻译…", bg="#2B2B2B", fg="#9E9E9E",
            font=("Microsoft YaHei", 9), anchor="w")
        self.status.pack(side=tk.LEFT)
        self.zoom_label = tk.Label(
            bottom, text="", bg="#2B2B2B", fg="#9E9E9E",
            font=("Microsoft YaHei", 9), width=5, anchor="e")
        self.zoom_label.pack(side=tk.RIGHT)
        for text, cmd in (
            ("＋", lambda: self._zoom_by(1.25)),
            ("适应", self._zoom_fit),
            ("－", lambda: self._zoom_by(1 / 1.25)),
        ):
            tk.Button(
                bottom, text=text, command=cmd, bg="#3C3C3C", fg="#DDDDDD",
                activebackground="#4A4A4A", activeforeground="#FFFFFF",
                relief=tk.FLAT, font=("Microsoft YaHei", 9), padx=6, pady=0,
                cursor="hand2", bd=0).pack(side=tk.RIGHT, padx=2)

        # Ctrl+滚轮缩放
        win.bind("<Control-MouseWheel>", self._on_ctrl_wheel)

        self._show_loading()
        self._place_near(anchor_rect)

    # ------------------------------------------------------------ 标题栏
    def _build_title_bar(self):
        bar = tk.Frame(self.win, bg="#1F1F1F", height=30)
        bar.pack(fill=tk.X)

        self.title_label = tk.Label(
            bar, text="  图片翻译", bg="#1F1F1F", fg="#FFFFFF",
            font=("Microsoft YaHei", 10, "bold"))
        self.title_label.pack(side=tk.LEFT, padx=6)

        for text, cmd, fg in (
            ("✕", self.close, "#FFFFFF"),
            ("置顶", self._toggle_topmost, "#9E9E9E"),
        ):
            btn = tk.Label(
                bar, text=text, bg="#1F1F1F", fg=fg,
                font=("Microsoft YaHei", 9 if text == "置顶" else 11),
                padx=8, cursor="hand2")
            btn.pack(side=tk.RIGHT, pady=2)
            btn.bind("<Button-1>", lambda e, c=cmd: c())
            if text == "置顶":
                self.pin_btn = btn
        self.pin_btn.config(fg="#07C160")   # 默认置顶已开启

        # 拖动
        for widget in (bar, self.title_label):
            widget.bind("<ButtonPress-1>", self._drag_start)
            widget.bind("<B1-Motion>", self._drag_move)

    def _drag_start(self, event):
        self._drag_x = event.x_root - self.win.winfo_x()
        self._drag_y = event.y_root - self.win.winfo_y()

    def _drag_move(self, event):
        self.win.geometry(
            f"+{event.x_root - self._drag_x}+{event.y_root - self._drag_y}")

    def _toggle_topmost(self):
        self._topmost.set(not self._topmost.get())
        self.win.attributes("-topmost", self._topmost.get())
        self.pin_btn.config(
            fg="#07C160" if self._topmost.get() else "#9E9E9E")

    # ------------------------------------------------------------ 工具栏
    def _build_toolbar(self):
        bar = tk.Frame(self.win, bg="#2B2B2B")
        bar.pack(fill=tk.X, padx=8, pady=6)

        self.view_var = tk.StringVar(value="trans")
        for text, value in (
            ("译文图", "trans"), ("原图", "orig"),
            ("对比", "compare"), ("文本", "text"),
        ):
            rb = tk.Radiobutton(
                bar, text=text, value=value, variable=self.view_var,
                indicatoron=False, width=6,
                bg="#3C3C3C", fg="#DDDDDD",
                selectcolor="#07C160",
                activebackground="#4A4A4A", activeforeground="#FFFFFF",
                font=("Microsoft YaHei", 9),
                command=lambda: self.show_view(self.view_var.get()))
            rb.pack(side=tk.LEFT, padx=(0, 4))
            setattr(self, f"_rb_{value}", rb)

        ttk.Separator(bar, orient=tk.VERTICAL).pack(
            side=tk.LEFT, fill=tk.Y, padx=6)

        lang_box = ttk.Combobox(
            bar, textvariable=self.target_name, state="readonly", width=10,
            values=[name for name, _ in IMG_LANGS])
        lang_box.pack(side=tk.LEFT)
        lang_box.bind("<<ComboboxSelected>>",
                      lambda e: self._retranslate())

        self._btn(bar, "重译", self._retranslate).pack(side=tk.LEFT, padx=4)
        self._btn(bar, "复制译文", self._copy_dst).pack(side=tk.LEFT, padx=2)
        self._btn(bar, "保存图片", self._save_image).pack(side=tk.LEFT, padx=2)

    def _btn(self, parent, text, cmd):
        return tk.Button(
            parent, text=text, command=cmd, bg="#3C3C3C", fg="#DDDDDD",
            activebackground="#4A4A4A", activeforeground="#FFFFFF",
            relief=tk.FLAT, font=("Microsoft YaHei", 9), padx=8, pady=1,
            cursor="hand2", bd=0)

    # ------------------------------------------------------------ 内容区
    def _show_loading(self):
        self._clear_content()
        self.zoom_label.config(text="")
        tk.Label(
            self.content, text="\n正在识别并翻译，请稍候…\n",
            bg="#2B2B2B", fg="#DDDDDD",
            font=("Microsoft YaHei", 12)).pack(padx=60, pady=30)
        self._apply_size()

    def _clear_content(self):
        for child in self.content.winfo_children():
            child.destroy()
        self._photo.clear()

    def _place_near(self, rect):
        self._apply_size()
        x0, y0, vw, vh = virtual_screen_rect()
        w = self.win.winfo_width()
        h = self.win.winfo_height()
        x = rect[2] + 8
        y = rect[3] + 8
        if x + w > x0 + vw:
            x = rect[0] - w - 8
        if x < x0:
            x = x0 + vw - w - 12
        if y + h > y0 + vh:
            y = rect[1] - h - 8
        if y < y0:
            y = y0 + vh - h - 12
        self.win.geometry(f"+{x}+{y}")

    def _apply_size(self):
        """保证窗口不小于可查看尺寸，同时不超过屏幕。"""
        self.win.update_idletasks()
        _, _, vw, vh = virtual_screen_rect()
        w = min(max(self.win.winfo_reqwidth(), PIC_WIN_MIN_W), vw - 16)
        h = min(max(self.win.winfo_reqheight(), PIC_WIN_MIN_H), vh - 40)
        self.win.geometry(f"{w}x{h}")
        self.win.update_idletasks()

    def _max_content(self):
        """内容区可用尺寸：比旧版更大，且不小于窗口最小尺寸。"""
        _, _, vw, vh = virtual_screen_rect()
        return (
            max(PIC_WIN_MIN_W - 40, int(vw * 0.62)),
            max(PIC_WIN_MIN_H - 100, int(vh * 0.72)),
        )

    def _scale_for(self, img, max_w, max_h, respect_zoom=True):
        """自动适应：大图缩小、小图保持 1:1（预放大已在送检前完成）。"""
        if respect_zoom and self.zoom is not None:
            return self.zoom
        return min(max_w / img.width, max_h / img.height, PIC_MAX_UPSCALE)

    def _render(self, img, max_w, max_h, respect_zoom=True):
        scale = self._scale_for(img, max_w, max_h, respect_zoom)
        self._last_scale = scale
        size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
        self._last_disp_size = size
        disp = img if abs(scale - 1.0) < 1e-3 else img.resize(size, Image.LANCZOS)
        photo = ImageTk.PhotoImage(disp)
        self._photo.append(photo)
        return photo

    def _show_pil(self, img, max_w=None, max_h=None):
        """图片视图：窗口内居中显示，放不下时提供滚动条。"""
        mw, mh = self._max_content()
        max_w, max_h = max_w or mw, max_h or mh
        photo = self._render(img, max_w, max_h)
        dw, dh = self._last_disp_size

        wrap = tk.Frame(self.content, bg="#2B2B2B")
        wrap.pack(expand=True)
        canvas = tk.Canvas(
            wrap, width=min(dw, max_w), height=min(dh, max_h),
            bg="#2B2B2B", highlightthickness=0)
        canvas.create_image(0, 0, anchor="nw", image=photo)

        need_v = dh > max_h
        need_h = dw > max_w
        vbar = hbar = None
        if need_v:
            vbar = tk.Scrollbar(wrap, orient=tk.VERTICAL, command=canvas.yview)
            vbar.grid(row=0, column=1, sticky="ns")
        if need_h:
            hbar = tk.Scrollbar(wrap, orient=tk.HORIZONTAL, command=canvas.xview)
            hbar.grid(row=1, column=0, sticky="ew")
        canvas.grid(row=0, column=0)
        canvas.config(scrollregion=(0, 0, dw, dh))
        if vbar is not None:
            canvas.config(yscrollcommand=vbar.set)
        if hbar is not None:
            canvas.config(xscrollcommand=hbar.set)
        canvas.bind(
            "<MouseWheel>",
            lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

    # ------------------------------------------------------------ 缩放
    def _on_ctrl_wheel(self, event):
        self._zoom_by(1.25 if event.delta > 0 else 1 / 1.25)

    def _zoom_by(self, factor):
        if self.view == "text":
            return
        base = self.zoom if self.zoom is not None else self._last_scale
        self.zoom = max(0.1, min(base * factor, 6.0))
        self.show_view(self.view)

    def _zoom_fit(self):
        if self.view == "text":
            return
        self.zoom = None
        self.show_view(self.view)

    def _update_zoom_label(self):
        if self.view == "text":
            self.zoom_label.config(text="文本")
        elif self.view == "compare":
            self.zoom_label.config(text="对比")
        else:
            self.zoom_label.config(text=f"{int(round(self._last_scale * 100))}%")

    # ------------------------------------------------------------ 视图
    def show_view(self, view):
        self.view = view
        self._clear_content()
        if view == "text":
            self._show_text_view()
            self._update_zoom_label()
            self._apply_size()
            return
        if view == "orig" and self.orig_pil is not None:
            self._show_pil(self.orig_pil)
        elif view == "trans":
            if self.trans_pil is not None:
                self._show_pil(self.trans_pil)
            else:
                self._show_text_view(note="该结果无贴合图片，已切换为文本视图")
        elif view == "compare":
            mw, mh = self._max_content()
            row = tk.Frame(self.content, bg="#2B2B2B")
            row.pack(expand=True)
            for title, img in (("原图", self.orig_pil), ("译文", self.trans_pil)):
                if img is None:
                    continue
                col = tk.Frame(row, bg="#2B2B2B")
                col.pack(side=tk.LEFT, padx=6)
                tk.Label(col, text=title, bg="#2B2B2B", fg="#9E9E9E",
                         font=("Microsoft YaHei", 9)).pack()
                photo = self._render(img, mw // 2 - 20, mh, respect_zoom=False)
                tk.Label(col, image=photo, bg="#2B2B2B").pack()
        self._update_zoom_label()
        self._apply_size()

    def _show_text_view(self, note=None):
        if note:
            tk.Label(self.content, text=note, bg="#2B2B2B", fg="#FFB74D",
                     font=("Microsoft YaHei", 9)).pack(anchor="w")
        if self.result is None:
            return
        paned = tk.Frame(self.content, bg="#2B2B2B")
        paned.pack(fill=tk.BOTH, expand=True, pady=4)
        for title, text in (
            ("原文（OCR）", self.result.src),
            ("译文", self.result.dst),
        ):
            col = tk.Frame(paned, bg="#2B2B2B")
            col.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=6)
            tk.Label(col, text=title, bg="#2B2B2B", fg="#9E9E9E",
                     font=("Microsoft YaHei", 9)).pack(anchor="w", pady=(0, 3))
            box = tk.Text(
                col, width=48, height=18, wrap=tk.WORD,
                bg="#383838", fg="#EEEEEE", insertbackground="#EEEEEE",
                relief=tk.FLAT, font=("Microsoft YaHei", 11))
            box.insert("1.0", text or "")
            box.config(state=tk.DISABLED)
            box.pack(fill=tk.BOTH, expand=True)

    # ------------------------------------------------------------ 数据
    def set_result(self, result, orig_bytes, trans_bytes):
        self.result = result
        self.zoom = None          # 新结果回到自动适应
        self.orig_pil = Image.open(io.BytesIO(orig_bytes)).convert("RGB")
        self.trans_pil = (
            Image.open(io.BytesIO(trans_bytes)).convert("RGB")
            if trans_bytes else None)
        self.title_label.config(
            text=f"  图片翻译  {result.source_lang} → {result.target_lang}")
        name = next(
            (n for n, c in IMG_LANGS if c == result.target_lang), "")
        self.target_name.set(name)
        self.status.config(
            text=f"耗时 {result.elapsed:.1f}s · {len(result.blocks)} 个文本块"
                 f" · paste={result.request.get('paste')}"
                 f" · view_type={result.request.get('view_type')}")
        # 无贴合图时默认落在文本视图
        self.view_var.set("trans" if self.trans_pil is not None else "text")
        self.show_view(self.view_var.get())
        self._place_near(self.anchor_rect)

    def set_error(self, message):
        self._clear_content()
        self.zoom_label.config(text="")
        tk.Label(
            self.content, text=f"\n翻译失败：{message}\n",
            bg="#2B2B2B", fg="#EF5350",
            font=("Microsoft YaHei", 10), wraplength=420,
            justify=tk.LEFT).pack(padx=20, pady=20)
        self.status.config(text="翻译失败")
        self._place_near(self.anchor_rect)

    # ------------------------------------------------------------ 动作
    def _retranslate(self):
        name = self.target_name.get()
        code = next((c for n, c in IMG_LANGS if n == name), None)
        if code and self.result and code != self.result.target_lang:
            self.app.start_pic_translate(
                self.app.last_png_bytes, code, reuse=self)

    def _copy_dst(self):
        if self.result and self.result.dst:
            self.win.clipboard_clear()
            self.win.clipboard_append(self.result.dst)
            self.status.config(text="译文已复制到剪贴板")

    def _save_image(self):
        img = self.trans_pil if self.view in ("trans", "compare") else self.orig_pil
        if img is None and self.trans_pil is not None:
            img = self.trans_pil
        if img is None:
            self.status.config(text="当前视图没有可保存的图片")
            return
        path = filedialog.asksaveasfilename(
            parent=self.win, defaultextension=".png",
            initialfile=f"pictrans_{int(time.time())}.png",
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg")])
        if path:
            img.save(path)
            self.status.config(text=f"已保存：{path}")

    def close(self):
        self.closed = True
        try:
            self.win.grab_release()
        except Exception:
            pass
        self.win.destroy()
        if self.app.pic_win is self:
            self.app.pic_win = None


# ---------------------------------------------------------------------------
# 主程序（保留 fanyi.py 全部文本翻译功能 + 新增截图翻译）
# ---------------------------------------------------------------------------
class TranslatorApp:
    # 编辑防抖延迟（毫秒）：停止输入超过该时长才自动翻译，避免频繁调用接口
    AUTO_TRANSLATE_DELAY_MS = 2000
    # 文本翻译兜底超时（毫秒）：大于 textrans 内部的 30 秒请求超时
    TEXT_TIMEOUT_MS = 45000
    INPUT_PLACEHOLDER = "输入或粘贴文本，自动翻译"

    def __init__(self, root):
        self.root = root
        self.root.title("桌面翻译（文本 + 截图）")
        self.root.geometry("760x620")
        self.root.minsize(640, 460)

        self.translating = False
        self._debounce_job = None          # 编辑防抖定时器
        self._last_translated_source = ""  # 上次翻译成功的原文，用于跳过重复请求
        self._text_request_id = 0          # 文本翻译请求序号，丢弃过期结果
        self._text_watchdog_job = None     # 请求超时兜底定时器
        self._auto_retry_pending = False   # 翻译期间又有新的自动翻译请求

        # 截图翻译状态
        self.events = queue.Queue()
        self.snipping = False
        self.pic_busy = False
        self.pic_win = None
        self.last_png_bytes = None

        self.hotkey_listener = HotkeyListener(self.events)
        self.hotkey_listener.start()

        self.create_ui()

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self._poll_events()

    # ============================================================ UI
    def create_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        input_label = ttk.Label(main, text="原文")
        input_label.pack(anchor="w")

        self.input_text = tk.Text(
            main, height=9, wrap=tk.WORD, font=("Microsoft YaHei", 11))
        self.input_text.pack(fill=tk.BOTH, expand=True, pady=(5, 10))

        self.input_text.bind("<<Paste>>", self.on_paste)
        self.input_text.bind("<KeyRelease>", self.on_input_edit)

        # 占位提示：输入框为空且未聚焦时显示灰色引导文字
        self._input_fg = self.input_text.cget("foreground")
        self._placeholder_active = False
        self.input_text.bind("<FocusIn>", self._hide_placeholder)
        self.input_text.bind("<FocusOut>", self._show_placeholder)
        self._show_placeholder()

        # 右键菜单：粘贴（无键盘/热键场景下也能粘贴）
        self.input_menu = tk.Menu(self.input_text, tearoff=0)
        self.input_menu.add_command(label="粘贴", command=self.paste_from_menu)
        self.input_text.bind("<Button-3>", self._show_input_menu)

        toolbar = ttk.Frame(main)
        toolbar.pack(fill=tk.X, pady=(0, 2))

        ttk.Label(toolbar, text="翻译为：").pack(side=tk.LEFT)

        self.target_language = tk.StringVar(value="中文")
        language_box = ttk.Combobox(
            toolbar, textvariable=self.target_language,
            values=[name for name, _ in TEXT_LANGS],
            state="readonly", width=4) #10太长了
        language_box.pack(side=tk.LEFT, padx=(5, 10))

        self.auto_translate_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            toolbar, text="自动翻译",
            variable=self.auto_translate_var).pack(side=tk.LEFT, padx=(0, 5))

        self.translate_button = ttk.Button(
            toolbar, text="翻译", command=self.translate_manually)
        self.translate_button.pack(side=tk.LEFT)

        self.status_label = ttk.Label(toolbar, text="等待输入文本")
        self.status_label.pack(side=tk.RIGHT)

        hint = ttk.Label(
            main,
            text=(f"勾选“自动翻译”后：粘贴约 150ms 后翻译，"
                  f"手动编辑停顿 {self.AUTO_TRANSLATE_DELAY_MS}ms 后翻译"),
            font=("Microsoft YaHei", 9), foreground="#666666")
        hint.pack(anchor="w", pady=(0, 8))

        output_label = ttk.Label(main, text="翻译结果")
        output_label.pack(anchor="w")

        self.output_text = tk.Text(
            main, height=8, wrap=tk.WORD,
            font=("Microsoft YaHei", 11), state=tk.DISABLED)
        self.output_text.pack(fill=tk.BOTH, expand=True, pady=(5, 0))

        # 右键菜单：复制译文（优先复制选中内容，无选中时复制全部）
        self.output_menu = tk.Menu(self.output_text, tearoff=0)
        self.output_menu.add_command(label="复制", command=self.copy_from_output_menu)
        self.output_text.bind("<Button-3>", self._show_output_menu)

        self.root.bind("<Control-Return>", lambda event: self.translate_manually())

        # -------------------- 截图图片翻译区 --------------------
        ttk.Separator(main).pack(fill=tk.X, pady=10)
        pic = ttk.LabelFrame(main, text="截图图片翻译（百度图片翻译 V2.0）", padding=8)
        pic.pack(fill=tk.X)

        row1 = ttk.Frame(pic)
        row1.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(
            row1, text="热键：Ctrl + Alt + D",
            font=("Microsoft YaHei", 10, "bold")).pack(side=tk.LEFT)
        self.hotkey_status = ttk.Label(row1, text="热键注册中…")
        self.hotkey_status.pack(side=tk.LEFT, padx=10)

        ttk.Button(
            row1, text="立即截图翻译",
            command=self.start_snip).pack(side=tk.RIGHT)

        row2 = ttk.Frame(pic)
        row2.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(row2, text="译文语言：").pack(side=tk.LEFT)
        default_name = next(
            (n for n, c in IMG_LANGS if c == PIC_DEFAULT_TARGET), IMG_LANGS[0][0])
        self.pic_target_name = tk.StringVar(value=default_name)
        ttk.Combobox(
            row2, textvariable=self.pic_target_name, state="readonly",
            width=10, values=[n for n, _ in IMG_LANGS]).pack(side=tk.LEFT, padx=(4, 10))
        ttk.Label(
            row2, text="按下热键后框选屏幕区域，自动识别并翻译图中文字",
            foreground="#666666").pack(side=tk.LEFT)

    # ============================================================ 文本翻译（保持原逻辑）
    def _show_placeholder(self, event=None):
        """输入框为空时显示占位提示文字（灰色）。"""
        if self._placeholder_active:
            return
        if not self.input_text.get("1.0", tk.END).strip():
            self._placeholder_active = True
            self.input_text.insert("1.0", self.INPUT_PLACEHOLDER)
            self.input_text.config(foreground="#999999")

    def _hide_placeholder(self, event=None):
        if self._placeholder_active:
            self._placeholder_active = False
            self.input_text.delete("1.0", tk.END)
            self.input_text.config(foreground=self._input_fg)

    def _get_input_text(self):
        """读取输入框内容；占位提示显示期间视为空。"""
        if self._placeholder_active:
            return ""
        return self.input_text.get("1.0", tk.END).strip()

    def _show_input_menu(self, event):
        """输入框右键菜单：剪贴板为空时禁用“粘贴”。"""
        try:
            self.input_text.clipboard_get()
            state = tk.NORMAL
        except tk.TclError:
            state = tk.DISABLED
        self.input_menu.entryconfig(0, state=state)
        self.input_menu.tk_popup(event.x_root, event.y_root)

    def paste_from_menu(self):
        """右键粘贴：插入剪贴板文本，并触发与 <<Paste>> 相同的自动翻译。"""
        self._hide_placeholder()
        try:
            text = self.input_text.clipboard_get()
        except tk.TclError:
            return
        try:
            self.input_text.delete("sel.first", "sel.last")   # 覆盖选中内容
        except tk.TclError:
            pass
        self.input_text.insert(tk.INSERT, text)
        self.on_paste()

    def on_paste(self, event=None):
        if not self.auto_translate_var.get():
            return
        if self._debounce_job is not None:
            # 编辑会话中的粘贴视为继续编辑，并入防抖，停顿后再翻译
            self.on_input_edit()
            return
        self.root.after(150, self.translate_manually)

    def on_input_edit(self, event=None):
        """编辑防抖：每次按键重置定时器，停止输入超过设定时长后才触发翻译。"""
        if not self.auto_translate_var.get():
            return
        if self._debounce_job is not None:
            self.root.after_cancel(self._debounce_job)
        self._debounce_job = self.root.after(
            self.AUTO_TRANSLATE_DELAY_MS, self._auto_translate_fire)

    def _auto_translate_fire(self):
        self._debounce_job = None
        text = self._get_input_text()
        # 内容为空，或与上次翻译的原文相同（如移动光标、粘贴残留的定时器），跳过
        if not text or text == self._last_translated_source:
            return
        self.translate_manually()

    def _show_output_menu(self, event):
        """结果框右键菜单：无任何内容时禁用“复制”。"""
        has_text = bool(self.output_text.get("1.0", tk.END).strip())
        self.output_menu.entryconfig(
            0, state=tk.NORMAL if has_text else tk.DISABLED)
        self.output_menu.tk_popup(event.x_root, event.y_root)

    def copy_from_output_menu(self):
        """右键复制：有选中复制选中，否则复制全部译文。"""
        try:
            text = self.output_text.get("sel.first", "sel.last")
        except tk.TclError:
            text = self.output_text.get("1.0", tk.END).strip()
        if not text:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.set_status("译文已复制到剪贴板")

    def translate_manually(self):
        text = self._get_input_text()
        if self.translating:
            # 上一次请求还没返回，本次触发先记下来，结果返回后自动补一次
            if self.auto_translate_var.get() and text:
                self._auto_retry_pending = True
            return

        if not text:
            self.set_status("请先输入或粘贴文本")
            return

        if textrans is None:
            self.show_error("文本翻译模块 textrans 未能加载，请检查 J:/us/C/spt 是否存在")
            return

        if self._debounce_job is not None:
            self.root.after_cancel(self._debounce_job)
            self._debounce_job = None

        target = next(
            (c for n, c in TEXT_LANGS if n == self.target_language.get()),
            "zh")

        self._text_request_id += 1
        request_id = self._text_request_id
        self.translating = True
        self.translate_button.config(state=tk.DISABLED)
        self.set_status("翻译中……")
        self._start_text_watchdog(request_id)

        thread = threading.Thread(
            target=self.do_translate, args=(text, target, request_id),
            daemon=True)
        thread.start()

    def do_translate(self, text, target, request_id):
        """工作线程：只做网络请求，结果通过队列交给主线程处理。"""
        try:
            translated_text = textrans.run(text, "auto", target)
            if not translated_text:
                raise RuntimeError("百度翻译没有返回翻译结果")
            self.events.put(("text_done", (request_id, text, translated_text)))
        except Exception as error:
            self.events.put(("text_error", (request_id, error)))

    def _start_text_watchdog(self, request_id):
        self._cancel_text_watchdog()
        self._text_watchdog_job = self.root.after(
            self.TEXT_TIMEOUT_MS, lambda: self._on_text_timeout(request_id))

    def _cancel_text_watchdog(self):
        if self._text_watchdog_job is not None:
            self.root.after_cancel(self._text_watchdog_job)
            self._text_watchdog_job = None

    def _on_text_timeout(self, request_id):
        self._text_watchdog_job = None
        if request_id != self._text_request_id or not self.translating:
            return
        self._text_request_id += 1      # 作废仍在运行的请求，迟到结果不再生效
        self.show_error(
            f"请求超时（{self.TEXT_TIMEOUT_MS // 1000} 秒内没有收到响应）。\n"
            "请检查网络连接后重新翻译。")

    def show_result(self, text, source=""):
        self.output_text.config(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", text)
        self.output_text.config(state=tk.DISABLED)

        self._last_translated_source = source
        self.set_status("翻译完成")
        self._finish_text_request()

    def show_error(self, error):
        # 错误信息直接写入翻译结果框，避免弹窗打断复制/编辑
        self.output_text.config(state=tk.NORMAL)
        self.output_text.delete("1.0", tk.END)
        self.output_text.insert("1.0", f"翻译失败：\n{error}")
        self.output_text.config(state=tk.DISABLED)

        self.set_status("翻译失败")
        self._finish_text_request()

    def _finish_text_request(self):
        """恢复 UI 状态；若有翻译期间被丢弃的自动翻译请求，补触发一次。"""
        self._cancel_text_watchdog()
        self.translating = False
        self.translate_button.config(state=tk.NORMAL)
        if self._auto_retry_pending:
            self._auto_retry_pending = False
            if self.auto_translate_var.get():
                self._auto_translate_fire()

    def set_status(self, text):
        self.status_label.config(text=text)

    # ============================================================ 事件队列（线程 -> 主线程）
    def _poll_events(self):
        try:
            while True:
                name, payload = self.events.get_nowait()
                try:
                    self._handle_event(name, payload)
                except Exception:
                    # 单个事件处理异常不能中断队列轮询，否则热键/翻译结果全部失效
                    traceback.print_exc()
        except queue.Empty:
            pass
        try:
            self.root.after(80, self._poll_events)
        except tk.TclError:          # 窗口已销毁，退出轮询
            pass

    def _handle_event(self, name, payload):
        if name == "hotkey":
            self.start_snip()
        elif name == "hotkey_ok":
            self.hotkey_status.config(text="热键已就绪", foreground="#2E7D32")
        elif name == "hotkey_fail":
            self.hotkey_status.config(
                text="热键注册失败（可能被其它程序占用）", foreground="#B71C1C")
        elif name == "text_done":
            request_id, source, translated_text = payload
            if request_id == self._text_request_id and self.translating:
                self.show_result(translated_text, source)
        elif name == "text_error":
            request_id, error = payload
            if request_id == self._text_request_id and self.translating:
                self.show_error(str(error))
        elif name == "pic_done":
            result, orig_bytes, trans_bytes, win = payload
            self.pic_busy = False
            if win is not None and win.app is self and not win.closed:
                win.set_result(result, orig_bytes, trans_bytes)
        elif name == "pic_error":
            err, win = payload
            self.pic_busy = False
            if win is not None and win.app is self and not win.closed:
                win.set_error(str(err))

    # ============================================================ 截图翻译流程
    def start_snip(self):
        if self.snipping or self.pic_busy:
            return
        if not (BAIDU_APPID and BAIDU_TOKEN):
            messagebox.showwarning(
                "缺少凭证",
                "请在本脚本配置区填写 BAIDU_APPID / BAIDU_TOKEN，"
                "或设置环境变量 BAIDU_FANYI_APPID / BAIDU_FANYI_TOKEN")
            return
        self.snipping = True
        self.root.withdraw()                # 隐藏主窗口
        self.root.after(300, self._open_snipper)

    def _open_snipper(self):
        try:
            SnipOverlay(
                self.root,
                on_done=self._on_snip_done,
                on_cancel=self._on_snip_cancel)
        except Exception as exc:
            self.snipping = False
            self.root.deiconify()
            messagebox.showerror("截图失败", str(exc))

    def _on_snip_cancel(self):
        self.snipping = False
        self.root.deiconify()

    def _on_snip_done(self, crop_img, rect):
        self.snipping = False
        self.root.deiconify()
        try:
            png_bytes = self._prepare_image(crop_img)
        except Exception as exc:
            messagebox.showerror("图片处理失败", str(exc))
            return
        self.last_png_bytes = png_bytes

        if self.pic_win is not None:
            self.pic_win.close()
        self.pic_win = PicResultWindow(self, rect)

        name = self.pic_target_name.get()
        code = next((c for n, c in IMG_LANGS if n == name), PIC_DEFAULT_TARGET)
        self.start_pic_translate(png_bytes, code, reuse=self.pic_win)

    def start_pic_translate(self, png_bytes, target_code, reuse=None):
        if self.pic_busy:
            return
        self.pic_busy = True
        win = reuse
        thread = threading.Thread(
            target=self._pic_worker,
            args=(png_bytes, target_code, win),
            daemon=True)
        thread.start()

    def _pic_worker(self, png_bytes, target_code, win):
        try:
            translator = PictureTranslator(
                appid=BAIDU_APPID,
                token=BAIDU_TOKEN,
                timeout=60, retries=2)
            result = translator.translate_bytes(
                png_bytes,
                source_lang="auto",
                target_lang=target_code,
                paste=PIC_PASTE,
                view_type=PIC_VIEW_TYPE,
                need_intervene=PIC_NEED_INTERVENE,
            )
            trans_bytes = None
            if PIC_PASTE == 1:
                trans_bytes = result.full_image
            elif any(b.paste_img for b in result.blocks):
                try:
                    trans_bytes = compose_blocks(png_bytes, result.blocks)
                except Exception:
                    trans_bytes = None
            self.events.put(("pic_done", (result, png_bytes, trans_bytes, win)))
        except Exception as exc:
            self.events.put(("pic_error", (exc, win)))

    @staticmethod
    def _prepare_image(img):
        """小图先预放大再送接口，保证 Base64 后不超过 5MB。

        接口是按传入图片的像素尺寸渲染译文的：小选区里字高只有十几像素，
        返回的贴合图字号也很小，本地再放大只会变糊。先把小图放大 1.5~3 倍，
        接口就会在更大的画布上渲染译文，显示时接近 1:1，文字明显更清晰。
        """
        orig_w, orig_h = img.size
        longest = max(orig_w, orig_h)
        if longest < 300:
            factor = 3.0
        elif longest < 600:
            factor = 2.0
        elif longest < 900:
            factor = 1.5
        else:
            factor = 1.0
        # 长边不超过 2400，避免小选区被放大得过大
        factor = min(factor, 2400 / longest)
        if factor > 1.01:
            img = img.resize(
                (max(1, round(orig_w * factor)),
                 max(1, round(orig_h * factor))),
                Image.LANCZOS)

        for scale in (1.0, 0.85, 0.7, 0.55, 0.4):
            cur = img if scale == 1.0 else img.resize(
                (max(1, int(img.width * scale)),
                 max(1, int(img.height * scale))), Image.LANCZOS)
            for as_jpeg, quality in ((False, 0), (True, 90), (True, 75)):
                buf = io.BytesIO()
                if as_jpeg:
                    cur.convert("RGB").save(buf, "JPEG", quality=quality)
                else:
                    cur.save(buf, "PNG")
                data = buf.getvalue()
                if len(base64.b64encode(data)) <= MAX_B64_SIZE:
                    return data
        raise PicTransError("图片过大，压缩后仍超过接口 5MB 限制，请框选更小区域")

    # ============================================================ 退出
    def on_close(self):
        self.hotkey_listener.stop()
        self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = TranslatorApp(root)
    root.mainloop()
