#!/usr/bin/env python3
"""Personal Dash agent.

Runs on each VPS, reports host metrics and service status to the dashboard and executes
allow-listed commands (restart, logs). Standard library only, Python 3.8+, no Docker required.

Config: /etc/dash-agent/config.json (or DASH_AGENT_CONFIG):
{
  "url": "https://dash.example.com",
  "token": "agent token from the dashboard",
  "interval": 20,
  "disks": ["/"],
  "discover_docker": true,
  "discover_failed_units": true,
  "auto_update": true,
  "services": [
    {"name": "api", "kind": "systemd", "unit": "myapi.service",
     "path": "/opt/myapi", "url": "http://127.0.0.1:8000/health"},
    {"name": "nightly-stats", "kind": "systemd", "unit": "stats.service", "max_age_hours": 26},
    {"name": "web", "kind": "docker", "container": "web", "version_file": "/opt/web/VERSION"},
    {"name": "worker", "kind": "process", "match": "celery worker"}
  ]
}
"""

import bz2
import io
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from typing import Any, Dict, List, Optional, Tuple

AGENT_VERSION = "0.4.0"
CONFIG_PATH = os.environ.get("DASH_AGENT_CONFIG", "/etc/dash-agent/config.json")
LOG_TAIL_MAX = 500
BIN_DIR = "/opt/dash-agent/bin"
RCLONE_CONF = "/etc/dash-agent/rclone.conf"
RESTORE_DIR = "/var/backups/dash-restore"


def log(msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), msg, flush=True)


def run(cmd: List[str], timeout: int = 20) -> "subprocess.CompletedProcess[str]":
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def which(name: str) -> bool:
    return shutil.which(name) is not None


# ---------- host metrics ----------


def read_meminfo() -> Dict[str, int]:
    out: Dict[str, int] = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    out[key] = int(parts[0]) * 1024  # kB -> bytes
    except OSError:
        pass
    return out


def host_metrics(disks: List[str]) -> Dict[str, Any]:
    mem = read_meminfo()
    total = mem.get("MemTotal", 0)
    avail = mem.get("MemAvailable", 0)
    try:
        load1, load5, load15 = os.getloadavg()
    except OSError:
        load1 = load5 = load15 = 0.0
    try:
        with open("/proc/uptime") as f:
            uptime = float(f.read().split()[0])
    except (OSError, ValueError):
        uptime = 0.0
    disk_list = []
    for path in disks or ["/"]:
        try:
            usage = shutil.disk_usage(path)
            disk_list.append(
                {"path": path, "total": usage.total, "used": usage.used, "pct": round(usage.used / usage.total * 100, 1) if usage.total else 0}
            )
        except OSError:
            continue
    return {
        "load1": round(load1, 2),
        "load5": round(load5, 2),
        "load15": round(load15, 2),
        "cpus": os.cpu_count() or 1,
        "mem_total": total,
        "mem_used": max(total - avail, 0),
        "mem_pct": round((total - avail) / total * 100, 1) if total else 0,
        "swap_total": mem.get("SwapTotal", 0),
        "swap_used": max(mem.get("SwapTotal", 0) - mem.get("SwapFree", 0), 0),
        "uptime": int(uptime),
        "disks": disk_list,
    }


# ---------- services ----------


def _systemd_show(unit: str, props: str) -> Dict[str, str]:
    res = run(["systemctl", "show", unit, "-p", props])
    return dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)


def _parse_systemd_ts(value: str) -> Optional[float]:
    """'Mon 2026-11-03 06:00:12 UTC' -> epoch seconds (None when empty/n/a)."""
    value = (value or "").strip()
    if not value or value in ("n/a", "0"):
        return None
    for fmt in ("%a %Y-%m-%d %H:%M:%S %Z", "%a %Y-%m-%d %H:%M:%S"):
        try:
            parts = value.split()
            if len(parts) == 4:  # drop timezone name, systemd prints local time
                return time.mktime(time.strptime(" ".join(parts[:3]), "%a %Y-%m-%d %H:%M:%S"))
            return time.mktime(time.strptime(value, fmt))
        except ValueError:
            continue
    return None


def _iso(ts: Optional[float]) -> Optional[str]:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(ts)) if ts else None


def systemd_status(unit: str, svc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Status of a systemd service. Oneshot services and services driven by a timer are
    reported as 'idle' between runs instead of 'stopped'; they fail only when the last run
    failed, the timer is disabled, or the last run is older than max_age_hours."""
    if not which("systemctl"):
        return {"status": "unknown", "detail": "no systemctl"}
    svc = svc or {}
    try:
        props = _systemd_show(
            unit,
            "ActiveState,SubState,NRestarts,ExecMainStartTimestamp,ExecMainExitTimestamp,"
            "MainPID,Result,Type,ExecMainStatus,LoadState",
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "unknown", "detail": str(exc)}
    if props.get("LoadState") == "not-found":
        return {"status": "missing", "detail": "unit not found"}

    active = props.get("ActiveState", "unknown")
    base = unit[:-8] if unit.endswith(".service") else unit
    timer: Dict[str, str] = {}
    try:
        t = _systemd_show(base + ".timer", "ActiveState,LastTriggerUSec,NextElapseUSecRealtime,LoadState")
        if t.get("LoadState") not in (None, "not-found"):
            timer = t
    except (subprocess.TimeoutExpired, OSError):
        pass
    scheduled = bool(svc.get("scheduled")) or props.get("Type") == "oneshot" or bool(timer)

    out: Dict[str, Any] = {
        "detail": props.get("SubState", ""),
        "restarts": int(props.get("NRestarts") or 0),
        "started_at": props.get("ExecMainStartTimestamp") or None,
        "pid": int(props.get("MainPID") or 0) or None,
    }
    if not scheduled:
        out["status"] = {
            "active": "running", "failed": "failed", "inactive": "stopped",
            "activating": "starting", "deactivating": "stopping",
        }.get(active, active)
        return out

    last_run = _parse_systemd_ts(props.get("ExecMainExitTimestamp", "")) or _parse_systemd_ts(timer.get("LastTriggerUSec", ""))
    next_run = _parse_systemd_ts(timer.get("NextElapseUSecRealtime", ""))
    result = props.get("Result", "")
    out.update({"scheduled": True, "last_run": _iso(last_run), "next_run": _iso(next_run), "result": result})
    max_age_h = svc.get("max_age_hours")
    if active in ("active", "activating"):
        out["status"] = "running"
    elif result not in ("", "success"):
        out["status"] = "failed"
        out["detail"] = "last run: %s (exit %s)" % (result, props.get("ExecMainStatus", "?"))
    elif timer and timer.get("ActiveState") not in ("active", "waiting"):
        out["status"] = "stopped"
        out["detail"] = "timer %s" % timer.get("ActiveState")
    elif max_age_h and last_run and time.time() - last_run > float(max_age_h) * 3600:
        out["status"] = "stale"
        out["detail"] = "no successful run for %.0f h" % ((time.time() - last_run) / 3600)
    else:
        out["status"] = "idle"
        out["detail"] = "по расписанию"
    return out


def docker_inspect(name: str) -> Dict[str, Any]:
    if not which("docker"):
        return {"status": "unknown", "detail": "no docker"}
    try:
        res = run(["docker", "inspect", name])
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "unknown", "detail": str(exc)}
    if res.returncode != 0:
        return {"status": "missing", "detail": res.stderr.strip()[:200]}
    try:
        data = json.loads(res.stdout)[0]
    except (ValueError, IndexError):
        return {"status": "unknown", "detail": "bad inspect output"}
    state = data.get("State", {})
    health = (state.get("Health") or {}).get("Status")
    status = state.get("Status", "unknown")
    if status == "running" and health == "unhealthy":
        status = "unhealthy"
    return {
        "status": status,
        "detail": health or "",
        "restarts": int(data.get("RestartCount") or 0),
        "started_at": state.get("StartedAt"),
        "image": (data.get("Config") or {}).get("Image"),
        "exit_code": state.get("ExitCode"),
    }


def process_status(match: str) -> Dict[str, Any]:
    """Fallback for things that are neither systemd nor docker: look for a command line substring."""
    found = 0
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            with open("/proc/%s/cmdline" % pid, "rb") as f:
                cmd = f.read().replace(b"\x00", b" ").decode(errors="ignore")
        except OSError:
            continue
        if match in cmd and str(os.getpid()) != pid:
            found += 1
    return {"status": "running" if found else "stopped", "detail": "%d process(es)" % found, "restarts": 0}


def discover_docker() -> List[str]:
    if not which("docker"):
        return []
    try:
        res = run(["docker", "ps", "-a", "--format", "{{.Names}}"])
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [n for n in res.stdout.split() if n]


def failed_units() -> List[str]:
    if not which("systemctl"):
        return []
    try:
        res = run(["systemctl", "list-units", "--type=service", "--state=failed", "--no-legend", "--plain"])
    except (subprocess.TimeoutExpired, OSError):
        return []
    return [line.split()[0] for line in res.stdout.splitlines() if line.strip()]


def service_version(svc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    path = svc.get("path")
    if path and os.path.isdir(os.path.join(path, ".git")) and which("git"):
        try:
            sha = run(["git", "-C", path, "rev-parse", "--short", "HEAD"]).stdout.strip()
            date = run(["git", "-C", path, "log", "-1", "--format=%cI"]).stdout.strip()
            branch = run(["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
            if sha:
                return {"kind": "git", "value": sha, "branch": branch, "date": date}
        except (subprocess.TimeoutExpired, OSError):
            pass
    vfile = svc.get("version_file")
    if vfile and os.path.isfile(vfile):
        try:
            with open(vfile) as f:
                return {"kind": "file", "value": f.read().strip()[:80]}
        except OSError:
            pass
    if path and os.path.exists(path):
        # not in git: report the newest modification time under the path as a "deploy marker"
        newest = 0.0
        for root, _dirs, files in os.walk(path):
            if "/." in root or "node_modules" in root:
                continue
            for name in files:
                try:
                    newest = max(newest, os.path.getmtime(os.path.join(root, name)))
                except OSError:
                    continue
        if newest:
            return {"kind": "mtime", "value": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(newest))}
    return None


def http_check(url: str, timeout: float = 5.0) -> Dict[str, Any]:
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dash-agent/%s" % AGENT_VERSION})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            code = resp.status
    except urllib.error.HTTPError as exc:
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "status": None, "latency_ms": int((time.time() - start) * 1000), "error": str(exc)[:200]}
    return {"ok": 200 <= code < 400, "status": code, "latency_ms": int((time.time() - start) * 1000)}


def collect_services(cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    configured_containers = set()
    for svc in cfg.get("services", []):
        kind = svc.get("kind", "systemd")
        if kind == "systemd":
            info = systemd_status(svc.get("unit") or svc["name"], svc)
        elif kind == "docker":
            configured_containers.add(svc.get("container") or svc["name"])
            info = docker_inspect(svc.get("container") or svc["name"])
        elif kind == "process":
            info = process_status(svc.get("match") or svc["name"])
        else:
            info = {"status": "unknown", "detail": "unknown kind %s" % kind}
        entry = {"name": svc["name"], "kind": kind, **info}
        version = service_version(svc)
        if version:
            entry["version"] = version
        if svc.get("url"):
            entry["http"] = http_check(svc["url"])
        out.append(entry)
    if cfg.get("discover_docker", True):
        for name in discover_docker():
            if name not in configured_containers:
                out.append({"name": name, "kind": "docker", "discovered": True, **docker_inspect(name)})
    return out


def build_report(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "agent_version": AGENT_VERSION,
        "hostname": socket.gethostname(),
        "os": " ".join(x for x in (platform.system(), platform.release()) if x),
        "pretty_os": pretty_os(),
        "python": platform.python_version(),
        "metrics": host_metrics(cfg.get("disks") or ["/"]),
        "services": collect_services(cfg),
        "failed_units": failed_units() if cfg.get("discover_failed_units", True) else [],
        "busy": busy_commands(),
        "sent_at": time.time(),
    }


def pretty_os() -> str:
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return platform.platform()


# ---------- commands from the dashboard ----------


def find_service(cfg: Dict[str, Any], name: str) -> Optional[Dict[str, Any]]:
    for svc in cfg.get("services", []):
        if svc["name"] == name:
            return svc
    if name in discover_docker():
        return {"name": name, "kind": "docker", "container": name}
    return None


def execute(cfg: Dict[str, Any], command: Dict[str, Any]) -> Dict[str, Any]:
    kind = command.get("kind")
    args = command.get("args") or {}
    try:
        if kind == "ping":
            return {"ok": True, "output": "pong from %s" % socket.gethostname()}
        if kind in ("restart", "logs", "stop", "start"):
            svc = find_service(cfg, args.get("service", ""))
            if not svc:
                return {"ok": False, "output": "unknown service %r" % args.get("service")}
            if svc.get("kind") == "docker":
                target = svc.get("container") or svc["name"]
                if kind == "logs":
                    n = min(int(args.get("lines", 200)), LOG_TAIL_MAX)
                    res = run(["docker", "logs", "--tail", str(n), target], timeout=30)
                    return {"ok": res.returncode == 0, "output": (res.stdout + res.stderr)[-60000:]}
                res = run(["docker", kind, target], timeout=90)
            elif svc.get("kind") == "systemd":
                unit = svc.get("unit") or svc["name"]
                if kind == "logs":
                    n = min(int(args.get("lines", 200)), LOG_TAIL_MAX)
                    res = run(["journalctl", "-u", unit, "-n", str(n), "--no-pager", "-o", "short-iso"], timeout=30)
                    return {"ok": res.returncode == 0, "output": (res.stdout + res.stderr)[-60000:]}
                res = run(["systemctl", kind, unit], timeout=90)
            else:
                return {"ok": False, "output": "%s is not supported for kind=%s" % (kind, svc.get("kind"))}
            out = (res.stdout + res.stderr).strip()
            return {"ok": res.returncode == 0, "output": out or "%s %s: done" % (kind, svc["name"])}
        if kind == "backup":
            return cmd_backup(args)
        if kind == "backup_snapshots":
            return cmd_snapshots(args)
        if kind == "restore":
            return cmd_restore(args)
        if kind == "backup_check":
            return cmd_check(args)
        if kind == "script":
            # only scripts the dashboard installed under /etc/dash-agent/scripts are allowed
            name = os.path.basename(str(args.get("name", "")))
            path = os.path.join("/etc/dash-agent/scripts", name)
            if not name or not os.path.isfile(path):
                return {"ok": False, "output": "script not found: %s" % name}
            res = run(["/bin/bash", path] + [str(a) for a in args.get("argv", [])], timeout=int(args.get("timeout", 1800)))
            return {"ok": res.returncode == 0, "output": (res.stdout + res.stderr)[-60000:], "exit_code": res.returncode}
        return {"ok": False, "output": "unknown command kind %r" % kind}
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "timeout"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "error: %s" % exc}



# ---------- backup tooling (restic + rclone) ----------


def _tool_env(extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    env = dict(os.environ)
    env["PATH"] = BIN_DIR + ":" + env.get("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["RCLONE_CONFIG"] = RCLONE_CONF
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def _download(url: str, timeout: int = 120) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "dash-agent/%s" % AGENT_VERSION})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def _arch() -> str:
    m = platform.machine()
    return {"x86_64": "amd64", "aarch64": "arm64", "armv7l": "arm"}.get(m, m)


def ensure_restic() -> str:
    """Return path to a restic binary, downloading the latest release when none is present."""
    path = os.path.join(BIN_DIR, "restic")
    if os.path.isfile(path):
        return path
    system = shutil.which("restic")
    if system:
        return system
    os.makedirs(BIN_DIR, exist_ok=True)
    meta = json.loads(_download("https://api.github.com/repos/restic/restic/releases/latest").decode())
    want = "_linux_%s.bz2" % _arch()
    asset = next((a for a in meta.get("assets", []) if a["name"].endswith(want)), None)
    if not asset:
        raise RuntimeError("no restic build for %s" % _arch())
    data = bz2.decompress(_download(asset["browser_download_url"], timeout=300))
    with open(path, "wb") as f:
        f.write(data)
    os.chmod(path, 0o755)
    return path


def ensure_rclone() -> str:
    path = os.path.join(BIN_DIR, "rclone")
    if os.path.isfile(path):
        return path
    system = shutil.which("rclone")
    if system:
        return system
    os.makedirs(BIN_DIR, exist_ok=True)
    data = _download("https://downloads.rclone.org/rclone-current-linux-%s.zip" % _arch(), timeout=300)
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = next(n for n in zf.namelist() if n.endswith("/rclone"))
        with open(path, "wb") as f:
            f.write(zf.read(member))
    os.chmod(path, 0o755)
    return path


def _write_rclone_conf(text: Optional[str]) -> None:
    if not text:
        return
    os.makedirs(os.path.dirname(RCLONE_CONF), exist_ok=True)
    with open(RCLONE_CONF, "w") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    os.chmod(RCLONE_CONF, 0o600)


def _restic(args: List[str], env: Dict[str, str], timeout: int = 3600, stdin=None) -> "subprocess.CompletedProcess[str]":
    return subprocess.run([ensure_restic()] + args, env=env, capture_output=True, text=True, timeout=timeout, stdin=stdin)


def _repo_env(a: Dict[str, Any]) -> Dict[str, str]:
    if a.get("rclone_conf"):
        _write_rclone_conf(a["rclone_conf"])
        ensure_rclone()
    extra = {"RESTIC_REPOSITORY": a["repo"], "RESTIC_PASSWORD": a["password"]}
    extra.update(a.get("env") or {})
    return _tool_env(extra)


def _ensure_repo(env: Dict[str, str]) -> str:
    probe = _restic(["cat", "config"], env, timeout=300)
    if probe.returncode == 0:
        return "repo ok"
    init = _restic(["init"], env, timeout=600)
    if init.returncode != 0:
        raise RuntimeError("restic init failed: %s" % (init.stderr or init.stdout).strip()[-500:])
    return "repo initialised"


def _parse_summary(stdout: str) -> Dict[str, Any]:
    """restic --json prints one JSON object per line; the last 'summary' line has the numbers."""
    summary: Dict[str, Any] = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("message_type") == "summary":
            summary = obj
    return summary


def _dump_command(item: Dict[str, Any]) -> Tuple[List[str], str]:
    """Shell-free argv for a database dump + the file name to store it under."""
    kind = item["type"]
    mode = item.get("mode", "docker")
    name = item.get("name") or item.get("database") or kind
    if kind == "pg_dump":
        pg = ["pg_dump", "--no-owner", "--clean", "--if-exists"]
        if item.get("user"):
            pg += ["-U", item["user"]]
        if item.get("database"):
            pg += ["-d", item["database"]]
        if mode == "docker":
            return ["docker", "exec", "-i", item["container"]] + pg, name + ".sql"
        if mode == "dsn":
            return ["pg_dump", "--no-owner", "--clean", "--if-exists", item["dsn"]], name + ".sql"
        return ["runuser", "-u", item.get("os_user", "postgres"), "--"] + pg, name + ".sql"
    if kind == "mysql_dump":
        my = ["mysqldump", "--single-transaction", "--routines", "--triggers"]
        if item.get("user"):
            my += ["-u", item["user"]]
        if item.get("password"):
            my += ["-p" + item["password"]]
        my += [item["database"]] if item.get("database") else ["--all-databases"]
        if mode == "docker":
            return ["docker", "exec", "-i", item["container"]] + my, name + ".sql"
        return my, name + ".sql"
    if kind == "docker_volume":
        vol = item["volume"]
        return ["docker", "run", "--rm", "-v", "%s:/data:ro" % vol, "alpine", "tar", "cf", "-", "-C", "/data", "."], (item.get("name") or vol) + ".tar"
    raise RuntimeError("unknown dump type %s" % kind)


def _backup_stream(item: Dict[str, Any], tags: List[str], env: Dict[str, str], timeout: int) -> Dict[str, Any]:
    argv, filename = _dump_command(item)
    producer = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        restic = subprocess.run(
            [ensure_restic(), "backup", "--stdin", "--stdin-filename", filename, "--json"] + sum([["--tag", t] for t in tags], []),
            stdin=producer.stdout, env=env, capture_output=True, text=True, timeout=timeout,
        )
    finally:
        if producer.stdout:
            producer.stdout.close()
    _, perr = producer.communicate(timeout=60)
    if producer.returncode != 0:
        raise RuntimeError("%s failed (exit %s): %s" % (argv[0], producer.returncode, perr.decode(errors="ignore")[-400:]))
    if restic.returncode != 0:
        raise RuntimeError("restic backup failed: %s" % (restic.stderr or restic.stdout)[-400:])
    summ = _parse_summary(restic.stdout)
    return {"item": item.get("name") or filename, "file": filename, "snapshot_id": summ.get("snapshot_id"), "added": summ.get("data_added"), "size": summ.get("total_bytes_processed")}


def _backup_path(item: Dict[str, Any], tags: List[str], env: Dict[str, str], timeout: int) -> Dict[str, Any]:
    path = item["path"]
    if not os.path.exists(path):
        raise RuntimeError("path not found: %s" % path)
    args = ["backup", path, "--json", "--one-file-system"] + sum([["--tag", t] for t in tags], [])
    for ex in item.get("exclude") or []:
        args += ["--exclude", ex]
    res = _restic(args, env, timeout=timeout)
    if res.returncode not in (0, 3):  # 3 = some files could not be read, still a snapshot
        raise RuntimeError("restic backup failed: %s" % (res.stderr or res.stdout)[-400:])
    summ = _parse_summary(res.stdout)
    return {"item": item.get("name") or path, "path": path, "snapshot_id": summ.get("snapshot_id"), "added": summ.get("data_added"), "size": summ.get("total_bytes_processed"), "warnings": res.returncode == 3}


def cmd_backup(a: Dict[str, Any]) -> Dict[str, Any]:
    started = time.time()
    env = _repo_env(a)
    log_lines = [_ensure_repo(env)]
    plan_tag = "plan:%s" % a["plan_id"]
    results, errors = [], []
    per_item_timeout = int(a.get("item_timeout", 3600))
    for item in a.get("items", []):
        tags = [plan_tag, "item:%s" % (item.get("name") or item.get("path") or item.get("database") or item["type"])]
        try:
            if item["type"] == "path":
                results.append(_backup_path(item, tags, env, per_item_timeout))
            else:
                results.append(_backup_stream(item, tags, env, per_item_timeout))
            log_lines.append("ok: %s -> %s" % (results[-1]["item"], results[-1].get("snapshot_id")))
        except Exception as exc:  # noqa: BLE001
            errors.append("%s: %s" % (item.get("name") or item["type"], exc))
            log_lines.append("FAILED: %s: %s" % (item.get("name") or item["type"], exc))
    ret = a.get("retention") or {}
    forget_args = ["forget", "--tag", plan_tag, "--group-by", "tags"]
    for key in ("daily", "weekly", "monthly", "yearly", "last"):
        if ret.get(key):
            forget_args += ["--keep-%s" % key, str(ret[key])]
    if a.get("prune"):
        forget_args.append("--prune")
    if len(forget_args) > 5 and results:
        fr = _restic(forget_args, env, timeout=7200)
        log_lines.append("forget%s: %s" % (" --prune" if a.get("prune") else "", "ok" if fr.returncode == 0 else (fr.stderr or fr.stdout)[-300:]))
    stats: Dict[str, Any] = {}
    st = _restic(["stats", "--json", "--mode", "raw-data"], env, timeout=900)
    if st.returncode == 0:
        try:
            stats = json.loads(st.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            pass
    return {
        "ok": not errors and bool(results),
        "output": "\n".join(log_lines)[-20000:],
        "results": results,
        "errors": errors,
        "repo_size": stats.get("total_size"),
        "duration": int(time.time() - started),
    }


def cmd_snapshots(a: Dict[str, Any]) -> Dict[str, Any]:
    env = _repo_env(a)
    args = ["snapshots", "--json"]
    if a.get("plan_id"):
        args += ["--tag", "plan:%s" % a["plan_id"]]
    res = _restic(args, env, timeout=600)
    if res.returncode != 0:
        return {"ok": False, "output": (res.stderr or res.stdout)[-1000:]}
    try:
        snaps = json.loads(res.stdout or "[]")
    except ValueError:
        return {"ok": False, "output": "bad snapshots json"}
    out = [
        {"id": s_.get("short_id") or s_.get("id", "")[:8], "time": s_.get("time"), "tags": s_.get("tags", []), "paths": s_.get("paths", []), "hostname": s_.get("hostname")}
        for s_ in snaps[-200:]
    ]
    return {"ok": True, "output": "%d snapshots" % len(out), "snapshots": out}


def cmd_restore(a: Dict[str, Any]) -> Dict[str, Any]:
    env = _repo_env(a)
    snap = a["snapshot_id"]
    target = os.path.join(RESTORE_DIR, str(a.get("plan_id", "x")), snap)
    os.makedirs(target, exist_ok=True)
    args = ["restore", snap, "--target", target]
    for inc in a.get("include") or []:
        args += ["--include", inc]
    res = _restic(args, env, timeout=int(a.get("timeout", 7200)))
    ok = res.returncode == 0
    listing = []
    for root, _d, files in os.walk(target):
        for name in files[:50]:
            full = os.path.join(root, name)
            try:
                listing.append("%s (%d bytes)" % (full, os.path.getsize(full)))
            except OSError:
                pass
        if len(listing) >= 50:
            break
    return {"ok": ok, "output": ("restored to %s\n" % target) + "\n".join(listing) + ("\n" + (res.stderr or "")[-500:] if not ok else ""), "target": target}


def cmd_check(a: Dict[str, Any]) -> Dict[str, Any]:
    env = _repo_env(a)
    args = ["check"]
    if a.get("read_data_subset"):
        args += ["--read-data-subset", str(a["read_data_subset"])]
    res = _restic(args, env, timeout=int(a.get("timeout", 7200)))
    return {"ok": res.returncode == 0, "output": (res.stdout + res.stderr)[-5000:]}


# ---------- background command runner ----------

_cmd_queue: "queue.Queue[Tuple[Dict[str, Any], Dict[str, Any]]]" = queue.Queue()
_busy_lock = threading.Lock()
_busy: Dict[int, Dict[str, Any]] = {}


def _worker() -> None:
    while True:
        cfg, command = _cmd_queue.get()
        cid = command.get("id")
        with _busy_lock:
            _busy[cid] = {"kind": command.get("kind"), "since": time.time()}
        try:
            result = execute(cfg, command)
        except Exception as exc:  # noqa: BLE001
            result = {"ok": False, "output": "agent error: %s" % exc}
        for attempt in range(5):
            try:
                post_json(cfg, "/api/agent/commands/%s/result" % cid, result, timeout=60)
                break
            except Exception as exc:  # noqa: BLE001
                log("result upload failed (%s/5): %s" % (attempt + 1, exc))
                time.sleep(10 * (attempt + 1))
        with _busy_lock:
            _busy.pop(cid, None)
        _cmd_queue.task_done()


def busy_commands() -> List[Dict[str, Any]]:
    with _busy_lock:
        return [{"id": k, "kind": v["kind"], "running_s": int(time.time() - v["since"])} for k, v in _busy.items()]


# ---------- transport ----------


def post_json(cfg: Dict[str, Any], path: str, payload: Dict[str, Any], timeout: int = 20) -> Dict[str, Any]:
    url = cfg["url"].rstrip("/") + path
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "X-Agent-Token": cfg["token"], "User-Agent": "dash-agent/%s" % AGENT_VERSION},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode()
    return json.loads(body) if body else {}


def self_update(cfg: Dict[str, Any], server_version: Optional[str]) -> None:
    """Replace this script with the dashboard's bundled version and restart (systemd restarts us)."""
    if not server_version or server_version == AGENT_VERSION or not cfg.get("auto_update", True):
        return
    if busy_commands():
        return  # never swap the binary under a running backup
    path = os.path.abspath(__file__)
    url = cfg["url"].rstrip("/") + "/api/agent/script"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "dash-agent/%s" % AGENT_VERSION})
        with urllib.request.urlopen(req, timeout=30) as resp:
            code = resp.read()
        if b"AGENT_VERSION" not in code or b"def main_loop" not in code:
            log("self-update: downloaded file does not look like the agent, skipping")
            return
        compile(code, path, "exec")  # syntax check before replacing ourselves
        tmp = path + ".new"
        with open(tmp, "wb") as f:
            f.write(code)
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
        log("self-update: %s -> %s, restarting" % (AGENT_VERSION, server_version))
        sys.stdout.flush()
        os.execv(sys.executable, [sys.executable, path] + sys.argv[1:])
    except Exception as exc:  # noqa: BLE001
        log("self-update failed: %s" % exc)


def load_config() -> Dict[str, Any]:
    with open(CONFIG_PATH) as f:
        cfg = json.load(f)
    cfg.setdefault("url", os.environ.get("DASH_URL", ""))
    cfg.setdefault("token", os.environ.get("DASH_AGENT_TOKEN", ""))
    if not cfg["url"] or not cfg["token"]:
        raise SystemExit("config needs url and token")
    return cfg


def main_loop() -> None:
    cfg = load_config()
    interval = int(cfg.get("interval", 20))
    log("dash-agent %s -> %s (every %ss)" % (AGENT_VERSION, cfg["url"], interval))
    threading.Thread(target=_worker, name="commands", daemon=True).start()
    backoff = interval
    while True:
        started = time.time()
        try:
            cfg = load_config()  # pick up config edits without restart
            response = post_json(cfg, "/api/agent/report", build_report(cfg))
            backoff = interval
            self_update(cfg, response.get("agent_version"))
            for command in response.get("commands", []):
                log("command %s %s queued" % (command.get("id"), command.get("kind")))
                _cmd_queue.put((cfg, command))
        except urllib.error.HTTPError as exc:
            log("report rejected: HTTP %s %s" % (exc.code, exc.read()[:200]))
            backoff = min(backoff * 2, 300)
        except Exception as exc:  # noqa: BLE001
            log("report failed: %s" % exc)
            backoff = min(backoff * 2, 300)
        time.sleep(max(1, backoff - (time.time() - started)))


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "report":
        cfg = load_config() if os.path.exists(CONFIG_PATH) else {"url": "x", "token": "x", "services": []}
        print(json.dumps(build_report(cfg), indent=2, ensure_ascii=False))
    else:
        main_loop()
