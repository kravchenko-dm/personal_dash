"""Telegram bot (aiogram 3, webhook mode).

Add the bot to any chat: it stores messages (disable privacy mode in @BotFather so it
sees everything), and the owner can ask for a summary with proposed tasks.
"""

import logging
import re
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import OWNER_ID, Feature, Summary, Task, TelegramChat, TelegramMessage
from app.schemas import TaskIn
from app.services import bot_access
from app.services import summaries as summaries_svc
from app.services.tasks import create_task, open_tasks_text

log = logging.getLogger(__name__)
router = Router()

_bot: Bot | None = None
_dp: Dispatcher | None = None
_bot_username: str | None = None


def get_bot() -> Bot | None:
    return _bot


def bot_username() -> str | None:
    return _bot_username


def is_owner(user_id: int | None) -> bool:
    return bot_access.is_owner(user_id)


# ---------- helpers ----------


async def _ensure_chat(chat_id: int, title: str, kind: str, user_id: int = OWNER_ID) -> TelegramChat:
    async with SessionLocal() as db:
        chat = await db.scalar(select(TelegramChat).where(TelegramChat.chat_id == chat_id))
        if not chat:
            chat = TelegramChat(chat_id=chat_id, title=title, kind=kind, user_id=user_id)
            db.add(chat)
        else:
            chat.title = title or chat.title
        await db.commit()
        await db.refresh(chat)
        return chat


def _chat_title(message: Message) -> str:
    chat = message.chat
    if chat.type == ChatType.PRIVATE:
        return " ".join(filter(None, [chat.first_name, chat.last_name])) or "Личный чат"
    return chat.title or str(chat.id)


def _tasks_keyboard(summary: Summary) -> InlineKeyboardMarkup | None:
    buttons = []
    for i, t in enumerate(summary.proposed_tasks[:8]):
        row = [InlineKeyboardButton(text=f"➕ {t['title'][:40]}", callback_data=f"addtask:{summary.id}:{i}")]
        if summary.project_id:
            row.append(InlineKeyboardButton(text="📋 на доску", callback_data=f"addfeat:{summary.id}:{i}"))
        buttons.append(row)
    return InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None


def _summary_text(summary: Summary) -> str:
    text = f"📝 Саммари за {summary.period_start.astimezone():%d.%m %H:%M} — {summary.period_end.astimezone():%d.%m %H:%M}\n\n{summary.text}"
    if summary.proposed_tasks:
        text += "\n\n💡 Предлагаю задачи — нажмите, чтобы добавить"
        text += " (или отправить на доску фич проекта):" if summary.project_id else ":"
        for t in summary.proposed_tasks[:8]:
            text += f"\n• {t['title']} — {t.get('reason', '')}"
    return text[:4000]


# ---------- commands ----------


GUEST_HELP = (
    "Пришлите голосовое, кружок, аудио- или видеофайл либо ссылку (YouTube, VK, Rutube, Яндекс.Диск, "
    "Google Диск) — расшифрую, сделаю выжимку, по кнопке переведу и сделаю субтитры.\n"
    "В подписи можно написать пожелание, например «переведи на английский». Файлы — до 20 МБ, "
    "больше — ссылкой на диск."
)

WORKSPACE_HELP = (
    "Я собираю сообщения из ваших групп (добавьте меня туда сами) и из этого чата, делаю саммари "
    "с идеями задач, по вечерам присылаю дайджест, по утрам — список задач.\n\n"
    "/login — ссылка для входа в веб-кабинет\n"
    "/summary [часов] — саммари этого чата (в личке — всех ваших чатов)\n"
    "/digest [часов] — саммари по всем вашим чатам\n"
    "/tasks — открытые задачи\n"
    "/add текст — добавить задачу\n"
    "/done id — закрыть задачу\n\n" + GUEST_HELP
)

LEVEL_NAMES = {"workspace": "свой кабинет", "media": "только расшифровка"}


def _who(user) -> str:
    name = user.full_name if user else "?"
    return (
        f"{name} (@{user.username}, id {user.id})"
        if user and user.username
        else f"{name} (id {user.id if user else '?'})"
    )


def _workspace(message: Message) -> bot_access.Account | None:
    """The sender's account when they have a workspace (owner or member)."""
    acc = bot_access.user_for(message.from_user.id if message.from_user else None)
    return acc if acc and acc.has_workspace else None


@router.message(Command("start", "help"))
async def cmd_start(message: Message, command: CommandObject | None = None) -> None:
    user = message.from_user
    uid = user.id if user else None
    acc = bot_access.user_for(uid)
    if acc and message.chat.type == ChatType.PRIVATE and command and (command.args or "").strip() == "login":
        await _send_login_link(message, acc)
        return
    if acc and acc.is_owner:
        await _ensure_chat(message.chat.id, _chat_title(message), message.chat.type, acc.id)
        await message.answer(
            "Привет! Я собираю сообщения из чатов, куда вы меня добавили, и делаю саммари с идеями задач.\n\n"
            "/summary [часов] — саммари этого чата (по умолчанию 24ч)\n"
            "/digest [часов] — саммари по всем чатам\n"
            "/tasks — открытые задачи\n"
            "/add текст — добавить задачу\n"
            "/done id — закрыть задачу\n"
            "/login — ссылка для входа в дашборд\n"
            "\nВ личке пришлите голосовое, аудио, видео или ссылку (YouTube, VK, Rutube, Яндекс.Диск) — "
            "расшифрую, сделаю выжимку и предложу задачи. В подписи можно написать пожелание, "
            "например «переведи на английский».\n"
            f"\nВаш user id: {uid}"
        )
        return
    if message.chat.type != ChatType.PRIVATE:
        return  # members and strangers get no help in groups
    if acc and acc.has_workspace:
        await _ensure_chat(message.chat.id, _chat_title(message), message.chat.type, acc.id)
        await message.answer(WORKSPACE_HELP)
        return
    if acc:
        await message.answer(GUEST_HELP)
        return
    if user and bot_access.should_ask_owner(user.id):
        await send_to_owner(
            f"👤 Просит доступ к боту: {_who(user)}\n"
            "«Свой кабинет» — свои задачи, сбор своих чатов, саммари и дайджесты, вход в веб-кабинет. "
            "«Только расшифровка» — расшифровка своих записей. Нейросеть и распознавание — за ваш счёт.",
            reply_markup=InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(text="🗂 Свой кабинет", callback_data=f"acc:workspace:{user.id}"),
                        InlineKeyboardButton(
                            text="🎙 Только расшифровка", callback_data=f"acc:media:{user.id}"
                        ),
                    ],
                    [InlineKeyboardButton(text="🚫 Отклонить", callback_data=f"acc:no:{user.id}")],
                ]
            ),
        )
    await message.answer("Это личный бот. Я отправил владельцу запрос на доступ — напишу, когда он ответит.")


@router.callback_query(F.data.startswith("acc:"))
async def cb_access(query: CallbackQuery) -> None:
    if not is_owner(query.from_user.id):
        await query.answer("Только владелец", show_alert=True)
        return
    _, verdict, raw_id = query.data.split(":")
    user_id = int(raw_id)
    request_text = (query.message.text or "") if query.message else ""
    if verdict in ("yes", "media", "workspace"):
        level = "media" if verdict == "yes" else verdict  # "yes" came from older request messages
        name, username = "", ""
        m = re.search(r"доступ к боту: (.+?) \((?:@(\w+), )?id \d+\)", request_text)
        if m:
            name, username = m.group(1), m.group(2) or ""
        async with SessionLocal() as db:
            await bot_access.add(db, user_id, name, username, level=level)
        welcome = WORKSPACE_HELP if level == "workspace" else GUEST_HELP
        try:
            await query.bot.send_message(user_id, "✅ Доступ открыт.\n\n" + welcome)
        except Exception as exc:
            log.warning("member notify failed: %s", exc)
        note = f"\n\n✅ Доступ выдан: {LEVEL_NAMES[level]}. Изменить — «Ещё → Пользователи»."
    else:
        note = "\n\n🚫 Отклонено."
    await query.answer()
    if query.message:
        try:
            await query.message.edit_text(request_text.split("\n")[0] + note)
        except Exception:
            pass


async def _send_login_link(message: Message, acc: bot_access.Account) -> None:
    if not acc.has_workspace:
        await message.answer("Веб-кабинет есть только при доступе «свой кабинет». Попросите владельца.")
        return
    from app.auth import CODE_TTL, new_login_code

    url = f"{get_settings().public_url}/login?code={new_login_code(acc.id)}"
    await message.answer(
        f"Ссылка для входа (действует {CODE_TTL // 60} минут, только один раз):\n{url}\n\n"
        "Никому её не пересылайте.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔑 Войти", url=url)]]),
    )


@router.message(Command("login"))
async def cmd_login(message: Message) -> None:
    if message.chat.type != ChatType.PRIVATE:
        return  # a login link must never land in a group
    acc = bot_access.user_for(message.from_user.id if message.from_user else None)
    if not acc:
        await message.answer("Это личный бот. /start — запросить доступ.")
        return
    await _send_login_link(message, acc)


@router.message(Command("tasks"))
async def cmd_tasks(message: Message) -> None:
    acc = _workspace(message)
    if not acc:
        return
    async with SessionLocal() as db:
        await message.answer(await open_tasks_text(db, acc.id))


@router.message(Command("add"))
async def cmd_add(message: Message, command: CommandObject) -> None:
    acc = _workspace(message)
    if not acc:
        return
    if not command.args:
        await message.answer("Использование: /add текст задачи")
        return
    async with SessionLocal() as db:
        task = await create_task(
            db,
            TaskIn(title=command.args.strip()),
            source="telegram",
            source_ref=str(message.chat.id),
            user_id=acc.id,
        )
    await message.answer(f"✅ Задача #{task.id} добавлена: {task.title}")


@router.message(Command("done"))
async def cmd_done(message: Message, command: CommandObject) -> None:
    acc = _workspace(message)
    if not acc:
        return
    try:
        task_id = int((command.args or "").strip().lstrip("#"))
    except ValueError:
        await message.answer("Использование: /done 12")
        return
    async with SessionLocal() as db:
        task = await db.get(Task, task_id)
        if not task or task.user_id != acc.id:
            await message.answer("Нет такой задачи")
            return
        task.status = "done"
        task.completed_at = datetime.now(timezone.utc)
        await db.commit()
    await message.answer(f"✅ #{task_id} закрыта")


async def _send_summary(message: Message, acc: bot_access.Account, chat_id: int | None, hours: int) -> None:
    waiting = await message.answer("⏳ Думаю…")
    try:
        async with SessionLocal() as db:
            summary = await summaries_svc.build_summary(db, chat_id=chat_id, hours=hours, user_id=acc.id)
    except Exception as exc:
        log.exception("summary failed")
        await waiting.edit_text(f"Не получилось: {exc}")
        return
    if not summary:
        await waiting.edit_text("За этот период сообщений нет.")
        return
    await waiting.edit_text(_summary_text(summary), reply_markup=_tasks_keyboard(summary))


@router.message(Command("summary"))
async def cmd_summary(message: Message, command: CommandObject) -> None:
    acc = _workspace(message)
    if not acc:
        return
    hours = int(command.args) if command.args and command.args.isdigit() else 24
    chat_id = None if message.chat.type == ChatType.PRIVATE else message.chat.id
    await _send_summary(message, acc, chat_id, hours)


@router.message(Command("digest"))
async def cmd_digest(message: Message, command: CommandObject) -> None:
    acc = _workspace(message)
    if not acc:
        return
    hours = int(command.args) if command.args and command.args.isdigit() else 24
    await _send_summary(message, acc, None, hours)


@router.callback_query(F.data.startswith("addtask:"))
async def cb_add_task(query: CallbackQuery) -> None:
    acc = bot_access.user_for(query.from_user.id)
    _, summary_id, idx = query.data.split(":")
    async with SessionLocal() as db:
        summary = await db.get(Summary, int(summary_id))
        if not acc or not summary or summary.user_id != acc.id:
            await query.answer("Нет доступа", show_alert=True)
            return
        if int(idx) >= len(summary.proposed_tasks):
            await query.answer("Предложение устарело")
            return
        proposal = summary.proposed_tasks[int(idx)]
        task = await create_task(
            db,
            TaskIn(
                title=proposal["title"],
                notes=proposal.get("reason"),
                priority=int(proposal.get("priority", 2)),
            ),
            source="ai",
            source_ref=f"summary:{summary.id}",
            user_id=summary.user_id,
        )
    await query.answer(f"Добавлено: #{task.id}")


@router.callback_query(F.data.startswith("addfeat:"))
async def cb_add_feature(query: CallbackQuery) -> None:
    if not is_owner(query.from_user.id):
        await query.answer("Только владелец", show_alert=True)
        return
    _, summary_id, idx = query.data.split(":")
    async with SessionLocal() as db:
        summary = await db.get(Summary, int(summary_id))
        if (
            not summary
            or summary.user_id != OWNER_ID
            or not summary.project_id
            or int(idx) >= len(summary.proposed_tasks)
        ):
            await query.answer("Предложение устарело")
            return
        from app.services import projects as projects_svc

        proposal = summary.proposed_tasks[int(idx)]
        feature = Feature(
            project_id=summary.project_id,
            title=proposal["title"],
            description=proposal.get("reason"),
            priority=int(proposal.get("priority", 2)),
            status="idea",
            source="ai",
            source_ref=f"summary:{summary.id}",
        )
        feature.position = await projects_svc.next_position(db, summary.project_id, "idea")
        db.add(feature)
        await db.commit()
    await query.answer("Добавлено на доску фич")


# ---------- membership + message collection ----------


@router.my_chat_member()
async def on_membership(event: ChatMemberUpdated) -> None:
    """A group belongs to whoever added the bot (the owner or a member with a workspace): their
    summaries and digests read it. Anyone else adding the bot makes it leave."""
    status = event.new_chat_member.status
    joined = status in ("member", "administrator") and event.old_chat_member.status in ("left", "kicked")
    if event.chat.type == ChatType.PRIVATE:
        return  # a user started or blocked the bot: nothing to collect
    adder = bot_access.user_for(event.from_user.id if event.from_user else None)
    title = event.chat.title or str(event.chat.id)
    if joined and not (adder and adder.has_workspace):
        try:
            await event.bot.leave_chat(event.chat.id)
        except Exception as exc:
            log.warning("leave_chat failed: %s", exc)
        await send_to_owner(
            f"🚪 Меня добавили в группу «{title}» — {_who(event.from_user)}. "
            "У этого человека нет своего кабинета, поэтому я вышел."
        )
        return
    async with SessionLocal() as db:
        chat = await db.scalar(select(TelegramChat).where(TelegramChat.chat_id == event.chat.id))
        if status in ("member", "administrator"):
            if not chat:
                db.add(
                    TelegramChat(
                        chat_id=event.chat.id, title=event.chat.title or "", kind=event.chat.type,
                        user_id=adder.id if adder else OWNER_ID,
                    )
                )  # fmt: skip
            elif joined:
                if adder and chat.user_id != adder.id and not chat.collect:
                    chat.user_id, chat.project_id = adder.id, None  # it was abandoned: the new adder takes it
                elif adder and chat.user_id != adder.id:
                    await db.commit()
                    await send_to_user(
                        event.from_user.id,
                        f"Группу «{title}» уже собирает другой пользователь дашборда, вам она не попадёт.",
                    )
                    return
                chat.collect = True
        elif chat:
            chat.collect = False
        await db.commit()


@router.message(F.migrate_to_chat_id)
async def on_migrate(message: Message) -> None:
    """A group became a supergroup and got a new id: keep collecting under the new id."""
    async with SessionLocal() as db:
        chat = await db.scalar(select(TelegramChat).where(TelegramChat.chat_id == message.chat.id))
        if chat:
            chat.chat_id = message.migrate_to_chat_id
            await db.commit()


@router.message(F.text | F.caption)
async def collect(message: Message) -> None:
    text = message.text or message.caption or ""
    if not text or text.startswith("/"):
        return
    uid = message.from_user.id if message.from_user else None
    if message.chat.type == ChatType.PRIVATE:
        acc = bot_access.user_for(uid)
        if not (acc and acc.has_workspace):  # strangers' and transcription-only private chats are not stored
            await message.answer(GUEST_HELP if acc else "Это личный бот. /start — запросить доступ.")
            return
        chat = await _ensure_chat(message.chat.id, _chat_title(message), message.chat.type, acc.id)
    else:
        # groups are stored only after someone with a workspace added the bot (on_membership)
        async with SessionLocal() as db:
            chat = await db.scalar(select(TelegramChat).where(TelegramChat.chat_id == message.chat.id))
        if chat is None:
            return
    if not chat.collect:
        return
    sender = message.from_user.full_name if message.from_user else "?"
    async with SessionLocal() as db:
        db.add(
            TelegramMessage(
                chat_id=message.chat.id,
                message_id=message.message_id,
                sender=sender,
                text=text,
                sent_at=message.date.astimezone(timezone.utc),
            )
        )
        await db.commit()


# ---------- lifecycle ----------


async def setup() -> None:
    """Create bot + dispatcher and register the webhook. Safe to call when no token is set."""
    global _bot, _dp, _bot_username
    s = get_settings()
    if not s.telegram_enabled:
        log.info("telegram: TELEGRAM_BOT_TOKEN not set, bot disabled")
        return
    _bot = Bot(token=s.telegram_bot_token)
    _dp = Dispatcher()
    from app.services import telegram_media

    _dp.include_router(telegram_media.router)  # first: links in the private chat must not be "collected"
    _dp.include_router(router)
    me = await _bot.get_me()
    _bot_username = me.username
    url = f"{s.public_url}/api/telegram/webhook"
    await _bot.set_webhook(
        url,
        secret_token=s.telegram_webhook_secret,
        allowed_updates=["message", "callback_query", "my_chat_member"],
        drop_pending_updates=False,
    )
    log.info("telegram: webhook set to %s (@%s)", url, me.username)


async def shutdown() -> None:
    if _bot:
        await _bot.session.close()


async def feed_update(payload: dict) -> None:
    from aiogram.types import Update

    if not (_bot and _dp):
        return
    update = Update.model_validate(payload, context={"bot": _bot})
    await _dp.feed_update(_bot, update)


async def send_to_owner(text: str, reply_markup: InlineKeyboardMarkup | None = None) -> None:
    s = get_settings()
    if not (_bot and s.telegram_owner_id):
        return
    try:
        await _bot.send_message(s.telegram_owner_id, text[:4000], reply_markup=reply_markup)
    except Exception as exc:
        log.warning("telegram send_to_owner failed: %s", exc)


async def send_to_user(
    telegram_id: int | None, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> None:
    if not (_bot and telegram_id):
        return
    try:
        await _bot.send_message(telegram_id, text[:4000], reply_markup=reply_markup)
    except Exception as exc:
        log.warning("telegram send_to_user failed: %s", exc)


async def send_summary(summary: Summary) -> None:
    """To whoever the summary belongs to."""
    acc = bot_access.get(summary.user_id)
    if acc is None:
        return
    if acc.is_owner:
        await send_to_owner(_summary_text(summary), reply_markup=_tasks_keyboard(summary))
    else:
        await send_to_user(acc.telegram_id, _summary_text(summary), reply_markup=_tasks_keyboard(summary))
