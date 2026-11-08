from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import BackupPlan
from app.services import backups
from tests.test_monitoring import fake_report, make_project_and_server

RCLONE_CONF = '[onedrive]\ntype = onedrive\ntoken = {"access_token":"x"}\ndrive_type = personal\n'


async def test_destination_roundtrip_and_masking(client, auth):
    assert (await client.get("/api/backups/destination", headers=auth)).json()["configured"] is False
    saved = (
        await client.put(
            "/api/backups/destination",
            json={"type": "rclone", "remote": "onedrive", "rclone_conf": RCLONE_CONF},
            headers=auth,
        )
    ).json()
    assert saved["configured"] and saved["has_rclone_conf"] and saved["has_password"]
    assert "rclone_conf" not in saved and "restic_password" not in saved
    # sending the mask keeps the stored secret
    again = (
        await client.put(
            "/api/backups/destination",
            json={"type": "rclone", "remote": "onedrive", "rclone_conf": "••••••", "base_path": "bk"},
            headers=auth,
        )
    ).json()
    assert again["has_rclone_conf"] and again["base_path"] == "bk"
    bad = await client.put(
        "/api/backups/destination", json={"type": "rclone", "rclone_conf": "no section here"}, headers=auth
    )
    assert bad.status_code == 400


async def _setup(client, auth):
    pid, server = await make_project_and_server(client, auth)
    await client.put(
        "/api/backups/destination",
        json={"type": "rclone", "remote": "onedrive", "rclone_conf": RCLONE_CONF, "base_path": "backups"},
        headers=auth,
    )
    body = {
        "name": "db+code",
        "server_id": server["id"],
        "items": [
            {
                "type": "pg_dump",
                "mode": "docker",
                "container": "pg",
                "user": "app",
                "database": "app",
                "name": "db",
            },
            {"type": "path", "path": "/opt/app", "exclude": ["node_modules"]},
        ],
        "schedule": "0 3 * * *",
        "retention": {"daily": 7, "weekly": 4, "monthly": 6},
        "repo_subpath": "shop",
    }
    plan = await client.post(f"/api/projects/{pid}/backups", json=body, headers=auth)
    assert plan.status_code == 201, plan.text
    return pid, server, plan.json()


async def test_plan_crud_validation_and_repo_url(client, auth):
    pid, server, plan = await _setup(client, auth)
    assert plan["repo"] == "rclone:onedrive:backups/shop" and plan["next_run_at"]
    assert plan["server_name"] == "vps-1"

    bad_cron = await client.post(
        f"/api/projects/{pid}/backups",
        json={
            "name": "x",
            "server_id": server["id"],
            "items": [{"type": "path", "path": "/x"}],
            "schedule": "nonsense",
        },
        headers=auth,
    )
    assert bad_cron.status_code == 400
    bad_item = await client.post(
        f"/api/projects/{pid}/backups",
        json={"name": "x", "server_id": server["id"], "items": [{"type": "pg_dump"}]},
        headers=auth,
    )
    assert bad_item.status_code == 400

    other = (await client.post("/api/projects", json={"name": "Other"}, headers=auth)).json()["id"]
    foreign = await client.post(
        f"/api/projects/{other}/backups",
        json={"name": "x", "server_id": server["id"], "items": [{"type": "path", "path": "/x"}]},
        headers=auth,
    )
    assert foreign.status_code == 400  # server belongs to another project

    upd = (
        await client.put(
            f"/api/projects/{pid}/backups/{plan['id']}",
            json={
                **{
                    k: plan[k]
                    for k in (
                        "name",
                        "server_id",
                        "items",
                        "retention",
                        "max_age_hours",
                        "repo_subpath",
                        "enabled",
                    )
                },
                "schedule": "0 */6 * * *",
            },
            headers=auth,
        )
    ).json()
    assert upd["schedule"] == "0 */6 * * *"
    assert (await client.delete(f"/api/projects/{pid}/backups/{plan['id']}", headers=auth)).status_code == 204


async def test_run_queues_command_with_secrets_only_on_the_wire(client, auth):
    pid, server, plan = await _setup(client, auth)
    hdr = {"X-Agent-Token": server["token"]}
    run = (await client.post(f"/api/projects/{pid}/backups/{plan['id']}/run", headers=auth)).json()
    assert run["status"] == "running" and run["kind"] == "backup"

    # the stored command has no secrets
    cmds = (await client.get(f"/api/projects/{pid}/servers/{server['id']}/commands", headers=auth)).json()
    assert (
        cmds[0]["kind"] == "backup"
        and "password" not in cmds[0]["args"]
        and "rclone_conf" not in cmds[0]["args"]
    )
    assert cmds[0]["args"]["items"][0]["container"] == "pg"

    # ...but the agent receives them
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    assert len(handed) == 1
    args = handed[0]["args"]
    assert (
        args["repo"] == "rclone:onedrive:backups/shop"
        and args["password"]
        and "[onedrive]" in args["rclone_conf"]
    )
    assert args["retention"]["daily"] == 7 and args["plan_id"] == plan["id"]

    # agent reports success
    result = {
        "ok": True,
        "output": "repo ok\nok: db -> abc123",
        "results": [
            {"item": "db", "snapshot_id": "abc123", "added": 1000},
            {"item": "/opt/app", "snapshot_id": "def456", "added": 500},
        ],
        "repo_size": 123456,
        "duration": 12,
    }
    await client.post(f"/api/agent/commands/{handed[0]['id']}/result", json=result, headers=hdr)
    plans = (await client.get(f"/api/projects/{pid}/backups", headers=auth)).json()
    assert plans[0]["last_status"] == "ok" and plans[0]["last_ok_at"] and plans[0]["repo_size"] == 123456
    runs = (await client.get(f"/api/projects/{pid}/backups/{plan['id']}/runs", headers=auth)).json()
    backup_runs = [r for r in runs if r["kind"] == "backup"]
    assert backup_runs[0]["status"] == "ok" and backup_runs[0]["added_bytes"] == 1500
    assert backup_runs[0]["duration_s"] == 12
    assert (await client.get("/api/incidents", headers=auth)).json() == []
    # a successful backup queues an automatic snapshots refresh; let the agent finish it
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    assert handed[0]["kind"] == "backup_snapshots"
    await client.post(
        f"/api/agent/commands/{handed[0]['id']}/result",
        json={"ok": True, "output": "", "snapshots": []},
        headers=hdr,
    )

    # failure opens an incident, next success resolves it
    run2 = (await client.post(f"/api/projects/{pid}/backups/{plan['id']}/run", headers=auth)).json()
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    await client.post(
        f"/api/agent/commands/{handed[0]['id']}/result",
        json={"ok": False, "output": "boom", "errors": ["db: pg_dump failed"]},
        headers=hdr,
    )
    inc = (await client.get("/api/incidents", headers=auth)).json()
    assert len(inc) == 1 and inc[0]["kind"] == "backup_failed" and "pg_dump failed" in inc[0]["message"]
    assert (
        await client.get(f"/api/projects/{pid}/backups/{plan['id']}/runs/{run2['id']}", headers=auth)
    ).json()["status"] == "failed"

    # snapshots listing is cached on the plan
    await client.post(f"/api/projects/{pid}/backups/{plan['id']}/snapshots", headers=auth)
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    assert handed[0]["kind"] == "backup_snapshots"
    await client.post(
        f"/api/agent/commands/{handed[0]['id']}/result",
        json={
            "ok": True,
            "output": "2 snapshots",
            "snapshots": [
                {
                    "id": "abc123",
                    "time": "2026-11-03T03:00:00Z",
                    "tags": ["plan:1", "item:db"],
                    "paths": ["/db.sql"],
                }
            ],
        },
        headers=hdr,
    )
    plans = (await client.get(f"/api/projects/{pid}/backups", headers=auth)).json()
    assert plans[0]["snapshots"][0]["id"] == "abc123"

    # restore is queued with the snapshot id
    r = await client.post(
        f"/api/projects/{pid}/backups/{plan['id']}/restore", json={"snapshot_id": "abc123"}, headers=auth
    )
    assert r.status_code == 200
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    assert (
        handed[0]["kind"] == "restore"
        and handed[0]["args"]["snapshot_id"] == "abc123"
        and handed[0]["args"]["password"]
    )

    sheet = await client.get("/api/backups/recovery-sheet", headers=auth)
    assert (
        sheet.status_code == 200
        and "rclone:onedrive:backups/shop" in sheet.text
        and "RESTIC_PASSWORD" in sheet.text
    )


async def test_run_requires_destination(client, auth):
    pid, server = await make_project_and_server(client, auth)
    plan = (
        await client.post(
            f"/api/projects/{pid}/backups",
            json={"name": "p", "server_id": server["id"], "items": [{"type": "path", "path": "/x"}]},
            headers=auth,
        )
    ).json()
    resp = await client.post(f"/api/projects/{pid}/backups/{plan['id']}/run", headers=auth)
    assert resp.status_code == 400


async def test_tick_schedules_due_plans_and_flags_overdue(client, auth):
    pid, server, plan = await _setup(client, auth)
    async with SessionLocal() as db:
        row = await db.get(BackupPlan, plan["id"])
        row.next_run_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        row.created_at = datetime.now(timezone.utc) - timedelta(days=3)  # never succeeded, older than max_age
        await db.commit()
        await backups.tick(db)
        await db.refresh(row)
        from app.utils import aware

        assert row.last_status == "running" and aware(row.next_run_at) > datetime.now(timezone.utc)
    cmds = (await client.get(f"/api/projects/{pid}/servers/{server['id']}/commands", headers=auth)).json()
    assert cmds[0]["kind"] == "backup"
    kinds = {i["kind"] for i in (await client.get("/api/incidents", headers=auth)).json()}
    assert kinds == {"backup_overdue"}

    # a run stuck for > 6h is failed by the tick
    async with SessionLocal() as db:
        from app.models import BackupRun

        run = (await db.scalars(__import__("sqlalchemy").select(BackupRun))).first()
        run.started_at = datetime.now(timezone.utc) - timedelta(hours=7)
        await db.commit()
        await backups.tick(db)
    runs = (await client.get(f"/api/projects/{pid}/backups/{plan['id']}/runs", headers=auth)).json()
    assert runs[0]["status"] == "failed" and "TIMEOUT" in runs[0]["output"]


def test_compute_next_run_and_validate():
    assert backups.validate_schedule("0 3 * * *") and not backups.validate_schedule("x y")
    nxt = backups.compute_next_run("0 3 * * *")
    assert nxt is not None and nxt > datetime.now(timezone.utc)


async def test_backup_success_refreshes_snapshots(client, auth):
    pid, server, plan = await _setup(client, auth)
    hdr = {"X-Agent-Token": server["token"]}
    await client.post(f"/api/projects/{pid}/backups/{plan['id']}/run", headers=auth)
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    await client.post(
        f"/api/agent/commands/{handed[0]['id']}/result",
        json={
            "ok": True,
            "output": "ok",
            "results": [{"item": "db", "snapshot_id": "a", "added": 1}],
            "duration": 1,
        },
        headers=hdr,
    )
    # a snapshots listing was queued automatically...
    cmds = (await client.get(f"/api/projects/{pid}/servers/{server['id']}/commands", headers=auth)).json()
    assert [c["kind"] for c in cmds[:2]] == ["backup_snapshots", "backup"]
    runs = (await client.get(f"/api/projects/{pid}/backups/{plan['id']}/runs", headers=auth)).json()
    assert runs[0]["kind"] == "snapshots" and runs[0]["trigger"] == "auto" and runs[0]["status"] == "running"
    # ...and its result lands in plan.snapshots
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    snaps = [{"short_id": "a1", "time": "2026-11-08T01:00:00Z", "paths": ["/db.sql"], "tags": ["item:db"]}]
    await client.post(
        f"/api/agent/commands/{handed[0]['id']}/result",
        json={"ok": True, "output": "", "snapshots": snaps, "duration": 1},
        headers=hdr,
    )
    async with SessionLocal() as db:
        assert (await db.get(BackupPlan, plan["id"])).snapshots == snaps


async def test_weekly_check_is_scheduled(client, auth):
    pid, server, plan = await _setup(client, auth)
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        row = await db.get(BackupPlan, plan["id"])
        row.last_status, row.last_ok_at = "ok", now
        row.next_run_at = now + timedelta(hours=5)
        await db.commit()
        await backups.tick(db)  # never checked -> check now
        await backups.tick(db)  # already running -> nothing new
    runs = (await client.get(f"/api/projects/{pid}/backups/{plan['id']}/runs", headers=auth)).json()
    assert [r["kind"] for r in runs] == ["check"] and runs[0]["trigger"] == "schedule"

    hdr = {"X-Agent-Token": server["token"]}
    handed = (await client.post("/api/agent/report", json=fake_report(), headers=hdr)).json()["commands"]
    assert handed[0]["kind"] == "backup_check"
    await client.post(
        f"/api/agent/commands/{handed[0]['id']}/result",
        json={"ok": True, "output": "no errors", "duration": 3},
        headers=hdr,
    )
    async with SessionLocal() as db:
        await backups.tick(db)  # checked minutes ago -> not again
        from sqlalchemy import select

        from app.models import BackupRun

        assert len((await db.scalars(select(BackupRun))).all()) == 1
        run = (await db.scalars(select(BackupRun))).first()
        run.started_at = now - timedelta(days=8)
        await db.commit()
        await backups.tick(db)  # a week passed -> again
        assert len((await db.scalars(select(BackupRun))).all()) == 2
