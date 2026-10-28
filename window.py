"""主窗口：国内版 / 国际版两页，右侧控制区随页切换，日志两页共用。

国内版页只列国内版账号，只给成长体系（签到 / 活跃 / 任务 / 旅行 / 管家）与积分包
明细；国际版页只列国际版账号，只给 GUI / CLI 两个登录槽位的切换、会话同步与凭证
续期。没有弹窗：确认、进度、结果、错误全部实时写进日志。PAD / GAP / COLS / SIDE_W /
BTN_PAD 是控制区网格的尺寸约定：卡片左右边距、按钮间隙、列数、右栏宽度与按钮留白。
"""
import datetime
import os
import queue
import shutil
import threading
import time
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor
from tkinter import filedialog, ttk

from api import Activity, Growth
from client import Client, Switcher
from slot import Slot
from store import Store
from util import (APP_NAME, BACKUP, CLI_CMD, FONT, MONO, Process, as_dict, due_stamp,
                  fmt_num, monthly, norm_ts, realm_label)

PAD = 14
GAP = 5
COLS = 5
SIDE_W = 479
BTN_PAD = 3


class Theme:
    """配色与 ttk 样式：两页共用同一套控件外观。"""

    BG = "#f3f4f6"
    PANEL = "#ffffff"
    PANEL2 = "#eceef2"
    LINE = "#dde1e8"
    EDGE = "#b9c2ce"
    EDGE_DIM = "#dfe3ea"
    TEXT = "#1f2329"
    MUTED = "#5f6673"
    DIM = "#8b919d"
    ACCENT = "#2f6feb"
    ACCENT_D = "#2559c9"
    ACCENT_SOFT = "#eef4ff"
    OK = "#1a7f37"
    WARN = "#9a6700"
    ERR = "#cf222e"
    LOG_BG = "#fbfbfc"
    SELECT = "#dbe7ff"
    CUR_ROW = "#eef4ff"
    TIP_BG = "#2b3038"
    TIP_FG = "#f2f4f8"

    @classmethod
    def apply(cls, root):
        """给当前 root 装上 clam 主题，并配好按钮 / 分段 / 输入 / 表格样式。"""
        st = ttk.Style(root)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        st.configure(".", background=cls.BG, foreground=cls.TEXT,
                     fieldbackground=cls.PANEL, bordercolor=cls.LINE,
                     focuscolor=cls.ACCENT, font=(FONT, 9))

        st.configure("TButton", background=cls.PANEL, foreground=cls.TEXT,
                     bordercolor=cls.EDGE, lightcolor=cls.PANEL,
                     darkcolor=cls.PANEL, borderwidth=1, focusthickness=0,
                     relief="solid", padding=(12, 5), font=(FONT, 9),
                     width=0, anchor="center")
        st.map("TButton",
               background=[("disabled", "#f5f6f8"), ("pressed", "#e6ecf6"),
                           ("active", "#f2f6fc")],
               foreground=[("disabled", "#b0b7c2")],
               bordercolor=[("disabled", cls.EDGE_DIM), ("pressed", cls.ACCENT),
                            ("active", "#8fb0e6")])

        st.configure("Primary.TButton", background=cls.ACCENT, foreground="#ffffff",
                     bordercolor=cls.ACCENT_D, lightcolor=cls.ACCENT,
                     darkcolor=cls.ACCENT, borderwidth=1, focusthickness=0,
                     relief="solid", padding=(12, 5),
                     font=(FONT, 9), anchor="center")
        st.map("Primary.TButton",
               background=[("disabled", "#cdd9ef"), ("pressed", cls.ACCENT_D),
                           ("active", "#4a83f2")],
               foreground=[("disabled", "#ffffff")],
               bordercolor=[("disabled", "#b6c6e4"), ("active", cls.ACCENT_D)])

        for name, bg, fg in (("Seg.TButton", cls.PANEL, cls.MUTED),
                             ("SegOn.TButton", cls.ACCENT, "#ffffff")):
            st.configure(name, background=bg, foreground=fg,
                         bordercolor=cls.EDGE, lightcolor=bg, darkcolor=bg,
                         borderwidth=1, focusthickness=0, relief="solid",
                         padding=(11, 4), width=0, font=(FONT, 9))
        st.map("Seg.TButton",
               background=[("pressed", "#eef1f5"), ("active", "#f5f7fa")],
               foreground=[("disabled", cls.DIM)],
               bordercolor=[("active", "#8fb0e6")])
        st.map("SegOn.TButton",
               background=[("pressed", cls.ACCENT_D), ("active", "#4a83f2")],
               bordercolor=[("active", cls.ACCENT_D)])

        st.configure("TEntry", fieldbackground=cls.PANEL, foreground=cls.TEXT,
                     insertcolor=cls.TEXT, bordercolor=cls.LINE,
                     lightcolor=cls.PANEL, darkcolor=cls.PANEL, padding=4)
        st.configure("TCombobox", fieldbackground=cls.PANEL, foreground=cls.TEXT,
                     bordercolor=cls.LINE, lightcolor=cls.PANEL,
                     darkcolor=cls.PANEL, padding=(4, 5), arrowsize=14)
        st.map("TCombobox", fieldbackground=[("readonly", cls.PANEL)],
               foreground=[("readonly", cls.TEXT)])
        st.configure("Treeview", background=cls.PANEL, fieldbackground=cls.PANEL,
                     foreground=cls.TEXT, rowheight=26, borderwidth=0, font=(FONT, 9))
        st.configure("Treeview.Heading", background="#f7f8fa", foreground=cls.MUTED,
                     relief="flat", padding=(7, 4), font=(FONT, 9))
        st.map("Treeview.Heading", background=[("active", "#eef0f4")],
               foreground=[("active", cls.TEXT)])
        st.map("Treeview", background=[("selected", cls.SELECT)],
               foreground=[("selected", cls.TEXT)])


class Tip:
    """悬浮提示：鼠标停 400ms 后弹一个无边框小窗，越界自动翻到上方。"""

    def __init__(self, widget, text, above=False):
        """绑定悬停与离开事件；提示文字可以是字符串，也可以是取文字的函数。"""
        self.widget = widget
        self.text = text
        self.above = above
        self.window = None
        self.timer = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _event=None):
        """鼠标进入后延时弹出，先取消上一次待弹。"""
        self._cancel()
        try:
            self.timer = self.widget.after(400, self._show)
        except Exception:
            pass

    def _cancel(self):
        """取消还没触发的弹出。"""
        if self.timer is not None:
            try:
                self.widget.after_cancel(self.timer)
            except Exception:
                pass
            self.timer = None

    def _show(self):
        """算出位置并弹出提示窗；贴边时改到上方或向内收。"""
        self.timer = None
        try:
            text = self.text() if callable(self.text) else self.text
        except Exception:
            return
        text = str(text or "")
        if not text or self.window is not None:
            return
        try:
            x = self.widget.winfo_rootx() + 10
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 6
            tip = tk.Toplevel(self.widget)
            tip.wm_overrideredirect(True)
            tip.attributes("-topmost", True)
            label = tk.Label(tip, text=text, bg=Theme.TIP_BG, fg=Theme.TIP_FG, font=(FONT, 8),
                             padx=8, pady=4, justify="left", wraplength=380)
            label.pack()
            tip.update_idletasks()
            width, height = tip.winfo_reqwidth(), tip.winfo_reqheight()
            if self.above or y + height > self.widget.winfo_screenheight() - 8:
                y = self.widget.winfo_rooty() - height - 6
            if x + width > self.widget.winfo_screenwidth() - 8:
                x = self.widget.winfo_screenwidth() - width - 8
            if y < 8:
                y = 8
            tip.geometry("+%d+%d" % (x, y))
            self.window = tip
        except Exception:
            self.window = None

    def _hide(self, _event=None):
        """销毁提示窗并清掉计时器。"""
        self._cancel()
        if self.window is not None:
            try:
                self.window.destroy()
            except Exception:
                pass
            self.window = None


class Page(object):
    """一页的全部控件与列定义（两页结构相同，列与功能区不同）。"""

    def __init__(self, realm, columns, titles, widths, actions):
        """记下列定义与按钮动作，控件在构建时再挂上来。"""
        self.realm = realm
        self.columns = columns
        self.titles = titles
        self.widths = widths
        self.actions = actions
        self.tree = None
        self.count_lbl = None
        self.filter_var = None
        self.sort = ("default", False)
        self.widgets = {}


class Window(object):
    """主窗口：两页账号表 + 右栏控制区 + 共用日志。"""

    def __init__(self, root):
        """准备数据容器与两页控件，随后载入账号并启动日志泵。"""
        self.root = root
        self.store = Store()
        self.entries = []
        self.by_uid = {}
        self.errors = []
        self.busy = False
        self.logq = queue.Queue()
        self.resq = queue.Queue()
        self.current = {"uid": "", "realm": ""}
        self.cli_cur = {"uid": "", "nickname": ""}
        self.slot_state = {}
        self.activity = {}
        self.tips = []
        self.slots = Slot()
        self.slot_sel = ""
        self.slot_metas = []
        self.last_folder = ""
        self.pages = {}
        self.cur_lbl = None
        self.cli_lbl = None
        self.btn_switch = None
        self.btn_switch_cli = None
        self.page = "cn"
        self._build()
        self.reload()
        self.root.after(120, self._pump)

    def _build(self):
        """装好菜单、主体与日志区。"""
        root = self.root
        root.title(APP_NAME)
        root.geometry("1240x780")
        root.minsize(1100, 660)
        root.configure(bg=Theme.BG)
        Theme.apply(root)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(0, weight=1)

        self._build_menu()
        self._build_body(root)
        self._build_log(root)

    def tip(self, widget, text, above=False):
        """给控件挂一条悬浮提示，返回控件本身便于链式调用。"""
        self.tips.append(Tip(widget, text, above))
        return widget

    def _build_body(self, parent):
        """建两页的列表与右栏，默认显示国内版。"""
        body = tk.Frame(parent, bg=Theme.BG)
        body.grid(row=0, column=0, sticky="nsew", padx=12, pady=(10, 0))
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        defs = (
            Page("cn",
                 columns=("name", "checkin", "streak", "energy", "credit", "expires"),
                 titles={"name": "账号", "checkin": "签到", "streak": "连登",
                         "energy": "能量", "credit": "积分", "expires": "最早到期"},
                 widths={"name": 150, "checkin": 54, "streak": 50,
                         "energy": 50, "credit": 62, "expires": 196},
                 actions=(("收录", "import_current"), ("恢复", "restore_backup"),
                          ("刷新", "reload"), ("续期", "refresh_credential"),
                          ("获取", "query_activity"), ("签到", "do_checkin"),
                          ("活跃", "do_report"), ("任务", "do_tasks"),
                          ("旅行", "do_travel"), ("管家", "do_streak"))),
            Page("intl",
                 columns=("name", "credit", "packages", "expires", "slots"),
                 titles={"name": "账号", "credit": "积分", "packages": "包数",
                         "expires": "到期", "slots": "登录槽位"},
                 widths={"name": 152, "credit": 68, "packages": 56,
                         "expires": 196, "slots": 96},
                 actions=(("收录", "import_current"), ("恢复", "restore_backup"),
                          ("刷新", "reload"), ("续期", "refresh_credential"),
                          ("获取", "query_activity"), ("同步", "sync_current"),
                          ("新建", "slot_new"), ("换号", "slot_switch"),
                          ("启动", "slot_launch"), ("停止", "slot_stop"),
                          ("删除", "slot_remove"))),
        )
        for spec in defs:
            self.pages[spec.realm] = spec
            spec.widgets["list"] = self._build_list(body, spec)
            spec.widgets["list"].grid(row=0, column=0, sticky="nsew")
        self.switch_page(self.page)

    def _build_list(self, parent, page):
        """一页的左栏：搜索 + 计数 + 切页 + 账号表；右栏给它自己的功能区。"""
        holder = tk.Frame(parent, bg=Theme.BG)
        holder.columnconfigure(0, weight=1)
        holder.columnconfigure(1, weight=0, minsize=SIDE_W)
        holder.rowconfigure(1, weight=1)

        head = tk.Frame(holder, bg=Theme.BG)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        page.filter_var = tk.StringVar()
        page.filter_var.trace_add("write", lambda *_a: self.refresh_tree())
        search = ttk.Entry(head, textvariable=page.filter_var)
        search.pack(side="left", fill="x", expand=True)
        search.bind("<Escape>", lambda _e: page.filter_var.set(""))
        self.tip(search, "按昵称 / UID 过滤本页账号；Esc 清空")
        page.count_lbl = tk.Label(head, text="", bg=Theme.BG, fg=Theme.DIM,
                                  font=(FONT, 8), anchor="w")
        page.count_lbl.pack(side="left", padx=(8, 10))
        self._page_switch(head, page)

        card = tk.Frame(holder, bg=Theme.PANEL, highlightthickness=1,
                        highlightbackground=Theme.LINE)
        card.grid(row=1, column=0, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.rowconfigure(0, weight=1)
        tree = ttk.Treeview(card, columns=page.columns, show="headings", selectmode="extended")
        for col in page.columns:
            tree.heading(col, text=page.titles[col],
                         anchor="w" if col == "name" else "center",
                         command=lambda c=col, p=page: self.sort_by(p, c))
            tree.column(col, width=page.widths[col], minwidth=48,
                        anchor="w" if col == "name" else "center",
                        stretch=True)
        tree.grid(row=0, column=0, sticky="nsew", padx=1, pady=1)
        tree.tag_configure("ok", foreground=Theme.TEXT)
        tree.tag_configure("dead", foreground=Theme.DIM)
        tree.tag_configure("current", background=Theme.CUR_ROW, foreground=Theme.TEXT,
                           font=(FONT, 9, "bold"))
        tree.bind("<<TreeviewSelect>>", lambda _e: (self.refresh_packs(),
                                                    self._sync_buttons()))
        tree.bind("<Button-3>", self._on_menu)
        tree.bind("<Control-a>", self._on_select_all)
        tree.bind("<Button-1>", self._on_click, add="+")
        page.tree = tree

        side = self._build_side(holder, page)
        side.grid(row=0, column=1, rowspan=2, sticky="nsew", padx=(8, 0))
        return holder

    def _build_side(self, parent, page):
        """一页的右栏：控制卡（按钮网格，国际版再加实例下拉与槽位行）+ 积分包明细。"""
        side = tk.Frame(parent, bg=Theme.BG, width=SIDE_W)
        side.grid_propagate(False)
        side.columnconfigure(0, weight=1)

        card = tk.Frame(side, bg=Theme.PANEL, highlightthickness=1,
                        highlightbackground=Theme.LINE)
        card.grid(row=0, column=0, sticky="ew")
        card.columnconfigure(0, weight=1)

        area = tk.Frame(card, bg=Theme.PANEL)
        area.grid(row=0, column=0, sticky="ew", padx=PAD - BTN_PAD, pady=(8, 8))
        for col in range(COLS):
            area.columnconfigure(col, weight=1, uniform="act")

        row = self._button_grid(area, 0, page)
        if page.realm == "intl":
            self.cur_lbl, self.btn_switch = self._current_line(
                area, row, "GUI", (FONT, 10, "bold"), Theme.TEXT,
                self.switch_desktop, lambda: self._tip("switch_desktop"))
            self.cli_lbl, self.btn_switch_cli = self._current_line(
                area, row + 1, "CLI", (FONT, 9), Theme.MUTED,
                self.switch_cli, lambda: self._tip("switch_cli"))

        packs = tk.Frame(side, bg=Theme.PANEL, highlightthickness=1,
                         highlightbackground=Theme.LINE)
        packs.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        packs.columnconfigure(0, weight=1)
        packs.rowconfigure(1, weight=1)
        side.rowconfigure(1, weight=1)
        phead = tk.Frame(packs, bg=Theme.PANEL)
        phead.grid(row=0, column=0, sticky="ew", padx=PAD, pady=(8, 2))
        tk.Label(phead, text="积分包", bg=Theme.PANEL, fg=Theme.DIM,
                 font=(FONT, 9), anchor="w").pack(side="left")
        page.widgets["pack_summary"] = tk.Label(phead, text="", bg=Theme.PANEL,
                                                fg=Theme.DIM, font=(FONT, 8), anchor="e")
        page.widgets["pack_summary"].pack(side="right")
        pack_cols = ("pname", "remain", "size", "days")
        pack_tree = ttk.Treeview(packs, columns=pack_cols, show="headings", height=6)
        for col, text, width in (("pname", "包", 136), ("remain", "剩余", 54),
                                 ("size", "总量", 54), ("days", "到期", 196)):
            pack_tree.heading(col, text=text, anchor="w" if col == "pname" else "center")
            pack_tree.column(col, width=width, minwidth=50,
                             anchor="w" if col == "pname" else "center",
                             stretch=(col == "pname"))
        pack_tree.grid(row=1, column=0, sticky="nsew", padx=1, pady=(0, 1))
        page.widgets["pack_hint"] = tk.Label(
            packs, text="选中账号查看积分包；不选则操作整页账号",
            bg=Theme.PANEL, fg=Theme.DIM, font=(FONT, 9))
        pack_tree.tag_configure("using", background=Theme.CUR_ROW)
        pack_tree.tag_configure("gone", foreground=Theme.DIM)
        page.widgets["pack_tree"] = pack_tree
        return side

    def _button_grid(self, area, top, page):
        """控制区的按钮网格：统一 COLS 列逐行排满，国际版的实例下拉接在末尾。"""
        page.widgets["buttons"] = {}
        for index, (text, action) in enumerate(page.actions):
            btn = ttk.Button(area, text=text, command=getattr(self, action))
            btn.grid(row=top + index // COLS, column=index % COLS, sticky="ew",
                     padx=BTN_PAD, pady=(0 if index < COLS else GAP, 0))
            self.tip(btn, lambda a=action, p=page: self._tip(a, p))
            page.widgets["buttons"][action] = btn
        row, col = top + len(page.actions) // COLS, len(page.actions) % COLS
        if page.realm != "intl":
            return row + (1 if col else 0)
        if col == 0:
            row, col = row + 1, 0
        self.slot_var = tk.StringVar()
        self.slot_box = ttk.Combobox(area, textvariable=self.slot_var,
                                     state="readonly", font=(FONT, 9))
        self.slot_box.grid(row=row, column=col, columnspan=COLS - col,
                           sticky="ew", padx=BTN_PAD, pady=(GAP, 0))
        self.slot_box.bind("<<ComboboxSelected>>", self._on_slot_selected)
        self.tip(self.slot_box, "命名 CLI 实例（可多开，各自账号与文件夹）")
        page.widgets["slot_box"] = self.slot_box
        return row + 1

    def _page_switch(self, parent, page):
        """切页按钮：顶在搜索框与账号数量右边，高宽对齐输入框那一行。"""
        holder = tk.Frame(parent, bg=Theme.BG)
        holder.pack(side="right")
        page.widgets["seg"] = {}
        for key, text in (("cn", "国内版"), ("intl", "国际版")):
            btn = ttk.Button(holder, text=text, style="Seg.TButton",
                             command=lambda k=key: self.switch_page(k))
            btn.pack(side="left")
            self.tip(btn, "切换到%s账号页" % text)
            page.widgets["seg"][key] = btn

    def _on_click(self, event):
        """点表格空白处取消选中：不选任何行时所有操作按「全部账号」执行。"""
        tree = self.page_spec.tree
        if tree.selection() and tree.identify_region(event.x, event.y) == "nothing":
            tree.selection_remove(*tree.selection())

    def _current_line(self, parent, row, tag, font, fg, command, hint):
        """控制区里的一行当前登录：槽位名、账号摘要、行尾「切换」（与按钮网格对齐）。"""
        tk.Label(parent, text=tag, bg=Theme.PANEL, fg=Theme.DIM,
                 font=(FONT, 9), anchor="w").grid(row=row, column=0, sticky="w",
                                                  padx=BTN_PAD, pady=(GAP, 0))
        lbl = tk.Label(parent, text="读取中…", bg=Theme.PANEL, fg=fg, font=font,
                       anchor="w")
        lbl.grid(row=row, column=1, columnspan=COLS - 2, sticky="w",
                 padx=BTN_PAD, pady=(GAP, 0))
        btn = ttk.Button(parent, text="切换", style="Primary.TButton",
                         command=command)
        btn.grid(row=row, column=COLS - 1, sticky="ew", padx=BTN_PAD, pady=(GAP, 0))
        self.tip(btn, hint)
        return lbl, btn

    def _build_log(self, parent):
        """底部共用日志：带时间戳、按级别着色，超过 300 行自动裁掉前半。"""
        card = tk.Frame(parent, bg=Theme.PANEL, highlightthickness=1,
                        highlightbackground=Theme.LINE)
        card.grid(row=1, column=0, sticky="ew", padx=12, pady=(8, 12))
        card.columnconfigure(0, weight=1)
        card.rowconfigure(1, weight=1)
        head = tk.Frame(card, bg=Theme.PANEL)
        head.grid(row=0, column=0, sticky="ew", padx=12, pady=(6, 2))
        tk.Label(head, text="运行日志", bg=Theme.PANEL, fg=Theme.DIM, font=(FONT, 8),
                 anchor="w").pack(side="left")
        self.busy_lbl = tk.Label(head, text="", bg=Theme.PANEL, fg=Theme.ACCENT,
                                 font=(FONT, 8), anchor="e")
        self.busy_lbl.pack(side="right")
        frame = tk.Frame(card, bg=Theme.PANEL)
        frame.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 8))
        frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(frame, height=4, bg=Theme.LOG_BG, fg=Theme.MUTED, bd=0,
                                relief="flat", font=(MONO, 8), padx=10, pady=6,
                                wrap="word", state="disabled", highlightthickness=0)
        self.log_text.grid(row=0, column=0, sticky="ew")
        self.log_text.tag_configure("dim", foreground=Theme.DIM)
        self.log_text.tag_configure("ok", foreground=Theme.OK)
        self.log_text.tag_configure("warn", foreground=Theme.WARN)
        self.log_text.tag_configure("err", foreground=Theme.ERR)

    def switch_page(self, key):
        """切到国内版 / 国际版页：只显示该页的控件，另一页整块隐藏。"""
        if key not in self.pages:
            return
        self.page = key
        for name, spec in self.pages.items():
            holder = spec.widgets["list"]
            if name == key:
                holder.grid()
            else:
                holder.grid_remove()
            for seg, btn in (spec.widgets.get("seg") or {}).items():
                btn.configure(style="SegOn.TButton" if seg == key else "Seg.TButton")
        self.refresh_tree()
        self.refresh_packs()
        self._sync_buttons()

    @property
    def page_spec(self):
        """当前页的定义对象。"""
        return self.pages[self.page]

    def _build_menu(self):
        """右键菜单：切换、同步、实例、成长活动、账号维护各一组。"""
        self.menu = tk.Menu(self.root, tearoff=0, bg=Theme.PANEL, fg=Theme.TEXT,
                            activebackground=Theme.ACCENT, activeforeground="#ffffff",
                            bd=0, font=(FONT, 9))
        self.menu.add_command(label="切换 GUI", command=self.switch_desktop)
        self.menu.add_command(label="切换 CLI", command=self.switch_cli)
        self.menu.add_command(label="同步会话", command=self.sync_current)
        self.menu.add_command(label="续期凭证", command=self.refresh_credential)
        self.menu.add_separator()
        self.menu.add_command(label="新建实例（用此账号）", command=self.slot_new)
        self.menu.add_command(label="换号到选中实例", command=self.slot_switch)
        self.menu.add_command(label="启动实例", command=self.slot_launch)
        self.menu.add_command(label="停止实例", command=self.slot_stop)
        self.menu.add_command(label="删除实例", command=self.slot_remove)
        self.menu.add_separator()
        self.menu.add_command(label="Buddy 加油站签到", command=self.do_checkin)
        self.menu.add_command(label="每日活跃", command=self.do_report)
        self.menu.add_command(label="一键做任务领积分", command=self.do_tasks)
        self.menu.add_command(label="猫猫旅行", command=self.do_travel)
        self.menu.add_command(label="连登管家与抽奖", command=self.do_streak)
        self.menu.add_command(label="获取积分", command=self.query_activity)
        self.menu.add_separator()
        self.menu.add_command(label="全选账号", command=self.select_all)
        self.menu.add_command(label="收录当前登录", command=self.import_current)
        self.menu.add_command(label="恢复备份", command=self.restore_backup)
        self.menu.add_command(label="刷新账号列表", command=lambda: self.reload())
        self.menu.add_command(label="复制 UID", command=self.copy_uid)

    def _tip(self, action, page=None):
        """按钮悬浮提示：按动作名给一句话，范围与计数都跟当前页走。"""
        page = page or self.page_spec
        entry = self.selected()
        if action == "switch_desktop":
            if not self._slot_ready(self.slot_state.get("gui")):
                return "GUI 槽位不可用：未检测到客户端安装或登录态文件"
            return ("把 GUI 登录态换成 %s，会重启客户端" % entry.nickname
                    if entry else "先选择账号")
        if action == "switch_cli":
            if not self._slot_ready(self.slot_state.get("cli")):
                return "CLI 槽位不可用：未检测到 codebuddy 命令或登录态文件"
            if not entry:
                return "先选择账号"
            if entry.realm != "intl":
                return "默认 CLI 槽位只支持国际版账号；国内版请用「实例」新建"
            return "把默认 CLI 槽位的登录态换成 %s，下次启动 CLI 生效" % entry.nickname
        if action == "import_current":
            return "把当前 GUI 登录态收录进账号库（已有账号只更新，不新建）"
        if action == "restore_backup":
            return ("用 data/Backup 快照恢复登录态与本地库，然后重启客户端"
                    if self._has_backup() else "还没有备份")
        if action == "reload":
            return "重新扫描 data/Account，刷新账号列表"
        if action == "sync_current":
            return ("把本地会话合并到当前登录账号，会重启客户端"
                    if self.current["uid"] else "当前未登录，无法同步会话")
        if action == "refresh_credential":
            count = len(self._targets(page))
            return ("续期 %d 个账号（换新 accessToken / refreshToken）" % count
                    if count else "没有可续期的凭证")
        if action == "query_activity":
            count = len(self._targets(page))
            return ("获取 %d 个账号的积分与积分包，并写回账号文件" % count if count
                    else "没有可取积分的凭证")
        if action in ("slot_new", "slot_switch", "slot_launch", "slot_stop", "slot_remove"):
            return self._tip_slot(action.split("_", 1)[1])
        labels = {"do_checkin": "Buddy 加油站签到", "do_report": "上报每日活跃点亮连登",
                  "do_tasks": "一键接取、点亮并领取成长任务", "do_travel": "猫猫旅行：领奖 / 派出",
                  "do_streak": "补签 / 礼包 / 档位兑换 / 抽奖"}
        base = labels.get(action, action)
        chosen = bool(self.selected_all())
        count = len(self._cn_targets())
        if not count:
            return "没有国内版个人号"
        return "对%s %d 个账号%s" % ("选中" if chosen else "本页全部", count, base)

    def _tip_slot(self, what):
        """实例类动作的提示：按动作与实例运行状态给一句话。"""
        entry, meta = self.selected(), self.slot_meta()
        if what in ("new", "switch") and not entry:
            return "先选择账号"
        if not meta:
            return "先在实例下拉里选中实例"
        running, name = bool(meta.get("pids")), meta["name"]
        folder, nick = meta.get("folder") or "", entry.nickname if entry else ""
        if what == "new":
            return "用 %s 新建独立 CLI 实例：自选工作文件夹，建好后立即启动" % nick
        if what == "switch":
            return "把实例 %s 的登录态换成 %s（运行中会自动重启）" % (name, nick)
        if what == "launch":
            return ("实例 %s 已在运行" % name if running
                    else "在新控制台启动实例 %s（工作文件夹 %s）" % (name, folder))
        if what == "stop":
            return ("停掉实例 %s 自己的 CLI 进程树，不影响其他实例" % name if running
                    else "实例 %s 没有在运行" % name)
        return "把实例 %s 的目录移到 data/CLI/.trash，可手动找回" % name

    @staticmethod
    def _has_backup():
        """data/Backup 里有没有可用的备份。"""
        return os.path.isfile(os.path.join(BACKUP, "manifest.json"))

    def realm_entries(self, realm=None):
        """本页的账号：按区域过滤，列表 / 计数 / 批量都只认这一份。"""
        realm = realm or self.page
        return [e for e in self.entries if e.realm == realm]

    def reload(self):
        """重扫账号目录并刷新两个槽位显示，读取告警写进日志。"""
        self.entries, self.errors = self.store.load()
        self.by_uid = {e.uid: e for e in self.entries}
        self.refresh_current()
        for err in self.errors:
            self.log("注意：" + err, "warn")

    def refresh_current(self):
        """刷新两个登录槽位显示（只属于国际版页）；程序没装时那一行留空。"""
        self._probe_slots()
        self.current = Client.current()
        self.cli_cur = Client.cli_current()
        uid = self.current["uid"]
        if self.cur_lbl is not None and self.cli_lbl is not None:
            gui = self.slot_state["gui"]
            if not gui["exe"]:
                self.cur_lbl.configure(text="")
            elif uid:
                entry = self.by_uid.get(uid)
                self.cur_lbl.configure(text="%s · %s" % (
                    entry.nickname if entry else uid[:8],
                    realm_label(self.current["realm"])))
            elif not gui["file"]:
                self.cur_lbl.configure(text="无登录态文件")
            else:
                self.cur_lbl.configure(text="未登录")
            cli = self.cli_cur
            entry = self.by_uid.get(cli["uid"])
            if not self.slot_state["cli"]["exe"]:
                self.cli_lbl.configure(text="")
            elif entry:
                self.cli_lbl.configure(text="%s · %s" % (entry.nickname,
                                                         realm_label(entry.realm)))
            elif cli["uid"]:
                self.cli_lbl.configure(text=cli["nickname"] or cli["uid"][:8])
            elif not self.slot_state["cli"]["file"]:
                self.cli_lbl.configure(text="无登录态文件")
            else:
                self.cli_lbl.configure(text="未登录")
        self.refresh_slots()
        self.refresh_tree()

    def refresh_tree(self):
        """重画两页：各自表格、计数与选中集都保持独立，切页时不用再补数据。"""
        for key in ("cn", "intl"):
            self._refresh_page(self.pages[key])
        self.refresh_packs()
        self._sync_buttons()

    def _refresh_page(self, page):
        """重画一页的表格：过滤、排序、标记当前登录行，并保住原有选中。"""
        tree = page.tree
        if tree is None:
            return
        keep = set(tree.selection())
        tree.delete(*tree.get_children())
        realm_rows = self.realm_entries(page.realm)
        rows = sorted(realm_rows, key=lambda e: self._sort_key(e, page), reverse=page.sort[1])
        text = page.filter_var.get().strip().lower() if page.filter_var else ""
        shown = 0
        for entry in rows:
            if text and text not in entry.nickname.lower() and text not in entry.uid.lower():
                continue
            shown += 1
            act = self.activity.get(entry.uid) or {}
            tags = ["dead" if entry.dead else "ok"]
            mark = ""
            if entry.uid == self.current["uid"]:
                tags.append("current")
                mark = "● "
            tree.insert("", "end", iid=entry.uid, tags=tuple(tags),
                        values=self._row_values(entry, act, page, mark))
        page.count_lbl.configure(text="%d / %d" % (shown, len(realm_rows)))
        for uid in keep:
            if tree.exists(uid):
                tree.selection_add(uid)

    @classmethod
    def _expire_text(cls, row):
        """到期文案：时间精确到秒，按月刷新的额度直接写月底时间，其余沿用刷新时的天数。"""
        if row.get("is_expired"):
            return "已过期"
        if monthly(row):
            stamp = due_stamp(row)
            if not stamp:
                return "—"
            days = (stamp - time.time()) / 86400.0
            return "%s (%.1f 天)" % (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp)), days)
        when = str(row.get("expire_time") or row.get("cycle_end_time") or "")[:19]
        if not when:
            return "—"
        try:
            days = float(row.get("days_left"))
        except (TypeError, ValueError):
            return when
        return "%s (%.1f 天)" % (when, days)

    def _row_values(self, entry, act, page, mark=""):
        """一行的单元格：按这一页的列定义取值，另一页的列完全不参与。"""
        credits = entry.credits or {}
        first = as_dict(credits.get("earliest_expiring"))
        if first:
            expires = self._expire_text(first)
        else:
            stamp = norm_ts(entry.expires_at)
            try:
                expires = (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
                           if stamp > 0 else "—")
            except (OSError, ValueError, OverflowError):
                expires = "—"
        cells = {"name": mark + entry.nickname,
                 "checkin": self._checkin_text(entry, act),
                 "streak": "—" if act.get("days") is None else "%d 天" % self._num(act["days"], 0),
                 "energy": "—" if act.get("energy") is None else str(act["energy"]),
                 "credit": self._credit_text(entry, act),
                 "packages": str(len(credits.get("packages") or [])) if credits else "—",
                 "expires": expires,
                 "slots": self._slot_text(entry)}
        return tuple(cells.get(col, "") for col in page.columns)

    def refresh_packs(self):
        """把选中账号的积分包铺到「积分包」卡：只有选中才显示，多选只取第一个。"""
        page = self.page_spec
        tree = page.widgets.get("pack_tree")
        if tree is None:
            return
        entry = self.selected()
        tree.delete(*tree.get_children())
        hint = page.widgets["pack_hint"]
        if not entry:
            page.widgets["pack_summary"].configure(text="")
            tree.grid_remove()
            hint.grid(row=1, column=0, sticky="nsew")
            return
        hint.grid_remove()
        tree.grid()
        credits = entry.credits or {}
        rows = [row for row in (credits.get("packages") or []) if isinstance(row, dict)]
        if not rows:
            page.widgets["pack_summary"].configure(text="还没有包明细，点「刷新」拉取")
            return
        alive = sum(1 for r in rows if not r.get("is_expired"))
        page.widgets["pack_summary"].configure(
            text="%d 个包" % len(rows) if alive == len(rows)
            else "%d 个包 · %d 个有效" % (len(rows), alive))
        rows = sorted(rows, key=lambda r: (0 if r.get("in_usage") else 1,
                                           1 if r.get("is_expired") else 0,
                                           due_stamp(r) or (1 << 62)))
        for row in rows:
            when = self._expire_text(row)
            tags = ("gone",) if row.get("is_expired") else (
                ("using",) if row.get("in_usage") else ())
            tree.insert("", "end", tags=tags, values=(
                str(row.get("name") or ""), fmt_num(row.get("remain"), 1),
                fmt_num(row.get("size"), 1), when))

    def _slot_text(self, entry):
        """国际版页：这个账号当前挂在哪个槽位（GUI / CLI / 都没有）。"""
        marks = []
        if entry.uid == self.current["uid"]:
            marks.append("GUI")
        if entry.uid == self.cli_cur["uid"]:
            marks.append("CLI")
        return " · ".join(marks) if marks else "—"

    def _checkin_text(self, entry, act):
        """签到列：优先用刚拉到的活动结果，其次看账号文件里的签到日期。"""
        if entry.realm != "cn":
            return "—"
        if act.get("checkin"):
            return act["checkin"]
        if act.get("checked") is not None:
            return "已签到" if act["checked"] else "未签"
        today = time.strftime("%Y-%m-%d")
        return "已签到" if str(entry.last_checkin)[:10] == today else "未签"

    def _credit_text(self, entry, act):
        """积分列：优先用刚拉到的额度，其次用账号文件里存的上次结果。"""
        if act.get("remain") is not None:
            return fmt_num(act["remain"], 0)
        credits = entry.credits or {}
        if credits.get("remain") is not None:
            return fmt_num(credits["remain"], 0)
        return "—"

    @staticmethod
    def _num(value, default=-1.0):
        """排序用数值：脏数据（字符串 / None）一律当 default。"""
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _sort_key(self, entry, page):
        """按当前排序列算排序键；未知列按昵称排。"""
        col = page.sort[0]
        act = self.activity.get(entry.uid) or {}
        credits = entry.credits or {}
        if col == "expires":
            first = as_dict(credits.get("earliest_expiring"))
            stamp = due_stamp(first) if first else norm_ts(entry.expires_at)
            return (stamp or (1 << 62),)
        if col == "checkin":
            return (self._checkin_text(entry, act), entry.nickname.lower())
        if col == "streak":
            return (self._num(act.get("days")),)
        if col == "energy":
            return (self._num(act.get("energy")),)
        if col == "credit":
            return (self._num(act.get("remain") if act.get("remain") is not None
                              else credits.get("remain")),)
        if col == "packages":
            return (len(credits.get("packages") or []),)
        if col == "slots":
            return (self._slot_text(entry),)
        return (entry.nickname.lower(), entry.uid)

    def sort_by(self, page, col):
        """点表头排序：同列再点切换升降序，并更新表头箭头。"""
        page.sort = (col, not page.sort[1]) if page.sort[0] == col else (col, False)
        arrow = " ▼" if page.sort[1] else " ▲"
        for key, text in page.titles.items():
            page.tree.heading(key, text=text + (arrow if key == col else ""))
        self.refresh_tree()

    def selected(self):
        """当前选中行的账号：优先取焦点行，其次取选中集的第一条。"""
        tree = self.page_spec.tree
        focus = tree.focus()
        if focus and focus in tree.selection():
            return self.by_uid.get(focus)
        selection = tree.selection()
        return self.by_uid.get(selection[0]) if selection else None

    def selected_all(self):
        """当前选中的全部账号。"""
        tree = self.page_spec.tree
        return [self.by_uid[uid] for uid in tree.selection() if uid in self.by_uid]

    def select_all(self):
        """选中当前页所有行。"""
        kids = self.page_spec.tree.get_children()
        if kids:
            self.page_spec.tree.selection_set(kids)
            self.page_spec.tree.focus(kids[0])

    def _probe_slots(self):
        """两个登录槽位的可用性：登录态文件在不在、程序装没装（不满足就禁用切换）。"""
        gui_file = cli_file = ""
        try:
            gui_file = Client.desktop_auth()[0]
        except Exception:
            pass
        try:
            cli_file = Client.cli_auth() or ""
        except Exception:
            pass
        self.slot_state = {
            "gui": {"file": bool(gui_file and os.path.isfile(gui_file)),
                    "exe": bool(Process.exe())},
            "cli": {"file": bool(cli_file and os.path.isfile(cli_file)),
                    "exe": bool(shutil.which(CLI_CMD))},
        }
        return self.slot_state

    @staticmethod
    def _slot_ready(state):
        """槽位可用 = 登录态文件与程序都在。"""
        return bool(state and state.get("file") and state.get("exe"))

    def _sync_buttons(self):
        """按当前页的按钮集合与当前选中 / 运行状态，逐个决定能否点。"""
        free = not self.busy
        entry = self.selected()
        meta = self.slot_meta()
        slots = self.slot_state or self._probe_slots()
        gui_ok = free and bool(entry) and self._slot_ready(slots["gui"])
        cli_ok = free and bool(entry) and self._slot_ready(slots["cli"])
        cli_bin = bool(slots["cli"]["exe"])
        buttons = self.page_spec.widgets.get("buttons") or {}
        for action, btn in buttons.items():
            if action == "slot_new":
                state = free and bool(entry) and cli_bin
            elif action == "restore_backup":
                state = free and self._has_backup()
            elif action == "sync_current":
                state = free
            elif action in ("refresh_credential", "query_activity"):
                state = free and bool(self._targets())
            elif action == "slot_switch":
                state = free and bool(entry) and bool(meta)
            elif action == "slot_launch":
                state = free and bool(meta) and not meta.get("pids") and cli_bin
            elif action == "slot_stop":
                state = free and bool(meta) and bool(meta.get("pids"))
            elif action == "slot_remove":
                state = free and bool(meta)
            elif action.startswith("do_"):
                state = free and bool(self._cn_targets())
            else:
                state = free
            btn.configure(state="normal" if state else "disabled")
        for widget, ok in ((getattr(self, "btn_switch", None), gui_ok),
                           (getattr(self, "btn_switch_cli", None), cli_ok)):
            if widget is not None:
                widget.configure(state="normal" if ok else "disabled")
        for label, ok in (("切换 GUI", gui_ok), ("切换 CLI", cli_ok),
                          ("收录当前登录", free),
                          ("恢复备份", free and self._has_backup()),
                          ("新建实例（用此账号）", free and bool(entry) and cli_bin),
                          ("启动实例", free and cli_bin)):
            self.menu.entryconfigure(label, state="normal" if ok else "disabled")
        box = self.page_spec.widgets.get("slot_box")
        if box is not None:
            box.configure(state="readonly" if free else "disabled")

    def _on_menu(self, event):
        """右键：先选中该行再弹菜单。"""
        tree = self.page_spec.tree
        row = tree.identify_row(event.y)
        if row:
            if row not in tree.selection():
                tree.selection_set(row)
            tree.focus(row)
            self._sync_buttons()
            try:
                self.menu.tk_popup(event.x_root, event.y_root)
            finally:
                self.menu.grab_release()

    def _on_select_all(self, _event):
        """Ctrl+A 全选，并吞掉默认行为。"""
        self.select_all()
        return "break"

    def copy_uid(self):
        """把选中账号的 uid 复制到剪贴板。"""
        entry = self.selected()
        if not entry:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(entry.uid)
        self.log("UID 已复制", "dim")

    def _switch_entry(self):
        """两个切换按钮共用的前置检查：选中一个能登录的账号。"""
        entry = self.selected()
        if not entry or self.busy:
            return None
        if entry.api_key:
            self.log("静态 API Key 没有登录身份，无法切换", "warn")
            return None
        if not entry.access:
            self.log("%s 没有 accessToken，先「续期」或重新收录" % entry.nickname, "warn")
            return None
        return entry

    def switch_desktop(self):
        """切换 GUI 登录态：确认槽位可用、不是当前账号后交给后台线程。"""
        entry = self._switch_entry()
        if not entry:
            return
        if not self._slot_ready(self.slot_state.get("gui")):
            self.log("GUI 槽位不可用：未检测到客户端安装或登录态文件", "warn")
            return
        if entry.uid == self.current["uid"]:
            self.log("已经是该账号；要合并会话请点「同步」", "dim")
            return
        self.log("将重启客户端并切换到 %s" % entry.nickname, "dim")
        self._run("切换", lambda: Switcher.switch(entry.data, BACKUP, sync=False,
                                                  log=self.thread_log))

    def switch_cli(self):
        """切换默认 CLI 槽位：只支持国际版，换完下次启动 CLI 生效。"""
        entry = self._switch_entry()
        if not entry:
            return
        if not self._slot_ready(self.slot_state.get("cli")):
            self.log("CLI 槽位不可用：未检测到 codebuddy 命令或登录态文件", "warn")
            return
        if entry.realm != "intl":
            self.log("默认 CLI 槽位只支持国际版账号；国内版请用「实例」新建", "warn")
            return
        if entry.uid == self.cli_cur["uid"]:
            self.log("CLI 已经是该账号", "dim")
            return
        self.log("将停掉 CLI 进程，把独立 CLI 的登录态换成 %s" % entry.nickname, "dim")
        self._run("CLI 切换", lambda: Switcher.switch_cli(entry.data, BACKUP,
                                                          log=self.thread_log))

    def sync_current(self):
        """把本地会话合并到当前登录账号。"""
        if self.busy or not self.current["uid"]:
            self.log("读不到当前登录账号，无法同步会话", "warn")
            return
        self._run("同步会话", lambda: Switcher.sync(BACKUP, log=self.thread_log))

    def refresh_credential(self):
        """批量续期选中 / 本页账号的凭证并写回账号文件。"""
        targets = self._targets()
        if not targets or self.busy:
            return
        self.log("用 refreshToken 续期 %d 个账号，成功后写回 JSON" % len(targets), "dim")

        def work():
            """逐个续期，返回成功与失败名单。"""
            done, failed = [], []
            for entry in targets:
                try:
                    Client.refresh(entry.data, log=self.thread_log)
                    entry.expires_at = entry.data.get("expiresAt") or 0
                    self.store.persist(entry)
                    done.append(entry.nickname)
                except Exception as exc:
                    failed.append("%s：%s" % (entry.nickname, exc))
            return {"done": done, "failed": failed}

        self._run("续期凭证", work)

    def restore_backup(self):
        """用 data/Backup 的快照恢复登录态与本地库。"""
        if self.busy:
            return
        if not self._has_backup():
            self.log("还没有备份", "warn")
            return
        self.log("将用 data/Backup 的快照恢复登录态与本地库，并重启客户端", "dim")
        self._run("恢复", lambda: Switcher.restore(BACKUP, log=self.thread_log))

    def import_current(self):
        """把当前 GUI 登录态收录进账号库。"""
        if self.busy:
            return

        def work():
            """读当前登录态并 upsert 成账号文件，返回昵称与文件名。"""
            row = Client.capture(log=self.thread_log)
            path = self.store.upsert(row)
            return {"nickname": row["nickname"], "path": os.path.basename(path)}

        self._run("收录", work)

    def refresh_slots(self):
        """刷新实例下拉：标签带账号与区域，尽量保住上次的选择。"""
        try:
            metas = self.slots.list()
        except Exception as exc:
            self.log("读实例列表失败：%s" % exc, "warn")
            metas = []
        self.slot_metas = metas
        box = self.pages["intl"].widgets.get("slot_box")
        if box is None:
            return
        labels = []
        for meta in metas:
            nick = meta.get("nickname") or (meta.get("uid") or "")[:8] or "?"
            label = meta["name"] if nick == meta["name"] else "%s · %s" % (meta["name"], nick)
            labels.append("%s · %s" % (label, realm_label(meta.get("realm") or "intl")))
        box["values"] = labels
        index = next((i for i, m in enumerate(metas) if m["name"] == self.slot_sel), -1)
        if index >= 0:
            box.current(index)
        else:
            self.slot_sel = ""
            self.slot_var.set("选择实例" if metas else "暂无实例")

    def _on_slot_selected(self, _event=None):
        """实例下拉换选后同步状态并刷新按钮。"""
        index = self.slot_box.current()
        self.slot_sel = (self.slot_metas[index]["name"]
                         if 0 <= index < len(self.slot_metas) else "")
        self._sync_buttons()

    def slot_meta(self):
        """下拉里选中的实例元数据。"""
        return next((m for m in self.slot_metas if m["name"] == self.slot_sel), None)

    def slot_fresh(self):
        """重新读一遍选中实例的元数据（避免用旧的 pid 状态）。"""
        return self.slots.find(self.slot_sel) if self.slot_sel else None

    def slot_new(self):
        """选好工作文件夹后，用选中账号新建一个命名实例。"""
        entry = self._switch_entry()
        if not entry:
            return
        folder = filedialog.askdirectory(
            title="选择这个实例的工作文件夹",
            initialdir=self.last_folder or os.path.expanduser("~"))
        if not folder:
            return
        folder = os.path.normpath(folder)
        self.last_folder = folder
        self.log("将新建实例 %s，工作文件夹 %s" % (entry.nickname, folder), "dim")
        self._run("新建实例", lambda: Switcher.new_cli(entry.data, folder,
                                                       log=self.thread_log))

    def slot_switch(self):
        """把选中实例的登录态换成选中账号。"""
        entry = self._switch_entry()
        meta = self.slot_fresh() if entry else None
        if not entry:
            return
        if not meta:
            self.log("先在实例下拉里选中要换号的实例", "warn")
            return
        if meta.get("uid") == entry.uid:
            self.log("实例 %s 已经是该账号" % meta["name"], "dim")
            return
        self.log("将把实例 %s 换号为 %s%s" % (
            meta["name"], entry.nickname, "（自动重启）" if meta.get("pids") else ""), "dim")
        self._run("实例换号", lambda: Switcher.switch_cli(entry.data, BACKUP,
                                                          slot=meta["name"],
                                                          log=self.thread_log))

    def _slot_action(self, label, run, running=None):
        """实例启停删的共用前置：校验实例状态后交给后台线程。"""
        meta = self.slot_fresh()
        if not meta or self.busy:
            return
        alive = bool(meta.get("pids"))
        if running is not None and alive != running:
            self.log("实例 %s %s" % (meta["name"], "已在运行" if alive else "没有在运行"), "dim")
            return
        self.log("将%s实例 %s" % (label[:2], meta["name"]), "dim")
        self._run(label, lambda: run(meta))

    def slot_launch(self):
        """启动选中实例。"""
        self._slot_action("启动实例", lambda meta: self.slots.launch(meta, log=self.thread_log),
                          running=False)

    def slot_stop(self):
        """停止选中实例。"""
        self._slot_action("停止实例", lambda meta: self.slots.stop(meta, log=self.thread_log),
                          running=True)

    def slot_remove(self):
        """删除选中实例（目录进回收区）。"""
        self._slot_action("删除实例", lambda meta: self.slots.remove(meta, log=self.thread_log))

    def _targets(self, page=None):
        """本页要操作的账号：选了就只做选中的，没选就整页（只挑可用的）。"""
        page = page or self.page_spec
        chosen = self.selected_all()
        rows = chosen or self.realm_entries(page.realm)
        return [e for e in rows if e.usable]

    def _cn_targets(self):
        """国内版成长体系的目标：选了就只做选中的，没选就整页个人号。"""
        chosen = self.selected_all()
        rows = chosen or self.realm_entries("cn")
        return [e for e in rows if e.usable and e.realm == "cn"
                and not e.data.get("enterpriseId")]

    def _cn_sweep(self, label, one, extra=None):
        """国内版活动的唯一执行路径：逐个账号跑 one(entry)，汇总成功 / 失败。"""
        targets = self._cn_targets()
        if self.busy:
            return
        if not targets:
            self.log("没有可操作的国内版账号", "warn")
            return

        def work():
            """逐个账号执行并收集结果，失败只记一行日志。"""
            out, bad = {}, []
            for entry in targets:
                try:
                    piece, why = one(entry)
                except Exception as exc:
                    piece, why = None, str(exc)
                if piece:
                    out[entry.uid] = piece
                if why:
                    bad.append("%s：%s" % (entry.nickname, why))
                    self.thread_log("! %s %s失败：%s" % (entry.nickname, label, why))
            result = {"activity": out, "ok": len(targets) - len(bad), "bad": bad,
                      "count": len(targets), "total": len(targets)}
            if extra:
                result.update(extra() or {})
            return result

        self._run(label, work)

    def query_activity(self):
        """获取积分：连积分包一起拉，并写回各自的账号文件。"""
        targets = self._targets()
        if not targets:
            self.log("没有可刷新的账号", "warn")
            return

        def one(entry):
            """取一个账号的额度与成长信息，失败把原因记进 error。"""
            info = {}
            try:
                info.update(Activity.credits(entry.data))
            except Exception as exc:
                info["error"] = str(exc)
            if entry.realm == "cn":
                try:
                    info.update(Activity.growth(entry.data))
                except Exception as exc:
                    info["error"] = info.get("error") or str(exc)
            if info.get("error"):
                self.thread_log("%s 刷新失败：%s" % (entry.nickname, info["error"]))
            return entry.uid, info

        def work():
            """两个线程并发取额度，汇总成 {uid: info}。"""
            with ThreadPoolExecutor(max_workers=2) as pool:
                out = dict(pool.map(one, targets))
            return {"activity": out, "count": len(targets),
                    "bad": sum(1 for info in out.values() if info.get("error"))}

        self._run("获取积分", work)

    def do_checkin(self):
        """Buddy 加油站签到：把状态翻成中文写进活动结果。"""
        labels = {"checked": "已签到", "already": "已签到", "pending": "处理中", "failed": "失败"}

        def one(entry):
            """给一个账号签到，返回活动结果与失败原因。"""
            res = Activity.checkin(entry.data, log=self.thread_log)
            return ({"checkin": labels.get(res["state"], res["state"])},
                    "" if res["ok"] else res["msg"])

        self._cn_sweep("Buddy 加油站签到", one)

    def do_report(self):
        """上报每日活跃，并顺带刷新成长数据。"""
        def one(entry):
            """给一个账号上报活跃，成功后补一次成长查询。"""
            res = Activity.report(entry.data, log=self.thread_log)
            if not res["ok"]:
                return None, res["msg"]
            try:
                return dict(Activity.growth(entry.data), daily=True), ""
            except Exception:
                return {"daily": True}, ""

        self._cn_sweep("每日活跃", one)

    def do_tasks(self):
        """一键做成长任务：逐个账号跑 Growth.run 并累计积分。"""
        earned = {"credit": 0}

        def one(entry):
            """给一个账号跑完整任务流水，返回快照并累计到账积分。"""
            res = Growth.run(entry.data, self.thread_log)
            earned["credit"] += res.get("credit") or 0
            return res.get("activity"), ""

        self._cn_sweep("成长任务", one, lambda: earned)

    def do_travel(self):
        """猫猫旅行：领奖或派出。"""
        def one(entry):
            """给一个账号处理旅行，失败返回原因。"""
            res = Growth.travel(entry.data)
            if res.get("ok"):
                self.thread_log("%s：%s" % (entry.nickname, res.get("msg") or ""))
            return None, ("" if res.get("ok") else (res.get("msg") or "失败"))

        self._cn_sweep("猫猫旅行", one)

    def do_streak(self):
        """连登管家：补签、礼包、兑换与抽奖。"""
        def one(entry):
            """给一个账号跑连登管家并返回最新快照。"""
            self.thread_log("—— %s · 连登管家 ——" % entry.nickname)
            res = Growth.streak(entry.data, self.thread_log)
            return res.get("activity"), ""

        self._cn_sweep("连登管家", one)

    def _run(self, label, func):
        """后台线程跑一个动作，忙时拒绝重复触发；结果经队列回到界面。"""
        if self.busy:
            return
        self.busy = True
        self._sync_buttons()
        self.busy_lbl.configure(text="%s中…" % label)
        self.log("%s…" % label, "dim")

        def worker():
            """在后台线程里执行动作，把结果或异常放进结果队列。"""
            try:
                result = func()
            except Exception as exc:
                self.resq.put((label, None, exc))
            else:
                self.resq.put((label, result, None))

        threading.Thread(target=worker, daemon=True).start()

    def _pump(self):
        """界面定时泵：把日志队列与结果队列排空，再排下一次。"""
        try:
            while True:
                self.log(self.logq.get_nowait())
        except queue.Empty:
            pass
        try:
            while True:
                label, result, error = self.resq.get_nowait()
                self._finish(label, result, error)
        except queue.Empty:
            pass
        self.root.after(120, self._pump)

    def thread_log(self, message):
        """后台线程写日志的唯一入口（只投递，不碰控件）。"""
        self.logq.put(str(message))

    def _finish(self, label, result, error):
        """收尾：写回活动结果、刷新列表、输出一行汇总。"""
        self.busy = False
        self.busy_lbl.configure(text="")
        if error is not None:
            self.log("%s失败：%s" % (label, error), "err")
        else:
            if isinstance(result, dict) and result.get("activity"):
                for uid, info in result["activity"].items():
                    self.activity.setdefault(uid, {}).update(info)
                    try:
                        self.store.save_activity(uid, info)
                    except Exception as exc:
                        self.log("活动结果写回 %s 失败：%s" % (uid[:8], exc), "warn")
            text = self._summary(label, result)
            self.log("%s完成%s" % (label, "：" + text if text else ""), "ok")
            slot = result.get("slot") if isinstance(result, dict) else ""
            if slot:
                self.slot_sel = slot
            elif label == "删除实例":
                self.slot_sel = ""
        self.reload()

    @staticmethod
    def _summary(label, result):
        """按动作名把结果压成一行中文摘要。"""
        if not isinstance(result, dict):
            return str(result or "")
        if label == "切换":
            text = result.get("nickname") or ""
            if result.get("moved"):
                text += " · 合并 %d 条会话" % result["moved"]
            if result.get("applied") is False:
                text += " · 未确认生效"
            return text
        if label == "CLI 切换":
            return "%s · 下次启动 CLI 生效" % (result.get("nickname") or "")
        if label == "新建实例":
            return "%s · %s" % (result.get("slot") or "", result.get("folder") or "")
        if label == "实例换号":
            text = "%s · %s" % (result.get("slot") or "", result.get("nickname") or "")
            return text + ("" if result.get("started") else " · 下次启动生效")
        if label == "启动实例":
            return "%s · %s" % (result.get("name") or "", result.get("folder") or "")
        if label in ("停止实例", "删除实例"):
            return result.get("name") or ""
        if label == "同步会话":
            if result.get("skipped") == "already":
                return "无需改动"
            return "合并 %d 条会话" % (result.get("moved") or 0)
        if label == "续期凭证":
            done, failed = result.get("done") or [], result.get("failed") or []
            return "%d 个" % len(done) + ("，%d 个失败" % len(failed) if failed else "")
        if label == "获取积分":
            text = "%d 个账号" % (result.get("count") or 0)
            return text + ("，%d 个失败" % result["bad"] if result.get("bad") else "")
        if label in ("Buddy 加油站签到", "每日活跃", "猫猫旅行"):
            text = "%d 个成功" % (result.get("ok") or 0)
            bad = result.get("bad") or []
            return text + ("，%d 个失败" % len(bad) if bad else "")
        if label == "成长任务":
            return "%d 个账号，共增加 +%d 积分" % (
                result.get("count") or 0, result.get("credit") or 0)
        if label == "连登管家":
            return "%d 个账号已处理" % (result.get("count") or 0)
        if label == "恢复":
            return "已重启" if result.get("started") else "客户端未启动"
        if label == "收录":
            return "%s → %s" % (result.get("nickname") or "", result.get("path") or "")
        return ""

    def log(self, message, level="info"):
        """写一行日志；任务流水线的 ✓ / ! / ⏭ 等标记自动着色。"""
        if level == "info":
            level = {"✓": "ok", "🎉": "ok", "!": "warn", "🌙": "warn", "?": "warn",
                     "⏭": "dim"}.get(str(message)[:1], "info")
        stamp = datetime.datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", "%s  %s\n" % (stamp, message), (level,))
        if int(self.log_text.index("end-1c").split(".")[0]) > 300:
            self.log_text.delete("1.0", "150.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
