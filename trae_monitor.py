# -*- coding: utf-8 -*-
"""
TRAE 工作状态监测模块

状态判定逻辑:
  红  (red)    : 未检测到 TRAE 进程 (TRAE Work / TRAE IDE 未运行)
  绿  (green)  : TRAE 正在运行, 且 AI agent 处于空闲 (日志写入速率低)
  黄  (yellow) : TRAE 正在运行, 且 AI agent 正在工作 (日志写入速率高)

“工作中” 信号来源: TRAE 的 ai-agent 进程会把执行过程写到
  <TRAE数据目录>/logs/<时间戳>/Modular/ai-agent_*_stdout.log

关键发现: TRAE 后台即使空闲也会持续低频写日志 (约 1-5 行/秒),
所以单看文件修改时间无法区分工作/空闲 —— 会一直显示工作中。
因此改为按 “日志写入速率” 判定:
  - 活跃工作: 约 20-33 行/秒 (调用工具、生成响应)
  - 空闲/后台: 约 1-5 行/秒 (心跳、状态轮询)
用最近 RATE_WINDOW 秒内的行数做滞回判定, 工作结束后静默超过 WORK_GRACE 秒回绿。
目前Trae IDE暂时还无法使用，可以使用Trae Work。
"""

from __future__ import annotations

import ctypes
import os
import re
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

try:
    import psutil  # type: ignore
    _HAS_PSUTIL = True
except Exception:
    psutil = None  # type: ignore
    _HAS_PSUTIL = False


_kernel32 = ctypes.windll.kernel32
_psapi = ctypes.windll.psapi
TH32CS_SNAPPROCESS = 0x00000002
_PROCESS_QUERY_INFORMATION = 0x0400
_PROCESS_VM_READ = 0x0010


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


class _PROCESS_MEMORY_COUNTERS(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("PageFaultCount", wintypes.DWORD),
        ("PeakWorkingSetSize", ctypes.c_size_t),
        ("WorkingSetSize", ctypes.c_size_t),
        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPagedPoolUsage", ctypes.c_size_t),
        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
        ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
        ("PagefileUsage", ctypes.c_size_t),
        ("PeakPagefileUsage", ctypes.c_size_t),
    ]


def _enum_processes() -> List[Tuple[int, str]]:
    out: List[Tuple[int, str]] = []
    snap = _kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap:
        return out
    try:
        pe = _PROCESSENTRY32W()
        pe.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
        if _kernel32.Process32FirstW(snap, ctypes.byref(pe)):
            while True:
                out.append((pe.th32ProcessID, pe.szExeFile))
                if not _kernel32.Process32NextW(snap, ctypes.byref(pe)):
                    break
    finally:
        _kernel32.CloseHandle(snap)
    return out


def _mem_mb_for_pid(pid: int) -> float:
    h = _kernel32.OpenProcess(_PROCESS_QUERY_INFORMATION | _PROCESS_VM_READ, False, pid)
    if not h:
        return 0.0
    try:
        pmc = _PROCESS_MEMORY_COUNTERS()
        pmc.cb = ctypes.sizeof(pmc)
        if _psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
            return pmc.WorkingSetSize / (1024.0 * 1024.0)
        return 0.0
    finally:
        _kernel32.CloseHandle(h)



class _CpuSampler:
    _last_idle = _last_kernel = _last_user = 0.0
    _has_base = False

    @classmethod
    def sample(cls) -> float:
        # 优先 psutil (非阻塞, 需已初始化基准)
        if _HAS_PSUTIL:
            try:
                # 触发 psutil 初始化基准 (首次返回 0 但会建立基准)
                psutil.cpu_percent(interval=None)
            except Exception:
                pass
        # 用 GetSystemTimes 计算 (可靠, 跨调用)
        idle = wintypes.FILETIME()
        kernel = wintypes.FILETIME()
        user = wintypes.FILETIME()
        if not _kernel32.GetSystemTimeAsFileTime(ctypes.byref(idle)) and False:
            pass
        if _kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
            i = float(idle.dwHighDateTime << 32 | idle.dwLowDateTime) / 1e7
            k = float(kernel.dwHighDateTime << 32 | kernel.dwLowDateTime) / 1e7
            u = float(user.dwHighDateTime << 32 | user.dwLowDateTime) / 1e7
            if cls._has_base:
                dt_idle = i - cls._last_idle
                dt_kernel = k - cls._last_kernel
                dt_user = u - cls._last_user
                dt_total = dt_kernel + dt_user
                if dt_total > 0:
                    busy = dt_total - dt_idle
                    pct = max(0.0, min(100.0, busy / dt_total * 100.0))
                    cls._last_idle, cls._last_kernel, cls._last_user = i, k, u
                    return round(pct, 1)
            cls._last_idle, cls._last_kernel, cls._last_user = i, k, u
            cls._has_base = True
        return 0.0

# ---------------------------------------------------------------------------
# 可调参数
# ---------------------------------------------------------------------------
# 主信号: 距上一条 “工作关键词行” 的时间。<= WORK_ACTIVE_THRESH => 正在工作。
# (工作关键词行: invoke_via_toolhost / run_execution_task / do_chat /
#   call_server_generate / process_ipc_request / Tool call 等, 空闲时不会出现)
WORK_ACTIVE_THRESH = 6.0
# 副信号: 最近 RATE_WINDOW 秒日志行数 (高速率也判为工作, 用于快速触发)
RATE_WINDOW = 3.0
ENTER_RATE = 22
# 进入工作后, 非活跃持续 WORK_GRACE 秒才回空闲 (覆盖工具间/思考短暂间隙)
WORK_GRACE = 3.0

# 进程名 (小写) 命中以下任一子串 => 视为 TRAE 进程
TRAE_PROCESS_PATTERNS = ("trae solo", "trae")

APPDATA = os.environ.get("APPDATA", "")
LOCALAPPDATA = os.environ.get("LOCALAPPDATA", "")

# 用于解析日志行
_RE_COMMAND = re.compile(r'command_id="([^"]+)"')
_RE_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:[+-]\d{2}:\d{2}|Z)?)")
_RE_SESSION = re.compile(r"session_id=([0-9a-f]{6,})")
_RE_TASK = re.compile(r"task_id=([0-9a-f]{6,})")

# 视为“主动工作”的关键词 (用于最近事件提取/展示)
_WORK_KEYWORDS = (
    "do_chat", "run_execution_task", "call_server_generate", "generate_plan",
    "invoke_via_toolhost", "Tool call", "ExecuteCommand", "emit_intermediate",
    "plan_item", "process_ipc_request",
)

# 判定“真实工作中”所用的精确标记: 只有真正执行工具/生成的日志行才算。
# (do_chat / process_ipc_request / run_execution_task 这些链路标签,
#  轮询命令 checkRunCommandStatus 也会产生, 无法区分, 故不作为真实工作信号。)
_REAL_WORK_MARKERS = (
    "invoke_via_toolhost",          # 实际调用工具
    "Tool call completed",          # 工具调用完成
    "Tool call",                    # 工具调用
    "ExecuteCommand request",       # 执行命令请求
    "call_server_generate_plan",    # 调用服务端生成
    "emit_intermediate",            # 生成中间输出
    "generate_plan_item",           # 生成计划项
)

# 轮询/后台心跳类命令 —— 它们也走 process_ipc_request 链路、命中工作关键词,
# 但不是用户触发的真实任务。判定“工作中”时排除这些 command_id。
_POLL_COMMANDS = {
    "checkruncommandstatus",     # 轮询命令执行状态 (最多)
    "getappprivacymode",         # 查隐私模式
    "getdynamicconfig",          # 查动态配置
    "getautorunconfig",          # 查自动运行配置
    "getsolomtcautorunconfig",
    "getconfigurationvalue",     # 查配置值
    "getrulesdetails",           # 查规则
    "getallagentextensions",     # 查 agent 扩展
    "getabtestconfigbykey",      # AB 实验
    "getproductdatafolderpath",  # 查数据目录
    "batch_get_model_detail_param",
    "listfolder",                # 列目录
    "getfilecontent",            # 读文件 (轮询常用)
    "getstat",
}
_RE_CMD_ID = re.compile(r'command_id="([^"]+)"')


@dataclass
class TraeEvent:
    ts: str = ""          # HH:MM:SS
    command: str = ""     # 简短命令名


@dataclass
class TraeStatus:
    state: str = "red"            # red / green / yellow
    running: bool = False
    product: str = ""            # TRAE Work / TRAE IDE / TRAE
    process_count: int = 0
    main_pid: Optional[int] = None
    main_name: str = ""
    total_mem_mb: float = 0.0
    sys_cpu: float = 0.0
    log_path: str = ""
    last_activity_ts: Optional[float] = None    # epoch
    last_activity_ago: str = ""                # "3秒前"
    events: List[TraeEvent] = field(default_factory=list)
    session_id: str = ""
    task_id: str = ""
    reason: str = ""             # 判定原因 (用于悬停面板)

    @property
    def state_label(self) -> str:
        return {
            "red": "未运行",
            "green": "空闲",
            "yellow": "工作中",
        }.get(self.state, self.state)


class TraeMonitor:
    """TRAE 状态监测器。调用 refresh() 返回最新 TraeStatus。"""

    def __init__(self) -> None:
        self._in_work = False
        self._low_since: Optional[float] = None   # 低速率开始的时刻
        self._cached_log: Optional[str] = None
        self._cache_tick = 0

    # ------------------------------------------------------------------ public
    def refresh(self) -> TraeStatus:
        st = TraeStatus()
        procs = self._find_trae_processes()
        st.running = bool(procs)
        st.process_count = len(procs)
        st.product = self._detect_product(procs)
        if procs:
            main = max(procs, key=lambda p: (p["mem"] or 0))
            st.main_pid = main["pid"]
            st.main_name = main["name"]
            st.total_mem_mb = round(sum(p["mem"] or 0 for p in procs), 1)
        try:
            st.sys_cpu = _CpuSampler.sample()
        except Exception:
            st.sys_cpu = 0.0

        mtime, logpath = self._find_active_log()
        if logpath:
            st.log_path = logpath
            st.last_activity_ts = mtime
            st.last_activity_ago = self._ago(mtime)
            evs, sid, tid = self._parse_recent(logpath)
            st.events = evs
            st.session_id = sid
            st.task_id = tid

        # ---- 状态判定: 工作关键词行新鲜度 (主) + 日志速率 (副), 含滞回 ----
        if not procs:
            st.state = "red"
            st.reason = "未检测到 TRAE 进程"
            self._in_work = False
            self._low_since = None
        else:
            now = time.time()
            rate = self._recent_rate(logpath, now)
            work_age = self._work_age(logpath, now)
            # 活跃 = 最近有工作关键词行, 或日志高速写入
            active = (work_age <= WORK_ACTIVE_THRESH) or (rate >= ENTER_RATE)
            if active:
                self._in_work = True
                self._low_since = None
            elif self._in_work:
                if self._low_since is None:
                    self._low_since = now
                if (now - self._low_since) >= WORK_GRACE:
                    self._in_work = False
            # 空闲中且非活跃 => 保持 green

            if self._in_work:
                st.state = "yellow"
                st.reason = (f"AI agent 正在工作 (工作行 {int(work_age)}s前, "
                             f"{int(rate)} 行/{int(RATE_WINDOW)}s)")
            else:
                st.state = "green"
                st.reason = "TRAE 运行中, 空闲"
        return st

    # ------------------------------------------------------------------ process
    def _find_trae_processes(self) -> List[dict]:
        out: List[dict] = []
        if _HAS_PSUTIL:
            for p in psutil.process_iter(attrs=["pid", "name", "memory_info"]):
                try:
                    name = (p.info["name"] or "")
                except Exception:
                    continue
                if not self._is_trae(name):
                    continue
                mi = p.info.get("memory_info")
                mem = mi.rss / (1024 * 1024) if mi else 0.0
                out.append({"pid": p.info["pid"], "name": name, "mem": mem})
        else:
            for pid, name in _enum_processes():
                if not self._is_trae(name):
                    continue
                out.append({"pid": pid, "name": name, "mem": _mem_mb_for_pid(pid)})
        return out

    @staticmethod
    def _is_trae(name: str) -> bool:
        low = name.lower()
        return any(pat in low for pat in TRAE_PROCESS_PATTERNS)

    @staticmethod
    def _detect_product(procs: List[dict]) -> str:
        for p in procs:
            n = p["name"].lower()
            if "solo" in n:
                return "TRAE Work"
            if "ide" in n:
                return "TRAE IDE"
        return "TRAE"

    # ------------------------------------------------------------------ logs
    def _find_log_roots(self) -> List[str]:
        roots: List[str] = []
        seen = set()
        for base in (APPDATA, LOCALAPPDATA):
            if not base or not os.path.isdir(base):
                continue
            try:
                for entry in os.listdir(base):
                    if "trae" not in entry.lower():
                        continue
                    ld = os.path.join(base, entry, "logs")
                    if os.path.isdir(ld) and ld not in seen:
                        seen.add(ld)
                        roots.append(ld)
            except OSError:
                continue
        return roots

    def _find_active_log(self) -> Tuple[Optional[float], Optional[str]]:
        # 优先用缓存路径快速取 mtime; 每隔若干 tick 重新扫描以发现新会话目录
        self._cache_tick += 1
        rescan = (self._cache_tick % 10 == 0) or not self._cached_log
        if not rescan and self._cached_log:
            try:
                return os.path.getmtime(self._cached_log), self._cached_log
            except OSError:
                self._cached_log = None

        best: Tuple[Optional[float], Optional[str]] = (None, None)
        for root in self._find_log_roots():
            try:
                for tsf in os.listdir(root):
                    moddir = os.path.join(root, tsf, "Modular")
                    if not os.path.isdir(moddir):
                        continue
                    for f in os.listdir(moddir):
                        if f.startswith("ai-agent_") and f.endswith("_stdout.log"):
                            p = os.path.join(moddir, f)
                            try:
                                mt = os.path.getmtime(p)
                            except OSError:
                                continue
                            if best[0] is None or mt > best[0]:
                                best = (mt, p)
            except OSError:
                continue
        if best[1]:
            self._cached_log = best[1]
        return best

    def _recent_rate(self, logpath: Optional[str], now: float) -> int:
        """统计最近 RATE_WINDOW 秒内的日志行数 (写入速率的代理指标)。"""
        if not logpath:
            return 0
        try:
            size = os.path.getsize(logpath)
            with open(logpath, "rb") as fh:
                fh.seek(max(0, size - 32768))
                data = fh.read()
        except OSError:
            return 0
        text = data.decode("utf-8", errors="replace")
        # 丢弃 seek 起始处可能被截断的首行
        if size > 32768 and "\n" in text:
            text = text.split("\n", 1)[1]
        cutoff = now - RATE_WINDOW
        cnt = 0
        for line in text.splitlines():
            m = _RE_TS.match(line)
            if not m:
                continue
            ts = self._parse_ts(m.group(1))
            if ts is not None and ts >= cutoff:
                cnt += 1
        return cnt

    @staticmethod
    def _parse_ts(s: str) -> Optional[float]:
        try:
            if s.endswith("Z"):
                s = s[:-1] + "+00:00"
            return datetime.fromisoformat(s).timestamp()
        except Exception:
            return None

    def _work_age(self, logpath: Optional[str], now: float) -> float:
        """距上一条 “真实工作行” 的秒数。无则返回一个大数。

        真实工作行 = 含 _REAL_WORK_MARKERS 之一 (实际执行工具/生成的标记)。
        这些标记只在我真正调用工具/生成回复时出现, 轮询心跳不会产生。
        """
        if not logpath:
            return 1e9
        try:
            size = os.path.getsize(logpath)
            with open(logpath, "rb") as fh:
                fh.seek(max(0, size - 131072))
                data = fh.read()
        except OSError:
            return 1e9
        text = data.decode("utf-8", errors="replace")
        if size > 131072 and "\n" in text:
            text = text.split("\n", 1)[1]
        # 从末尾向前找最近一条真实工作行
        for line in reversed(text.splitlines()):
            if not any(mk in line for mk in _REAL_WORK_MARKERS):
                continue
            m = _RE_TS.match(line)
            if not m:
                continue
            ts = self._parse_ts(m.group(1))
            if ts is not None:
                return max(0.0, now - ts)
        return 1e9

    def _parse_recent(self, logpath: str) -> Tuple[List[TraeEvent], str, str]:
        try:
            size = os.path.getsize(logpath)
            with open(logpath, "rb") as fh:
                fh.seek(max(0, size - 16384))
                data = fh.read()
            lines = data.decode("utf-8", errors="replace").splitlines()[-60:]
        except OSError:
            return [], "", ""

        events: List[TraeEvent] = []
        last_sid = last_tid = ""
        for line in lines:
            mc = _RE_COMMAND.search(line)
            if mc:
                full = mc.group(1)
                short = full.rsplit(".", 1)[-1]
                mt = _RE_TS.match(line)
                ts = mt.group(1)[11:19] if mt and len(mt.group(1)) >= 19 else ""
                events.append(TraeEvent(ts=ts, command=short))
            ms = _RE_SESSION.search(line)
            if ms:
                last_sid = ms.group(1)
            mt2 = _RE_TASK.search(line)
            if mt2:
                last_tid = mt2.group(1)

        # 去除连续重复
        dedup: List[TraeEvent] = []
        for e in events:
            if not dedup or dedup[-1].command != e.command:
                dedup.append(e)
        return dedup[-6:], last_sid, last_tid

    # ------------------------------------------------------------------ utils
    @staticmethod
    def _ago(ts: Optional[float]) -> str:
        if not ts:
            return ""
        d = time.time() - ts
        if d < 1.5:
            return "刚刚"
        if d < 60:
            return f"{int(d)}秒前"
        if d < 3600:
            return f"{int(d // 60)}分前"
        return f"{int(d // 3600)}小时前"


# ---------------------------------------------------------------------------
# 命令行自检:  python trae_monitor.py
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import time as _t
    m = TraeMonitor()
    # 连续采样 3 次, 看速率变化
    for i in range(3):
        s = m.refresh()
        rate = m._recent_rate(s.log_path, _t.time()) if s.log_path else 0
        print(f"[{i}] 状态: {s.state} ({s.state_label})  速率: {rate} 行/{int(RATE_WINDOW)}s  产品: {s.product}")
        if i < 2:
            _t.sleep(1.0)
    print(f"运行中: {s.running}  进程数: {s.process_count}  主进程: {s.main_name} (PID {s.main_pid})")
    print(f"总内存: {s.total_mem_mb} MB   系统 CPU: {s.sys_cpu}%")
    print(f"日志: {s.log_path or '(无)'}")
    print(f"最近活动: {s.last_activity_ago or '(无)'}")
    print(f"判定原因: {s.reason}")
    if s.session_id:
        print(f"会话: {s.session_id}   任务: {s.task_id or '(无)'}")
    if s.events:
        print("最近事件:")
        for e in s.events:
            print(f"  {e.ts}  {e.command}")
