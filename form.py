"""输入表单：注册、导入、导出、定时设置共用的一套窗口控件。

界面约定是「结果一律写日志、不弹提示框」，但输入总得有地方填，所以这里只做
一种窗口——一张卡片的表单：字段在卡片里排成几行，底部「取消 / 确定」。校验失败
时错误写在同一张卡片里，不另开对话框。Enter 提交、Esc 取消，窗口居中并模态。
"""
import tkinter as tk
from tkinter import ttk


def _theme():
    """取主窗口的配色与字体；导入顺序不对时退回一套安全默认值。"""
    try:
        from window import Theme
        return {"bg": Theme.BG, "panel": Theme.PANEL, "line": Theme.LINE,
                "text": Theme.TEXT, "dim": Theme.DIM, "muted": Theme.MUTED,
                "accent": Theme.ACCENT, "err": Theme.ERR}
    except Exception:
        return {"bg": "#f5f6f8", "panel": "#ffffff", "line": "#d8dbe0",
                "text": "#202124", "dim": "#8a8f98", "muted": "#5f6368",
                "accent": "#1a73e8", "err": "#c5221f"}


class Form(object):
    """一条表单：fields 里每项一个控件，返回 {key: 值}。"""

    @classmethod
    def open(cls, parent, title, fields, note="", submit="确定", validate=None,
             buttons=(), width=46, rows=()):
        """打开表单并等它关闭；取消返回 None。rows 给出多行按钮。"""
        dialog = _Dialog(parent, title, fields, note, submit, validate, buttons,
                         width, rows)
        return dialog.wait()

    @classmethod
    def text(cls, parent, title, label, value="", note=""):
        """多行文本输入（导入粘贴用）。"""
        result = cls.open(parent, title,
                          [{"key": "text", "label": label, "value": value, "height": 10}],
                          note=note, width=70)
        return (result or {}).get("text")

    @classmethod
    def ask(cls, parent, title, label, value="", note=""):
        """单行输入，返回字符串或 None。"""
        result = cls.open(parent, title,
                          [{"key": "value", "label": label, "value": value}], note=note)
        return (result or {}).get("value")

    @classmethod
    def choice(cls, parent, title, label, options, value="", note=""):
        """下拉选择，返回选中的字符串或 None。"""
        result = cls.open(parent, title,
                          [{"key": "value", "label": label, "choices": list(options),
                            "value": value}], note=note)
        return (result or {}).get("value")


class _Dialog(object):
    """表单窗口本体：建控件、收值、校验、等关闭。"""

    def __init__(self, parent, title, fields, note, submit, validate, buttons, width,
                 rows=()):
        """一次性把窗口与控件建好。"""
        cfg = _theme()
        self.cfg = cfg
        self.fields = [dict(item) for item in fields]
        self.entries = {}
        self.validate = validate
        self.result = None
        self.errors = []
        root = parent.winfo_toplevel() if parent is not None else None
        self.top = tk.Toplevel(parent)
        self.top.title(title)
        self.top.configure(bg=cfg["bg"])
        self.top.resizable(True, False)
        self.top.transient(root)
        holder = tk.Frame(self.top, bg=cfg["panel"], highlightthickness=1,
                          highlightbackground=cfg["line"])
        holder.pack(fill="both", expand=True, padx=12, pady=12)
        if not rows:
            tk.Label(holder, text=title, bg=cfg["panel"], fg=cfg["text"],
                     font=("Microsoft YaHei UI", 11, "bold"), anchor="w").grid(
                row=0, column=0, columnspan=2, sticky="w", padx=14, pady=(12, 6))
        if note and not rows:
            tk.Label(holder, text=note, bg=cfg["panel"], fg=cfg["dim"],
                     font=("Microsoft YaHei UI", 8), anchor="w", justify="left",
                     wraplength=520).grid(row=1, column=0, columnspan=2, sticky="w",
                                          padx=14, pady=(0, 6))
        row = 2 if (note and not rows) else 1
        for item in self.fields:
            row = self._field(holder, row, item, width)
        self.message = tk.Label(holder, text="", bg=cfg["panel"], fg=cfg["err"],
                                font=("Microsoft YaHei UI", 8), anchor="w", justify="left",
                                wraplength=520)
        self.message.grid(row=row, column=0, columnspan=2, sticky="w", padx=14)
        row += 1
        row_no = row
        bar = tk.Frame(holder, bg=cfg["panel"])
        for group in (([list(buttons)] if buttons else []) + [list(row) for row in rows]):
            line = tk.Frame(holder, bg=cfg["panel"])
            line.grid(row=row_no, column=0, columnspan=2, sticky="ew", padx=14,
                      pady=(2, 0))
            for text, callback in group:
                ttk.Button(line, text=text,
                           command=lambda fn=callback: self._extra(fn)).pack(
                    side="left", padx=(0, 6))
            row_no += 1
        if submit:
            bar.grid(row=row_no, column=0, columnspan=2, sticky="ew", padx=14,
                     pady=(6, 12))
            ttk.Button(bar, text="取消", command=self.cancel).pack(side="right")
            ttk.Button(bar, text=submit, style="Primary.TButton",
                       command=self.submit).pack(side="right", padx=(0, 8))
        holder.columnconfigure(1, weight=1)
        self.top.bind("<Return>", lambda _e: self.submit())
        self.top.bind("<Escape>", lambda _e: self.cancel())
        self.top.protocol("WM_DELETE_WINDOW", self.cancel)
        self.top.update_idletasks()
        self._center(root)
        self.top.grab_set()
        first = next(iter(self.entries.values()), None)
        if first is not None:
            first.focus_set()
        self.top.wait_window()

    def _field(self, holder, row, item, width):
        """一个字段：标签 + 输入控件（下拉、多行、密码各有分支）。"""
        cfg, key = self.cfg, item["key"]
        tk.Label(holder, text=item.get("label") or key, bg=cfg["panel"], fg=cfg["text"],
                 font=("Microsoft YaHei UI", 9), anchor="w").grid(
            row=row, column=0, sticky="w", padx=(14, 10), pady=3)
        box = tk.Frame(holder, bg=cfg["panel"])
        box.grid(row=row, column=1, sticky="ew", padx=(0, 14), pady=3)
        height = int(item.get("height") or 0)
        if item.get("choices"):
            var = tk.StringVar(value=str(item.get("value") or ""))
            widget = ttk.Combobox(box, textvariable=var, values=item["choices"],
                                  state="readonly", font=("Microsoft YaHei UI", 9),
                                  width=width)
            widget.pack(fill="x")
            self.entries[key] = ("choice", var)
        elif height:
            widget = tk.Text(box, height=height, width=width, font=("Consolas", 9),
                             wrap="none", bd=1, relief="solid", highlightthickness=0,
                             bg="#fbfbfd", fg=cfg["text"])
            widget.insert("1.0", str(item.get("value") or ""))
            widget.pack(fill="both", expand=True)
            self.entries[key] = ("text", widget)
        else:
            line = tk.Frame(box, bg=cfg["panel"])
            line.pack(fill="x")
            var = tk.StringVar(value=str(item.get("value") or ""))
            widget = ttk.Entry(line, textvariable=var, width=width,
                               font=("Microsoft YaHei UI", 9),
                               show="*" if item.get("secret") else "")
            widget.pack(side="left", fill="x", expand=True)
            for text, callback in item.get("buttons") or ():
                ttk.Button(line, text=text,
                           command=lambda fn=callback: self._extra(fn)).pack(
                    side="left", padx=(6, 0))
            self.entries[key] = ("entry", var)
        return row + 1

    def _center(self, root):
        """相对主窗口居中。"""
        try:
            self.top.update_idletasks()
            width, height = self.top.winfo_width(), self.top.winfo_height()
            if root is not None:
                x = root.winfo_rootx() + max(0, (root.winfo_width() - width) // 2)
                y = root.winfo_rooty() + max(0, (root.winfo_height() - height) // 3)
            else:
                x, y = 200, 160
            self.top.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    def values(self):
        """当前所有控件的值。"""
        out = {}
        for key, (kind, widget) in self.entries.items():
            if kind == "text":
                out[key] = widget.get("1.0", "end").rstrip("\n")
            else:
                out[key] = widget.get().strip()
        return out

    def set(self, key, value):
        """改某个控件的值（发送验证码这类附属动作用）。"""
        item = self.entries.get(key)
        if not item:
            return
        kind, widget = item
        if kind == "text":
            widget.delete("1.0", "end")
            widget.insert("1.0", str(value or ""))
        else:
            widget.set(str(value or ""))

    def note(self, text):
        """把一行提示写进卡片（不清空已有错误用）。"""
        self.message.configure(text=str(text or ""), fg=self.cfg["dim"]
                               if not text else self.cfg["err"])

    def _extra(self, callback):
        """附属按钮：回调拿到 self，可以读写字段。"""
        try:
            callback(self)
        except Exception as exc:
            self.note(str(exc))

    def submit(self):
        """校验并收值；不通过就把原因写在卡片里。"""
        values = self.values()
        if self.validate:
            try:
                problem = self.validate(values)
            except Exception as exc:
                problem = str(exc)
            if problem:
                self.note(problem)
                return
        self.result = values
        self.top.destroy()

    def cancel(self):
        """取消：结果留 None。"""
        self.result = None
        self.top.destroy()

    def close(self):
        """从外部关窗（后台动作成功后调用）；结果留 None，由调用方自己取数据。"""
        self.result = None
        try:
            self.top.destroy()
        except Exception:
            pass

    def wait(self):
        """等窗口关闭（构造时已经 wait 过，这里只是取值）。"""
        return self.result
