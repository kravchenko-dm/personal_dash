"""YouGile alerts: deadline reminders, change detection with actors, morning brief block."""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.config import get_settings
from app.db import SessionLocal
from app.services import push, telegram_bot, yougile_alerts
from app.services import yougile as yg

ME, ANNA, OLEG = "u-me", "u-anna", "u-oleg"
NOW = datetime(2026, 11, 9, 9, 0, tzinfo=timezone.utc)  # 12:00 MSK
MS = lambda d: int(d.timestamp() * 1000)  # noqa: E731


def task(tid, title, *, assigned=(), created=ANNA, completed=False, deadline=None, with_time=False, by=None):
    t = {
        "id": tid,
        "title": title,
        "columnId": "col1",
        "assigned": list(assigned),
        "createdBy": created,
        "completed": completed,
        "archived": False,
        "deleted": None,
    }
    if deadline is not None:
        t["deadline"] = {"deadline": deadline, "withTime": with_time, "history": [{"by": by or created}]}
    return t


def allday(d: date) -> int:
    return MS(datetime(d.year, d.month, d.day, 12, tzinfo=timezone.utc))


@pytest.fixture
def world(monkeypatch):
    w = {"tasks": [], "logs": {}, "push": [], "tg": [], "log_calls": 0}
    monkeypatch.setattr(get_settings(), "yougile_api_key", "k")

    async def me():
        return {"id": ME}

    async def tasks(column_id=None, include_deleted=False):
        return [dict(t) for t in w["tasks"]]

    async def columns(board_id=None):
        return [{"id": "col1", "boardId": "b1", "title": "В работе"}]

    async def boards(project_id=None):
        return [{"id": "b1", "projectId": "p1", "title": "Доска"}]

    async def projects():
        return [{"id": "p1", "title": "ВСМ "}]

    async def users():
        return [
            {"id": ME, "realName": "Дмитрий"},
            {"id": ANNA, "realName": "Анна"},
            {"id": OLEG, "realName": "Олег"},
        ]

    async def task_log(tid, limit=20):
        w["log_calls"] += 1
        return w["logs"].get(tid, [])

    async def notify_all(db, title, body, url="/", tag=None):
        w["push"].append((title, body, tag))
        return 1

    async def send_to_owner(text, reply_markup=None):
        w["tg"].append(text)

    for name, fn in dict(
        me=me, tasks=tasks, columns=columns, boards=boards, projects=projects, users=users, task_log=task_log
    ).items():
        monkeypatch.setattr(yg, name, fn)
    monkeypatch.setattr(push, "notify_all", notify_all)
    monkeypatch.setattr(telegram_bot, "send_to_owner", send_to_owner)
    return w


async def test_reminder_before_timed_deadline(world):
    world["tasks"] = [
        task("a", "Сдать отчёт", assigned=[OLEG], deadline=MS(NOW + timedelta(minutes=40)), with_time=True),
        task("b", "Без времени", deadline=allday(NOW.date())),
        task("c", "Готово", completed=True, deadline=MS(NOW + timedelta(minutes=10)), with_time=True),
        task("d", "Не скоро", deadline=MS(NOW + timedelta(hours=5)), with_time=True),
    ]
    async with SessionLocal() as db:
        stats = await yougile_alerts.tick(db, now=NOW)
        assert stats["reminded"] == 1
        assert world["push"][0][0] == "Через 40 мин срок: Сдать отчёт"
        assert "ВСМ · срок 09.11 12:40 · Олег" == world["push"][0][1]
        assert world["tg"][0].startswith("🗂 Через 40 мин срок: Сдать отчёт")
        stats = await yougile_alerts.tick(db, now=NOW + timedelta(minutes=5))
        assert stats["reminded"] == 0  # once per deadline


async def test_changes_with_actors_and_own_actions_skipped(world):
    base = MS(NOW + timedelta(days=2))
    world["tasks"] = [
        task("done1", "Макет", assigned=[ANNA], deadline=base),
        task("done2", "Мой отчёт", assigned=[ME], created=ME),
        task("asg", "Проверить", assigned=[ANNA]),
        task("mv", "Пост", deadline=base),
        task("mv2", "Пост 2", deadline=base),
    ]
    async with SessionLocal() as db:
        stats = await yougile_alerts.tick(db, now=NOW)  # baseline only
        assert not any(stats.values()) and not world["tg"] and not world["push"]

        world["tasks"] = [
            task("done1", "Макет", assigned=[ANNA], deadline=base, completed=True),
            task("done2", "Мой отчёт", assigned=[ME], created=ME, completed=True),
            task("asg", "Проверить", assigned=[ANNA, ME]),
            task("mv", "Пост", deadline=base + 86_400_000, by=OLEG),
            task("mv2", "Пост 2", deadline=base + 86_400_000, by=ME),
            task("new1", "Новая от Олега", created=OLEG),
            task("new2", "Сам себе", created=ME),
            task("new3", "Тебе", created=OLEG, assigned=[ME]),
        ]
        world["logs"] = {
            "done1": [{"type": "move", "userId": OLEG}, {"type": "done", "userId": ANNA}],
            "done2": [{"type": "done", "userId": ME}],
            "asg": [{"type": "executor", "userId": OLEG}],
        }
        stats = await yougile_alerts.tick(db, now=NOW + timedelta(minutes=5))
        assert stats == {"reminded": 0, "added": 1, "assigned": 2, "completed": 1, "moved": 1}
        msg = world["tg"][-1]
        assert "✅ Выполнено: Макет (Анна)" in msg and "Мой отчёт" not in msg
        assert "👤 Назначена на вас: Проверить (Олег)" in msg
        assert "📆 Срок перенесён: Пост (Олег)" in msg and "было 11.11" in msg and "Пост 2" not in msg
        assert "➕ Новая от Олега (Олег)" in msg and "Сам себе" not in msg
        assert "👤 Новая задача на вас: Тебе (Олег)" in msg
        # 5 changes > PUSH_EACH_LIMIT: one summary push
        assert len(world["push"]) == 1 and world["push"][0][0] == "YouGile: 5 изменений"


async def test_scope_mine_filters_others(world):
    world["tasks"] = [task("x", "Чужая", assigned=[ANNA])]
    async with SessionLocal() as db:
        await yougile_alerts.set_config(db, {"scope": "mine"})
        await yougile_alerts.tick(db, now=NOW)
        world["tasks"] = [
            task("x", "Чужая", assigned=[ANNA], completed=True),
            task("y", "Ещё чужая", created=OLEG),
        ]
        stats = await yougile_alerts.tick(db, now=NOW + timedelta(minutes=5))
        assert not any(stats.values()) and world["log_calls"] == 0


async def test_brief_text(world):
    today = date(2026, 11, 9)
    world["tasks"] = [
        task("o", "Просрочка", assigned=[ANNA], deadline=allday(today - timedelta(days=2))),
        task(
            "t",
            "Сегодня созвон",
            assigned=[ME, OLEG],
            deadline=MS(datetime(2026, 11, 9, 13, tzinfo=timezone.utc)),
            with_time=True,
        ),
        task("m", "Завтра", deadline=allday(today + timedelta(days=1))),
        task("later", "Потом", deadline=allday(today + timedelta(days=5))),
        task("done", "Сделано", completed=True, deadline=allday(today)),
    ]
    async with SessionLocal() as db:
        text = await yougile_alerts.brief_text(db, today=today)
        assert text.splitlines() == [
            "⚠️ Просрочено (1):",
            "• Просрочка — ВСМ · срок 07.11 · Анна",
            "Сегодня (1):",
            "• Сегодня созвон — ВСМ · 16:00 · вы, Олег",
            "Завтра (1):",
            "• Завтра — ВСМ",
        ]
        await yougile_alerts.set_config(db, {"brief": False})
        assert await yougile_alerts.brief_text(db, today=today) == ""


async def test_alerts_endpoint(client, auth):
    r = await client.get("/api/yougile/alerts", headers=auth)
    assert r.status_code == 200 and r.json()["scope"] == "all" and r.json()["remind_minutes"] == 60
    r = await client.put(
        "/api/yougile/alerts",
        json={"scope": "mine", "remind_minutes": 30, "watch_changes": False, "brief": True},
        headers=auth,
    )
    assert r.status_code == 200 and r.json()["scope"] == "mine"
    assert (await client.get("/api/yougile/alerts", headers=auth)).json()["remind_minutes"] == 30
    r = await client.put("/api/yougile/alerts", json={"remind_minutes": 7}, headers=auth)
    assert r.status_code == 400
    r = await client.put("/api/yougile/alerts", json={"scope": "boss"}, headers=auth)
    assert r.status_code == 422


def test_short_titles():
    assert yougile_alerts._short("учет  работы\n\n\nкак   это") == "учет работы как это"
    long = yougile_alerts._short("слово " * 50, 30)
    assert len(long) == 30 and long.endswith("…")
