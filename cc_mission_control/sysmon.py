"""System monitor (SPEC 11.16): host sampling in a worker thread plus Mission Control's own server metrics.

Standard library only. On macOS the host is read with `sysctl`, `vm_stat` and `ps` (argument lists, 2 s timeout, never a
shell); on Linux with /proc and `os.statvfs`; elsewhere every metric is null. The parsing functions are pure.
Process names are the basename of the command only (never its arguments), redacted, then cut to 64 characters.
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

INTERVAL = 5.0  # seconds between host samples
RING = 720  # 60 min at 5 s
CMD_TIMEOUT = 2.0
NAME_MAX = 64
TOP_N = 8
CPU_ALERT_PCT = 90.0
CPU_ALERT_SECS = 120.0
CPU_ALERT_COVER = 0.8
GB = 1024 ** 3

SERIES = ("cpu", "mem_used", "swap_used", "load1", "disk_free_home", "disk_free_data")

# One call per key: a key missing on some macOS version loses only that value. Parsed by key, never by position.
SYSCTL_KEYS = ("hw.ncpu", "hw.memsize", "vm.loadavg", "vm.swapusage", "kern.memorystatus_vm_pressure_level")
MAC_TOP = ["top", "-l", "2", "-n", "0", "-s", "1"]
TOP_TIMEOUT = 3.0
MAC_VM_STAT = ["vm_stat"]
MAC_PS = ["ps", "-A", "-o", "pid=,ppid=,pcpu=,rss=,comm="]

Runner = Callable[..., Optional[str]]


# ---- pure parsers ----------------------------------------------------------------------------------------------


def _float(s: str) -> Optional[float]:
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None


def _int(s: str) -> Optional[int]:
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


_SWAP_RE = re.compile(r"total\s*=\s*([\d.]+)([KMGT]?)\s+used\s*=\s*([\d.]+)([KMGT]?)", re.I)
_UNITS = {"": 1, "K": 1024, "M": 1024 ** 2, "G": 1024 ** 3, "T": 1024 ** 4}


def parse_swapusage(line: str) -> Optional[Tuple[int, int]]:
    """`total = 12288.00M  used = 11310.69M ...` -> (used, total) in bytes."""
    m = _SWAP_RE.search(line)
    if not m:
        return None
    total, used = _float(m.group(1)), _float(m.group(3))
    if total is None or used is None:
        return None
    return int(used * _UNITS[m.group(4).upper()]), int(total * _UNITS[m.group(2).upper()])


def parse_sysctl_value(key: str, text: str) -> Any:
    """One `sysctl -n <key>` output -> a value, or None when it is not the expected shape."""
    line = text.strip()
    if key in ("hw.ncpu", "hw.memsize"):
        n = _int(line)
        return n if n is not None and n > 0 else None
    if key == "vm.loadavg":
        nums = [_float(p) for p in line.strip("{} ").split()]
        return [n for n in nums[:3] if n is not None] if len(nums) >= 3 and all(n is not None for n in nums[:3]) else None
    if key == "vm.swapusage":
        return parse_swapusage(line)
    if key == "kern.memorystatus_vm_pressure_level":
        return {1: "normal", 2: "warn", 4: "critical"}.get(_int(line))  # type: ignore[arg-type]
    return None


_TOP_CPU_RE = re.compile(r"CPU usage:.*?([\d.]+)%\s*idle")


def parse_top_cpu(text: str) -> Optional[float]:
    """`top -l 2` -> 100 - idle from the SECOND `CPU usage:` line (the first is an average since boot). None if there is no second."""
    idles = [_float(m.group(1)) for m in _TOP_CPU_RE.finditer(text)]
    if len(idles) < 2 or idles[1] is None:
        return None
    return round(max(0.0, min(100.0, 100.0 - idles[1])), 1)


def parse_vm_stat(text: str) -> Optional[dict]:
    """`vm_stat` -> page size and the page counts used for the memory figure, or None when it is not vm_stat output."""
    m = re.search(r"page size of (\d+) bytes", text)
    if not m:
        return None
    pages: Dict[str, int] = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if sep:
            n = _int(val.strip().rstrip("."))
            if n is not None:
                pages[key.strip()] = n
    needed = ("Pages free", "Pages speculative", "File-backed pages")
    if not all(k in pages for k in needed):
        return None
    return {"page_size": int(m.group(1)), "free": pages["Pages free"], "speculative": pages["Pages speculative"],
            "file_backed": pages["File-backed pages"]}


def mac_memory(total: int, vm: dict) -> Tuple[int, int]:
    """(used, cached) in bytes. used = total - free - speculative - file-backed; cached = file-backed (SPEC 11.16)."""
    size = vm["page_size"]
    cached = max(0, min(total, vm["file_backed"] * size))
    unused = (vm["free"] + vm["speculative"]) * size
    return max(0, min(total, total - unused - cached)), cached


def parse_ps(text: str) -> List[dict]:
    """`ps -A -o pid=,ppid=,pcpu=,rss=,comm=` -> [{pid, ppid, cpu, rss_bytes, comm}]. comm is the full path (may hold spaces)."""
    out = []
    for line in text.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 5:
            continue
        pid, ppid, cpu, rss = _int(parts[0]), _int(parts[1]), _float(parts[2]), _int(parts[3])
        if pid is None or ppid is None or cpu is None or rss is None:
            continue
        out.append({"pid": pid, "ppid": ppid, "cpu": max(0.0, cpu), "rss_bytes": max(0, rss) * 1024, "comm": parts[4].strip()})
    return out


def parse_proc_cpu(text: str) -> Optional[Tuple[int, int]]:
    """First `cpu ` line of /proc/stat -> (busy, total) jiffies."""
    for line in text.splitlines():
        if line.startswith("cpu "):
            nums = [_int(p) for p in line.split()[1:9]]
            if len(nums) < 4 or any(n is None for n in nums):
                return None
            vals = [n for n in nums if n is not None]
            total = sum(vals)
            idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
            return total - idle, total
    return None


def parse_meminfo(text: str) -> dict:
    """/proc/meminfo -> {name: bytes}."""
    out = {}
    for line in text.splitlines():
        key, sep, val = line.partition(":")
        if not sep:
            continue
        bits = val.split()
        n = _int(bits[0]) if bits else None
        if n is not None:
            out[key.strip()] = n * (1024 if len(bits) > 1 and bits[1].lower() == "kb" else 1)
    return out


def parse_loadavg(text: str) -> Optional[List[float]]:
    bits = text.split()
    nums = [_float(b) for b in bits[:3]]
    return [n for n in nums if n is not None] if len(nums) == 3 and all(n is not None for n in nums) else None


def parse_proc_pid_stat(text: str) -> Optional[dict]:
    """/proc/<pid>/stat -> {pid, comm, ppid, ticks, rss_pages}. comm sits in parentheses and may contain spaces or ')'."""
    lo, hi = text.find("("), text.rfind(")")
    if lo < 0 or hi < lo:
        return None
    pid = _int(text[:lo].strip())
    rest = text[hi + 1:].split()
    if pid is None or len(rest) < 22:
        return None
    ppid, utime, stime, rss = _int(rest[1]), _int(rest[11]), _int(rest[12]), _int(rest[21])
    if ppid is None or utime is None or stime is None or rss is None:
        return None
    return {"pid": pid, "comm": text[lo + 1:hi], "ppid": ppid, "ticks": utime + stime, "rss_pages": max(0, rss)}


def proc_name(comm: str, redact: Callable[[str], str]) -> str:
    """Basename of the command only, redacted first and then cut to NAME_MAX. Arguments never get here."""
    base = comm.rstrip("/").rsplit("/", 1)[-1].strip() or "?"
    return redact(base)[:NAME_MAX]


def claude_totals(procs: List[dict]) -> dict:
    """Summed cpu and memory of every process whose ancestry (itself included) has a process named `claude`."""
    by_pid = {p["pid"]: p for p in procs}
    memo: Dict[int, bool] = {}

    def under(pid: int) -> bool:
        seen = []
        cur: Optional[int] = pid
        result = False
        while cur is not None and cur in by_pid:
            if cur in memo:
                result = memo[cur]
                break
            if cur in seen:  # a cycle in a corrupt table
                break
            seen.append(cur)
            if by_pid[cur]["name"] == "claude":
                result = True
                break
            parent = by_pid[cur]["ppid"]
            cur = parent if parent != cur else None
        for s in seen:
            memo[s] = result
        return result

    count, cpu, rss = 0, 0.0, 0
    for p in procs:
        if under(p["pid"]):
            count += 1
            cpu += p["cpu_pct"]
            rss += p["rss_bytes"]
    return {"count": count, "cpu_pct": round(cpu, 1), "rss_bytes": rss}


def top_processes(procs: List[dict]) -> Tuple[List[dict], List[dict]]:
    def row(p: dict) -> dict:
        return {"pid": p["pid"], "name": p["name"], "cpu_pct": round(p["cpu_pct"], 1), "rss_bytes": p["rss_bytes"]}

    by_cpu = sorted(procs, key=lambda p: (-p["cpu_pct"], -p["rss_bytes"], p["pid"]))[:TOP_N]
    by_mem = sorted(procs, key=lambda p: (-p["rss_bytes"], -p["cpu_pct"], p["pid"]))[:TOP_N]
    return [row(p) for p in by_cpu], [row(p) for p in by_mem]


def compute_alerts(host: Optional[dict], cpu_points: List[Tuple[float, float]], now: float) -> List[dict]:
    """SPEC 11.16 alert thresholds. `host` is the newest sample; `cpu_points` is [(t, cpu%)], oldest first."""
    if not host:
        return []
    out: List[dict] = []

    def add(level: str, code: str, text: str) -> None:
        out.append({"level": level, "code": code, "text": text})

    pressure = host.get("mem_pressure")
    if pressure == "critical":
        add("action", "mem_pressure", "Memory pressure is critical")
    elif pressure == "warn":
        add("watch", "mem_pressure", "Memory pressure is high")

    disks = host.get("disks") or {}
    for key, label in (("home", "home folder"), ("data", "data folder")):
        if key == "data" and disks.get("same_volume"):
            continue  # one alert per volume
        d = disks.get(key)
        if not d or d.get("free") is None or not d.get("total"):
            continue
        frac = d["free"] / d["total"]
        gb = d["free"] / GB
        if frac < 0.05 or d["free"] < 5 * GB:
            add("action", f"disk_{key}", f"Disk for the {label} is almost full: {gb:.1f} GB free ({frac * 100:.0f}%)")
        elif frac < 0.10:
            add("watch", f"disk_{key}", f"Disk for the {label} is getting full: {gb:.1f} GB free ({frac * 100:.0f}%)")

    window = [(t, v) for t, v in cpu_points if t >= now - CPU_ALERT_SECS]
    # at least 80% of the samples a full window should hold, and every one of them high
    if len(window) >= CPU_ALERT_COVER * CPU_ALERT_SECS / INTERVAL and all(v > CPU_ALERT_PCT for _, v in window):
        add("watch", "cpu", "CPU has been above 90% for 2 minutes")

    su, st = host.get("swap_used"), host.get("swap_total")
    if su is not None and st and su > st * 0.5:
        add("watch", "swap", f"Swap is {su / st * 100:.0f}% used")
    return out


def percentile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))]


# ---- host sampler ----------------------------------------------------------------------------------------------


def run_command(args: List[str], timeout: Optional[float] = None) -> Optional[str]:
    """Run one command (argument list, no shell, 2 s timeout, C locale so numbers parse alike everywhere). None on any failure."""
    try:
        res = subprocess.run(args, capture_output=True, text=True, timeout=CMD_TIMEOUT if timeout is None else timeout,
                             shell=False, stdin=subprocess.DEVNULL, errors="replace",
                             env={**os.environ, "LC_ALL": "C", "LANG": "C"})
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return res.stdout if res.returncode == 0 else None


def read_text(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _platform_kind(platform: str) -> str:
    if platform == "darwin":
        return "darwin"
    if platform.startswith("linux"):
        return "linux"
    return "other"


def _null_host(kind: str) -> dict:
    return {
        "platform": kind, "sampled_at": None, "cores": None, "cpu_pct": None, "cpu_source": None, "load1": None, "load5": None, "load15": None,
        "mem_used": None, "mem_cached": None, "mem_total": None, "mem_pressure": None, "swap_used": None, "swap_total": None,
        "disks": {"home": None, "data": None},
    }


class Sampler:
    """Samples the host every INTERVAL seconds in a worker thread and keeps SERIES for 60 minutes (in memory)."""

    def __init__(
        self,
        data_dir: Path,
        redact: Callable[[str], str] = lambda s: s,
        *,
        platform: str = sys.platform,
        run: Runner = run_command,
        read: Callable[[Path], Optional[str]] = read_text,
        proc_root: Path = Path("/proc"),
        statvfs: Callable[[str], Any] = lambda p: os.statvfs(p),
        device: Callable[[str], Any] = lambda p: os.stat(p).st_dev,
        home: Optional[Path] = None,
        clock: Callable[[], float] = time.time,
        interval: float = INTERVAL,
    ) -> None:
        self.data_dir = Path(data_dir)
        self.redact = redact
        self.kind = _platform_kind(platform)
        self.run, self.read, self.proc_root, self.statvfs, self.device = run, read, proc_root, statvfs, device
        self.home = home if home is not None else Path.home()
        self.clock = clock
        self.interval = interval
        self._lock = threading.Lock()
        self._sample_lock = threading.Lock()
        self._series: Dict[str, Deque[Tuple[float, float]]] = {k: deque(maxlen=RING) for k in SERIES}
        self._host: Optional[dict] = None
        self._procs: Optional[dict] = None
        self._alerts: List[dict] = []
        self._prev_cpu: Optional[Tuple[int, int]] = None  # linux /proc/stat (busy, total)
        self._prev_ticks: Dict[int, Tuple[int, float]] = {}  # linux pid -> (ticks, time)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle
    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._loop, name="ccmc-sysmon", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.monotonic()
            try:
                self.sample()
            except Exception:  # a bad sample must never end the monitor
                pass
            self._stop.wait(max(0.5, self.interval - (time.monotonic() - started)))

    # -- sampling
    def sample(self) -> None:
        with self._sample_lock:
            now = self.clock()
            if self.kind == "darwin":
                host, procs = self._sample_macos(now)
            elif self.kind == "linux":
                host, procs = self._sample_linux(now)
            else:
                host, procs = _null_host(self.kind), None
            if self.kind != "other":
                home, hdev = self._disk(self.home)
                data, ddev = self._disk(self.data_dir)
                host["disks"] = {"home": home, "data": data, "same_volume": hdev is not None and hdev == ddev}
            host["sampled_at"] = now
            summary = self._summarise(procs)
            with self._lock:
                self._host, self._procs = host, summary
                cpu = host["cpu_pct"]
                values = {
                    "cpu": cpu, "mem_used": host["mem_used"], "swap_used": host["swap_used"], "load1": host["load1"],
                    "disk_free_home": (host["disks"]["home"] or {}).get("free"),
                    "disk_free_data": (host["disks"]["data"] or {}).get("free"),
                }
                for key, v in values.items():
                    if v is not None:
                        self._series[key].append((now, float(v)))
                self._alerts = compute_alerts(host, list(self._series["cpu"]), now)

    def _summarise(self, procs: Optional[List[dict]]) -> Optional[dict]:
        if procs is None:
            return None
        for p in procs:
            p["name"] = proc_name(p["comm"], self.redact)
        by_cpu, by_mem = top_processes(procs)
        return {"by_cpu": by_cpu, "by_mem": by_mem, "claude": claude_totals(procs)}

    def _disk(self, path: Path) -> Tuple[Optional[dict], Any]:
        """({free, total}, device id) of the volume holding `path`, using the nearest folder that exists."""
        p = Path(path)
        for cand in (p, *p.parents):
            try:
                st = self.statvfs(str(cand))
            except (OSError, AttributeError, ValueError):
                continue
            total = int(st.f_blocks * st.f_frsize)
            if total <= 0:
                return None, None
            try:
                dev = self.device(str(cand))
            except (OSError, ValueError):
                dev = None
            return {"free": int(st.f_bavail * st.f_frsize), "total": total}, dev
        return None, None

    def _sample_macos(self, now: float) -> Tuple[dict, Optional[List[dict]]]:
        host = _null_host("darwin")
        vals: Dict[str, Any] = {}
        for key in SYSCTL_KEYS:
            text = self.run(["sysctl", "-n", key], CMD_TIMEOUT)
            vals[key] = parse_sysctl_value(key, text) if text else None
        host["cores"], host["mem_total"] = vals["hw.ncpu"], vals["hw.memsize"]
        host["mem_pressure"] = vals["kern.memorystatus_vm_pressure_level"]
        if vals["vm.swapusage"]:
            host["swap_used"], host["swap_total"] = vals["vm.swapusage"]
        if vals["vm.loadavg"]:
            host["load1"], host["load5"], host["load15"] = vals["vm.loadavg"]
        vm_text = self.run(MAC_VM_STAT, CMD_TIMEOUT)
        vm = parse_vm_stat(vm_text) if vm_text else None
        if vm and host["mem_total"]:
            host["mem_used"], host["mem_cached"] = mac_memory(host["mem_total"], vm)
        ps_text = self.run(MAC_PS, CMD_TIMEOUT)
        raw = parse_ps(ps_text) if ps_text else None
        procs = None
        if raw:
            procs = [{"pid": p["pid"], "ppid": p["ppid"], "cpu_pct": p["cpu"], "rss_bytes": p["rss_bytes"], "comm": p["comm"]} for p in raw]
        top_text = self.run(MAC_TOP, TOP_TIMEOUT)
        cpu = parse_top_cpu(top_text) if top_text else None
        if cpu is not None:
            host["cpu_pct"], host["cpu_source"] = cpu, "top"
        elif procs and host["cores"]:  # a decayed average that understates load: say so
            host["cpu_pct"] = round(min(100.0, sum(p["cpu_pct"] for p in procs) / host["cores"]), 1)
            host["cpu_source"] = "ps"
        return host, procs

    def _sample_linux(self, now: float) -> Tuple[dict, Optional[List[dict]]]:
        host = _null_host("linux")
        root = self.proc_root
        stat = self.read(root / "stat")
        cpu = parse_proc_cpu(stat) if stat else None
        cores = None
        if stat:
            cores = sum(1 for ln in stat.splitlines() if re.match(r"cpu\d+ ", ln)) or None
        host["cores"] = cores
        if cpu is not None:
            if self._prev_cpu is not None and cpu[1] > self._prev_cpu[1]:
                host["cpu_pct"] = round(max(0.0, min(100.0, (cpu[0] - self._prev_cpu[0]) / (cpu[1] - self._prev_cpu[1]) * 100)), 1)
                host["cpu_source"] = "proc"
            self._prev_cpu = cpu
        text = self.read(root / "meminfo")
        mi = parse_meminfo(text) if text else {}
        if "MemTotal" in mi and "MemAvailable" in mi:
            host["mem_total"] = mi["MemTotal"]
            host["mem_used"] = max(0, mi["MemTotal"] - mi["MemAvailable"])
        if "SwapTotal" in mi and "SwapFree" in mi:
            host["swap_total"] = mi["SwapTotal"]
            host["swap_used"] = max(0, mi["SwapTotal"] - mi["SwapFree"])
        text = self.read(root / "loadavg")
        load = parse_loadavg(text) if text else None
        if load:
            host["load1"], host["load5"], host["load15"] = load
        return host, self._linux_procs(now)

    def _linux_procs(self, now: float) -> Optional[List[dict]]:
        try:
            entries = [e for e in os.listdir(self.proc_root) if e.isdigit()]
        except OSError:
            return None
        try:
            hz = os.sysconf("SC_CLK_TCK")
            page = os.sysconf("SC_PAGE_SIZE")
        except (ValueError, OSError, AttributeError):
            hz, page = 100, 4096
        procs, ticks = [], {}
        for e in entries:
            text = self.read(self.proc_root / e / "stat")
            st = parse_proc_pid_stat(text) if text else None
            if st is None:
                continue
            ticks[st["pid"]] = (st["ticks"], now)
            prev = self._prev_ticks.get(st["pid"])
            cpu = 0.0
            if prev is not None and now > prev[1] and st["ticks"] >= prev[0]:
                cpu = (st["ticks"] - prev[0]) / hz / (now - prev[1]) * 100
            procs.append({"pid": st["pid"], "ppid": st["ppid"], "cpu_pct": cpu, "rss_bytes": st["rss_pages"] * page, "comm": st["comm"]})
        self._prev_ticks = ticks
        return procs or None

    # -- reading
    @property
    def has_sample(self) -> bool:
        with self._lock:
            return self._host is not None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "host": dict(self._host) if self._host is not None else None,
                "series": {k: [[round(t, 1), v] for t, v in ring] for k, ring in self._series.items()},
                "processes": self._procs,
                "alerts": list(self._alerts),
            }


# ---- Mission Control's own server -------------------------------------------------------------------------------


def own_rss_bytes(run: Runner = run_command, pid: Optional[int] = None) -> Optional[int]:
    """Resident memory of this process: /proc on Linux, `ps -o rss= -p <pid>` on macOS, null elsewhere."""
    pid = os.getpid() if pid is None else pid
    if sys.platform.startswith("linux"):
        text = read_text(Path("/proc/self/statm"))
        bits = text.split() if text else []
        try:
            return int(bits[1]) * os.sysconf("SC_PAGE_SIZE")
        except (IndexError, ValueError, OSError):
            return None
    if sys.platform == "darwin":
        text = run(["ps", "-o", "rss=", "-p", str(pid)], CMD_TIMEOUT)
        n = _int(text.strip()) if text else None
        return n * 1024 if n is not None else None
    return None


def own_threads() -> int:
    if sys.platform.startswith("linux"):
        text = read_text(Path("/proc/self/status")) or ""
        for line in text.splitlines():
            if line.startswith("Threads:"):
                n = _int(line.split(":", 1)[1].strip())
                if n is not None:
                    return n
    return threading.active_count()


class ServerMetrics:
    """Rolling measurements of this server: event-loop lag, /hook timing, broadcast ticks and own CPU. Cheap and I/O-free."""

    LAG_WINDOW = 300  # 1 s samples = 5 min

    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.Lock()
        self._lag: Deque[float] = deque(maxlen=self.LAG_WINDOW)
        self._hook_times: Deque[float] = deque(maxlen=5000)  # arrival times, for events per minute
        self._hook_ms: Deque[float] = deque(maxlen=1000)
        self._tick_ms: Deque[float] = deque(maxlen=300)
        self._cpu: Deque[Tuple[float, float]] = deque(maxlen=8)  # (wall, process cpu seconds)

    def record_lag(self, seconds: float) -> None:
        with self._lock:
            self._lag.append(max(0.0, seconds) * 1000)

    def record_hook(self, seconds: float) -> None:
        with self._lock:
            self._hook_times.append(self.clock())
            self._hook_ms.append(max(0.0, seconds) * 1000)

    def record_tick(self, seconds: float) -> None:
        with self._lock:
            self._tick_ms.append(max(0.0, seconds) * 1000)

    def mark_cpu(self, wall: float, cpu_seconds: float) -> None:
        with self._lock:
            self._cpu.append((wall, cpu_seconds))

    @staticmethod
    def _r(v: Optional[float]) -> Optional[float]:
        return None if v is None else round(v, 2)

    def snapshot(self) -> dict:
        now = self.clock()
        with self._lock:
            lag, hook_ms, tick_ms = list(self._lag), list(self._hook_ms), list(self._tick_ms)
            per_min = sum(1 for t in self._hook_times if now - t <= 60.0)
            cpu = list(self._cpu)
        cpu_pct = None
        if len(cpu) >= 2 and cpu[-1][0] > cpu[0][0]:
            cpu_pct = round(max(0.0, (cpu[-1][1] - cpu[0][1]) / (cpu[-1][0] - cpu[0][0]) * 100), 1)
        r = self._r
        return {
            "loop_lag_ms": {"p50": r(percentile(lag, 0.5)), "p95": r(percentile(lag, 0.95)), "max": r(max(lag) if lag else None)},
            "hook": {"per_min": per_min, "p50_ms": r(percentile(hook_ms, 0.5)), "p95_ms": r(percentile(hook_ms, 0.95))},
            "broadcast": {"tick_p95_ms": r(percentile(tick_ms, 0.95))},
            "cpu_pct": cpu_pct,
        }


async def lag_sampler(metrics: ServerMetrics, interval: float = 1.0) -> None:
    """Measures how late a sleep of `interval` wakes up (the event loop's lag) and samples the process CPU as it goes."""
    while True:
        t0 = time.perf_counter()
        await asyncio.sleep(interval)
        metrics.record_lag(time.perf_counter() - t0 - interval)
        metrics.mark_cpu(time.perf_counter(), time.process_time())
