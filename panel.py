"""紧凑面板：账号、实例这类二级小窗共用的一套排布。

一个面板只有三块：顶上「键 · 值」排成一行，中间若干行「标签 + 控件」，底下
一组等宽动作按钮。不放内部标题，也不放取消 / 确定——原生标题栏的关闭键就是
关闭，Esc 同样，省下来的两行留给状态。行距按 4px 一格收，一屏能看全。
"""
import tkinter as tk
from tkinter import ttk

from util import FONT

GAP = 4  # 按钮之间的缝


def _theme():
    """取主窗口的配色；单独导入本模块时退回一套安全默认值。"""
    try:
        from window import Theme
        return {"bg": Theme.BG, "panel": Theme.PANEL, "line": Theme.LINE,
                "text": Theme.TEXT, "muted": Theme.MUTED, "dim": Theme.DIM,
                "ok": Theme.OK, "err": Theme.ERR}
    except Exception:
        return {"bg": "#f3f4f6", "panel": "#ffffff", "line": "#dde1e8",
                "text": "#1f2329", "muted": "#5f6673", "dim": "#8b919d",
                "ok": "#1a7f37", "err": "#cf222e"}


class Panel(object):
    """一块紧凑面板：按调用顺序往下排，最后 wait() 等着关闭。"""

    def __init__(self, parent, title, width=380):
        """建窗口与卡片；width 是最小宽度，内容更宽时按内容走。"""
        cfg = _theme()
        self.cfg = cfg
        self.fields = {}
        self.msg = None
        self.right = None
        self.closed = False
        self.row = 0
        self.root = None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.configure(bg=cfg["bg"])
        self.top.resizable(False, False)
        try:
            self.root = parent.winfo_toplevel() if parent is not None else None
            self.top.transient(self.root)
        except Exception:
            pass
        card = tk.Frame(self.top, bg=cfg["panel"], highlightthickness=1,
                        highlightbackground=cfg["line"])
        card.pack(fill="both", expand=True, padx=7, pady=7)
        card.columnconfigure(0, weight=1)
        self.box = tk.Frame(card, bg=cfg["panel"])
        self.box.grid(row=0, column=0, sticky="nsew", padx=9, pady=8)
        self.box.columnconfigure(1, weight=1)
        # 一张零高的底板：只用来把面板撑到 width 宽，窄内容也不会忽大忽小
        tk.Frame(card, bg=cfg["panel"], width=int(width), height=0).grid(
            row=1, column=0)
        self.top.bind("<Escape>", lambda _event: self.close())
        self.top.protocol("WM_DELETE_WINDOW", self.close)

    def head(self, pairs):
        """顶部状态行：dim 的键配正色的值，圆点分隔，一行放完几项数字。"""
        cfg = self.cfg
        line = tk.Frame(self.box, bg=cfg["panel"])
        line.grid(row=self.row, column=0, columnspan=3, sticky="ew")
        for index, (key, value) in enumerate(pairs):
            if index:
                tk.Label(line, text="·", bg=cfg["panel"], fg=cfg["dim"],
                         font=(FONT, 9)).pack(side="left", padx=6)
            tk.Label(line, text=key, bg=cfg["panel"], fg=cfg["dim"],
                     font=(FONT, 8)).pack(side="left")
            tk.Label(line, text=str(value), bg=cfg["panel"], fg=cfg["text"],
                     font=(FONT, 9)).pack(side="left", padx=(3, 0))
        self.row += 1
        return self

    def rule(self):
        """一条 1px 分隔线：状态、输入、动作三段之间各来一条。"""
        tk.Frame(self.box, bg=self.cfg["line"], height=1).grid(
            row=self.row, column=0, columnspan=3, sticky="ew", pady=(6, 5))
        self.row += 1
        return self

    def field(self, label, key, value="", choices=None, secret=False, width=18,
              buttons=(), right="", on_change=None):
        """一行「标签 + 控件」，右侧可跟附属按钮或一句现状文案。"""
        cfg = self.cfg
        tk.Label(self.box, text=label, bg=cfg["panel"], fg=cfg["muted"],
                 font=(FONT, 9), anchor="w").grid(
            row=self.row, column=0, sticky="w", padx=(0, 8), pady=1)
        var = tk.StringVar(value=str(value or ""))
        if choices is not None:
            widget = ttk.Combobox(self.box, textvariable=var, values=list(choices),
                                  state="readonly", font=(FONT, 9), width=width)
            widget.grid(row=self.row, column=1, sticky="ew", pady=1)
            if on_change is not None:
                widget.bind("<<ComboboxSelected>>",
                            lambda _event: on_change(widget.get()))
        else:
            widget = ttk.Entry(self.box, textvariable=var, font=(FONT, 9),
                               width=width, show="*" if secret else "")
            widget.grid(row=self.row, column=1, sticky="ew", pady=1)
        side = tk.Frame(self.box, bg=cfg["panel"])
        side.grid(row=self.row, column=2, sticky="w", padx=(8, 0), pady=1)
        for text, callback in buttons:
            ttk.Button(side, text=text, style="Mini.TButton",
                       command=lambda fn=callback: self._call(fn)).pack(
                side="left", padx=(0, GAP))
        if right:
            self.right = tk.Label(side, text=right, bg=cfg["panel"],
                                  fg=cfg["muted"], font=(FONT, 8), anchor="w")
            self.right.pack(side="left")
        self.fields[key] = (var, widget)
        self.row += 1
        return self

    def grid(self, buttons, cols=3, primary=False):
        """一组等宽按钮：按 cols 列铺满整行，行内只留 GAP 的缝。"""
        cfg = self.cfg
        area = tk.Frame(self.box, bg=cfg["panel"])
        area.grid(row=self.row, column=0, columnspan=3, sticky="ew", pady=(5, 0))
        for col in range(cols):
            area.columnconfigure(col, weight=1, uniform="act")
        for index, (text, callback) in enumerate(buttons):
            style = "MiniPrimary.TButton" if (primary and index == 0) else "Mini.TButton"
            ttk.Button(area, text=text, style=style,
                       command=lambda fn=callback: self._call(fn)).grid(
                row=index // cols, column=index % cols, sticky="ew",
                padx=(0 if index % cols == 0 else GAP, 0),
                pady=(0 if index < cols else GAP, 0))
        self.row += 1
        return self

    def inline(self, label, buttons):
        """一行「dim 标签 + 并排小按钮」，同一件事的几种来源收进一行。"""
        cfg = self.cfg
        line = tk.Frame(self.box, bg=cfg["panel"])
        line.grid(row=self.row, column=0, columnspan=3, sticky="ew", pady=(3, 0))
        tk.Label(line, text=label, bg=cfg["panel"], fg=cfg["dim"],
                 font=(FONT, 9)).pack(side="left", padx=(2, 8))
        for text, callback in buttons:
            ttk.Button(line, text=text, style="Mini.TButton", width=6,
                       command=lambda fn=callback: self._call(fn)).pack(
                side="left", padx=(0, GAP))
        self.row += 1
        return self

    def status(self, text="", tone=""):
        """底部一行提示；没内容也占着位置，免得面板忽高忽低。"""
        if self.msg is None:
            self.msg = tk.Label(self.box, text="", bg=self.cfg["panel"],
                                font=(FONT, 8), anchor="w")
            self.msg.grid(row=self.row, column=0, columnspan=3, sticky="w",
                          pady=(5, 0))
            self.row += 1
        colors = {"": self.cfg["dim"], "ok": self.cfg["ok"], "err": self.cfg["err"]}
        try:
            self.msg.configure(text=str(text or ""),
                               fg=colors.get(tone, self.cfg["dim"]))
        except Exception:
            pass

    def set_right(self, text):
        """改右侧那句现状（下拉换选时跟着变）。"""
        if self.right is not None:
            try:
                self.right.configure(text=str(text or ""))
            except Exception:
                pass

    def values(self):
        """当前控件的值：不管下拉还是输入框，都按字符串取。"""
        return {key: str(item[0].get()).strip() for key, item in self.fields.items()}

    def set(self, key, value):
        """改某个控件的值（发码这类附属动作回填用）。"""
        item = self.fields.get(key)
        if item:
            try:
                item[0].set(str(value or ""))
            except Exception:
                pass

    def _call(self, callback):
        """按钮回调：把面板交给它，异常压成一行提示，不弹框。"""
        try:
            callback(self)
        except Exception as exc:
            self.status(str(exc))

    def close(self):
        """关面板；要用的值由回调在关闭前取走。"""
        self.closed = True
        try:
            self.top.destroy()
        except Exception:
            pass

    def wait(self):
        """居中、模态地等它关掉，返回自己，方便外面再取 values()。"""
        if self.closed:
            return self
        try:
            self.top.update_idletasks()
            width, height = self.top.winfo_reqwidth(), self.top.winfo_reqheight()
            if self.root is not None:
                x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - width) // 2)
                y = self.root.winfo_rooty() + max(0, (self.root.winfo_height() - height) // 3)
            else:
                x, y = 240, 180
            self.top.geometry("+%d+%d" % (x, y))
            self.top.grab_set()
            first = next((item[1] for item in self.fields.values()), None)
            if first is not None:
                first.focus_set()
        except Exception:
            pass
        self.top.wait_window()
        return self
