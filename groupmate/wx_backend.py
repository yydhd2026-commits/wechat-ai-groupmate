# -*- coding: utf-8 -*-
"""微信后端:接 wechatauto-replica。

设计要点(都是实测踩过的坑):
  * 本机 Python 枚举不到 Weixin.exe 进程,而库靠 tasklist 找 PID →
    启动时自动探测并用环境变量 WECHATAUTO_PID 喂给它(sitecustomize.py 读取)。
  * 读消息优先用数据库路线(Listener/WeChatDB);数据库不可用时退化为
    「记录日志但不回复」,绝不盲目猜内容。
  * 发送走 WeChatGUI(坐标+OCR)。这条路线需要微信窗口在前台且桌面已解锁。
"""
from __future__ import annotations

import ctypes
import glob
import os
import subprocess
import sys
import threading
import time
from typing import Callable, Dict, List, Optional

from .backend import Backend
from .message import GroupMessage


# ---------------------------------------------------------------- 进程特权

class _LUID(ctypes.Structure):
    _fields_ = [("LowPart", ctypes.c_ulong), ("HighPart", ctypes.c_long)]


class _LUID_AND_ATTRIBUTES(ctypes.Structure):
    _fields_ = [("Luid", _LUID), ("Attributes", ctypes.c_ulong)]


class _TOKEN_PRIVILEGES(ctypes.Structure):
    _fields_ = [("PrivilegeCount", ctypes.c_ulong),
                ("Privileges", _LUID_AND_ATTRIBUTES * 1)]


def enable_se_debug_privilege() -> str:
    """启用当前进程的 SeDebugPrivilege。

    为什么需要:读微信这种高完整性进程的内存需要有这个特权。管理员账户
    的令牌里本来就带它,但 Windows 默认不启用,必须显式开启。

    这是标准做法(任务管理器、Process Explorer 等微软官方工具同样如此),
    不是绕过安全机制——只是在你自己账户、自己的进程上,启用你自己本就
    拥有的权限。拿不到就返回原因,由调用方决定是否继续。

    返回:人类可读的结果字符串(以 "✓" 或 "✗" 开头)。
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)

        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.OpenProcessToken.argtypes = [ctypes.c_void_p, ctypes.c_ulong,
                                         ctypes.POINTER(ctypes.c_void_p)]
        k32.OpenProcessToken.restype = ctypes.c_int
        adv.LookupPrivilegeValueW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p,
                                              ctypes.c_void_p]
        adv.LookupPrivilegeValueW.restype = ctypes.c_int
        adv.AdjustTokenPrivileges.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                              ctypes.c_void_p, ctypes.c_ulong,
                                              ctypes.c_void_p, ctypes.c_void_p]
        adv.AdjustTokenPrivileges.restype = ctypes.c_int

        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x0002

        tok = ctypes.c_void_p()
        if not k32.OpenProcessToken(k32.GetCurrentProcess(),
                                    TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                    ctypes.byref(tok)):
            return "✗ OpenProcessToken 失败(err=%d)" % ctypes.get_last_error()
        try:
            luid = _LUID()
            if not adv.LookupPrivilegeValueW(None, "SeDebugPrivilege",
                                             ctypes.byref(luid)):
                return "✗ LookupPrivilegeValue 失败(err=%d)" % ctypes.get_last_error()
            tp = _TOKEN_PRIVILEGES()
            tp.PrivilegeCount = 1
            tp.Privileges[0].Luid = luid
            tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
            ctypes.set_last_error(0)
            ok = adv.AdjustTokenPrivileges(tok, False, ctypes.byref(tp), 0, None, None)
            err = ctypes.get_last_error()
            if not ok:
                return "✗ AdjustTokenPrivileges 失败(err=%d)" % err
            if err == 1300:      # ERROR_NOT_ALL_ASSIGNED
                return ("✗ 特权未授予(err=1300)。账户/组策略没给,"
                        "或安全软件拦截了令牌修改")
            return "✓ 已启用 SeDebugPrivilege"
        finally:
            k32.CloseHandle(tok)
    except Exception as e:
        return "✗ 异常:%s: %s" % (type(e).__name__, e)


# ---------------------------------------------------------------- PID 探测

def detect_weixin_pids() -> List[int]:
    """用几种方式找微信主进程 PID,按可靠性排序。

    1. 环境变量(用户显式指定最可靠)
    2. PowerShell 脚本文件(-File 方式;在部分环境下比 -Command 可靠,
       因为后者对 Get-CimInstance 的返回可能为空)
    3. PowerShell CIM(-Command 内联)
    4. ctypes Toolhelp(旧方式,部分环境下可用)
    """
    raw = os.environ.get("WECHATAUTO_PID", "")
    pids = [int(x) for x in raw.replace(",", " ").split() if x.strip().isdigit()]
    if pids:
        return pids

    # PowerShell 脚本文件(-File 方式)
    try:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        ps1 = os.path.join(here, "get_pid.ps1")
        if os.path.exists(ps1):
            r = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                                "-File", ps1],
                               capture_output=True, text=True, timeout=30,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            # get_pid.ps1 输出形如 "1234,5678";按逗号/空白切分
            cleaned = "".join(c if (c.isdigit() or c == ",") else " "
                              for c in (r.stdout or ""))
            pids = [int(x) for x in cleaned.replace(",", " ").split() if x.strip().isdigit()]
            if pids:
                return pids
    except Exception:
        pass

    # PowerShell CIM(内联)
    try:
        cmd = ("Get-CimInstance Win32_Process -Filter \"Name='Weixin.exe'\" | "
               "Sort-Object WorkingSetSize -Descending | "
               "ForEach-Object { $_.ProcessId }")
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
                           capture_output=True, text=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pids = [int(x) for x in (r.stdout or "").split() if x.strip().isdigit()]
        if pids:
            return pids
    except Exception:
        pass

    # Toolhelp
    try:
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE

        class PE(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]

        snap = k32.CreateToolhelp32Snapshot(0x2, 0)
        if snap and snap != ctypes.c_void_p(-1).value:
            out = []
            try:
                e = PE()
                e.dwSize = ctypes.sizeof(PE)
                ok = k32.Process32FirstW(snap, ctypes.byref(e))
                while ok:
                    if e.szExeFile.lower() == "weixin.exe":
                        out.append(int(e.th32ProcessID))
                    ok = k32.Process32NextW(snap, ctypes.byref(e))
            finally:
                k32.CloseHandle(snap)
            if out:
                return out
    except Exception:
        pass
    return []


# ---------------------------------------------------------------- 后端实现

class WxBackend(Backend):
    """用 wechatauto-replica 收发群消息。

    参数
    ----
    groups       : 要监听的群名列表(显示名,和微信里看到的一致)
    poll_interval: 数据库轮询间隔(秒)
    on_log       : 日志回调
    account      : 指定微信账号目录名(多账号机器上必须给,否则可能选错)
    """

    #: 图片/语音的落盘根目录(None = 项目根)。
    #  类属性,方便测试一次性覆盖到临时目录,免得在仓库里留一堆媒体文件。
    media_cache_dir: Optional[str] = None

    @classmethod
    def _media_dir(cls, name: str) -> str:
        base = cls.media_cache_dir or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..")
        return os.path.join(base, name)

    def __init__(self, groups: List[str], poll_interval: float = 2.0,
                 on_log: Optional[Callable[[str], None]] = None,
                 account: str = "",
                 mention_names: Optional[List[str]] = None):
        self.groups = list(groups)
        self.poll_interval = poll_interval
        self.account = account
        # 被 @ 的名字。用于在把媒体正文换成 [图片]/[语音] 之前
        # 判断这条到底有没有 @ 我(否则会漏掉"@我 + 发图"这种情况)。
        self._mention_names = list(mention_names or [])
        # 图片库能力(按真实签名探测,不同版本参数不同)
        self._media_supports_shard = False
        self._media_supports_orig_shard = False
        self._log = on_log or (lambda m: print(m, flush=True))
        self.db = None
        self.gui = None
        self._media = None          # MediaDownloader,用于图片解密
        self._buffer: List[GroupMessage] = []
        self._buffer_lock = threading.Lock()
        self._watermarks: Dict[str, float] = {}
        self._self_wxid = ""
        self._nick_cache: Dict[str, str] = {}
        self._poll_thread: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._db_ok = False
        self._group_usernames: Dict[str, str] = {}

    # ---------- 启动 ----------

    def start(self) -> None:
        # ⚠ 必须清掉停止标记:否则 stop() 之后再 start(),新线程第一次
        #   判 is_set() 就直接退出,轮询永久失效(而且没有任何报错)。
        self._stop_flag.clear()
        self._prepare_env()
        self._init_db()
        self._init_gui()
        if self._db_ok:
            self._poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
            self._poll_thread.start()

    def _prepare_env(self) -> None:
        """启动前准备:启用读内存所需的特权 + 确保库能拿到 PID。"""
        # 1. SeDebugPrivilege:读微信进程内存的前提
        result = enable_se_debug_privilege()
        self._log("进程特权: %s" % result)
        if result.startswith("✗"):
            self._log(">>> 没有该特权时,读消息(数据库密钥提取)会失败;"
                      "发送不受影响。")
            self._log(">>> 若密钥提取失败,请检查:账户是否有『调试程序』权限、"
                      "是否有安全软件拦截令牌修改。")

        # 2. PID 喂给库(库靠 tasklist,部分机器上枚举不到微信)
        if not os.environ.get("WECHATAUTO_PID"):
            pids = detect_weixin_pids()
            if pids:
                os.environ["WECHATAUTO_PID"] = ",".join(str(p) for p in pids)
                self._log("已探测到微信 PID: %s" % pids)
            else:
                self._log("警告: 没探测到 Weixin.exe 进程(微信没登录?)")

    def _init_db(self) -> None:
        try:
            from wechatauto import WeChatDB, list_accounts
        except Exception as e:
            self._log("导入 wechatauto 失败: %r" % (e,))
            return

        # 多账号机器上必须显式指定,否则库可能挑到未登录的那个账号
        account = self.account
        if not account:
            try:
                accts = [a.get("account") for a in list_accounts() if a.get("account")]
            except Exception:
                accts = []
            if len(accts) > 1:
                self._log("检测到 %d 个微信账号: %s" % (len(accts), accts))
                self._log(">>> 未配置 wechat_account,库可能选错账号。"
                          "请在 config.json 里指定 wechat_account。")
                # 挑最近的(通常就是刚登录的)
                try:
                    data = list_accounts()
                    data.sort(key=lambda a: a.get("last_activity") or 0, reverse=True)
                    account = data[0].get("account") if data else ""
                    self._log("自动选择最近活跃的账号: %s" % account)
                except Exception:
                    account = accts[0]

        try:
            self.db = WeChatDB(account=account) if account else WeChatDB()
            self._self_wxid = getattr(self.db, "wxid", "") or ""
            self._log("数据库已就绪,账号=%s (wxid=%s)"
                      % (getattr(self.db, "account", "?"), self._self_wxid))
        except Exception as e:
            self._log("数据库不可用: %s" % e)

        # 图片下载器(视觉功能用)
        try:
            from wechatauto import MediaDownloader
            self._media = MediaDownloader(self.db)
            self._log("图片通道已就绪(MediaDownloader)")
            # 探测库版本能力:1.2.6.1 的 download_image 支持 shard,
            # 1.2.4.4 不支持 —— 传了会抛 TypeError,图片就永远下不到。
            try:
                import inspect as _inspect
                _sig = _inspect.signature(self._media.download_image)
                self._media_supports_shard = "shard" in _sig.parameters
                _sig2 = _inspect.signature(self._media.download_image_original)
                self._media_supports_orig_shard = "shard" in _sig2.parameters
            except Exception:
                self._media_supports_shard = False
                self._media_supports_orig_shard = False
            try:
                import wechatauto as _wa
                _ver = getattr(_wa, "__version__", "?")
            except Exception:
                _ver = "?"
            self._log("图片库版本 %s (支持 shard 参数: %s)"
                      % (_ver, "是" if self._media_supports_shard else "否"))
        except Exception as e:
            self._media = None
            self._log("图片通道不可用(不影响文字朗读与发送): %r" % (e,))
            # ⚠ 以前这里直接 return,会连"群名映射 + _db_ok=True"一起跳过,
            #   结果数据库明明可用,机器人却一个字都读不到。绝不能 return。

        # 群名 -> username
        # 注意:get_groups() 返回的字段是 username(群 id),不是 wxid
        try:
            if self.db is None:
                raise RuntimeError("数据库对象不存在")
            for g in self.db.get_groups():
                if not isinstance(g, dict):
                    continue
                nm = g.get("name")
                gid = g.get("username") or g.get("wxid") or g.get("chatroom_wxid")
                if nm and gid:
                    self._group_usernames[nm] = gid
            self._log("已映射 %d 个群" % len(self._group_usernames))
            missing = [g for g in self.groups if g not in self._group_usernames]
            if missing:
                self._log("警告: 以下群名没匹配到,可能是名字写错了: %s" % missing)
                self._log("  本机群名: %s"
                          % ", ".join(list(self._group_usernames)[:15]))
        except Exception as e:
            self._log("枚举群列表失败: %r" % (e,))

        # 只有"数据库在 + 群名能映射出来"才算读消息能力就绪。
        # 以前无条件置 True,会让轮询线程空转 —— 表面在跑、永远不回。
        self._db_ok = self.db is not None and bool(self._group_usernames)
        if not self._db_ok:
            self._log(">>> 读消息能力未就绪(数据库=%s, 群映射=%d)"
                      % ("有" if self.db is not None else "无",
                         len(self._group_usernames)))

    def _init_gui(self) -> None:
        try:
            from wechatauto.guia import WeChatGUI
            self.gui = WeChatGUI()
            self._log("发送通道已就绪(WeChatGUI)")
        except Exception as e:
            self._log("发送通道初始化失败: %r" % (e,))
            self.gui = None

    # ---------- 轮询 ----------

    def _poll_loop(self) -> None:
        while not self._stop_flag.is_set():
            try:
                self._poll_once()
            except Exception as e:
                self._log("轮询出错: %r" % (e,))
            self._stop_flag.wait(self.poll_interval)

    #: 单次轮询最多取多少条。比"一次真正会积压的量"大得多即可。
    _POLL_LIMIT = 300

    def _poll_once(self) -> None:
        for name in self.groups:
            username = self._group_usernames.get(name)
            if not username:
                continue
            since = self._watermarks.get(username, 0.0)
            try:
                msgs = self.db.get_messages(username, limit=self._POLL_LIMIT)
            except Exception as e:
                self._log("读「%s」消息失败: %r" % (name, e))
                continue
            if not msgs:
                continue

            # ⚠ 只能推进到「本条真正见过的最新时间」，而且要看有没有截断。
            #   老实现固定 limit=30 又把水位直接推到返回集的最大值：
            #   两次轮询之间来了 >30 条(活跃群 + WAL 合并失败时单查询要 10~30 秒)
            #   中间那些消息会被永久跳过，再也不会回。
            def _ct(m):
                try:
                    return float(m.get("create_time") or 0)
                except (TypeError, ValueError):
                    return 0.0

            times = [_ct(m) for m in msgs]
            latest = max(times) if times else 0.0

            # 首次只建水位,不回复历史消息(避免上线就把历史刷一遍)
            if since == 0.0:
                self._watermarks[username] = latest
                self._log("「%s」水位初始化于 %s(不回历史消息)" % (name, latest))
                continue

            fresh = [m for m in msgs if _ct(m) > since]
            if not fresh:
                continue

            # 返回集已经顶到 limit ⇒ 更早的可能被截断。先把水位推到一个
            # "安全点"(本批最旧那条之前),下一轮继续往前追,直到不再截断。
            truncated = len(msgs) >= self._POLL_LIMIT
            if truncated:
                oldest_in_batch = min(t for t in times if t > since) \
                    if any(t > since for t in times) else since
                advance_to = max(since, oldest_in_batch - 0.001)
            else:
                advance_to = latest
            self._watermarks[username] = advance_to

            for m in sorted(fresh, key=_ct):
                gm = self._to_group_message(name, m)
                if gm is not None:
                    with self._buffer_lock:
                        self._buffer.append(gm)

    def ensure_images(self, messages=None) -> int:
        """把图片消息的图片下载下来,并把路径写回这些消息对象。

        重要:必须接收外部传入的消息列表。
        因为 poll() 会把内部 _buffer 清空,如果只从 _buffer 里找,
        这里永远看到空列表 —— 图片就再也不会被下载(踩过这个坑)。

        参数
        ----
        messages : 要处理的 GroupMessage 列表。不传则退化为看内部缓冲
                   (仅在调用方还没 poll 时有效)。

        返回成功下载的张数。
        """
        if self._media is None:
            return 0

        if messages is None:
            with self._buffer_lock:
                messages = list(self._buffer)

        pending = [m for m in messages
                   if getattr(m, "is_image_msg", False)
                   and not m.image_path and not m.is_self]
        done = 0
        for gm in pending:
            row = {
                "local_id": gm.msg_id,
                "shard": gm.shard or None,
            }
            path = self._fetch_image(gm.group, row)
            if path:
                gm.image_path = path
                done += 1
        return done

    def _to_group_message(self, group_name: str, row: dict) -> Optional[GroupMessage]:
        content = row.get("content") or ""
        sender = (row.get("sender_name") or row.get("sender_username")
                  or row.get("sender") or "")
        sender_id = row.get("sender_username") or ""
        # 群消息正文常带 "wxid_xxx:" 前缀,剥掉
        if ":" in content[:40] and content.lstrip().startswith("wxid_"):
            head, _, tail = content.partition(":")
            content = tail.strip()
        is_self = bool(sender_id) and bool(self._self_wxid) and sender_id == self._self_wxid

        # ⚠ 必须在把正文换成 "[图片]"/"[语音]" **之前**判断有没有 @ 我。
        #   否则媒体消息的 @ 信息被抹掉,又没人填 mentioned_me,
        #   就认不出"被点名发图/发语音"了。
        mentioned = False
        try:
            names = getattr(self, "_mention_names", None) or []
            if names and not is_self:
                from .message import is_at_me
                mentioned = is_at_me(content, names)
        except Exception:
            mentioned = False

        msg_type = str(row.get("type") or "")
        # 语音/图片等媒体消息的正文是一坨 XML,直接喂给模型会被当成乱码问题。
        # 统一换成占位文字,真正的理解交给语音识别 / 视觉。
        if "语音" in msg_type:
            content = "[语音]"
        elif "图片" in msg_type:
            content = "[图片]"
        gm = GroupMessage(
            group=group_name,
            sender="我" if is_self else (sender or "群友"),
            content=content,
            timestamp=float(row.get("create_time") or time.time()),
            msg_id=str(row.get("local_id") or ""),
            is_self=is_self,
            sender_id=sender_id,
            msg_type=msg_type,
            shard=str(row.get("shard") or ""),
            mentioned_me=mentioned,
        )
        # 图片不在这里下载:留给 ensure_images() 在合适的时机批量处理,
        # 避免"消息刚入库、图片还没落盘"时白跑一次。
        # 语音同理:留给 ensure_voices() 处理。
        return gm

    def ensure_voices(self, messages=None) -> int:
        """把语音消息的 SILK 音频取下来,写回消息对象。

        参数必须传外部消息列表:poll() 会清空内部缓冲(和图片同一个坑)。
        """
        if self._media is None:
            return 0
        if messages is None:
            with self._buffer_lock:
                messages = list(self._buffer)

        pending = [m for m in messages
                   if getattr(m, "is_voice_msg", False)
                   and not m.voice_path and not m.is_self]
        done = 0
        for gm in pending:
            path = self.fetch_voice(gm.group, gm.msg_id)
            if path:
                gm.voice_path = path
                done += 1
        return done

    def _download_image(self, username: str, local_id: int, save_dir: str,
                        tier: str, shard: Optional[str] = None) -> Optional[str]:
        """调用 download_image,自动适配不同版本库的参数差异。

        ⚠ 踩过的坑:1.2.6.1 有 `shard` 参数,而 1.2.4.4 **没有** ——
        直接传 shard 会抛 TypeError,图片就永远下载不到(而且只在异常里体现,
        表面看只是"图片取不到")。所以按真实签名决定要不要传。
        """
        kwargs = {"save_dir": save_dir}
        if tier:
            kwargs["tier"] = tier
        if shard and self._media_supports_shard:
            kwargs["shard"] = shard
        return self._media.download_image(username, int(local_id), **kwargs)

    def _fetch_image(self, group_name: str, row: dict,
                     retries: int = 6, delay: float = 1.2,
                     only_clear: bool = False) -> Optional[str]:
        """下载并解密一条图片消息,返回本地图片路径。失败返回 None。

        only_clear=True 时只接受清晰图(原图/压缩版),拿不到就返回 None
        —— 用于"把缩略图升级成清晰图"的场景,避免又把缩略图当结果。

        重试次数给得比较多:刚发的图微信要过几秒才下载到本地,
        早点重试往往能从缩略图变成清晰的压缩版(作业题能看清)。
        """
        if self._media is None:
            return None
        username = self._group_usernames.get(group_name)
        if not username:
            return None
        local_id = row.get("local_id")
        if local_id is None:
            return None

        save_dir = self._media_dir("_images")
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception:
            pass

        # 微信写盘比判定晚:刚到的图可能还没落盘,所以重试几次
        last_err = None
        tries = 5 if only_clear else max(retries, 1)
        for attempt in range(tries):
            try:
                # tier='best' = 原图 > 压缩版 > 缩略图
                path = self._download_image(
                    username, local_id, save_dir, "best",
                    shard=row.get("shard") or None)
                if path and os.path.exists(path):
                    good = "_thumb" not in path          # 不是缩略图才算拿到清晰图
                    if good or not only_clear:
                        size_kb = os.path.getsize(path) // 1024
                        tier_name = ("原图" if "_h." in path
                                     else "缩略图" if "_thumb" in path else "压缩版")
                        self._log("已获取图片(%s, %d KB): %s"
                                  % (tier_name, size_kb, os.path.basename(path)))
                        return path
                    # 只要清晰图、但本机当前只有缩略图 -> 再等等,微信可能正在下载
            except Exception as e:
                last_err = e
            if attempt < tries - 1:
                time.sleep(delay)

        if only_clear:
            # 明确要求清晰图时不要拿缩略图糊弄调用方
            self._log("暂无清晰图片(local_id=%s, 微信可能还没下载完)" % local_id)
            return None

        # 本机没有原图/压缩版时,只用缩略图会看不清作业内容。
        # 这里不再驱动界面去下载原件 —— 那会操作微信窗口(右键/滚轮/点对话框),
        # 对后台运行的机器人风险太大,可能干扰用户正在用的微信。
        # 折中:退回缩略图并明确标注,让上层/用户知道画质有限。
        try:
            for attempt in range(retries if retries > 0 else 3):
                path = self._download_image(
                    username, local_id, save_dir, "thumb",
                    shard=row.get("shard") or None)
                if path and os.path.exists(path):
                    self._log("⚠ 只有缩略图可用(%d KB),作业类内容可能看不清。"
                              "稍后会自动重试拿清晰版。"
                              % (os.path.getsize(path) // 1024))
                    return path
                if attempt < (retries if retries > 0 else 3) - 1:
                    time.sleep(1.5)
        except Exception as e:
            last_err = e

        self._log("图片取不到(local_id=%s): %s" % (local_id, last_err or "本机无可用档位"))
        return None

    def recent_image_paths(self, group_name: str, sender_id: str,
                           lookback_seconds: float = 600.0,
                           limit: int = 4) -> List[str]:
        """直接从数据库捞某人最近发过的图片并下载好。

        为什么需要它:机器人重启后水位会跳到"最新",之前发的图被当成历史跳过;
        于是会出现"你明明发了图,它却说图是空的"。这里绕过水位,直接查库兜底。
        """
        if self._media is None:
            return []
        gid = self._group_usernames.get(group_name)
        if not gid:
            return []
        out: List[str] = []
        try:
            msgs = self.db.get_messages(gid, limit=60)
        except Exception as e:
            self._log("补捞图片:读消息失败 %r" % (e,))
            return []
        now = time.time()
        for row in msgs:                     # get_messages 是 sort_seq 降序,msgs[0] 最新
            typ = str(row.get("type") or "")
            if "图片" not in typ:
                continue
            try:
                ts = float(row.get("create_time") or 0)
            except (TypeError, ValueError):
                continue
            if lookback_seconds and (now - ts) > lookback_seconds:
                continue
            # 只收提问者自己发的(群消息正文带 wxid 前缀)
            content = str(row.get("content") or "")
            if sender_id and content.startswith("wxid_"):
                head = content.split(":", 1)[0].strip()
                if head and head != sender_id:
                    continue
            path = self._fetch_image(group_name, {
                "local_id": row.get("local_id"),
                "shard": row.get("shard") or None,
            })
            if path and os.path.exists(path):
                out.append(path)
                if len(out) >= limit:
                    break
        return out

    def upgrade_image(self, gm) -> bool:
        """把已经拿到的缩略图升级成清晰版(微信稍后才下载完时用)。

        返回 True 表示成功换成了清晰图。
        """
        if self._media is None or not gm.image_path:
            return False
        if gm.is_self:
            return False
        if "_thumb" not in gm.image_path:
            return False                     # 已经是清晰图
        row = {"local_id": gm.msg_id, "shard": gm.shard or None}
        path = self._fetch_image(gm.group, row, retries=6, delay=1.0,
                                 only_clear=True)
        if path and os.path.exists(path):
            old = gm.image_path
            gm.image_path = path
            self._log("缩略图已升级为清晰图: %s" % os.path.basename(path))
            try:
                if old != path and os.path.exists(old):
                    os.remove(old)
            except OSError:
                pass
            return True
        return False

    # ---------- 语音 ----------

    def fetch_voice(self, group_name: str, msg_id: str,
                    retries: int = 4, delay: float = 1.5) -> Optional[str]:
        """取一条语音的 SILK 音频文件,返回本地路径。

        微信把语音存在 media_*.db 的 VoiceInfo.voice_data 里(SILK 二进制)。
        ⚠ 关键:微信是**延迟下载**语音的 —— 刚收到的语音,音频常要 30~60 秒
        才写进 VoiceInfo。所以这里只做**短重试**(不阻塞太久),
        真正兜底的是 bot 里的"语音补试队列"(_retry_pending_voices):
        拿不到就排队,之后每轮 tick 再试,直到拿到或超时。
        """
        if self._media is None:
            return None
        username = self._group_usernames.get(group_name)
        if not username or not msg_id:
            return None

        save_dir = self._media_dir("_voices")
        try:
            os.makedirs(save_dir, exist_ok=True)
        except Exception:
            pass

        last_reason = ""
        tries = max(retries, 1)
        for attempt in range(tries):
            try:
                path = self._media.download_voice(username, int(msg_id),
                                                  save_dir=save_dir)
                if path and os.path.exists(path):
                    size_kb = os.path.getsize(path) / 1024.0
                    extra = ("（第 %d 次尝试才拿到）" % (attempt + 1)
                             if attempt else "")
                    self._log("已获取语音(%.1f KB)%s: %s"
                              % (size_kb, extra, os.path.basename(path)))
                    return path
                # 拿不到:看是"微信还没下载"还是"真的没有"
                try:
                    st = self._media.voice_status(username, int(msg_id)) or {}
                    last_reason = str(st.get('reason') or '')
                except Exception:
                    last_reason = "unknown"
                # 只有"还没下载完"才值得等;真没有就不用重试了
                if last_reason not in ("audio_missing_from_media_db",
                                       "audio_not_downloaded", ""):
                    self._log("语音 id=%s 确定没有音频(reason=%s),放弃"
                              % (msg_id, last_reason))
                    break
            except Exception as e:
                last_reason = repr(e)
                break
            if attempt < tries - 1:
                time.sleep(delay)

        self._log("语音取不到(id=%s, 试了 %d 次, 最后原因=%s)"
                  % (msg_id, tries, last_reason or "未知"))
        return None

    def recent_voice_ids(self, group_name: str, sender_id: str,
                         lookback_seconds: float = 600.0,
                         limit: int = 3) -> List[str]:
        """从数据库捞某人最近发过的语音消息 id(绕过水位,供重启后补捞)。"""
        gid = self._group_usernames.get(group_name)
        if not gid:
            return []
        out: List[str] = []
        try:
            msgs = self.db.get_messages(gid, limit=60)
        except Exception:
            return []
        now = time.time()
        for row in msgs:                     # 同理:降序,最新在前
            typ = str(row.get("type") or "")
            if "语音" not in typ:
                continue
            try:
                ts = float(row.get("create_time") or 0)
            except (TypeError, ValueError):
                continue
            if lookback_seconds and (now - ts) > lookback_seconds:
                continue
            content = str(row.get("content") or "")
            if sender_id and content.startswith("wxid_"):
                head = content.split(":", 1)[0].strip()
                if head and head != sender_id:
                    continue
            lid = row.get("local_id")
            if lid is not None:
                out.append(str(lid))
                if len(out) >= limit:
                    break
        return out

    # ---------- Backend 接口 ----------

    def poll(self) -> List[GroupMessage]:
        with self._buffer_lock:
            out = self._buffer[:]
            self._buffer.clear()
        return out

    def send(self, group: str, text: str) -> bool:
        if self.gui is None:
            self._log("没有可用的发送通道")
            return False
        try:
            from wechatauto.guia import quick_send
            # verify=False:数据库可能不可用,回读校验会失败;发送本身不受影响
            res = quick_send(text, group, verify=False)
            # ⚠ WxResponse 是 dict 子类,status 是**键**不是属性:
            #   hasattr(res, "status") 恒为 False ⇒ 以前的判定永远返回 True,
            #   发送失败被当成成功,熔断(consecutive_failures)永远不增长。
            #   它自带 __bool__,读 status=='成功',直接用。
            if res is None:
                return True                      # 老版本不返回东西,只能当成功
            try:
                ok = bool(res)
            except Exception:
                ok = True
            if not ok:
                msg = ""
                try:
                    msg = str(res.get("message") or res.get("status") or "")
                except Exception:
                    pass
                self._log("发送被库拒绝: %s" % (msg or res))
            return ok
        except Exception as e:
            self._log("发送异常: %r" % (e,))
            return False

    def recent(self, group: str, limit: int = 15) -> List[GroupMessage]:
        username = self._group_usernames.get(group)
        if not self._db_ok or not username:
            return []
        try:
            rows = self.db.get_messages(username, limit=limit)
        except Exception:
            return []
        out = []
        for r in sorted(rows, key=lambda x: x.get("create_time") or 0):
            gm = self._to_group_message(group, r)
            if gm:
                out.append(gm)
        return out

    def stop(self) -> None:
        self._stop_flag.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=5)
