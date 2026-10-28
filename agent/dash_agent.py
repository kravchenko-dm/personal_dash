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
  "services": [
    {"name": "api", "kind": "systemd", "unit": "myapi.service",
     "path": "/opt/myapi", "url": "http://127.0.0.1:8000/health"},
    {"name": "web", "kind": "docker", "container": "web", "version_file": "/opt/web/VERSION"},
    {"name": "worker", "kind": "process", "match": "celery worker"}
  ]
}
"""

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

AGENT_VERSION = "0.2.0"
CONFIG_PATH = os.environ.get("DASH_AGENT_CONFIG", "/etc/dash-agent/config.json")
LOG_TAIL_MAX = 500


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


def systemd_status(unit: str) -> Dict[str, Any]:
    if not which("systemctl"):
        return {"status": "unknown", "detail": "no systemctl"}
    try:
        res = run(["systemctl", "show", unit, "-p", "ActiveState,SubState,NRestarts,ExecMainStartTimestamp,MainPID,Result"])
    except (subprocess.TimeoutExpired, OSError) as exc:
        return {"status": "unknown", "detail": str(exc)}
    props = dict(line.split("=", 1) for line in res.stdout.splitlines() if "=" in line)
    active = props.get("ActiveState", "unknown")
    status = {"active": "running", "failed": "failed", "inactive": "stopped", "activating": "starting", "deactivating": "stopping"}.get(active, active)
    return {
        "status": status,
        "detail": props.get("SubState", ""),
        "restarts": int(props.get("NRestarts") or 0),
        "started_at": props.get("ExecMainStartTimestamp") or None,
        "pid": int(props.get("MainPID") or 0) or None,
    }


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
            info = systemd_status(svc.get("unit") or svc["name"])
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
    backoff = interval
    while True:
        started = time.time()
        try:
            cfg = load_config()  # pick up config edits without restart
            response = post_json(cfg, "/api/agent/report", build_report(cfg))
            backoff = interval
            for command in response.get("commands", []):
                log("command %s %s" % (command.get("id"), command.get("kind")))
                result = execute(cfg, command)
                try:
                    post_json(cfg, "/api/agent/commands/%s/result" % command["id"], result, timeout=30)
                except Exception as exc:  # noqa: BLE001
                    log("result upload failed: %s" % exc)
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
