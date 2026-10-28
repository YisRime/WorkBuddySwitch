"""路径与区域配置、通用工具、客户端进程控制。

REALMS 按区域列出各端点与 UA：chat 是对话 / 成长端点，billing 是计费 / 积分端点，
domain 兜底 X-Domain，info 是 auth 目录里的槽位文件名；WEB_BASE 是任务领奖的网页域
（桌面域返回 400 时降级）。
"""
import base64
import csv
import datetime
import json
import os
import re
import secrets
import subprocess
import time
import uuid
import urllib.request

APP_NAME = "WorkBuddy Switch"
BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
ACCOUNT_DIR = os.path.join(DATA, "Account")
BACKUP = os.path.join(DATA, "Backup")
CLI_DIR = os.path.join(DATA, "CLI")
CLI_INFO = "Tencent-Cloud.coding-copilot.info"
CLI_CMD = os.environ.get("WB_CLI_CMD", "codebuddy")

FONT = "Microsoft YaHei UI"
MONO = "Consolas"

REALMS = {
    "intl": {
        "name": "国际版",
        "chat": "https://www.workbuddy.ai",
        "billing": "https://www.workbuddy.ai",
        "origin": "https://www.workbuddy.ai",
        "domain": "www.workbuddy.ai",
        "version": "5.8.4",
        "ua": "WorkBuddy/5.8.4 workbuddy-ai/5.8.4 CLI/2.161.0",
        "billing_ua": "WorkBuddy/5.8.4",
        "info": "workbuddy-desktop-ai.info",
        "cli_domain": "www.codebuddy.ai",
    },
    "cn": {
        "name": "国内版",
        "chat": "https://copilot.tencent.com",
        "billing": "https://www.codebuddy.cn",
        "origin": "https://www.codebuddy.cn",
        "domain": "copilot.tencent.com",
        "version": "5.8.3",
        "ua": "WorkBuddy/5.8.3 WorkBuddy/5.8.3 CLI/2.161.0",
        "billing_ua": "WorkBuddy/5.8.3",
        "info": "workbuddy-desktop.info",
    },
}

WEB_BASE = "https://www.workbuddy.cn"
WEB_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36")


class CoreError(RuntimeError):
    """可预期的业务失败，消息直接给用户看。"""


def say(log, message):
    """把消息交给日志回调；回调缺失或自己抛错都不影响主流程。"""
    if log:
        try:
            log(message)
        except Exception:
            pass


def read_json(path):
    """读一个 UTF-8 JSON 文件。"""
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def as_dict(value) -> dict:
    """只认 dict，其余（字符串 / null / 列表）一律当空表。"""
    return value if isinstance(value, dict) else {}


def write_json(path, doc):
    """原子写：先写同目录临时文件，fsync 后再替换目标。"""
    tmp = "%s.tmp-%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def uuid7():
    """RFC 9562 UUIDv7：客户端会话 / 消息 id 就是这个形态。"""
    ms = int(time.time() * 1000) & ((1 << 48) - 1)
    return uuid.UUID(int=(ms << 80) | (0x7 << 76) | (secrets.randbits(12) << 64)
                     | (0b10 << 62) | secrets.randbits(62))


def url_open(request, proxy="", timeout=25):
    """带可选代理的 urlopen（urlopen 本身不吃 proxy）。"""
    if proxy:
        opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def realm_of(account):
    """账号区域：先认 realm 字段，缺了就按 domain / token 的域名猜，默认国际版。"""
    account = account or {}
    value = str(account.get("realm") or "").strip().lower()
    if value in REALMS:
        return value
    domain = str(account.get("domain") or "").lower()
    if not domain:
        domain = str(jwt_claims(account.get("accessToken")).get("iss") or "").lower()
    return "cn" if ("codebuddy.cn" in domain or "tencent.com" in domain) else "intl"


def realm_label(realm):
    """区域的中文名；未知区域按国际版显示。"""
    return REALMS.get(realm, REALMS["intl"])["name"]


def jwt_claims(token):
    """解出 JWT 的 payload；格式不对给空表。"""
    try:
        part = str(token or "").split(".")[1]
        part += "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part))
    except Exception:
        return {}


def jwt_uid(token):
    """JWT 的 sub，也就是账号 uid。"""
    return str(jwt_claims(token).get("sub") or "")


def jwt_exp(token):
    """JWT 的 exp（秒级时间戳）；解不出来给 0。"""
    try:
        return int(jwt_claims(token).get("exp") or 0)
    except Exception:
        return 0


def norm_ts(value):
    """统一成秒级时间戳；毫秒、字符串、None 都能吃。"""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0
    if number > 10 ** 11:
        number /= 1000.0
    return int(number)


def parse_ts(value):
    """到期时间戳：秒级 / 毫秒数字，或 `2026-10-31 23:59:59` 这类日期串。"""
    text = str(value or "").strip()
    for shape, size in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%dT%H:%M:%S", 19),
                        ("%Y-%m-%d", 10)):
        if len(text) >= size:
            try:
                return int(time.mktime(time.strptime(text[:size], shape)))
            except (ValueError, OverflowError):
                continue
    return norm_ts(value)


def monthly(row):
    """按月刷新的额度：标了标记、有效期写到十年后，或本来就是 203x 年。"""
    if row.get("monthly") or row.get("no_expiry"):
        return True
    year = str(row.get("expire_time") or "")[:4]
    return year.isdigit() and int(year) >= 2030


def due_stamp(row):
    """一行的到期时间戳；按月刷新的额度到期点就是本月底。"""
    if row.get("is_expired") or not monthly(row):
        return parse_ts(row.get("expire_time") or row.get("cycle_end_time"))
    stamp = parse_ts(row.get("cycle_end_time"))
    if stamp > time.time():
        return stamp
    when = datetime.datetime.fromtimestamp(time.time())
    return int((datetime.datetime(when.year + (when.month == 12), when.month % 12 + 1, 1)
                - datetime.timedelta(seconds=1)).timestamp())


def earliest_expiring(packages):
    """有剩余的在用包里最早到期的那一个；无到期信息的不参与。"""
    active = [item for item in (packages or [])
              if item.get("remain", 0) > 0 and not item.get("is_expired")]
    if not active:
        return None
    first = min([item for item in active if not monthly(item)] or active, key=due_stamp)
    stamp, per_month = due_stamp(first), monthly(first)
    return {"name": first.get("name", ""),
            "package_code": first.get("package_code", ""),
            "remain": first.get("remain", 0),
            "cycle_end_time": (time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stamp))
                               if stamp else ""),
            "days_left": (round((stamp - time.time()) / 86400.0, 1)
                          if per_month and stamp else first.get("days_left")),
            "monthly": per_month}


def fmt_num(value, digits=1):
    """额度数字：过万折成「万」，非法值给破折号。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "—"
    if abs(number) >= 10000:
        return "%.1f万" % (number / 10000)
    return ("%%.%df" % digits) % number


class Process:
    """客户端 / CLI 进程：枚举、身份核对与停止。"""

    NAMES = ("WorkBuddyAI.exe", "WorkBuddy.exe")
    CLI_NAMES = ("cbnode.exe", "cbc-prewarm.exe")

    @staticmethod
    def _run(args, timeout=15):
        """静默执行外部命令（Windows 下不弹窗）；起不来返回 None。"""
        kwargs = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            try:
                info = subprocess.STARTUPINFO()
                info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
                info.wShowWindow = 0
                kwargs["startupinfo"] = info
            except Exception:
                pass
        try:
            return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                  timeout=timeout, check=False, **kwargs)
        except Exception:
            return None

    @classmethod
    def _scan(cls, names, attempts=3):
        """按进程名枚举 [(pid, exe)]；扫描本身失败返回 None（None 与 [] 必须区分）。"""
        if os.name != "nt":
            out = []
            for name in names:
                proc = cls._run(["pgrep", "-f", name])
                if not proc or proc.returncode != 0:
                    continue
                for line in proc.stdout.decode("utf-8", "replace").splitlines():
                    try:
                        out.append((int(line.strip()), name))
                    except ValueError:
                        pass
            return out
        wanted = {name.lower() for name in names}
        for _attempt in range(max(1, attempts)):
            proc = cls._run(["tasklist", "/FO", "CSV", "/NH"])
            if proc and proc.returncode == 0:
                out = []
                for row in csv.reader(proc.stdout.decode("utf-8", "replace").splitlines()):
                    if len(row) < 2 or row[0].strip().lower() not in wanted:
                        continue
                    try:
                        out.append((int(row[1]), row[0].strip()))
                    except ValueError:
                        pass
                return out
            time.sleep(0.3)
        return None

    @classmethod
    def pids(cls):
        """客户端进程的 pid 列表。"""
        return [pid for pid, _name in (cls._scan(cls.NAMES) or ())]

    @classmethod
    def cli_pids(cls):
        """CLI（node 宿主）进程的 pid 列表。"""
        return [pid for pid, _name in (cls._scan(cls.CLI_NAMES) or ())]

    @classmethod
    def creation_time(cls, pid):
        """pid 的创建时间（FILETIME 整数，转成字符串便于写进 JSON）；查不到返回空串。"""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return ""
        if os.name != "nt" or pid <= 0:
            return ""
        try:
            import ctypes
            from ctypes import wintypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x1000, False, pid)
            if not handle:
                return ""
            try:
                created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
                ok = kernel32.GetProcessTimes(handle, ctypes.byref(created),
                                              ctypes.byref(exited), ctypes.byref(kernel),
                                              ctypes.byref(user))
                if not ok:
                    return ""
                return str((created.dwHighDateTime << 32) | created.dwLowDateTime)
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return ""

    @classmethod
    def alive(cls, pid, names=None, created=None):
        """pid 还活着，且给了 created 时确实还是当初那个进程；扫描失败宁可当作还活着。"""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if pid <= 0:
            return False
        if os.name != "nt":
            try:
                os.kill(pid, 0)
                return True
            except OSError:
                return False
        if created:
            return cls.creation_time(pid) == str(created)
        rows = cls._scan(tuple(names or ("cmd.exe",)))
        return True if rows is None else any(pid == row_pid for row_pid, _name in rows)

    @staticmethod
    def kill(pid):
        """强杀单个 pid，成功返回 True。"""
        if os.name != "nt":
            try:
                os.kill(pid, 15)
                return True
            except Exception:
                return False
        try:
            import ctypes
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(0x0001, False, int(pid))
            if not handle:
                return False
            try:
                return bool(kernel32.TerminateProcess(handle, 1))
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False

    @classmethod
    def stop_pid(cls, pid, created=None):
        """连进程树强制停掉一个 pid；创建时间对不上就放手（pid 已被回收）。"""
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            return False
        if created and cls.creation_time(pid) != str(created):
            return False
        if os.name == "nt":
            cls._run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=10)
        else:
            cls.kill(pid)
        return True

    @staticmethod
    def _parents():
        """{pid: ppid}：用 ctypes 拿父子关系，不依赖外部命令输出。"""
        if os.name != "nt":
            return {}
        try:
            import ctypes
            from ctypes import wintypes

            class Entry(ctypes.Structure):
                """PROCESSENTRY32W 的字段布局，只取 pid 与父 pid。"""
                _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                            ("th32ProcessID", wintypes.DWORD),
                            ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                            ("th32ModuleID", wintypes.DWORD),
                            ("cntThreads", wintypes.DWORD),
                            ("th32ParentProcessID", wintypes.DWORD),
                            ("pcPriClassBase", ctypes.c_long),
                            ("dwFlags", wintypes.DWORD),
                            ("szExeFile", ctypes.c_wchar * 260)]

            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            snap = kernel32.CreateToolhelp32Snapshot(0x2, 0)
            entry = Entry()
            entry.dwSize = ctypes.sizeof(Entry)
            table = {}
            if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
                while True:
                    table[entry.th32ProcessID] = entry.th32ParentProcessID
                    if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                        break
            kernel32.CloseHandle(snap)
            return table
        except Exception:
            return {}

    @classmethod
    def subtree(cls, roots):
        """roots 及其全部后代 pid；保留某个实例时，它的子进程也得一起保留。"""
        keep = {int(pid) for pid in roots}
        table = cls._parents()
        changed = True
        while changed:
            changed = False
            for pid, ppid in table.items():
                if ppid in keep and pid not in keep:
                    keep.add(pid)
                    changed = True
        return keep

    @classmethod
    def stop(cls, timeout=15.0, log=None, cli=False, keep=()):
        """停对应槽位的进程；keep 里的进程及其整棵子树保持不动。"""
        names = cls.CLI_NAMES if cli else cls.NAMES
        label = "CLI" if cli else "客户端"

        def targets():
            """要停的进程；扫描失败返回 None（不知道 = 不敢动）。"""
            rows = cls._scan(names)
            return None if rows is None else [row for row in rows if row[0] not in keep]

        keep = cls.subtree(keep) if keep else set()
        rows = targets()
        if rows is None:
            raise CoreError("无法枚举 %s 进程" % label)
        if not rows:
            return []
        pids = [pid for pid, _name in rows]
        if keep:
            for pid in pids:
                cls._run(["taskkill", "/PID", str(pid)], timeout=10)
        else:
            for name in sorted({name for _pid, name in rows}):
                cls._run(["taskkill", "/IM", name], timeout=10)
        time.sleep(2.0)
        for pid, _name in (targets() or ()):
            cls.kill(pid)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if targets() == []:
                say(log, "%s已停止（%d 个进程）" % (label, len(pids)))
                return pids
            time.sleep(0.5)
        raise CoreError("无法完全停止 %s" % label)

    @classmethod
    def exe(cls):
        """客户端 exe 路径：环境变量 → URL scheme 注册表 → 常见安装位置。"""
        env = os.environ.get("WB_CLIENT_EXE")
        if env and os.path.isfile(env):
            return os.path.abspath(env)
        if os.name == "nt":
            try:
                import winreg
                for scheme in ("workbuddy-ai", "workbuddy", "buddy-app"):
                    try:
                        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                            r"Software\Classes\%s\shell\open\command" % scheme) as key:
                            command = str(winreg.QueryValueEx(key, "")[0] or "")
                    except OSError:
                        continue
                    match = (re.match(r'\s*"([^"]+\.exe)"', command, re.I)
                             or re.match(r"\s*(\S+\.exe)", command, re.I))
                    path = match.group(1) if match else ""
                    if path and os.path.isfile(path):
                        return path
            except Exception:
                pass
        for candidate in (r"%ProgramFiles%\WorkBuddyAI\WorkBuddyAI.exe",
                          r"%ProgramFiles%\WorkBuddy\WorkBuddy.exe",
                          r"%LOCALAPPDATA%\Programs\WorkBuddyAI\WorkBuddyAI.exe",
                          r"%LOCALAPPDATA%\Programs\WorkBuddy\WorkBuddy.exe"):
            path = os.path.expandvars(candidate)
            if os.path.isfile(path):
                return path
        return ""

    @classmethod
    def start(cls, timeout=20.0, log=None):
        """拉起客户端，并等它出现在进程表里。"""
        exe = cls.exe()
        if not exe:
            raise CoreError("找不到 WorkBuddy 客户端；可用 WB_CLIENT_EXE 指定 exe")
        if os.name == "nt":
            cls._run(["explorer.exe", exe], timeout=15)
        else:
            try:
                subprocess.Popen([exe], close_fds=True, start_new_session=True)
            except Exception as exc:
                raise CoreError("启动客户端失败：%s" % exc)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cls.pids():
                say(log, "客户端已启动")
                return True
            time.sleep(0.5)
        raise CoreError("客户端没有启动起来")

    @classmethod
    def wait_uid(cls, path, uid, timeout=25.0):
        """等客户端把 auth 文件写成目标 uid。"""
        deadline, last = time.time() + timeout, ""
        while time.time() < deadline:
            try:
                last = str((read_json(path).get("account") or {}).get("uid") or "")
                if last == uid:
                    return True
            except Exception:
                pass
            if not cls.pids():
                return False
            time.sleep(0.5)
        return last == uid
