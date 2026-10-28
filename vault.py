"""桌面端 at-rest 加密信封与静态密钥回收。

信封是 base64(JSON{suite,keyId,nonce,authTag,ciphertext})，算法 AES-256-GCM；
key = sha256(atRestSecretKey 的 base64 字符串)，keyId = sha256(key).hex[:16]。
静态密钥编译在客户端原生模块里，只能从运行中的主进程内存里只读取回。
本文件会被自己以 `python vault.py --atrest-scan-worker` 拉起当扫描子进程。
"""
import base64
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time

from util import as_dict, read_json, say


class AtRestError(RuntimeError):
    """at-rest 信封损坏、密钥对不上或密钥回收失败。"""
    pass


class AtRest:
    """at-rest 加密信封 + 从客户端进程内存回收静态密钥。"""

    ENVELOPE = "$wbEncrypted"
    MAGIC = {"file": "WBEF1", "field": "WBEV1", "record": "WBER1", "stream": "WBES1"}
    CODE = {"file": 1, "field": 2, "record": 3, "stream": 4}
    SCHEME, SUITE = "sym-v1", 1
    KEYBLOB = "keyblob"
    CONFIG_DIRS = (".workbuddy-ai", ".workbuddy")

    CHUNK, OVERLAP, SLICE = 4 << 20, 31, 64 << 20
    MODE_LITERAL, MODE_SECRET, MODE_WINDOW = "literal", "secret", "window"
    WORKER_FLAG = "--atrest-scan-worker"
    EXE_NAMES = ("workbuddy.exe", "workbuddyai.exe")

    _cache = {"key": None, "at": 0.0, "source": ""}
    _lock = threading.Lock()

    @classmethod
    def _lp(cls, text):
        """4 字节大端长度前缀 + 原文，AAD 里的变长字段都用它编码。"""
        raw = str(text).encode("utf-8")
        return len(raw).to_bytes(4, "big") + raw

    @classmethod
    def aad(cls, key_id, suite, framing):
        """按 framing 拼出 AAD：魔数、suite、keyId 与 framing 码。"""
        if framing not in cls.MAGIC:
            raise AtRestError("未知 framing：%r" % (framing,))
        return b"".join((
            b"WB-AAD\0", b"\x01",
            cls._lp(cls.MAGIC[framing]), cls._lp(cls.SCHEME), int(suite).to_bytes(4, "big"),
            cls._lp(key_id), bytes([cls.CODE[framing]]), b"\x00", b"\x00",
        ))

    @staticmethod
    def key_id(key):
        """密钥指纹：sha256(key) 的前 16 位 hex。"""
        return hashlib.sha256(key).hexdigest()[:16]

    @classmethod
    def is_envelope(cls, value):
        """判断一个值是不是 `$wbEncrypted` 信封（字典或 JSON 串）。"""
        if isinstance(value, dict):
            return isinstance(value.get("envelope"), str) and cls.ENVELOPE in value
        if isinstance(value, str):
            head = value.lstrip()
            return head.startswith("{") and cls.ENVELOPE in head[:64]
        return False

    @staticmethod
    def parse_envelope(value):
        """把信封解成 suite / keyId / nonce / authTag / ciphertext；坏信封抛 AtRestError。"""
        if isinstance(value, dict):
            value = value.get("envelope") or ""
        try:
            env = json.loads(base64.b64decode(str(value)).decode("utf-8"))
            return {
                "suite": int(env.get("suite") or AtRest.SUITE),
                "keyId": str(env.get("keyId") or ""),
                "nonce": base64.b64decode(env["nonce"]),
                "authTag": base64.b64decode(env["authTag"]),
                "ciphertext": base64.b64decode(env["ciphertext"]),
            }
        except Exception as exc:
            raise AtRestError("信封解析失败：%s" % exc)

    @classmethod
    def open_envelope(cls, key, value, framing):
        """校验 keyId 后解出明文；keyId 对不上直接抛错。"""
        env = cls.parse_envelope(value)
        expect = cls.key_id(key)
        if env["keyId"] and env["keyId"] != expect:
            raise AtRestError("信封 keyId %s 与当前密钥（%s）不匹配" % (env["keyId"], expect))
        return _gcm_open(key, env["nonce"], cls.aad(env["keyId"] or expect, env["suite"], framing),
                         env["ciphertext"], env["authTag"])

    @classmethod
    def field(cls, key, value):
        """解密字段；明文原样返回，空值给空串，非字符串非信封给 None。"""
        if value is None or value == "":
            return ""
        if not cls.is_envelope(value):
            return value if isinstance(value, str) else None
        return cls.open_envelope(key, value, "field").decode("utf-8", "replace")

    @classmethod
    def seal(cls, key, plaintext, framing, nonce=None):
        """把明文封成 `$wbEncrypted` 信封（写登录态时用）。"""
        if isinstance(plaintext, str):
            plaintext = plaintext.encode("utf-8")
        key_id = cls.key_id(key)
        nonce = nonce or os.urandom(12)
        aad_bytes = cls.aad(key_id, cls.SUITE, framing)
        aes = _AES(key)
        h = int.from_bytes(aes.encrypt_block(b"\x00" * 16), "big")
        j0 = _j0(aes, nonce)
        cipher = _gctr(aes, j0, plaintext)
        tag = (int.from_bytes(aes.encrypt_block(j0), "big") ^ _ghash(h, aad_bytes, cipher)).to_bytes(16, "big")
        blob = json.dumps({
            "suite": cls.SUITE, "keyId": key_id,
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "authTag": base64.b64encode(tag).decode("ascii"),
            "ciphertext": base64.b64encode(cipher).decode("ascii"),
        }, separators=(",", ":")).encode("utf-8")
        return {cls.ENVELOPE: cls.SUITE, "envelope": base64.b64encode(blob).decode("ascii")}

    @classmethod
    def config_dir(cls):
        """客户端配置目录：环境变量优先，否则在 ~ 下找带 keyblob 的那个。"""
        env = os.environ.get("WORKBUDDY_CONFIG_DIR") or os.environ.get("CODEBUDDY_CONFIG_DIR")
        if env:
            return os.path.realpath(os.path.abspath(os.path.expanduser(env)))
        home = os.path.expanduser("~")
        for name in cls.CONFIG_DIRS:
            candidate = os.path.join(home, name)
            if os.path.isfile(os.path.join(candidate, cls.KEYBLOB)):
                return os.path.realpath(candidate)
        return os.path.realpath(os.path.join(home, cls.CONFIG_DIRS[0]))

    @classmethod
    def keyblob_path(cls):
        """配置目录下的 keyblob 路径。"""
        return os.path.join(cls.config_dir(), cls.KEYBLOB)

    @classmethod
    def load_keyblob(cls):
        """读 keyblob；文件缺失或读不出来都抛 AtRestError。"""
        path = cls.keyblob_path()
        if not os.path.isfile(path):
            raise AtRestError("keyblob 不存在：%s（客户端没装或没登录过？）" % path)
        try:
            return as_dict(read_json(path))
        except Exception as exc:
            raise AtRestError("keyblob 读取失败：%s" % exc)

    @classmethod
    def read_key_id(cls):
        """keyblob 里 static-v1 槽的 protectorKeyId，也就是要找的密钥指纹。"""
        for slot in (cls.load_keyblob().get("slots") or []):
            if slot.get("type") == "static-v1" and slot.get("protectorKeyId"):
                return str(slot["protectorKeyId"]).lower()
        raise AtRestError("keyblob 里没有 static-v1 槽（%s）" % cls.keyblob_path())

    @classmethod
    def unwrap(cls, key):
        """自检：keyId 对得上，且能解出 32 字节主密钥。"""
        blob = cls.load_keyblob()
        for slot in (blob.get("slots") or []):
            if slot.get("type") != "static-v1" or not slot.get("wrapped"):
                continue
            if str(slot.get("protectorKeyId") or "").lower() != cls.key_id(key):
                raise AtRestError("密钥与 keyblob 的 protectorKeyId 不匹配")
            master = cls.open_envelope(key, slot["wrapped"], "file")
            if len(master) != 32 or cls.key_id(master) != str(blob.get("keyId") or ""):
                raise AtRestError("keyblob 解出的主密钥校验失败")
            return master
        raise AtRestError("keyblob 里没有可用的 static-v1 槽")

    @classmethod
    def cached(cls):
        """缓存的密钥；keyblob 换了或对不上就作废。"""
        with cls._lock:
            key = cls._cache["key"]
        if not key:
            return None
        try:
            cls.unwrap(key)
        except Exception:
            with cls._lock:
                cls._cache.update({"key": None, "source": ""})
            return None
        return key

    @classmethod
    def recover(cls, force=False, log=None):
        """取密钥：先用缓存，缓存没有或强制重找就去内存里回收。"""
        if not force:
            cached = cls.cached()
            if cached:
                return cached
        key = bytes.fromhex(cls.hunt(log=log))
        cls.unwrap(key)
        with cls._lock:
            cls._cache.update({"key": key, "at": time.time(), "source": "hunt"})
        say(log, "已从客户端进程内存回收 at-rest 密钥（keyId=%s）" % cls.key_id(key))
        return key

    @classmethod
    def _win32(cls):
        """懒加载 kernel32 与两个 ctypes 结构体，返回句柄与常量表。"""
        if cls.__dict__.get("_win"):
            return cls._win
        import ctypes as C
        import ctypes.wintypes as W

        k32 = C.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = W.HANDLE
        k32.CreateToolhelp32Snapshot.restype = W.HANDLE
        k32.VirtualQueryEx.restype = C.c_size_t

        class PROCESSENTRY32(C.Structure):
            """PROCESSENTRY32 结构体，进程快照用。"""
            _fields_ = [
                ("dwSize", W.DWORD), ("cntUsage", W.DWORD), ("th32ProcessID", W.DWORD),
                ("th32DefaultHeapID", C.c_void_p), ("th32ModuleID", W.DWORD),
                ("cntThreads", W.DWORD), ("th32ParentProcessID", W.DWORD),
                ("pcPriClassBase", C.c_long), ("dwFlags", W.DWORD),
                ("szExeFile", C.c_char * 260),
            ]

        class MEMORY_BASIC_INFORMATION(C.Structure):
            """MEMORY_BASIC_INFORMATION 结构体，内存段枚举用。"""
            _fields_ = [
                ("BaseAddress", C.c_void_p), ("AllocationBase", C.c_void_p),
                ("AllocationProtect", W.DWORD), ("__pad1", W.DWORD),
                ("RegionSize", C.c_size_t), ("State", W.DWORD), ("Protect", W.DWORD),
                ("Type", W.DWORD), ("__pad2", W.DWORD),
            ]

        cls._win = {
            "C": C, "k32": k32, "PROCESSENTRY32": PROCESSENTRY32, "MBI": MEMORY_BASIC_INFORMATION,
            "TH32CS_SNAPPROCESS": 0x00000002, "QUERY": 0x0400, "VM_READ": 0x0010,
            "MEM_COMMIT": 0x1000, "MEM_PRIVATE": 0x20000, "PAGE_GUARD": 0x100,
            "READABLE": 0x02 | 0x04 | 0x08 | 0x20 | 0x40 | 0x80,
        }
        return cls._win

    @classmethod
    def _procs(cls):
        """进程快照：[(pid, ppid, exe)]。"""
        win = cls._win32()
        C, k32 = win["C"], win["k32"]
        snap = k32.CreateToolhelp32Snapshot(win["TH32CS_SNAPPROCESS"], 0)
        if snap == -1:
            raise AtRestError("CreateToolhelp32Snapshot 失败")
        out = []
        entry = win["PROCESSENTRY32"]()
        entry.dwSize = C.sizeof(entry)
        ok = k32.Process32First(snap, C.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID,
                        entry.szExeFile.decode("latin-1")))
            ok = k32.Process32Next(snap, C.byref(entry))
        k32.CloseHandle(snap)
        return out

    @classmethod
    def _main_pids(cls):
        """客户端主进程 pid：父进程不在客户端集合里的那些。"""
        rows = [(pid, ppid) for pid, ppid, name in cls._procs()
                if name.lower() in cls.EXE_NAMES]
        if not rows:
            raise AtRestError("没有发现运行中的 WorkBuddy 客户端 —— 请先启动并登录")
        own = {pid for pid, _ppid in rows}
        return [pid for pid, ppid in rows if ppid not in own]

    @classmethod
    def _regions(cls, handle):
        """客户端进程里可读的私有已提交内存段 [(基址, 长度)]。"""
        win = cls._win32()
        C, k32 = win["C"], win["k32"]
        out, addr, mbi = [], 0, win["MBI"]()
        while addr < 0x7FFFFFFFFFFF:
            if not k32.VirtualQueryEx(handle, C.c_void_p(addr), C.byref(mbi), C.sizeof(mbi)):
                break
            base, size = mbi.BaseAddress or 0, mbi.RegionSize or 0
            if (mbi.State == win["MEM_COMMIT"] and not (mbi.Protect & win["PAGE_GUARD"])
                    and (mbi.Protect & win["READABLE"]) and mbi.Type == win["MEM_PRIVATE"]):
                out.append((base, size))
            addr = base + size
        return out

    @classmethod
    def _scan_data(cls, data, start, key_id, mode, stride, target):
        """在内存块里按模式找密钥，命中返回 (地址, hex)。"""
        digest = hashlib.sha256
        if mode == cls.MODE_LITERAL:
            for literal, utf16 in ((b"atRestSecretKey", False),
                                   (b"a\x00t\x00R\x00e\x00s\x00t\x00S\x00e\x00c\x00r\x00e\x00t\x00"
                                    b"K\x00e\x00y\x00", True)):
                idx = data.find(literal)
                while idx >= 0:
                    match = _B64_44.search(data, idx, idx + 128)
                    if match:
                        cand = match.group().replace(b"\x00", b"") if utf16 else match.group()
                        key = digest(cand).digest()
                        if digest(key).hexdigest()[:16] == key_id:
                            return start + idx, key.hex()
                    idx = data.find(literal, idx + 1)
            return None
        if mode == cls.MODE_SECRET:
            for pattern, utf16 in _SECRET_PATTERNS:
                for match in pattern.finditer(data):
                    cand = match.group().replace(b"\x00", b"") if utf16 else match.group()
                    key = digest(cand).digest()
                    if digest(key).hexdigest()[:16] == key_id:
                        return start + match.start(), key.hex()
            return None
        first, view = (-start) % stride, memoryview(data)
        for i in range(first, len(data) - 31, stride):
            if digest(view[i:i + 32]).digest()[:8] == target:
                return start + i, bytes(view[i:i + 32]).hex()
        return None

    @classmethod
    def _scan_job(cls, job):
        """按 job（pid / 段 / keyId / 模式 / 步长）读内存扫描，命中返回 (地址, hex)。"""
        pid, regions, key_id, mode, stride = job
        win = cls._win32()
        C, k32 = win["C"], win["k32"]
        handle = k32.OpenProcess(win["QUERY"] | win["VM_READ"], False, pid)
        if not handle:
            return None
        target, buf, read = bytes.fromhex(key_id), C.create_string_buffer(cls.CHUNK), C.c_size_t(0)
        try:
            for base, size in regions:
                off, carry = 0, b""
                while off < size:
                    want = min(cls.CHUNK, size - off)
                    ok = k32.ReadProcessMemory(handle, C.c_void_p(base + off), buf, want, C.byref(read))
                    data = carry + (buf.raw[:read.value] if ok and read.value else b"")
                    start = base + off - len(carry)
                    hit = cls._scan_data(data, start, key_id, mode, stride, target)
                    if hit:
                        return hit
                    carry = data[-cls.OVERLAP:] if len(data) > cls.OVERLAP else data
                    off += want
        finally:
            k32.CloseHandle(handle)
        return None

    @classmethod
    def _slices(cls, regions):
        """把大段内存切成带重叠的片，便于并行扫描。"""
        out = []
        for base, size in regions:
            off = 0
            while off < size:
                piece = min(cls.SLICE, size - off)
                out.append((base + off, piece))
                if off + piece >= size:
                    break
                off += piece - cls.OVERLAP
        return out

    @classmethod
    def _worker_python(cls):
        """扫描子进程用的解释器：pythonw 换成 python。"""
        exe = sys.executable or ""
        if exe.lower().endswith("pythonw.exe"):
            alt = os.path.join(os.path.dirname(exe), "python.exe")
            if os.path.isfile(alt):
                return alt
        return exe or "python"

    @classmethod
    def _scan_parallel(cls, jobs, say):
        """多进程分段扫描；并行不可用时退回单进程，保证不漏。"""
        if len(jobs) == 1:
            hit = cls._scan_job(jobs[0])
            return [hit] if hit else []
        try:
            return cls._scan_workers(jobs, say)
        except Exception as exc:
            say("并行扫描不可用（%s），改用单进程…" % exc)
        for job in jobs:
            hit = cls._scan_job(job)
            if hit:
                return [hit]
        return []

    @classmethod
    def _scan_workers(cls, jobs, say):
        """按 job 起子进程并行扫描，命中就杀掉其余子进程。"""
        import subprocess as sp
        from concurrent.futures import ThreadPoolExecutor, as_completed

        script, live, lock = os.path.abspath(__file__), {}, threading.Lock()

        def one(job):
            """起一个扫描子进程，把 job 从 stdin 灌进去并取回结果。"""
            proc = sp.Popen([cls._worker_python(), script, cls.WORKER_FLAG],
                            stdin=sp.PIPE, stdout=sp.PIPE, stderr=sp.DEVNULL,
                            text=True, cwd=os.path.dirname(script))
            with lock:
                live[id(proc)] = proc
            out, _ = proc.communicate(json.dumps(job))
            return json.loads(out) if out and out.strip() else None

        def kill_all():
            """杀掉还活着的扫描子进程。"""
            with lock:
                procs = list(live.values())
            for proc in procs:
                try:
                    proc.kill()
                except Exception:
                    pass

        hits, failed = [], 0
        pool = ThreadPoolExecutor(max_workers=len(jobs))
        try:
            futures = [pool.submit(one, job) for job in jobs]
            for future in as_completed(futures):
                try:
                    hit = future.result()
                except Exception:
                    failed += 1
                    continue
                if hit:
                    hits.append(hit)
                    kill_all()
                    break
        finally:
            kill_all()
            pool.shutdown(wait=True)
        if failed == len(jobs):
            raise AtRestError("所有扫描子进程都启动失败")
        return hits

    @classmethod
    def hunt(cls, key_id=None, log=None):
        """从客户端主进程内存里找回静态密钥（只读），返回 64 位 hex。"""
        if os.name != "nt":
            raise AtRestError("从进程内存回收密钥只支持 Windows；其他平台请手动导入密钥")
        key_id = (key_id or os.environ.get("WB_ATREST_KEYID") or cls.read_key_id()).strip().lower()
        if len(key_id) != 16 or any(c not in "0123456789abcdef" for c in key_id):
            raise AtRestError("keyId 必须是 16 个 hex 字符，得到 %r" % key_id)
        win = cls._win32()
        k32, workers = win["k32"], max(1, min(24, os.cpu_count() or 4))
        last_error = None
        for pid in cls._main_pids():
            handle = k32.OpenProcess(win["QUERY"] | win["VM_READ"], False, pid)
            if not handle:
                last_error = "无法读取 pid %d 的内存（错误码 %d）—— 试试以管理员身份运行" % (
                    pid, k32.GetLastError())
                say(log, last_error)
                continue
            regions = cls._regions(handle)
            k32.CloseHandle(handle)
            if not regions:
                continue
            say(log, "pid %d：%d 个可读私有段，共 %.0f MB"
                % (pid, len(regions), sum(s for _b, s in regions) / 1e6))

            pieces = cls._slices(regions)
            slots = max(1, min(workers, len(pieces)))
            groups, load = [], [0] * slots
            for piece in pieces:
                slot = load.index(min(load))
                if slot == len(groups):
                    groups.append([])
                groups[slot].append(piece)
                load[slot] += piece[1]

            for mode, stride, label in ((cls.MODE_LITERAL, 1, "载荷字段"),
                                        (cls.MODE_SECRET, 1, "载荷 base64"),
                                        (cls.MODE_WINDOW, 8, "8 字节栅格"),
                                        (cls.MODE_WINDOW, 1, "逐字节兜底")):
                jobs = [(pid, group, key_id, mode, stride) for group in groups if group]
                started = time.time()
                hits = cls._scan_parallel(jobs, lambda m: say(log, m))
                say(log, "按%s扫描：%.1fs，%d 个候选" % (label, time.time() - started, len(hits)))
                for _addr, key_hex in hits:
                    try:
                        cls.unwrap(bytes.fromhex(key_hex))
                    except Exception:
                        continue
                    return key_hex
        raise AtRestError(last_error or "没能在客户端进程内存里找到密钥（keyId=%s）" % key_id)

    @classmethod
    def worker_main(cls):
        """扫描子进程入口：从 stdin 读 job，把命中写到 stdout。"""
        job = json.loads(sys.stdin.read() or "[]")
        if not job:
            return 2
        hit = cls._scan_job(tuple(job))
        sys.stdout.write(json.dumps(hit))
        sys.stdout.flush()
        return 0


def _build_sbox():
    """用生成元递推构造 AES 的 S 盒。"""
    sbox = [0] * 256
    p = q = 1
    while True:
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        sbox[p] = (q ^ ((q << 1) | (q >> 7)) ^ ((q << 2) | (q >> 6))
                   ^ ((q << 3) | (q >> 5)) ^ ((q << 4) | (q >> 4)) ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _build_sbox()
_MUL2 = [((i << 1) ^ 0x1B) & 0xFF if i & 0x80 else (i << 1) for i in range(256)]
_MUL3 = [_MUL2[i] ^ i for i in range(256)]
_REDUCTION = 0xE1000000000000000000000000000000
_B64_44 = re.compile(rb"[A-Za-z0-9+/]{43}=")
_SECRET_PATTERNS = (
    (re.compile(rb"(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{43}=(?![A-Za-z0-9+/=])"), False),
    (re.compile(rb"(?<![A-Za-z0-9+/]\x00)(?:[A-Za-z0-9+/]\x00){43}=\x00(?![A-Za-z0-9+/=]\x00)"), True),
)


class _AES(object):
    """AES-256 正向加密（GCM 只用得到这个），状态按列主序排 16 字节。"""

    __slots__ = ("_w",)

    def __init__(self, key):
        """把 32 字节密钥扩展成 60 个轮密钥字。"""
        if len(key) != 32:
            raise AtRestError("AES-256 需要 32 字节密钥，得到 %d" % len(key))
        words = [list(key[4 * i:4 * i + 4]) for i in range(8)]
        rcon = 1
        for i in range(8, 60):
            t = list(words[i - 1])
            if i % 8 == 0:
                t = [_SBOX[b] for b in t[1:] + t[:1]]
                t[0] ^= rcon
                rcon = _MUL2[rcon]
            elif i % 8 == 4:
                t = [_SBOX[b] for b in t]
            words.append([words[i - 8][j] ^ t[j] for j in range(4)])
        self._w = words

    def encrypt_block(self, block):
        """加密一个 16 字节分组。"""
        w = self._w
        s = [block[i] ^ w[i // 4][i % 4] for i in range(16)]
        for rnd in range(1, 14):
            s = [_SBOX[b] for b in s]
            s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]
            s = _mix_columns(s)
            base = rnd * 4
            s = [s[i] ^ w[base + i // 4][i % 4] for i in range(16)]
        s = [_SBOX[b] for b in s]
        s = [s[(i + 4 * (i % 4)) % 16] for i in range(16)]
        return bytes(s[i] ^ w[56 + i // 4][i % 4] for i in range(16))


def _mix_columns(s):
    """MixColumns：每列做 GF(2^8) 上的固定矩阵乘法。"""
    out = [0] * 16
    for c in range(4):
        a0, a1, a2, a3 = s[4 * c:4 * c + 4]
        out[4 * c + 0] = _MUL2[a0] ^ _MUL3[a1] ^ a2 ^ a3
        out[4 * c + 1] = a0 ^ _MUL2[a1] ^ _MUL3[a2] ^ a3
        out[4 * c + 2] = a0 ^ a1 ^ _MUL2[a2] ^ _MUL3[a3]
        out[4 * c + 3] = _MUL3[a0] ^ a1 ^ a2 ^ _MUL2[a3]
    return out


def _gf_mul(x, y):
    """GF(2^128) 乘法，模数取 GCM 的 x^128 + x^7 + x^2 + x + 1。"""
    z, v = 0, y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        v = (v >> 1) ^ _REDUCTION if v & 1 else v >> 1
    return z


def _blocks(data):
    """把数据补齐成 16 字节分组。"""
    if len(data) % 16:
        data = data + b"\x00" * (16 - len(data) % 16)
    return [data[i:i + 16] for i in range(0, len(data), 16)]


def _ghash(h, aad_bytes, ciphertext):
    """GHASH：AAD 与密文一起做 GF(2^128) 累加。"""
    x = 0
    tail = (len(aad_bytes) * 8).to_bytes(8, "big") + (len(ciphertext) * 8).to_bytes(8, "big")
    for block in _blocks(aad_bytes) + _blocks(ciphertext) + [tail]:
        x = _gf_mul(x ^ int.from_bytes(block, "big"), h)
    return x


def _j0(aes, nonce):
    """GCM 的初始计数器块：12 字节 nonce 直接拼 1，否则对 nonce 做 GHASH。"""
    if len(nonce) == 12:
        return nonce + b"\x00\x00\x00\x01"
    h = int.from_bytes(aes.encrypt_block(b"\x00" * 16), "big")
    return _ghash(h, b"", nonce).to_bytes(16, "big")


def _gctr(aes, j0, data):
    """GCTR：按计数器生成密钥流并异或。"""
    out, counter = bytearray(), int.from_bytes(j0, "big")
    for off in range(0, len(data), 16):
        counter = (counter & ~0xFFFFFFFF) | ((counter + 1) & 0xFFFFFFFF)
        stream = aes.encrypt_block(counter.to_bytes(16, "big"))
        out += bytes(a ^ b for a, b in zip(data[off:off + 16], stream))
    return bytes(out)


def _gcm_open(key, nonce, aad_bytes, ciphertext, tag):
    """GCM 解密并校验 authTag；校验不过抛 AtRestError。"""
    aes = _AES(key)
    h = int.from_bytes(aes.encrypt_block(b"\x00" * 16), "big")
    j0 = _j0(aes, nonce)
    expect = int.from_bytes(aes.encrypt_block(j0), "big") ^ _ghash(h, aad_bytes, ciphertext)
    if not hmac.compare_digest(expect.to_bytes(16, "big"), tag):
        raise AtRestError("GCM 校验失败——密钥不对或信封被改过")
    return _gctr(aes, j0, ciphertext)


if __name__ == "__main__":
    if AtRest.WORKER_FLAG in sys.argv:
        sys.exit(AtRest.worker_main())
    sys.exit("vault.py 是库；唯一 CLI 入口是 %s" % AtRest.WORKER_FLAG)
