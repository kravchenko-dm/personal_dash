import importlib.util
import json

import pytest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.db import SessionLocal
from app.models import Server
from app.services import alerts, monitoring

AGENT = Path(__file__).resolve().parents[2] / "agent" / "dash_agent.py"


def load_agent():
    spec = importlib.util.spec_from_file_location("dash_agent", AGENT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def make_project_and_server(client, auth):
    pid = (await client.post("/api/projects", json={"name": "Svc"}, headers=auth)).json()["id"]
    created = await client.post(f"/api/projects/{pid}/servers", json={"name": "vps-1"}, headers=auth)
    assert created.status_code == 201, created.text
    return pid, created.json()


def fake_report(**over):
    base = {
        "agent_version": "0.2.0",
        "hostname": "vps-1",
        "pretty_os": "Ubuntu 24.04",
        "metrics": {"load1": 0.5, "mem_pct": 40.0, "disks": [{"path": "/", "pct": 30.0}]},
        "services": [{"name": "api", "kind": "systemd", "status": "running", "restarts": 0}],
        "failed_units": [],
    }
    base.update(over)
    return base


async def test_server_token_and_install_script(client, auth):
    pid, server = await make_project_and_server(client, auth)
    token = server["token"]
    assert token.startswith("dash_") and "api/agent/install?token=" in server["install_command"]

    # token is stored hashed and listing does not leak it
    listed = (await client.get(f"/api/projects/{pid}/servers", headers=auth)).json()
    assert "token" not in listed[0] and listed[0]["online"] is False

    install = await client.get(f"/api/agent/install?token={token}")
    assert (
        install.status_code == 200
        and token in install.text
        and "systemctl enable --now dash-agent" in install.text
    )
    assert (await client.get("/api/agent/install?token=dash_wrongwrongwrong")).status_code == 401
    script = await client.get("/api/agent/script")
    assert script.status_code == 200 and "def build_report" in script.text

    assert (
        await client.post("/api/agent/report", json={}, headers={"X-Agent-Token": "nope"})
    ).status_code == 401


async def test_report_metrics_incidents_and_commands(client, auth):
    pid, server = await make_project_and_server(client, auth)
    hdr = {"X-Agent-Token": server["token"]}

    r = await client.post("/api/agent/report", json=fake_report(), headers=hdr)
    assert r.status_code == 200 and r.json()["commands"] == []
    s = (await client.get(f"/api/projects/{pid}/servers", headers=auth)).json()[0]
    assert s["online"] and s["hostname"] == "vps-1" and s["last_report"]["services"][0]["name"] == "api"
    metrics = (await client.get(f"/api/projects/{pid}/servers/{s['id']}/metrics", headers=auth)).json()[
        "samples"
    ]
    assert len(metrics) == 1 and metrics[0]["disk_pct"] == 30.0

    # disk high + service failed -> two incidents, project health critical
    bad = fake_report(
        metrics={"load1": 1, "mem_pct": 50, "disks": [{"path": "/", "pct": 91.0}]},
        services=[{"name": "api", "kind": "systemd", "status": "failed", "restarts": 1}],
    )
    await client.post("/api/agent/report", json=bad, headers=hdr)
    incidents = (await client.get(f"/api/projects/{pid}/incidents", headers=auth)).json()
    kinds = sorted(i["kind"] for i in incidents if i["resolved_at"] is None)
    assert kinds == ["disk_high", "service_down"]
    proj = (await client.get("/api/projects", headers=auth)).json()[0]
    assert proj["health"] == "critical" and proj["open_incidents"] == 2

    # same bad report again -> no duplicate incidents
    await client.post("/api/agent/report", json=bad, headers=hdr)
    assert len((await client.get(f"/api/projects/{pid}/incidents", headers=auth)).json()) == 2

    # recovery resolves both
    await client.post("/api/agent/report", json=fake_report(), headers=hdr)
    incidents = (await client.get(f"/api/projects/{pid}/incidents", headers=auth)).json()
    assert all(i["resolved_at"] for i in incidents)
    assert (await client.get("/api/incidents", headers=auth)).json() == []

    # command queue roundtrip
    cmd = (
        await client.post(
            f"/api/projects/{pid}/servers/{s['id']}/commands",
            json={"kind": "logs", "args": {"service": "api"}},
            headers=auth,
        )
    ).json()
    assert cmd["status"] == "queued"
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    assert handed == [{"id": cmd["id"], "kind": "logs", "args": {"service": "api"}}]
    assert (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()[
        "commands"
    ] == []  # sent once
    res = await client.post(
        f"/api/agent/commands/{cmd['id']}/result", json={"ok": True, "output": "line1\nline2"}, headers=hdr
    )
    assert res.status_code == 200
    done = (
        await client.get(f"/api/projects/{pid}/servers/{s['id']}/commands/{cmd['id']}", headers=auth)
    ).json()
    assert done["status"] == "done" and done["output"] == "line1\nline2"


async def test_failed_units_and_restart_loop(client, auth):
    pid, server = await make_project_and_server(client, auth)
    hdr = {"X-Agent-Token": server["token"]}
    await client.post("/api/agent/report", json=fake_report(failed_units=["cron.service"]), headers=hdr)
    inc = (await client.get(f"/api/projects/{pid}/incidents", headers=auth)).json()
    assert inc[0]["kind"] == "unit_failed" and "cron.service" in inc[0]["message"]
    await client.post("/api/agent/report", json=fake_report(), headers=hdr)
    assert (await client.get(f"/api/projects/{pid}/incidents", headers=auth)).json()[0]["resolved_at"]

    await client.post(
        "/api/agent/report",
        json=fake_report(services=[{"name": "api", "kind": "docker", "status": "running", "restarts": 1}]),
        headers=hdr,
    )
    await client.post(
        "/api/agent/report",
        json=fake_report(services=[{"name": "api", "kind": "docker", "status": "running", "restarts": 6}]),
        headers=hdr,
    )
    kinds = [
        i["kind"]
        for i in (await client.get(f"/api/projects/{pid}/incidents", headers=auth)).json()
        if not i["resolved_at"]
    ]
    assert kinds == ["restart_loop"]
    # restarts stop growing -> resolved
    await client.post(
        "/api/agent/report",
        json=fake_report(services=[{"name": "api", "kind": "docker", "status": "running", "restarts": 6}]),
        headers=hdr,
    )
    assert (await client.get("/api/incidents", headers=auth)).json() == []


async def test_silent_agent_detection(client, auth):
    pid, server = await make_project_and_server(client, auth)
    await client.post("/api/agent/report", json=fake_report(), headers={"X-Agent-Token": server["token"]})
    async with SessionLocal() as db:
        row = await db.get(Server, server["id"])
        row.last_seen_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await db.commit()
        await monitoring.check_silent_agents(db)
    inc = (await client.get("/api/incidents", headers=auth)).json()
    assert len(inc) == 1 and inc[0]["kind"] == "agent_silent"
    # agent comes back -> resolved
    await client.post("/api/agent/report", json=fake_report(), headers={"X-Agent-Token": server["token"]})
    assert (await client.get("/api/incidents", headers=auth)).json() == []


async def test_http_monitor_checks(client, auth, monkeypatch):
    pid = (await client.post("/api/projects", json={"name": "Site"}, headers=auth)).json()["id"]
    state = {"code": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(state["code"], text="hello world")

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))

    m = (
        await client.post(
            f"/api/projects/{pid}/monitors",
            json={"name": "Главная", "url": "https://example.org/", "expect_text": "hello"},
            headers=auth,
        )
    ).json()
    assert m["last_ok"] is True and m["last_status"] == 200 and m["consecutive_failures"] == 0

    state["code"] = 503
    first = (await client.post(f"/api/projects/{pid}/monitors/{m['id']}/check", headers=auth)).json()
    assert first["last_ok"] is False and first["consecutive_failures"] == 1
    assert (
        await client.get("/api/incidents", headers=auth)
    ).json() == []  # one failure is not an incident yet
    second = (await client.post(f"/api/projects/{pid}/monitors/{m['id']}/check", headers=auth)).json()
    assert second["consecutive_failures"] == 2
    inc = (await client.get("/api/incidents", headers=auth)).json()
    assert len(inc) == 1 and inc[0]["kind"] == "http_down" and "503" in inc[0]["message"]

    state["code"] = 200
    await client.post(f"/api/projects/{pid}/monitors/{m['id']}/check", headers=auth)
    assert (await client.get("/api/incidents", headers=auth)).json() == []
    up = (await client.get(f"/api/projects/{pid}/monitors/{m['id']}/uptime", headers=auth)).json()
    assert up["uptime_pct"] == 50.0 and len(up["samples"]) == 4

    # expect_text mismatch is a failure too
    upd = {"name": "Главная", "url": "https://example.org/", "expect_text": "absent"}
    await client.put(f"/api/projects/{pid}/monitors/{m['id']}", json=upd, headers=auth)
    chk = (await client.post(f"/api/projects/{pid}/monitors/{m['id']}/check", headers=auth)).json()
    assert chk["last_ok"] is False and "absent" in chk["last_error"]


async def test_manual_resolve_and_health_warning(client, auth):
    pid = (await client.post("/api/projects", json={"name": "W"}, headers=auth)).json()["id"]
    async with SessionLocal() as db:
        await alerts.open_incident(db, pid, "disk_high", "x", "диск", "warning")
        await db.commit()
    proj = (await client.get("/api/projects", headers=auth)).json()[0]
    assert proj["health"] == "warning"
    inc = (await client.get("/api/incidents", headers=auth)).json()[0]
    resolved = (await client.post(f"/api/projects/{pid}/incidents/{inc['id']}/resolve", headers=auth)).json()
    assert resolved["resolved_at"]
    assert (await client.get("/api/projects", headers=auth)).json()[0]["health"] == "ok"


def test_agent_script_collects_and_executes(tmp_path):
    import subprocess

    agent = load_agent()
    marker = subprocess.Popen(["sleep", "30"])  # a process we know exists, independent of the runner
    try:
        cfg = {
            "url": "http://x",
            "token": "t",
            "services": [
                {"name": "nope", "kind": "systemd", "unit": "definitely-missing.service"},
                {"name": "self", "kind": "process", "match": "sleep 30"},
            ],
            "discover_docker": False,
        }
        report = agent.build_report(cfg)
    finally:
        marker.kill()
        marker.wait()
    assert report["metrics"]["mem_total"] > 0 and report["metrics"]["disks"][0]["path"] == "/"
    names = {s["name"]: s for s in report["services"]}
    assert names["self"]["status"] == "running"
    assert names["nope"]["status"] in ("stopped", "unknown", "failed", "missing")
    json.dumps(report)  # serialisable

    assert agent.execute(cfg, {"kind": "ping"})["ok"] is True
    assert agent.execute(cfg, {"kind": "restart", "args": {"service": "ghost"}})["ok"] is False
    assert agent.execute(cfg, {"kind": "script", "args": {"name": "../etc/passwd"}})["ok"] is False
    assert agent.execute(cfg, {"kind": "weird"})["ok"] is False

    # version detection: file marker, then mtime fallback for non-git dirs
    (tmp_path / "VERSION").write_text("1.4.2\n")
    assert agent.service_version({"version_file": str(tmp_path / "VERSION")}) == {
        "kind": "file",
        "value": "1.4.2",
    }
    (tmp_path / "app.py").write_text("print(1)")
    assert agent.service_version({"path": str(tmp_path)})["kind"] == "mtime"


def test_agent_scheduled_services(monkeypatch):
    agent = load_agent()

    class R:
        def __init__(self, out):
            self.stdout, self.stderr, self.returncode = out, "", 0

    def fake_run(cmd, timeout=20):
        unit = cmd[2]
        if unit == "tg-metrics.service":
            return R(
                "ActiveState=inactive\nSubState=dead\nNRestarts=0\nExecMainStartTimestamp=Mon 2026-11-03 06:00:01 UTC\n"
                "ExecMainExitTimestamp=Mon 2026-11-03 06:00:12 UTC\nMainPID=0\nResult=success\nType=oneshot\nExecMainStatus=0\nLoadState=loaded\n"
            )
        if unit == "tg-metrics.timer":
            return R(
                "ActiveState=active\nLastTriggerUSec=Mon 2026-11-03 06:00:00 UTC\nNextElapseUSecRealtime=Tue 2026-11-04 06:00:00 UTC\nLoadState=loaded\n"
            )
        if unit == "broken.service":
            return R(
                "ActiveState=failed\nSubState=failed\nNRestarts=0\nExecMainExitTimestamp=Mon 2026-11-03 06:00:12 UTC\n"
                "MainPID=0\nResult=exit-code\nType=oneshot\nExecMainStatus=1\nLoadState=loaded\n"
            )
        if unit == "broken.timer":
            return R("LoadState=not-found\n")
        if unit == "api.service":
            return R(
                "ActiveState=active\nSubState=running\nNRestarts=2\nMainPID=42\nResult=success\nType=simple\nLoadState=loaded\n"
            )
        if unit == "api.timer":
            return R("LoadState=not-found\n")
        if unit == "tg-social.service":  # Type=simple, timer with a different name
            return R(
                "ActiveState=inactive\nSubState=dead\nNRestarts=0\nExecMainExitTimestamp=Mon 2026-11-03 18:20:00 UTC\n"
                "MainPID=0\nResult=success\nType=simple\nExecMainStatus=0\nLoadState=loaded\nTriggeredBy=tg-social-hourly.timer\n"
            )
        if unit == "tg-social-hourly.timer":
            return R(
                "ActiveState=active\nLastTriggerUSec=Mon 2026-11-03 18:00:00 UTC\nNextElapseUSecRealtime=Mon 2026-11-03 19:00:00 UTC\nLoadState=loaded\n"
            )
        if unit == "cronjob.service":  # started by cron via systemctl start, no timer at all
            return R(
                "ActiveState=inactive\nSubState=dead\nNRestarts=0\nExecMainExitTimestamp=Mon 2026-11-03 18:20:00 UTC\n"
                "MainPID=0\nResult=success\nType=simple\nExecMainStatus=0\nLoadState=loaded\nTriggeredBy=\n"
            )
        if unit == "crashed.service":  # long-running service that died: still 'stopped'
            return R(
                "ActiveState=inactive\nSubState=dead\nNRestarts=3\nExecMainExitTimestamp=Mon 2026-11-03 18:20:00 UTC\n"
                "MainPID=0\nResult=success\nType=simple\nExecMainStatus=0\nLoadState=loaded\nTriggeredBy=\n"
            )
        if unit.endswith(".timer"):
            return R("LoadState=not-found\n")
        if unit == "ghost.service":
            return R("LoadState=not-found\n")
        return R("")

    monkeypatch.setattr(agent, "run", fake_run)
    monkeypatch.setattr(agent, "which", lambda name: True)

    idle = agent.systemd_status("tg-metrics.service")
    assert idle["status"] == "idle" and idle["scheduled"] is True
    assert idle["last_run"].startswith("2026-11-03") and idle["next_run"].startswith("2026-11-04")

    stale = agent.systemd_status("tg-metrics.service", {"max_age_hours": 1})
    assert stale["status"] == "stale"

    broken = agent.systemd_status("broken.service")
    assert broken["status"] == "failed" and "exit-code" in broken["detail"]

    plain = agent.systemd_status("api.service")
    assert plain["status"] == "running" and plain["restarts"] == 2 and "scheduled" not in plain

    assert agent.systemd_status("ghost.service")["status"] == "missing"

    other_timer = agent.systemd_status("tg-social.service")
    assert other_timer["status"] == "idle" and other_timer["next_run"].startswith("2026-11-03T")
    assert agent.systemd_status("cronjob.service")["status"] == "idle"  # clean exit, no restarts -> a job
    assert (
        agent.systemd_status("crashed.service")["status"] == "stopped"
    )  # restarts > 0 -> a daemon that died
    assert agent.systemd_status("cronjob.service", {"scheduled": False})["status"] == "stopped"


async def test_idle_scheduled_service_is_not_an_incident(client, auth):
    pid, server = await make_project_and_server(client, auth)
    hdr = {"X-Agent-Token": server["token"]}
    r = await client.post(
        "/api/agent/report",
        json=fake_report(
            services=[
                {"name": "tg-metrics", "kind": "systemd", "status": "idle", "scheduled": True, "restarts": 0}
            ]
        ),
        headers=hdr,
    )
    assert r.json()["agent_version"] == load_agent().AGENT_VERSION
    assert (await client.get("/api/incidents", headers=auth)).json() == []
    await client.post(
        "/api/agent/report",
        json=fake_report(
            services=[{"name": "tg-metrics", "kind": "systemd", "status": "stale", "scheduled": True}]
        ),
        headers=hdr,
    )
    inc = (await client.get("/api/incidents", headers=auth)).json()
    assert len(inc) == 1 and "stale" in inc[0]["message"]


def test_agent_cron_toggle_roundtrip():
    agent = load_agent()
    text = "MAILTO=x\n30 23 * * * /home/dev/backup.sh >> /var/log/b.log 2>&1\n0 1 * * * echo hi\n"
    disabled, changed = agent._toggle_lines(text, "backup.sh", True)
    assert changed == ["30 23 * * * /home/dev/backup.sh >> /var/log/b.log 2>&1"]
    assert "#dash-disabled# 30 23" in disabled and "0 1 * * * echo hi" in disabled
    restored, changed2 = agent._toggle_lines(disabled, "backup.sh", False)
    assert restored == text and changed2 == changed
    # a second disable is a no-op, short matches are refused, only real cron files may be edited
    assert agent._toggle_lines(disabled, "backup.sh", True)[1] == []
    assert agent.cmd_cron({"action": "disable", "match": "ab"})["ok"] is False
    assert agent.cmd_cron({"action": "disable", "match": "backup", "file": "/etc/shadow"})["ok"] is False


def test_agent_config_command(tmp_path, monkeypatch):
    agent = load_agent()
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text('{"url": "https://d", "token": "dash_secret_value", "services": []}')
    monkeypatch.setattr(agent, "CONFIG_PATH", str(cfg_path))
    monkeypatch.setattr(agent, "CRON_BACKUP_DIR", str(tmp_path / "bak"))
    monkeypatch.setattr(agent, "collect_services", lambda cfg: [{"name": s["name"], "status": "running", "detail": ""} for s in cfg["services"]])
    cfg = json.loads(cfg_path.read_text())
    shown = agent.cmd_config(cfg, {"action": "get"})
    assert shown["ok"] and "dash_secret_value" not in shown["output"]
    assert agent.cmd_config(cfg, {"action": "set", "set": {"token": "x"}})["ok"] is False
    assert agent.cmd_config(cfg, {"action": "set", "set": {"services": [{"name": "a", "kind": "weird"}]}})["ok"] is False
    res = agent.cmd_config(cfg, {"action": "set", "set": {"services": [{"name": "api", "kind": "docker", "container": "api-1"}], "disks": ["/"]}})
    assert res["ok"], res
    new = json.loads(cfg_path.read_text())
    assert new["token"] == "dash_secret_value" and new["services"][0]["container"] == "api-1" and new["disks"] == ["/"]
    assert list((tmp_path / "bak").iterdir())  # previous copy kept


def test_agent_clickhouse_dump_command():
    agent = load_agent()
    argv, fname = agent._dump_command({"type": "clickhouse", "container": "ch", "database": "metrics", "table": "post_metrics"})
    assert argv[:4] == ["docker", "exec", "-i", "ch"] and argv[-1].startswith("SELECT * FROM `metrics`.`post_metrics`")
    assert fname == "metrics.post_metrics.native"
    with pytest.raises(RuntimeError):
        agent._dump_command({"type": "clickhouse", "container": "ch", "database": "metrics", "table": "x; drop"})
