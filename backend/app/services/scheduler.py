"""Background jobs: balance refresh, task reminders, daily digest, morning brief."""

import logging
from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select

from app.config import get_settings
from app.db import SessionLocal
from app.models import Task
from app.services import agenda, apple_reminders, balances, push, summaries, telegram_bot
from app.services.tasks import open_tasks_text

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=get_settings().tz)


async def job_refresh_balances() -> None:
    async with SessionLocal() as db:
        await balances.refresh_all(db)


async def job_task_reminders() -> None:
    now = datetime.now(timezone.utc)
    horizon = now + timedelta(minutes=15)
    async with SessionLocal() as db:
        stmt = select(Task).where(
            Task.status != "done",
            Task.due_at.is_not(None),
            Task.due_at <= horizon,
            Task.reminded_at.is_(None),
        )
        due = list((await db.scalars(stmt)).all())
        for task in due:
            when = task.due_at.astimezone().strftime("%H:%M")
            await push.notify_all(
                db, f"Задача: {task.title}", f"Срок {when}", url="/tasks", tag=f"task-{task.id}"
            )
            await telegram_bot.send_to_owner(f"⏰ {task.title} — срок {when}\n/done {task.id}")
            task.reminded_at = now
        if due:
            await db.commit()


async def job_daily_digest() -> None:
    if not get_settings().llm_enabled:
        return
    async with SessionLocal() as db:
        try:
            summary = await summaries.build_summary(db, chat_id=None, hours=24, mark_summarized=True)
        except Exception as exc:
            log.exception("daily digest failed: %s", exc)
            return
        if not summary:
            return
        await telegram_bot.send_summary_to_owner(summary)
        await push.notify_all(db, "Вечерний дайджест", summary.text[:120], url="/summaries", tag="digest")


async def job_caldav_reminders() -> None:
    async with SessionLocal() as db:
        try:
            await apple_reminders.sync_caldav(db)
        except Exception as exc:
            log.info("caldav reminders sync skipped: %s", exc)


async def job_morning_brief() -> None:
    async with SessionLocal() as db:
        tasks = await open_tasks_text(db, limit=10)
        cal = ""
        try:
            cal = await agenda.upcoming_text(db, days=1)
        except Exception as exc:
            log.info("morning brief: calendar skipped: %s", exc)
        text = f"☀️ Доброе утро!\n\nЗадачи:\n{tasks}"
        reminders = apple_reminders.reminders_text(await apple_reminders.list_reminders(db))
        if reminders:
            text += f"\n\nНапоминания Apple:\n{reminders}"
        if cal:
            text += f"\n\nСегодня в календаре:\n{cal}"
        await telegram_bot.send_to_owner(text)
        await push.notify_all(
            db, "Утренний бриф", tasks.splitlines()[0] if tasks else "", url="/", tag="morning"
        )


def start() -> None:
    scheduler.add_job(job_refresh_balances, IntervalTrigger(minutes=30), id="balances", replace_existing=True)
    scheduler.add_job(job_task_reminders, IntervalTrigger(minutes=5), id="reminders", replace_existing=True)
    scheduler.add_job(job_daily_digest, CronTrigger(hour=21, minute=0), id="digest", replace_existing=True)
    scheduler.add_job(job_morning_brief, CronTrigger(hour=8, minute=0), id="morning", replace_existing=True)
    scheduler.add_job(
        job_caldav_reminders, IntervalTrigger(hours=1), id="caldav_reminders", replace_existing=True
    )
    scheduler.start()
    log.info("scheduler started (%s)", get_settings().tz)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
