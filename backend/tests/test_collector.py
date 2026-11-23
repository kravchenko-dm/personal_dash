"""Request collector: supplier mail over IMAP and private MAX messages, both forwarded to the bot."""

from email.message import EmailMessage
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import InboxItem, MailAccount, MailRule, MaxAccount, Task
from app.services import bot_access, inbox, mail_collector, max_collector, telegram_bot
from tests.conftest import MEMBER_TG

# ---------- matching ----------


def test_patterns_and_matching():
    pats = mail_collector.normalize_patterns("Ivan@X.ru, supplier.ru; @Other.com  ivan@x.ru")
    assert pats == ["ivan@x.ru", "@supplier.ru", "@other.com"]
    assert mail_collector.matches("IVAN@x.ru", pats)
    assert mail_collector.matches("sales@supplier.ru", pats)
    assert mail_collector.matches("a@mail.supplier.ru", pats)  # subdomain
    assert not mail_collector.matches("a@notsupplier.ru", pats)
    assert not mail_collector.matches("petr@x.ru", pats)
    assert not mail_collector.matches("garbage", pats)


# ---------- parsing ----------


def _letter(
    sender="Иван <ivan@supplier.ru>", subject="Счёт №5", plain=None, html=None, attach=None, msgid="<1@s>"
) -> bytes:
    m = EmailMessage()
    m["From"] = sender
    m["To"] = "me@mail.ru"
    m["Subject"] = subject
    m["Message-ID"] = msgid
    if plain is not None:
        m.set_content(plain, cte="quoted-printable")
        if html is not None:
            m.add_alternative(html, subtype="html")
    elif html is not None:
        m.set_content(html, subtype="html", cte="base64")
    if attach:
        m.add_attachment(b"%PDF", maintype="application", subtype="pdf", filename=attach)
    return m.as_bytes()


def test_parse_plain_with_cyrillic_subject_and_quotes():
    body = "Добрый день!\nСчёт во вложении.\n\nВ пн, 18 нояб. Пётр написал:\n> старое письмо\n> ещё"
    letter = mail_collector.parse(_letter(plain=body, attach="счёт.pdf"))
    assert letter["subject"] == "Счёт №5"
    assert letter["addresses"] == ["ivan@supplier.ru"]
    assert letter["text"] == "Добрый день!\nСчёт во вложении."
    assert letter["attachments"] == ["счёт.pdf"]
    text = mail_collector.bot_text("Поставщик", letter)
    assert text.startswith("📧 В «Поставщик» пришло письмо")
    assert "Тема: Счёт №5" in text and "📎 счёт.pdf" in text


def test_parse_html_only():
    html = "<html><head><style>p{}</style></head><body><p>Цена&nbsp;100&nbsp;₽</p><div>Срок: 3 дня<br>Иван</div></body></html>"
    letter = mail_collector.parse(_letter(html=html))
    assert letter["text"] == "Цена 100 ₽\nСрок: 3 дня\nИван"


def test_parse_prefers_plain_and_cuts_original_message():
    plain = "Ответ выше\n\n-----Original Message-----\nFrom: me\nстарое"
    letter = mail_collector.parse(_letter(plain=plain, html="<p>html</p>"))
    assert letter["text"] == "Ответ выше"


# ---------- collecting with a fake IMAP server ----------


class FakeImap:
    """Just enough of imaplib.IMAP4_SSL; `box` is {uid: raw letter}."""

    box: dict[int, bytes] = {}
    validity = 7
    readonly: list[bool] = []
    fetched: list[str] = []

    def __init__(self, host, port, timeout=None):
        pass

    def login(self, login, password):
        if password != "app-pass":
            raise mail_collector.imaplib.IMAP4.error("AUTHENTICATIONFAILED")

    def select(self, folder, readonly=False):
        FakeImap.readonly.append(readonly)
        return "OK", [str(len(self.box)).encode()]

    def response(self, code):
        return code, [str(FakeImap.validity).encode()]

    def uid(self, cmd, *args):
        if cmd == "SEARCH":
            query = args[-1]
            uids = sorted(self.box)
            if query.startswith("UID "):
                lo = int(query[4:].split(":")[0])
                # like real servers, "N:*" returns the last message even when N is past the end
                uids = [u for u in uids if u >= lo] or uids[-1:]
            return "OK", [" ".join(map(str, uids)).encode()]
        if cmd == "FETCH":
            FakeImap.fetched.append(args[1])
            return "OK", [(f"{args[0]} (BODY[] {{1}}".encode(), self.box[int(args[0])]), b")"]
        raise AssertionError(cmd)

    def logout(self):
        pass


@pytest.fixture
def fake_imap(monkeypatch):
    FakeImap.box = {1: _letter(msgid="<old@s>")}
    FakeImap.validity = 7
    FakeImap.readonly = []
    FakeImap.fetched = []
    monkeypatch.setattr(mail_collector.imaplib, "IMAP4_SSL", FakeImap)
    return FakeImap


@pytest.fixture
def sent(monkeypatch):
    out: list[tuple[int, str, object]] = []

    async def fake_send(telegram_id, text, reply_markup=None):
        out.append((telegram_id, text, reply_markup))

    monkeypatch.setattr(telegram_bot, "send_to_user", fake_send)
    return out


async def test_mail_collector_forwards_only_new_supplier_mail(client, member_auth, member, fake_imap, sent):
    r = await client.post(
        "/api/collector/mail/rules", headers=member_auth, json={"name": "Альфа", "patterns": "supplier.ru"}
    )
    assert r.status_code == 201 and r.json()["patterns"] == ["@supplier.ru"]

    bad = await client.put(
        "/api/collector/mail", headers=member_auth, json={"login": "p@mail.ru", "password": "x"}
    )
    assert bad.status_code == 400 and "пароль" in bad.json()["detail"]
    ok = await client.put(
        "/api/collector/mail", headers=member_auth, json={"login": "p@mail.ru", "password": "app-pass"}
    )
    assert ok.status_code == 200
    account = ok.json()["account"]
    assert account["ready"] and account["has_password"] and "password" not in account
    assert sent == []  # the letter already in the box is history

    fake_imap.box[2] = _letter(sender="spam@other.ru", msgid="<2@o>")
    fake_imap.box[3] = _letter(plain="Новый прайс", subject="Прайс", msgid="<3@s>")
    await mail_collector.collect_all()
    assert len(sent) == 1
    tg, text, markup = sent[0]
    assert tg == MEMBER_TG
    assert text.startswith("📧 В «Альфа» пришло письмо") and "Новый прайс" in text
    assert markup.inline_keyboard[0][0].callback_data.startswith("inbox:task:")
    assert all(fake_imap.readonly)  # never opened read-write
    assert set(fake_imap.fetched) == {"(BODY.PEEK[])"}

    await mail_collector.collect_all()  # nothing new: "4:*" returns uid 3 again, which is skipped
    assert len(sent) == 1

    # the server reset UIDVALIDITY: remember the new position, do not resend the box
    fake_imap.validity = 8
    await mail_collector.collect_all()
    assert len(sent) == 1
    async with SessionLocal() as db:
        acc = await db.scalar(select(MailAccount))
        assert acc.uidvalidity == 8 and acc.last_uid == 3


async def test_mail_errors_alert_once(member, fake_imap, sent):
    async with SessionLocal() as db:
        db.add(MailAccount(user_id=member, login="p@mail.ru", password="wrong", last_uid=1, uidvalidity=7))
        await db.commit()
    for _ in range(4):
        await mail_collector.collect_all()
    assert len(sent) == 1 and "не подключается" in sent[0][1]
    async with SessionLocal() as db:
        acc = await db.scalar(select(MailAccount))
        assert acc.errors == 4 and "пароль" in acc.last_error


async def test_mail_api_is_per_user(client, auth, member_auth, member):
    await client.post(
        "/api/collector/mail/rules", headers=member_auth, json={"name": "Альфа", "patterns": ["a@b.ru"]}
    )
    mine = (await client.get("/api/collector/mail", headers=auth)).json()
    assert mine == {"account": None, "rules": []}
    rule_id = (await client.get("/api/collector/mail", headers=member_auth)).json()["rules"][0]["id"]
    assert (
        await client.patch(f"/api/collector/mail/rules/{rule_id}", headers=auth, json={"enabled": False})
    ).status_code == 404
    assert (await client.delete(f"/api/collector/mail/rules/{rule_id}", headers=auth)).status_code == 404
    r = await client.patch(
        f"/api/collector/mail/rules/{rule_id}", headers=member_auth, json={"patterns": "@c.ru"}
    )
    assert r.json()["patterns"] == ["@c.ru"]
    assert (
        await client.post(
            "/api/collector/mail/rules", headers=member_auth, json={"name": "x", "patterns": " , "}
        )
    ).status_code == 400


# ---------- MAX ----------


def _msg(text="Привет", chat_id=10, sender=42, mid=1, attaches=()):
    return SimpleNamespace(id=mid, chat_id=chat_id, sender=sender, text=text, attaches=list(attaches))


class FakeMaxClient:
    def __init__(self):
        self.chats = [SimpleNamespace(id=10, type=SimpleNamespace(value="DIALOG"))]

    async def get_chat(self, chat_id):
        return SimpleNamespace(id=chat_id, type="CHAT")

    async def get_user(self, user_id):
        return SimpleNamespace(names=[SimpleNamespace(name=None, first_name="Анна", last_name="Смирнова")])


async def test_max_forwards_private_messages_only(member, sent):
    r = max_collector.Runner(user_id=member, me_id=1)
    c = FakeMaxClient()
    await max_collector.handle_message(r, _msg(), c)
    await max_collector.handle_message(r, _msg(mid=1), c)  # the same message again
    await max_collector.handle_message(r, _msg(chat_id=20, mid=2), c)  # a group
    await max_collector.handle_message(r, _msg(sender=1, mid=3), c)  # my own
    photo = SimpleNamespace(type=SimpleNamespace(value="PHOTO"))
    doc = SimpleNamespace(type=SimpleNamespace(value="FILE"), name="прайс.xlsx")
    await max_collector.handle_message(r, _msg(text="", mid=4, attaches=[photo, doc]), c)
    assert [t for _, t, _ in sent] == [
        "💬 MAX, «Анна Смирнова»:\nПривет",
        "💬 MAX, «Анна Смирнова»:\n[фото]\n[файл: прайс.xlsx]",
    ]
    assert r.dialogs == {10: True, 20: False}


async def test_max_password_and_qr(member, monkeypatch):
    r = max_collector.Runner(user_id=member)
    max_collector._clients[member] = r
    try:
        await max_collector.DashboardQr(r).show_qr("https://max.ru/:auth/abc")
        async with SessionLocal() as db:
            assert (await db.get(MaxAccount, member)).status == "waiting_qr"
        assert "<svg" in max_collector.qr_svg(r.qr_url)
        assert not max_collector.submit_password(member, "x")  # nobody is asking yet

        import asyncio

        waiting = asyncio.create_task(max_collector.DashboardPassword(r).get_password("кот"))
        await asyncio.sleep(0.05)
        assert r.password_hint == "кот" and r.qr_url == ""
        assert max_collector.submit_password(member, "secret")
        assert await waiting == "secret"
    finally:
        max_collector._clients.pop(member, None)


async def test_max_api_states(client, member_auth, member, monkeypatch, tmp_path):
    monkeypatch.setattr(max_collector, "DATA_DIR", tmp_path)
    started: list[int] = []

    async def fake_start(user_id):
        started.append(user_id)
        r = max_collector.Runner(user_id=user_id, qr_url="https://max.ru/:auth/x")
        max_collector._clients[user_id] = r
        await max_collector._set(user_id, status="waiting_qr")

    monkeypatch.setattr(max_collector, "start", fake_start)
    st = (await client.get("/api/collector/max", headers=member_auth)).json()
    assert st["status"] == "off" and st["qr_svg"] is None
    st = (await client.post("/api/collector/max/connect", headers=member_auth)).json()
    assert started == [member] and st["status"] == "waiting_qr" and st["qr_svg"].startswith("<")
    assert (
        await client.post("/api/collector/max/password", headers=member_auth, json={"password": "x"})
    ).status_code == 409

    (tmp_path / str(member)).mkdir()
    (tmp_path / str(member) / "session.db").write_bytes(b"x")
    st = (await client.post("/api/collector/max/logout", headers=member_auth)).json()
    assert st["status"] == "off" and not st["enabled"]
    assert not (tmp_path / str(member)).exists()


# ---------- inbox: task button, removal ----------


class FakeQuery:
    def __init__(self, tg, data):
        self.from_user = SimpleNamespace(id=tg)
        self.data = data
        self.answers: list[str] = []

    async def answer(self, text="", show_alert=False):
        self.answers.append(text)


async def test_inbox_task_button(member, monkeypatch):
    monkeypatch.setattr(bot_access, "get_settings", lambda: SimpleNamespace(telegram_owner_id=999))
    async with SessionLocal() as db:
        item = await inbox.store(db, member, "mail", "<1@s>", "Альфа", "Счёт", "Текст письма")
        assert await inbox.store(db, member, "mail", "<1@s>", "Альфа", "Счёт", "Текст письма") is None
    stranger = FakeQuery(777, f"inbox:task:{item.id}")
    await inbox.cb_inbox_task(stranger)
    assert stranger.answers == ["Нет доступа"]
    q = FakeQuery(MEMBER_TG, f"inbox:task:{item.id}")
    await inbox.cb_inbox_task(q)
    assert q.answers[0].startswith("Добавлено")
    async with SessionLocal() as db:
        task = await db.scalar(select(Task))
        assert task.user_id == member and task.title == "Альфа: Счёт" and task.notes == "Текст письма"
        assert task.source == "mail" and task.source_ref == f"inbox:{item.id}"


async def test_removing_user_drops_collector_data(member, monkeypatch, tmp_path):
    monkeypatch.setattr(max_collector, "DATA_DIR", tmp_path)
    (tmp_path / str(member)).mkdir()
    async with SessionLocal() as db:
        db.add(MailAccount(user_id=member, login="p@mail.ru", password="x"))
        db.add(MailRule(user_id=member, name="A", patterns=["@a.ru"]))
        db.add(MaxAccount(user_id=member, enabled=True, status="online"))
        await db.commit()
        await inbox.store(db, member, "max", "10:1", "Анна", "", "hi")
        assert await bot_access.remove(db, member)
    async with SessionLocal() as db:
        for model in (MailAccount, MailRule, MaxAccount, InboxItem):
            assert (await db.scalars(select(model))).all() == []
    assert not (tmp_path / str(member)).exists()
