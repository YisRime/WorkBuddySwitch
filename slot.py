"""命名 CLI 实例：每个实例一套独立用户目录，可同时多开互不干扰。

    <root>/<name>/home/      实例的 USERPROFILE（CLI 由它推出 auth 位置）
    <root>/<name>/config/    CODEBUDDY_CONFIG_DIR（设置、会话、技能）
    <root>/<name>/slot.json  元数据：name / uid / nickname / realm / folder / cmd / pids
    <root>/<name>/launch.cmd 烘焙好 env 的启动脚本，可脱离本工具双击

CLI 没有「指定 auth 文件」的环境变量，登录态路径写死成
`os.homedir()/AppData/Local/CodeBuddyExtension/Data/Public/auth/<authId>.info`，
所以隔离靠给每个实例一个自己的 USERPROFILE；HOME 保持真实用户目录给 git / bash。
"""
import json
import os
import re
import shutil
import subprocess
import time

from util import (CLI_CMD, CLI_DIR, CLI_INFO, CoreError, Process, realm_of, say,
                  write_json)


class Slot:
    """命名实例的目录布局与生命周期：建槽、信任工作目录、启停、删除。"""

    NAME_MAX = 24

    def __init__(self, root=None):
        """记住实例根目录，默认 data/CLI。"""
        self.root = os.path.abspath(root or CLI_DIR)

    def dir_of(self, name):
        """实例目录 `<root>/<name>`。"""
        return os.path.join(self.root, name)

    def home_path(self, name):
        """实例的 USERPROFILE 目录。"""
        return os.path.join(self.dir_of(name), "home")

    def auth_path(self, name):
        """实例登录态路径：必须与 CLI 的固定推导一致。"""
        return os.path.join(self.home_path(name), "AppData", "Local", "CodeBuddyExtension",
                            "Data", "Public", "auth", CLI_INFO)

    def config_path(self, name):
        """实例的 CODEBUDDY_CONFIG_DIR 目录。"""
        return os.path.join(self.dir_of(name), "config")

    @staticmethod
    def sanitize(name):
        """净化实例名：去掉路径非法字符与 `$`，并挡掉点开头和 Windows 保留设备名。"""
        name = re.sub(r'[\\/:*?"<>|$%]+', "-", str(name or "").strip())
        name = name.strip(". ")[:Slot.NAME_MAX].rstrip(". ")
        if name.split(".")[0].upper() in (
                "CON", "PRN", "AUX", "NUL",
                *(("COM%d" % i) for i in range(1, 10)),
                *(("LPT%d" % i) for i in range(1, 10))):
            name += "_"
        return name

    def list(self):
        """全部实例；顺手清掉已退出的 pid。"""
        if not os.path.isdir(self.root):
            return []
        return [meta for meta in (self.read(name) for name in sorted(os.listdir(self.root))
                                  if not name.startswith(".")) if meta]

    def find(self, name):
        """按名字取单个实例，名字为空给 None。"""
        return self.read(name) if name else None

    def read(self, name):
        """读单个实例；目录缺失、slot.json 损坏或名字不符返回 None。"""
        try:
            with open(os.path.join(self.dir_of(name), "slot.json"), encoding="utf-8") as fh:
                meta = json.load(fh)
        except Exception:
            return None
        if not isinstance(meta, dict) or str(meta.get("name") or "") != name:
            return None
        changed = False
        if not isinstance(meta.get("pids"), list):
            meta["pids"], changed = [], True
        created = meta.get("createdPid")
        if not created and meta.get("pids"):
            if Process.alive(meta["pids"][0]):
                created = Process.creation_time(meta["pids"][0])
                if created:
                    meta["createdPid"] = created
                    changed = True
        pids = [pid for pid in (meta.get("pids") or [])
                if Process.alive(pid, created=created)]
        if pids != (meta.get("pids") or []):
            meta["pids"] = pids
            if not pids:
                meta.pop("createdPid", None)
            changed = True
        if changed:
            self.write(meta)
        return meta

    def write(self, meta):
        """把实例元数据写回 slot.json。"""
        path = os.path.join(self.dir_of(meta["name"]), "slot.json")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        write_json(path, meta)

    def ensure(self, name):
        """建好实例的家目录与配置目录；旧版放在实例根下的 auth.info 顺手迁进去。"""
        home = self.home_path(name)
        os.makedirs(os.path.join(home, "AppData", "Local", "CodeBuddyExtension",
                                 "Data", "Public", "auth"), exist_ok=True)
        os.makedirs(self.config_path(name), exist_ok=True)
        legacy = os.path.join(self.dir_of(name), "auth.info")
        if os.path.isfile(legacy):
            target = self.auth_path(name)
            os.replace(legacy, legacy + ".legacy" if os.path.isfile(target) else target)
        return home

    def trust(self, name, folder):
        """把工作目录登记进实例的 trustedDirectories，免掉首次启动的信任菜单。"""
        folder = os.path.abspath(os.path.expanduser(str(folder or "")))
        if not folder:
            return ""
        path = os.path.join(self.config_path(name), "settings.json")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                data = {}
        except Exception:
            data = {}
        wanted = folder.replace("\\", "/")
        trusted = [str(item).replace("\\", "/")
                   for item in (data.get("trustedDirectories") or [])]
        for pattern in (wanted, wanted + "/**"):
            if pattern not in trusted:
                trusted.append(pattern)
        data["trustedDirectories"] = trusted
        write_json(path, data)
        return path

    def create(self, account, folder):
        """由账号派生实例名建槽（重名自动 -2/-3…），返回元数据；登录态另写。"""
        folder = os.path.abspath(os.path.expanduser(str(folder or "")))
        if not os.path.isdir(folder):
            raise CoreError("工作文件夹不存在：%s" % folder)
        base = self.sanitize(account.get("nickname")) or "cli"
        taken = set()
        if os.path.isdir(self.root):
            taken = {name for name in os.listdir(self.root)
                     if os.path.isdir(os.path.join(self.root, name))}
        name, index = base, 1
        while name in taken:
            index += 1
            name = "%s-%d" % (base, index)
        self.ensure(name)
        self.trust(name, folder)
        meta = {
            "name": name,
            "uid": str(account.get("uid") or ""),
            "nickname": str(account.get("nickname") or ""),
            "realm": realm_of(account),
            "folder": folder,
            "cmd": CLI_CMD,
            "pids": [],
            "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self.write(meta)
        return meta

    @staticmethod
    def _instance_env(home, cfg, real_home):
        """实例专属环境变量：USERPROFILE 决定 auth 位置，HOME 留给 git / bash。"""
        drive, tail = os.path.splitdrive(home)
        return {
            "USERPROFILE": home,
            "HOMEDRIVE": drive,
            "HOMEPATH": tail,
            "CODEBUDDY_CONFIG_DIR": cfg,
            "HOME": real_home or os.path.expanduser("~"),
        }

    @classmethod
    def launch_text(cls, name, folder, home, cfg, exe, real_home=""):
        """launch.cmd 内容：env 与工作目录全部烘焙，双击即可用该实例启动 CLI。"""
        lines = ["@echo off", "title CLI · %s" % name]
        lines += ['set "%s=%s"' % item
                  for item in cls._instance_env(home, cfg, real_home).items()]
        lines += ['set "TERM=xterm-256color"', 'set "COLORTERM=truecolor"']
        lines += ['cd /d "%s"' % folder, 'cmd.exe /K call "%s"' % exe]
        return "\r\n".join(lines) + "\r\n"

    @classmethod
    def env_for(cls, home, cfg, real_home=None):
        """把实例变量盖到当前进程环境上，并清掉会让 TUI 静默降级的继承变量。"""
        env = dict(os.environ)
        env.update(cls._instance_env(home, cfg, real_home))
        term = str(env.get("TERM") or "").strip().lower()
        if not term or term in ("dumb", "vt52") or term.startswith("cons25"):
            env["TERM"] = "xterm-256color"
        for key in ("NO_COLOR", "FORCE_COLOR"):
            env.pop(key, None)
        env.setdefault("COLORTERM", "truecolor")
        return env

    def launch(self, meta, log=None):
        """在新控制台启动实例的 CLI；成功后把根 pid 记回 slot.json。"""
        name = meta["name"]
        if meta.get("pids"):
            raise CoreError("实例 %s 已在运行" % name)
        folder = str(meta.get("folder") or "")
        if not os.path.isdir(folder):
            raise CoreError("实例 %s 的工作文件夹不存在：%s" % (name, folder))
        command = str(meta.get("cmd") or CLI_CMD)
        exe = shutil.which(command)
        if not exe:
            raise CoreError("找不到 CLI 命令 %s（可用环境变量 WB_CLI_CMD 指定）" % command)
        home, cfg = self.ensure(name), self.config_path(name)
        self.trust(name, folder)
        real_home = os.path.expanduser("~")
        cmd = os.path.join(self.dir_of(name), "launch.cmd")
        with open(cmd, "w", encoding="mbcs" if os.name == "nt" else "utf-8",
                  errors="replace", newline="") as fh:
            fh.write(self.launch_text(name, folder, home, cfg, exe, real_home))
        env = self.env_for(home, cfg, real_home)
        if os.name == "nt":
            proc = subprocess.Popen(
                ["cmd.exe", "/C", cmd], cwd=folder, env=env,
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
        else:
            proc = subprocess.Popen([command], cwd=folder, env=env,
                                    close_fds=True, start_new_session=True)
        created = ""
        deadline = time.time() + 3.0
        while time.time() < deadline:
            created = Process.creation_time(proc.pid)
            if created or proc.poll() is not None:
                break
            time.sleep(0.2)
        if not created:
            if proc.poll() is None:
                Process.stop_pid(proc.pid)
            raise CoreError("实例 %s 没能启动：%s 立刻退出" % (name, command))
        meta["pids"], meta["createdPid"] = [proc.pid], created
        self.write(meta)
        say(log, "实例 %s 已启动（pid %s）：%s @ %s"
            % (name, proc.pid, meta.get("nickname") or meta.get("uid") or "?", folder))
        return meta

    def stop(self, meta, log=None):
        """停掉实例自己的进程树；创建时间对不上的 pid 一律放手，绝不误杀。"""
        created = meta.get("createdPid")
        pids = [int(pid) for pid in (meta.get("pids") or [])
                if Process.alive(pid, created=created)]
        for pid in pids:
            Process.stop_pid(pid, created)
        deadline = time.time() + 15.0
        while pids and time.time() < deadline:
            alive = [pid for pid in pids if Process.alive(pid, created=created)]
            if not alive:
                break
            time.sleep(1.0)
            for pid in alive:
                Process.stop_pid(pid, created)
        still = [pid for pid in pids if Process.alive(pid, created=created)]
        meta["pids"] = still
        if not still:
            meta.pop("createdPid", None)
        self.write(meta)
        if still:
            raise CoreError("实例 %s 停不下来（pid %s）" % (meta["name"], still))
        if pids:
            say(log, "实例 %s 已停止" % meta["name"])
        return meta

    def claimed_pids(self):
        """所有命名实例认领的 pid：动默认槽位时要绕开这些进程。"""
        claimed = set()
        for meta in self.list():
            claimed.update(int(pid) for pid in (meta.get("pids") or []))
        return claimed

    def remove(self, meta, log=None):
        """删除实例：先停进程，再把目录整体挪进 <root>/.trash（可手动找回）。"""
        if not meta or not meta.get("name"):
            raise CoreError("没有选中实例")
        if meta.get("pids"):
            self.stop(meta, log=log)
        trash = os.path.join(self.root, ".trash")
        os.makedirs(trash, exist_ok=True)
        stem = "%s-%s" % (meta["name"], time.strftime("%Y%m%d-%H%M%S"))
        dest, index = os.path.join(trash, stem), 1
        while os.path.exists(dest):
            index += 1
            dest = os.path.join(trash, "%s-%d" % (stem, index))
        try:
            os.rename(self.dir_of(meta["name"]), dest)
        except OSError as exc:
            raise CoreError("实例 %s 挪不进回收区：%s" % (meta["name"], exc))
        say(log, "实例 %s 已移到回收区：%s（不再需要可手动删除）" % (meta["name"], dest))
        return {"name": meta["name"], "trash": dest}
