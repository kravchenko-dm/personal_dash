"""Personal workspaces: every member sees only their own tasks, chats and summaries."""

from types import SimpleNamespace as NS

import pytest_asyncio
from sqlalchemy import select

from app import auth as auth_core
from app.config import get_settings
from app.db import SessionLocal
from app.models import PushSubscription, Summary, Task, TelegramChat, TelegramMessage
from app.services import bot_access, push, scheduler, summaries, telegram_bot
from app.services.summarizer import ProposedTask, SummaryResult

from .conftest import MEMBER_TG

OWNER_TG = 100


@pytest_asyncio.fixture(autouse=True)
async def owner_tg(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", str(OWNER_TG))
    get_settings.cache_clear()
    async with SessionLocal() as db:
        await bot_access.load(db)
    yield
    get_settings.cache_clear()


async def test_member_sees_only_own_tasks_and_no_owner_pages(client, auth, member_auth, member):
    await client.post("/api/tasks", json={"title": "владельца"}, headers=auth)
    mine = (await client.post("/api/tasks", json={"title": "моя"}, headers=member_auth)).json()
    assert [t["title"] for t in (await client.get("/api/tasks", headers=member_auth)).json()] == ["моя"]
    assert [t["title"] for t in (await client.get("/api/tasks", headers=auth)).json()] == ["владельца"]
    owner_task = (await client.get("/api/tasks", headers=auth)).json()[0]["id"]
    assert (
        await client.patch(f"/api/tasks/{owner_task}", json={"status": "done"}, headers=member_auth)
    ).status_code == 404
    assert (await client.delete(f"/api/tasks/{owner_task}", headers=member_auth)).status_code == 404
    assert (
        await client.patch(f"/api/tasks/{mine['id']}", json={"status": "done"}, headers=member_auth)
    ).status_code == 200

    for url in ("/api/balances", "/api/projects", "/api/incidents", "/api/auth/events", "/api/telegram/users",
                "/api/calendar/events", "/api/apple/reminders", "/api/yougile/alerts"):  # fmt: skip
        assert (await client.get(url, headers=member_auth)).status_code == 403, url
    me = (await client.get("/api/auth/me", headers=member_auth)).json()
    assert me["role"] == "member" and me["level"] == "workspace" and me["name"] == "Пётр"
    status = (await client.get("/api/status", headers=member_auth)).json()
    assert status["llm_model"] == "" and status["yougile"] is False


async def test_media_level_cannot_log_in_and_logout_all_is_personal(client, auth, member_auth, member):
    async with SessionLocal() as db:
        guest = await bot_access.add(db, 777, "Гость", level="media")
    assert (
        await client.get("/api/tasks", headers={"Authorization": f"Bearer {auth_core.issue_token(guest.id)}"})
    ).status_code == 401
    # a member's "log out everywhere" does not touch the owner
    import time

    time.sleep(1.1)
    new = (await client.post("/api/auth/logout-all", headers=member_auth)).json()["token"]
    assert (await client.get("/api/tasks", headers=member_auth)).status_code == 401
    assert (await client.get("/api/tasks", headers={"Authorization": f"Bearer {new}"})).status_code == 200
    assert (await client.get("/api/tasks", headers=auth)).status_code == 200
    # legacy owner tokens ("sub": "owner") keep working
    import jwt

    legacy = jwt.encode({"sub": "owner", "iat": int(time.time()), "exp": int(time.time()) + 60},
                        get_settings().app_secret, algorithm="HS256")  # fmt: skip
    assert (
        await client.get("/api/balances", headers={"Authorization": f"Bearer {legacy}"})
    ).status_code == 200


async def test_login_code_once_and_expiry(client, member):
    code = auth_core.new_login_code(member)
    resp = await client.post("/api/auth/telegram", json={"code": code})
    assert resp.status_code == 200
    token = resp.json()["token"]
    assert (await client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})).json()[
        "id"
    ] == member
    assert (await client.post("/api/auth/telegram", json={"code": code})).status_code == 401  # used
    old = auth_core.new_login_code(member)
    h = auth_core._hash(old)
    auth_core._codes[h] = (member, 0)  # expired
    assert (await client.post("/api/auth/telegram", json={"code": old})).status_code == 401
    for _ in range(3):
        await client.post("/api/auth/telegram", json={"code": "guess"})
    assert (
        await client.post("/api/auth/telegram", json={"code": "guess"})
    ).status_code == 429  # 5 fails: locked


async def test_bot_login_link_and_commands_are_personal(monkeypatch, member):
    answers = []

    class Msg:
        def __init__(self, uid, text="", chat_type="private"):
            self.from_user = NS(id=uid, full_name="Пётр", username="petr")
            self.chat = NS(id=uid, type=chat_type, first_name="Пётр", last_name=None, title=None)
            self.text, self.caption, self.message_id = text, None, 1

        async def answer(self, text, **kw):
            answers.append((text, kw.get("reply_markup")))

    await telegram_bot.cmd_login(Msg(MEMBER_TG))
    assert "/login?code=" in answers[-1][0]
    code = answers[-1][0].split("code=")[1].split()[0]
    assert auth_core.redeem_login_code(code) == member
    await telegram_bot.cmd_login(Msg(999))
    assert "запросить доступ" in answers[-1][0]

    await telegram_bot.cmd_add(Msg(MEMBER_TG), NS(args="позвонить"))
    await telegram_bot.cmd_add(Msg(OWNER_TG), NS(args="своё"))
    await telegram_bot.cmd_tasks(Msg(MEMBER_TG))
    assert "позвонить" in answers[-1][0] and "своё" not in answers[-1][0]
    async with SessionLocal() as db:
        owner_task = await db.scalar(select(Task).where(Task.title == "своё"))
    await telegram_bot.cmd_done(Msg(MEMBER_TG), NS(args=str(owner_task.id)))
    assert answers[-1][0] == "Нет такой задачи"


async def test_summary_reads_only_own_chats(monkeypatch, member):
    seen = {}

    async def fake_summarize(messages, open_tasks, calendar_text="", period_label=""):
        seen.update(messages=messages, tasks=open_tasks, calendar=calendar_text)
        return SummaryResult(summary="ok", highlights=[], proposed_tasks=[ProposedTask(title="Сделать")])

    monkeypatch.setattr(summaries.summarizer, "summarize", fake_summarize)
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        db.add_all([
            TelegramChat(chat_id=-1, title="Владелец", kind="group", user_id=1),
            TelegramChat(chat_id=-2, title="Петра", kind="group", user_id=member),
            TelegramMessage(chat_id=-1, message_id=1, sender="A", text="секрет владельца", sent_at=now),
            TelegramMessage(chat_id=-2, message_id=2, sender="B", text="дело Петра", sent_at=now),
            Task(title="задача владельца", user_id=1),
        ])  # fmt: skip
        await db.commit()
        row = await summaries.build_summary(db, user_id=member)
        assert row.user_id == member
        assert "дело Петра" in seen["messages"] and "секрет" not in seen["messages"]
        assert "задача владельца" not in seen["tasks"] and seen["calendar"] == ""
        try:
            await summaries.build_summary(db, chat_id=-1, user_id=member)
            raise AssertionError("foreign chat summarized")
        except RuntimeError:
            pass


async def test_reminders_and_alerts_go_to_the_right_people(monkeypatch, member):
    pushed, sent = [], []

    async def fake_notify(db, user_id, title, body, url="/", tag=None):
        pushed.append((user_id, title))
        return 1

    async def fake_send_user(tid, text, reply_markup=None):
        sent.append((tid, text))

    async def fake_send_owner(text, reply_markup=None):
        sent.append(("owner", text))

    monkeypatch.setattr(push, "_notify", fake_notify)
    monkeypatch.setattr(telegram_bot, "send_to_user", fake_send_user)
    monkeypatch.setattr(telegram_bot, "send_to_owner", fake_send_owner)
    from datetime import datetime, timezone

    async with SessionLocal() as db:
        db.add(Task(title="Звонок", user_id=member, due_at=datetime.now(timezone.utc)))
        await db.commit()
    await scheduler.job_task_reminders()
    assert pushed == [(member, "Задача: Звонок")] and sent[0][0] == MEMBER_TG

    async with SessionLocal() as db:
        await push.notify_owner(db, "Сервер упал", "x")
    assert pushed[-1] == (1, "Сервер упал")

    await scheduler.job_morning_brief()
    assert any(t == MEMBER_TG and "Звонок" in text for t, text in sent)
    assert any(t == "owner" and "Звонок" not in text for t, text in sent)


async def test_push_subscription_is_bound_to_user(client, member_auth, member):
    body = {"endpoint": "https://push/1", "keys": {"p256dh": "a", "auth": "b"}}
    await client.post("/api/push/subscribe", json=body, headers=member_auth)
    async with SessionLocal() as db:
        assert (await db.scalar(select(PushSubscription))).user_id == member


async def test_removing_member_deletes_their_data(member):
    async with SessionLocal() as db:
        db.add_all([Task(title="x", user_id=member), Summary(user_id=member, period_start=__import__("datetime").datetime.now(), period_end=__import__("datetime").datetime.now(), text="t", proposed_tasks=[])])  # fmt: skip
        await db.commit()
        assert await bot_access.remove(db, member)
        assert (await db.scalars(select(Task))).all() == [] and (
            await db.scalars(select(Summary))
        ).all() == []
