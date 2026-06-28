# -*- coding: utf-8 -*-
"""
TRAE 工作状态红绿灯  (vibe coding traffic light) — tkinter 版

始终置顶在屏幕最前方的小窗:
  - 默认只显示红绿灯 (红=未运行 / 绿=空闲 / 黄=工作中)
  - 鼠标悬停时向屏幕内侧展开, 显示详细状态
  - 工作中黄灯呼吸闪烁
  - 可拖动; 单击把 TRAE 窗口置前; 右键菜单 (含退出)

零依赖: 仅使用系统自带 Python + tkinter + ctypes。
启动:  pythonw.exe trae_traffic_light.py
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import sys
import time
from ctypes import wintypes
from typing import Optional

import tkinter as tk
from tkinter import font as tkfont

from trae_monitor import TraeMonitor, TraeStatus

# ---------------------------------------------------------------------------
# Windows API: 把 TRAE 窗口置前
# ---------------------------------------------------------------------------
_user32 = ctypes.windll.user32


def bring_trae_to_front() -> bool:
    EnumProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
    hits: list = []

    def _cb(hwnd, _lp):
        if not _user32.IsWindowVisible(hwnd):
            return True
        n = _user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        _user32.GetWindowTextW(hwnd, buf, n + 1)
        if buf.value and ("trae" in buf.value.lower()):
            hits.append(hwnd)
        return True

    _user32.EnumWindows(EnumProc(_cb), 0)
    if not hits:
        return False
    hwnd = hits[0]
    _user32.ShowWindow(hwnd, 9)          # SW_RESTORE
    _user32.SetForegroundWindow(hwnd)
    return True


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
H = 214
W0 = 72
W1 = 400
POLL_MS = 1500
TICK_MS = 33
TRANSPARENT = "#010101"      # 透明色 (会变成点击穿透区域)

# 灯: (name, on_rgb hex, off_rgb hex, y_center)
# off 用低饱和暗色, 让未亮的灯明显淡; on 实色填满
LIGHTS = [
    ("red",    "#ff4d4d", "#3a1818", 44),
    ("yellow", "#ffc638", "#3a2e12", 106),
    ("green",  "#34d65c", "#16351f", 168),
]
R = 13
HOUSING_W = 44
HOUSING_GAP = 14

PANEL_BG = "#1b1b21"
PANEL_BORDER = "#34343c"
HOUSING_BG = "#1b1b21"
TEXT = "#e6e6ea"
LABEL = "#8c929a"
MONO = "#cfd2d8"
ACCENT = "#ffc638"

APP_DIR = os.path.dirname(os.path.abspath(__file__))
POS_FILE = os.path.join(APP_DIR, "pos.json")
_SINGLE_PORT = 48731


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class TrafficLightApp:
    def __init__(self) -> None:
        self.root = tk.Tk()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-transparentcolor", TRANSPARENT)
        self.root.config(bg=TRANSPARENT)

        self.canvas = tk.Canvas(self.root, width=W0, height=H,
                                bg=TRANSPARENT, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)

        self._status: TraeStatus = TraeStatus()
        self._mon = TraeMonitor()
        self._panel_w = 0.0
        self._panel_target = 0.0
        self._pulse = 1.0
        self._pulse_dir = -1
        self._curW = W0
        self._anchor_right = 0
        self._anchor_left = 0
        self._anchor_mode = "right"
        self._leave_after: Optional[str] = None
        self._drag_start = None
        self._win_start = (0, 0)
        self._moved = False

        # 字体
        self._f_title = tkfont.Font(family="Microsoft YaHei UI", size=11, weight="bold")
        self._f_sub = tkfont.Font(family="Microsoft YaHei UI", size=8)
        self._f_body = tkfont.Font(family="Microsoft YaHei UI", size=9)
        self._f_mono = tkfont.Font(family="Consolas", size=8)

        # 事件
        self.canvas.bind("<Enter>", self._on_enter)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<ButtonPress-3>", self._on_right)

        # 初始位置
        self._restore_pos()
        self.root.update_idletasks()

        # 启动循环
        self._refresh()
        self._tick()

    # ---------------------------------------------------------- 几何
    def _set_geometry(self, w: int, x: int, y: int) -> None:
        self._curW = w
        try:
            self.root.geometry(f"{w}x{H}+{x}+{y}")
        except tk.TclError:
            pass

    def _current_xy(self):
        return self.root.winfo_x(), self.root.winfo_y()

    def _apply_anchor(self) -> None:
        w = int(W0 + (W1 - W0) * self._panel_w)
        x, y = self._current_xy()
        if self._anchor_mode == "right":
            x = self._anchor_right - w
        else:
            x = self._anchor_left
        self._set_geometry(w, x, y)

    # ---------------------------------------------------------- 悬停
    def _on_enter(self, _e=None) -> None:
        if self._leave_after is not None:
            self.root.after_cancel(self._leave_after)
            self._leave_after = None
        if self._panel_target < 0.5:
            self._anchor_right = self.root.winfo_x() + self._curW
            self._anchor_left = self.root.winfo_x()
            screen = self.root.winfo_screenwidth()
            if self._anchor_right - W1 < 4:
                self._anchor_mode = "left"
            else:
                self._anchor_mode = "right"
        self._panel_target = 1.0

    def _on_leave(self, _e=None) -> None:
        self._leave_after = self.root.after(180, self._do_leave)

    def _do_leave(self) -> None:
        self._leave_after = None
        self._panel_target = 0.0

    # ---------------------------------------------------------- 拖动/点击
    def _on_press(self, e) -> None:
        self._drag_start = (e.x_root, e.y_root)
        self._win_start = self._current_xy()
        self._moved = False

    def _on_motion(self, e) -> None:
        if self._drag_start is None:
            return
        dx = e.x_root - self._drag_start[0]
        dy = e.y_root - self._drag_start[1]
        if abs(dx) + abs(dy) > 5:
            self._moved = True
        nx = self._win_start[0] + dx
        ny = self._win_start[1] + dy
        self._set_geometry(self._curW, nx, ny)
        self._anchor_right = nx + self._curW
        self._anchor_left = nx

    def _on_release(self, _e) -> None:
        moved = self._moved
        self._drag_start = None
        self._moved = False
        if not moved:
            bring_trae_to_front()
        else:
            self._save_pos()

    def _on_right(self, e) -> None:
        m = tk.Menu(self.root, tearoff=0, bg=PANEL_BG, fg=TEXT,
                    activebackground="#2e2e36", activeforeground=TEXT,
                    borderwidth=0, relief="flat")
        m.add_command(label="将 TRAE 置前", command=bring_trae_to_front)
        m.add_command(label="重置到屏幕右上角", command=self._reset_pos)
        m.add_separator()
        m.add_command(label="关于 TRAE 红绿灯", command=self._about)
        m.add_separator()
        m.add_command(label="退出", command=self._quit)
        try:
            m.tk_popup(e.x_root, e.y_root)
        finally:
            m.grab_release()

    # ---------------------------------------------------------- 刷新状态
    def _refresh(self) -> None:
        try:
            self._status = self._mon.refresh()
        except Exception:
            pass
        self.root.after(POLL_MS, self._refresh)

    # ---------------------------------------------------------- 动画
    def _tick(self) -> None:
        # 缓动展开
        diff = self._panel_target - self._panel_w
        if abs(diff) > 0.001:
            self._panel_w += diff * 0.22
            self._apply_anchor()
        else:
            if self._panel_w != self._panel_target:
                self._panel_w = self._panel_target
                self._apply_anchor()
        # 黄灯呼吸 (内部高亮内核缩放)
        if self._status.state == "yellow":
            self._pulse += self._pulse_dir * 0.06
            if self._pulse <= 0.20:
                self._pulse = 0.20
                self._pulse_dir = 1
            elif self._pulse >= 1.0:
                self._pulse = 1.0
                self._pulse_dir = -1
        else:
            self._pulse = 1.0
        self._draw()
        self.root.after(TICK_MS, self._tick)

    # ---------------------------------------------------------- 绘制
    def _draw(self) -> None:
        c = self.canvas
        c.delete("all")
        st = self._status
        W = self._curW
        active = st.state

        # 展开面板 (直角)
        if self._panel_w > 0.02:
            c.create_rectangle(6, 6, W - 6, H - 6,
                               fill=PANEL_BG, outline=PANEL_BORDER, width=1)
        # 分隔线 (展开时)
        if self._panel_w > 0.5:
            self._canvas_line(c, 16, 52, W - HOUSING_W - HOUSING_GAP - 14, 52, "#2a2a32")

        # 灯箱 (直角)
        hx1 = W - HOUSING_W - HOUSING_GAP
        hx2 = W - HOUSING_GAP
        c.create_rectangle(hx1, 12, hx2, H - 12,
                           fill=HOUSING_BG, outline=PANEL_BORDER, width=1)
        cx = (hx1 + hx2) / 2

        pulse = self._pulse
        for name, on, off, cy in LIGHTS:
            is_on = (name == active)
            if is_on:
                # 内部脉冲 (无向外扩散): 实心亮色底 + 同色高亮内核随 pulse 缩放.
                # 边缘始终填实, 中心更亮, 实现"边闪烁边亮灯".
                c.create_oval(cx - R, cy - R, cx + R, cy + R, fill=on, outline="")
                # 高亮内核 (白偏该色): pulse 1 -> 大(最亮); pulse 低 -> 小(偏暗)
                hi = max(1.0, R * (0.45 + 0.50 * pulse))
                # 中心高亮用更亮的同色调
                bright = {"red": "#ff9a9a", "yellow": "#ffe9a8",
                          "green": "#9af0b5"}.get(name, "#ffffff")
                c.create_oval(cx - hi, cy - hi, cx + hi, cy + hi,
                              fill=bright, outline="")
            else:
                # 未亮: 淡色填实
                c.create_oval(cx - R, cy - R, cx + R, cy + R, fill=off, outline="")

        # 详情文字 (展开足够时)
        if self._panel_w > 0.7:
            self._draw_details(c, st, W)

    def _draw_details(self, c, st: TraeStatus, W: int) -> None:
        x = 16
        right = W - HOUSING_W - HOUSING_GAP - 14
        dot = {"red": "#ff4d4d", "yellow": "#ffc638", "green": "#34d65c"}.get(st.state, "#909090")
        c.create_oval(x + 1, 14, x + 11, 24, fill=dot, outline="")
        c.create_text(x + 18, 15, anchor="nw",
                      text=f"{st.product or 'TRAE'}  ·  {st.state_label}",
                      fill=TEXT, font=self._f_title)
        c.create_text(x + 18, 33, anchor="nw", text=st.reason,
                      fill=LABEL, font=self._f_sub)

        y = 66
        lh = 19
        rows = [
            ("进程", f"{st.process_count} 个   {st.main_name} (PID {st.main_pid})"),
            ("内存", f"{st.total_mem_mb:.0f} MB"),
            ("系统", f"CPU {st.sys_cpu:.0f}%"),
            ("最近活动", st.last_activity_ago or "—"),
        ]
        if st.session_id:
            rows.append(("会话/任务", f"{st.session_id[:12]}  /  {st.task_id[:12]}"))
        for k, v in rows:
            c.create_text(x, y, anchor="nw", text=k, fill=LABEL, font=self._f_body)
            col = ACCENT if k == "最近活动" else TEXT
            c.create_text(x + 76, y, anchor="nw", text=v, fill=col, font=self._f_body)
            y += lh

        y += 4
        self._canvas_line(c, x, y, right, y, "#2a2a32")
        y += 8
        c.create_text(x, y, anchor="nw", text="最近事件", fill=LABEL, font=self._f_body)
        y += lh + 2
        if st.events:
            for e in st.events[-5:]:
                c.create_text(x, y, anchor="nw", text=e.ts, fill=LABEL, font=self._f_mono)
                c.create_text(x + 66, y, anchor="nw", text=e.command, fill=TEXT, font=self._f_mono)
                y += 15
        else:
            c.create_text(x, y, anchor="nw", text="暂无", fill=LABEL, font=self._f_mono)

    # ---------------------------------------------------------- 画图工具
    @staticmethod
    def _canvas_line(c, x1, y1, x2, y2, color):
        return c.create_line(x1, y1, x2, y2, fill=color, width=1)

    # ---------------------------------------------------------- 位置
    def _reset_pos(self) -> None:
        sw = self.root.winfo_screenwidth()
        x = sw - W0 - 14
        y = 18
        self._set_geometry(W0, x, y)
        self._save_pos()

    def _restore_pos(self) -> None:
        x = y = None
        try:
            with open(POS_FILE, "r", encoding="utf-8") as f:
                d = json.load(f)
            x, y = int(d["x"]), int(d["y"])
        except Exception:
            x = y = None
        if x is None:
            sw = self.root.winfo_screenwidth()
            x = sw - W0 - 14
            y = 18
        self._set_geometry(W0, x, y)

    def _save_pos(self) -> None:
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.root.winfo_x(), "y": self.root.winfo_y()}, f)
        except OSError:
            pass

    # ---------------------------------------------------------- 菜单动作
    def _about(self) -> None:
        top = tk.Toplevel(self.root)
        top.title("关于 TRAE 红绿灯")
        top.configure(bg=PANEL_BG)
        top.attributes("-topmost", True)
        msg = ("TRAE 工作状态红绿灯\n\n"
               "红 = TRAE 未运行\n"
               "绿 = 运行中, 空闲\n"
               "黄 = 工作中 (AI agent 执行中)\n\n"
               "悬停查看详情 · 拖动移动 · 单击置前 TRAE · 右键退出")
        lbl = tk.Label(top, text=msg, justify="left", bg=PANEL_BG, fg=TEXT,
                       font=self._f_body, padx=18, pady=16)
        lbl.pack()
        top.update_idletasks()
        top.geometry(f"+{self.root.winfo_x() - 60}+{self.root.winfo_y() + 30}")
        top.focus_force()

    def _quit(self) -> None:
        self.root.destroy()

    # ---------------------------------------------------------- 运行
    def run(self) -> None:
        self.root.mainloop()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
def main() -> int:
    # 单实例
    lock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        lock.bind(("127.0.0.1", _SINGLE_PORT))
    except OSError:
        return 0
    lock.listen(1)

    app = TrafficLightApp()
    try:
        app.run()
    finally:
        try:
            lock.close()
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
