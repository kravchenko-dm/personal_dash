"""Bot: transcription of voice notes, audio/video files and links (YouTube, VK, Rutube, Yandex Disk…).

The owner sends media or a link to the bot in a private chat. The bot transcribes it, replies with
a Plaud-style summary (with task buttons) and the full transcript as a file; buttons translate the
transcript or export subtitles. A caption or text next to the link is an instruction: "переведи на
английский" also translates right away, anything else steers the summary.

Work runs in the background (one job at a time) so the webhook answers Telegram immediately."""

import asyncio
import logging
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import Filter
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from sqlalchemy import delete

from app.db import SessionLocal
from app.models import MediaJob
from app.schemas import TaskIn
from app.services import bot_access, media, media_text
from app.services.tasks import create_task

log = logging.getLogger(__name__)
router = Router(name="media")

BOT_API_LIMIT = 20 * 1024 * 1024  # cloud Bot API refuses to hand out bigger files
KEEP = timedelta(days=180)

_sem = asyncio.Semaphore(1)
_tasks: set[asyncio.Task] = set()
_waiting = 0


class AllowedPrivate(Filter):
    """The owner or an approved guest, in a private chat with the bot."""

    async def __call__(self, message: Message) -> bool:
        return message.chat.type == ChatType.PRIVATE and bot_access.can_use(
            message.from_user.id if message.from_user else None
        )


def _media_of(message: Message) -> tuple[str, object] | None:
    for kind in ("voice", "audio", "video", "video_note"):
        obj = getattr(message, kind)
        if obj:
            return kind, obj
    doc = message.document
    if doc and (
        (doc.mime_type or "").startswith(("audio/", "video/"))
        or re.search(
            r"\.(mp3|m4a|wav|ogg|oga|opus|flac|aac|webm|mp4|mov|mkv|avi)$", doc.file_name or "", re.I
        )
    ):
        return "document", doc
    return None


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _safe(coro) -> None:
    try:
        await coro
    except Exception:
        log.exception("media background task failed")


def _safe_name(title: str) -> str:
    name = re.sub(r"[^\w\s.-]+", "", title, flags=re.U).strip()[:60]
    return re.sub(r"\s+", " ", name) or "запись"


class Status:
    """One message that shows progress, edited in place."""

    def __init__(self, message: Message):
        self.message = message
        self.text = message.text or ""

    async def set(self, text: str) -> None:
        if text == self.text:
            return
        self.text = text
        try:
            await self.message.edit_text(text)
        except Exception as exc:  # "message is not modified", deleted message, flood limits
            log.debug("status edit failed: %s", exc)


# ---------- entry points ----------


@router.message(AllowedPrivate(), F.voice | F.audio | F.video | F.video_note | F.document)
async def on_media(message: Message, bot: Bot) -> None:
    found = _media_of(message)
    if not found:
        await message.answer("Это не похоже на аудио или видео. Пришлите запись, голосовое или ссылку.")
        return
    kind, obj = found
    size = getattr(obj, "file_size", 0) or 0
    if size > BOT_API_LIMIT:
        await message.answer(
            f"Файл {size / 1024 / 1024:.0f} МБ, а Telegram отдаёт ботам файлы только до 20 МБ.\n"
            "Загрузите его на Яндекс.Диск или Google Диск и пришлите публичную ссылку — скачаю сам."
        )
        return
    title = getattr(obj, "file_name", None) or getattr(obj, "title", None) or {
        "voice": "Голосовое", "video_note": "Кружок", "video": "Видео", "audio": "Аудио"
    }.get(kind, "Запись")  # fmt: skip
    status = await _queued(message)
    _spawn(
        _process(
            bot, message.chat.id, status, source=f"telegram:{kind}", title=title,
            file_id=obj.file_id, file_name=getattr(obj, "file_name", None) or "",
            instruction=(message.caption or "").strip(),
        )
    )  # fmt: skip


@router.message(AllowedPrivate(), F.text.regexp(media.URL_RE.pattern), ~F.text.startswith("/"))
async def on_link(message: Message, bot: Bot) -> None:
    url = media.URL_RE.search(message.text).group(0).rstrip(").,")
    instruction = message.text.replace(url, " ").strip()
    status = await _queued(message)
    _spawn(_process(bot, message.chat.id, status, source=url, url=url, instruction=instruction))


async def _queued(message: Message) -> Status:
    text = "⏳ Принял, начинаю…" if not _sem.locked() else f"⏳ В очереди (впереди {_waiting + 1})…"
    return Status(await message.reply(text))


# ---------- pipeline ----------


async def _process(
    bot: Bot,
    chat_id: int,
    status: Status,
    source: str,
    title: str = "",
    url: str | None = None,
    file_id: str | None = None,
    file_name: str = "",
    instruction: str = "",
) -> None:
    global _waiting
    _waiting += 1
    job_id = None
    try:
        async with _sem:
            _waiting -= 1
            job_id = await _run(bot, chat_id, status, source, title, url, file_id, file_name, instruction)
    except media.MediaError as exc:
        await status.set(f"❌ Не получилось: {exc}")
    except Exception as exc:
        log.exception("media job failed")
        await status.set(f"❌ Ошибка: {exc.__class__.__name__}: {str(exc)[:300]}")
    target = media_text.wants_translation(instruction)
    if job_id and target is not None:  # after the queue slot is released: _translate takes it again
        try:
            await _translate(bot, chat_id, job_id, target)
        except Exception:
            log.exception("media translation failed")


async def _run(bot, chat_id, status, source, title, url, file_id, file_name, instruction) -> int | None:
    async def stt_progress(done: int, total: int) -> None:
        if total > 1:
            await status.set(f"🎙 Распознаю речь: {done}/{total}…")

    with tempfile.TemporaryDirectory(prefix="media-") as tmp:
        work = Path(tmp)
        if url:
            await status.set("⏬ Скачиваю…")
            audio = await media.download_url(url, work)
            src, title = audio.path, audio.title or title or url
        else:
            await status.set("⏬ Получаю файл…")
            src = work / ("src" + (Path(file_name).suffix or ".bin"))
            await bot.download(file_id, destination=src)
        await status.set("🎙 Распознаю речь…")
        tr = await media.prepare_and_transcribe(src, work, stt_progress)

    if not tr.segments:
        await status.set("🤷 Речи в записи не нашлось.")
        return None

    async with SessionLocal() as db:
        await db.execute(delete(MediaJob).where(MediaJob.created_at < datetime.now(timezone.utc) - KEEP))
        job = MediaJob(
            chat_id=chat_id, source=source[:500], title=(title or "")[:300], language=tr.language,
            duration=tr.duration, stt_model=tr.model, text=tr.text, segments=tr.segments,
            cost_rub=tr.cost_rub, proposed_tasks=[],
        )  # fmt: skip
        db.add(job)
        await db.commit()
        await db.refresh(job)

    await status.set("🧠 Делаю выжимку…")
    digest: media_text.Digest | None = None
    try:
        digest = await media_text.summarize(
            media.paragraphs(tr.segments), job.title, tr.duration, tr.language, instruction
        )
    except Exception as exc:
        log.exception("media summary failed")
        await bot.send_message(chat_id, f"⚠️ Выжимка не получилась ({exc}), но расшифровка готова.")

    async with SessionLocal() as db:
        job = await db.get(MediaJob, job.id)
        if digest:
            job.summary = digest.as_text()
            job.proposed_tasks = [p.model_dump() for p in digest.proposed_tasks]
            if digest.title and job.source.startswith("telegram:"):
                job.title = digest.title[:300]
        await db.commit()
        await db.refresh(job)

    await bot.send_message(chat_id, _digest_message(job), reply_markup=_keyboard(job))
    await bot.send_document(
        chat_id,
        BufferedInputFile(_transcript_md(job).encode(), filename=f"{_safe_name(job.title)}.md"),
        caption="Полная расшифровка",
    )
    try:
        await status.message.delete()
    except Exception:
        pass
    return job.id


def _meta_line(job: MediaJob) -> str:
    parts = [f"⏱ {media.ts(job.duration)}", f"🌐 {media_text.lang_name(job.language)}"]
    if job.cost_rub:
        parts.append(f"💸 {job.cost_rub:.2f} ₽ за распознавание".replace(".", ","))
    return " · ".join(parts)


def _digest_message(job: MediaJob) -> str:
    head = f"🎙 {job.title}\n{_meta_line(job)}"
    body = job.summary or "Выжимки нет — расшифровка в файле."
    tail = ""
    if job.proposed_tasks and bot_access.is_owner(job.chat_id):
        tail = "\n\n💡 Задачи — нажмите, чтобы добавить:\n" + "\n".join(
            f"• {t['title']} — {t.get('reason', '')}" for t in job.proposed_tasks[:8]
        )
    room = 4000 - len(head) - len(tail) - 4
    if len(body) > room:
        body = body[: room - 30].rstrip() + "…\n(полностью — в файле)"
    return f"{head}\n\n{body}{tail}"


def _keyboard(job: MediaJob) -> InlineKeyboardMarkup:
    tasks = job.proposed_tasks[:8] if bot_access.is_owner(job.chat_id) else []  # guests: no owner's tasks
    rows = [
        [InlineKeyboardButton(text=f"➕ {t['title'][:40]}", callback_data=f"mtask:{job.id}:{i}")]
        for i, t in enumerate(tasks)
    ]
    target = media_text.default_target(job.language)
    rows.append(
        [
            InlineKeyboardButton(
                text=f"🌐 Перевод {media_text.LANGS[target][1]}", callback_data=f"mtr:{job.id}:{target}"
            ),
            InlineKeyboardButton(text="🎬 Субтитры", callback_data=f"msrt:{job.id}"),
        ]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _transcript_md(job: MediaJob, texts: list[str] | None = None, lang: str | None = None) -> str:
    head = f"# {job.title}\n\n{_meta_line(job)}"
    if job.source.startswith("http"):
        head += f"\n\nИсточник: {job.source}"
    if texts is not None:
        head += f"\n\nПеревод {media_text.LANGS.get(lang or '', ('', lang))[1]}"
    elif job.summary:
        head += f"\n\n## Выжимка\n\n{job.summary}"
    return f"{head}\n\n## {'Текст' if texts is not None else 'Расшифровка'}\n\n{media.paragraphs(job.segments, texts)}\n"


async def _translate(bot: Bot, chat_id: int, job_id: int, target: str) -> None:
    async with SessionLocal() as db:
        job = await db.get(MediaJob, job_id)
    if not job:
        await bot.send_message(chat_id, "Эта запись уже удалена.")
        return
    target = target or media_text.default_target(job.language)
    if target == job.language:
        await bot.send_message(chat_id, f"Запись и так на языке «{media_text.lang_name(target)}».")
        return
    status = Status(
        await bot.send_message(chat_id, f"🌐 Перевожу {media_text.LANGS.get(target, ('', target))[1]}…")
    )

    async def progress(done: int, total: int) -> None:
        if total > 1:
            await status.set(f"🌐 Перевожу: {done}/{total}…")

    async with _sem:
        try:
            texts = await media_text.translate(job.segments, target, progress)
        except Exception as exc:
            log.exception("media translation failed")
            await status.set(f"❌ Перевод не получился: {exc}")
            return
    name = _safe_name(job.title)
    await bot.send_document(
        chat_id,
        BufferedInputFile(_transcript_md(job, texts, target).encode(), filename=f"{name}.{target}.md"),
    )
    await bot.send_document(
        chat_id,
        BufferedInputFile(media.srt(job.segments, texts).encode(), filename=f"{name}.{target}.srt"),
        caption="Субтитры с переводом",
    )
    try:
        await status.message.delete()
    except Exception:
        pass


# ---------- buttons ----------


async def _job_for(query: CallbackQuery, owner_only: bool = False) -> tuple[MediaJob | None, list[str]]:
    """The job behind a button; a guest may only touch their own recordings."""
    uid = query.from_user.id
    if not (bot_access.is_owner(uid) or (not owner_only and bot_access.is_guest(uid))):
        await query.answer("Нет доступа", show_alert=True)
        return None, []
    parts = query.data.split(":")
    async with SessionLocal() as db:
        job = await db.get(MediaJob, int(parts[1]))
    if not job:
        await query.answer("Запись уже удалена")
        return None, parts
    if not bot_access.is_owner(uid) and job.chat_id != uid:
        await query.answer("Нет доступа", show_alert=True)
        return None, parts
    return job, parts


@router.callback_query(F.data.startswith("mtask:"))
async def cb_task(query: CallbackQuery) -> None:
    job, parts = await _job_for(query, owner_only=True)
    if not job:
        return
    idx = int(parts[2])
    if idx >= len(job.proposed_tasks):
        await query.answer("Предложение устарело")
        return
    proposal = job.proposed_tasks[idx]
    async with SessionLocal() as db:
        task = await create_task(
            db,
            TaskIn(
                title=proposal["title"],
                notes=f"{proposal.get('reason', '')}\nИз записи: {job.title}".strip(),
                priority=int(proposal.get("priority", 2)),
            ),
            source="ai",
            source_ref=f"media:{job.id}",
        )
    await query.answer(f"Добавлено: #{task.id}")


@router.callback_query(F.data.startswith("mtr:"))
async def cb_translate(query: CallbackQuery, bot: Bot) -> None:
    job, parts = await _job_for(query)
    if not job:
        return
    await query.answer("Перевожу…")
    _spawn(_safe(_translate(bot, query.message.chat.id, job.id, parts[2])))


@router.callback_query(F.data.startswith("msrt:"))
async def cb_srt(query: CallbackQuery, bot: Bot) -> None:
    job, _ = await _job_for(query)
    if not job:
        return
    await query.answer()
    await bot.send_document(
        query.message.chat.id,
        BufferedInputFile(media.srt(job.segments).encode(), filename=f"{_safe_name(job.title)}.srt"),
        caption="Субтитры на языке оригинала"
        + ("" if job.stt_model in media.VERBOSE_MODELS else " (грубые: модель не дала таймкоды)"),
    )
