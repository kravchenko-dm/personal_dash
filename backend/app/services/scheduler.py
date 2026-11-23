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
from app.services import (
    agenda,
    apple_reminders,
    backups,
    balances,
    bot_access,
    calendar_alerts,
    monitoring,
    push,
    summaries,
    telegram_bot,
    yougile_alerts,
)
from app.services.tasks import open_tasks_text

log = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone=get_settings().tz)


async def job_refresh_balances() -> None:
    async with SessionLocal() as db:
        await balances.refresh_all(db)


async def _tell(
    db, user_id: int, title: str, body: str, telegram: str, url: str, tag: str, markup=None
) -> None:
    """Push + Telegram to one user: the owner through the usual owner channel, members to their DM."""
    await push.notify_user(db, user_id, title, body, url=url, tag=tag)
    acc = bot_access.get(user_id)
    if acc is None:
        return
    if acc.is_owner:
        await telegram_bot.send_to_owner(telegram, reply_markup=markup)
    else:
        await telegram_bot.send_to_user(acc.telegram_id, telegram, reply_markup=markup)


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
            await _tell(
                db, task.user_id, f"Задача: {task.title}", f"Срок {when}",
                f"⏰ {task.title} — срок {when}\n/done {task.id}", url="/tasks", tag=f"task-{task.id}",
            )  # fmt: skip
            task.reminded_at = now
        if due:
            await db.commit()


async def job_daily_digest() -> None:
    """Every user with a workspace and the evening digest on gets a summary of their own chats."""
    if not get_settings().llm_enabled:
        return
    for acc in bot_access.workspace_accounts():
        if not acc.evening_digest:
            continue
        async with SessionLocal() as db:
            try:
                summary = await summaries.build_summary(
                    db, chat_id=None, hours=24, mark_summarized=True, user_id=acc.id
                )
            except Exception as exc:
                log.exception("daily digest for user %s failed: %s", acc.id, exc)
                continue
            if not summary:
                continue
            await telegram_bot.send_summary(summary)
            await push.notify_user(
                db, acc.id, "Вечерний дайджест", summary.text[:120], url="/summaries", tag="digest"
            )


async def job_caldav_reminders() -> None:
    async with SessionLocal() as db:
        try:
            await apple_reminders.sync_caldav(db)
        except Exception as exc:
            log.info("caldav reminders sync skipped: %s", exc)


async def job_morning_brief() -> None:
    for acc in bot_access.workspace_accounts():
        if not acc.morning_brief:
            continue
        try:
            async with SessionLocal() as db:
                await _morning_brief(db, acc)
        except Exception as exc:
            log.exception("morning brief for user %s failed: %s", acc.id, exc)


async def _morning_brief(db, acc) -> None:
    tasks = await open_tasks_text(db, acc.id, limit=10)
    text = f"☀️ Доброе утро!\n\nЗадачи:\n{tasks}"
    if acc.is_owner:  # calendar, Apple and YouGile are the owner's
        cal = ""
        try:
            cal = await agenda.upcoming_text(db, days=1)
        except Exception as exc:
            log.info("morning brief: calendar skipped: %s", exc)
        reminders = apple_reminders.reminders_text(await apple_reminders.list_reminders(db))
        if reminders:
            text += f"\n\nНапоминания Apple:\n{reminders}"
        if cal:
            text += f"\n\nСегодня в календаре:\n{cal}"
        try:
            ygl = await yougile_alerts.brief_text(db)
        except Exception as exc:
            ygl = ""
            log.info("morning brief: yougile skipped: %s", exc)
        if ygl:
            text += f"\n\nYouGile:\n{ygl}"
    elif tasks == "Открытых задач нет.":
        return  # members get no brief on an empty list
    await _tell(
        db, acc.id, "Утренний бриф", tasks.splitlines()[0] if tasks else "", text, url="/", tag="morning"
    )


async def job_calendar_alerts() -> None:
    async with SessionLocal() as db:
        try:
            stats = await calendar_alerts.tick(db)
            if any(stats[k] for k in ("reminded", "added", "changed", "removed")):
                log.info("calendar alerts: %s", stats)
        except Exception as exc:
            log.warning("calendar alerts failed: %s", exc)


async def job_yougile_alerts() -> None:
    async with SessionLocal() as db:
        try:
            stats = await yougile_alerts.tick(db)
            if any(stats.values()):
                log.info("yougile alerts: %s", stats)
        except Exception as exc:
            log.warning("yougile alerts failed: %s", exc)


async def job_http_checks() -> None:
    async with SessionLocal() as db:
        try:
            await monitoring.run_http_checks(db)
        except Exception as exc:
            log.warning("http checks failed: %s", exc)


async def job_silent_agents() -> None:
    async with SessionLocal() as db:
        await monitoring.check_silent_agents(db)


async def job_backups_tick() -> None:
    async with SessionLocal() as db:
        try:
            await backups.tick(db)
        except Exception as exc:
            log.warning("backups tick failed: %s", exc)


async def job_cleanup_samples() -> None:
    async with SessionLocal() as db:
        await monitoring.cleanup_old_samples(db)


async def job_mail_collect() -> None:
    from app.services import mail_collector

    await mail_collector.collect_all()


async def job_max_watchdog() -> None:
    from app.services import max_collector

    await max_collector.watchdog()


def start() -> None:
    scheduler.add_job(job_refresh_balances, IntervalTrigger(minutes=30), id="balances", replace_existing=True)
    scheduler.add_job(job_task_reminders, IntervalTrigger(minutes=5), id="reminders", replace_existing=True)
    scheduler.add_job(job_daily_digest, CronTrigger(hour=21, minute=0), id="digest", replace_existing=True)
    scheduler.add_job(job_morning_brief, CronTrigger(hour=8, minute=0), id="morning", replace_existing=True)
    scheduler.add_job(
        job_caldav_reminders, IntervalTrigger(hours=1), id="caldav_reminders", replace_existing=True
    )
    # monitoring + backups: HTTP checks honour each monitor's own interval, so a 1-minute tick is fine
    scheduler.add_job(
        job_calendar_alerts, IntervalTrigger(minutes=5), id="calendar_alerts", replace_existing=True
    )
    scheduler.add_job(
        job_yougile_alerts, IntervalTrigger(minutes=5), id="yougile_alerts", replace_existing=True
    )
    scheduler.add_job(job_http_checks, IntervalTrigger(minutes=1), id="http_checks", replace_existing=True)
    scheduler.add_job(
        job_silent_agents, IntervalTrigger(minutes=1), id="silent_agents", replace_existing=True
    )
    scheduler.add_job(job_backups_tick, IntervalTrigger(minutes=1), id="backups_tick", replace_existing=True)
    scheduler.add_job(
        job_cleanup_samples, CronTrigger(hour=3, minute=30), id="cleanup_samples", replace_existing=True
    )
    # request collector: supplier mail, restarting dead MAX clients
    scheduler.add_job(job_mail_collect, IntervalTrigger(minutes=2), id="mail_collect", replace_existing=True)
    scheduler.add_job(job_max_watchdog, IntervalTrigger(minutes=1), id="max_watchdog", replace_existing=True)
    scheduler.start()
    log.info("scheduler started (%s)", get_settings().tz)


def stop() -> None:
    if scheduler.running:
        scheduler.shutdown(wait=False)
