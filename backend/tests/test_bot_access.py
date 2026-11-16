from types import SimpleNamespace as NS

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import MediaJob, TelegramChat, TelegramMessage
from app.services import bot_access, telegram_bot, telegram_media

OWNER, GUEST, STRANGER = 100, 200, 300


@pytest_asyncio.fixture(autouse=True)
async def owner(monkeypatch):
    monkeypatch.setenv("TELEGRAM_OWNER_ID", str(OWNER))
    get_settings.cache_clear()
    async with SessionLocal() as db:
        await bot_access.load(db)  # the owner row gets TELEGRAM_OWNER_ID
    yield
    get_settings.cache_clear()


@pytest.fixture
def to_owner(monkeypatch):
    sent = []

    async def fake(text, reply_markup=None):
        sent.append((text, reply_markup))

    monkeypatch.setattr(telegram_bot, "send_to_owner", fake)
    return sent


def user(uid, name="Пётр", username="petr"):
    return NS(id=uid, full_name=name, username=username)


class Msg:
    def __init__(self, uid, chat_type="private", chat_id=None, text="hi"):
        self.from_user = user(uid) if uid else None
        self.chat = NS(id=chat_id or uid, type=chat_type, title="Группа", first_name="Пётр", last_name=None)
        self.text, self.caption, self.message_id = text, None, 1
        self.date = __import__("datetime").datetime.now(__import__("datetime").timezone.utc)
        self.answers = []

    async def answer(self, text, **kw):
        self.answers.append(text)


async def test_guest_list_persists_and_owner_cannot_be_guest():
    async with SessionLocal() as db:
        acc = await bot_access.add(db, GUEST, "Пётр", "@petr")
        with pytest.raises(ValueError):
            await bot_access.add(db, OWNER)
    bot_access._by_tg.clear()
    async with SessionLocal() as db:
        await bot_access.load(db)
    assert bot_access.is_guest(GUEST) and bot_access.guest_name(GUEST) == "Пётр"
    assert bot_access.can_use(OWNER) and not bot_access.can_use(STRANGER)
    assert not bot_access.has_workspace(GUEST) and bot_access.has_workspace(OWNER)
    assert bot_access.user_for(OWNER).id == 1 and bot_access.user_for(GUEST).username == "petr"
    async with SessionLocal() as db:
        assert await bot_access.remove(db, acc.id)
        assert not await bot_access.remove(db, 1)  # the owner stays
    assert not bot_access.can_use(GUEST)


async def test_users_api(client, auth):
    resp = await client.post(
        "/api/telegram/users", json={"telegram_id": GUEST, "name": "Пётр", "level": "media"}, headers=auth
    )
    assert resp.status_code == 200
    uid = resp.json()["id"]
    assert (
        await client.post("/api/telegram/users", json={"telegram_id": OWNER}, headers=auth)
    ).status_code == 400
    async with SessionLocal() as db:
        db.add(MediaJob(chat_id=GUEST, source="telegram:voice", cost_rub=1.5, segments=[], proposed_tasks=[]))
        await db.commit()
    data = (await client.get("/api/telegram/users", headers=auth)).json()["users"]
    assert data == [{**data[0], "id": uid, "telegram_id": GUEST, "name": "Пётр", "jobs": 1, "cost_rub": 1.5}]
    assert "token_epoch" not in data[0]
    resp = await client.patch(f"/api/telegram/users/{uid}", json={"level": "workspace"}, headers=auth)
    assert resp.json()["level"] == "workspace" and bot_access.has_workspace(GUEST)
    assert (
        await client.patch("/api/telegram/users/1", json={"level": "media"}, headers=auth)
    ).status_code == 400
    assert (await client.delete(f"/api/telegram/users/{uid}", headers=auth)).status_code == 200
    assert (await client.get("/api/telegram/users")).status_code == 401


async def test_stranger_start_asks_owner_once(to_owner):
    m = Msg(STRANGER)
    await telegram_bot.cmd_start(m)
    await telegram_bot.cmd_start(m)
    assert len(to_owner) == 1 and "Пётр (@petr, id 300)" in to_owner[0][0]
    datas = [b.callback_data for row in to_owner[0][1].inline_keyboard for b in row]
    assert datas == ["acc:workspace:300", "acc:media:300", "acc:no:300"]
    assert m.answers[0].startswith("Это личный бот") and "/summary" not in m.answers[0]

    group = Msg(STRANGER, chat_type="group", chat_id=-5)
    await telegram_bot.cmd_start(group)
    assert group.answers == []


async def test_owner_approves_request(to_owner):
    await telegram_bot.cmd_start(Msg(STRANGER))
    sent, edits = [], []
    query = NS(
        data="acc:workspace:300",
        from_user=user(OWNER),
        message=NS(text=to_owner[0][0], edit_text=lambda t: _append(edits, t)),
        bot=NS(send_message=lambda chat_id, text: _append(sent, (chat_id, text))),
        answer=lambda *a, **k: _append([], a),
    )
    await telegram_bot.cb_access(query)
    acc = bot_access.user_for(STRANGER)
    assert acc.username == "petr" and acc.name == "Пётр" and acc.has_workspace
    assert sent[0][0] == STRANGER and "Доступ открыт" in sent[0][1]
    assert "Доступ выдан" in edits[0]


async def _append(lst, item):
    lst.append(item)


async def test_group_added_by_stranger_is_left(to_owner):
    left = []
    event = NS(
        chat=NS(id=-7, type="supergroup", title="Чужая"),
        from_user=user(STRANGER),
        new_chat_member=NS(status="member"),
        old_chat_member=NS(status="left"),
        bot=NS(leave_chat=lambda chat_id: _append(left, chat_id)),
    )
    await telegram_bot.on_membership(event)
    assert left == [-7] and "Чужая" in to_owner[0][0]  # a stranger cannot add the bot
    async with SessionLocal() as db:
        assert (await db.scalars(select(TelegramChat))).all() == []

    event.from_user = user(OWNER)
    await telegram_bot.on_membership(event)
    async with SessionLocal() as db:
        assert [(c.chat_id, c.user_id) for c in (await db.scalars(select(TelegramChat))).all()] == [(-7, 1)]

    # a member with a workspace adds the bot to their own group: it is theirs
    async with SessionLocal() as db:
        member = await bot_access.add(db, GUEST, "Пётр", level="workspace")
    event.chat, event.from_user = NS(id=-70, type="group", title="Своя"), user(GUEST)
    await telegram_bot.on_membership(event)
    async with SessionLocal() as db:
        chat = await db.scalar(select(TelegramChat).where(TelegramChat.chat_id == -70))
    assert chat.user_id == member.id


async def test_only_owner_groups_and_owner_private_are_collected():
    stranger_dm = Msg(STRANGER, text="привет")
    await telegram_bot.collect(stranger_dm)
    assert "/start" in stranger_dm.answers[0]
    await telegram_bot.collect(Msg(STRANGER, chat_type="group", chat_id=-9, text="чужая группа"))
    async with SessionLocal() as db:
        db.add(TelegramChat(chat_id=-8, title="Своя", kind="group"))
        await db.commit()
    await telegram_bot.collect(Msg(STRANGER, chat_type="group", chat_id=-8, text="своя группа"))
    await telegram_bot.collect(Msg(OWNER, text="заметка себе"))
    async with SessionLocal() as db:
        texts = sorted(m.text for m in (await db.scalars(select(TelegramMessage))).all())
    assert texts == ["заметка себе", "своя группа"]


async def test_guest_media_without_tasks_and_only_own_jobs():
    async with SessionLocal() as db:
        await bot_access.add(db, GUEST, "Пётр")
        job = MediaJob(
            chat_id=GUEST, source="telegram:voice", title="Голос", language="ru", duration=60, text="…",
            segments=[[0, 1, "a"]], summary="Суть", proposed_tasks=[{"title": "Задача", "reason": ""}],
        )  # fmt: skip
        other = MediaJob(chat_id=OWNER, source="telegram:voice", segments=[], proposed_tasks=[])
        db.add_all([job, other])
        await db.commit()
        await db.refresh(job)
        await db.refresh(other)

    assert await telegram_media.AllowedPrivate()(Msg(GUEST))
    assert not await telegram_media.AllowedPrivate()(Msg(STRANGER))
    assert "Задача" not in telegram_media._digest_message(job)
    datas = [b.callback_data for row in telegram_media._keyboard(job).inline_keyboard for b in row]
    assert not any(d.startswith("mtask:") for d in datas)

    answers = []

    def q(data, uid):
        return NS(data=data, from_user=user(uid), answer=lambda *a, **k: _append(answers, a))

    assert (await telegram_media._job_for(q(f"msrt:{job.id}", GUEST)))[0] is not None
    assert (await telegram_media._job_for(q(f"msrt:{other.id}", GUEST)))[0] is None
    assert (await telegram_media._job_for(q(f"mtask:{job.id}:0", GUEST), need_workspace=True))[0] is None
    assert (await telegram_media._job_for(q(f"msrt:{job.id}", STRANGER)))[0] is None
