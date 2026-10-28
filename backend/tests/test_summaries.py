from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import TelegramChat, TelegramMessage
from app.services import summarizer


async def test_summary_flow(client, auth, monkeypatch):
    async with SessionLocal() as db:
        db.add(TelegramChat(chat_id=-100, title="Проект X", kind="supergroup"))
        now = datetime.now(timezone.utc)
        db.add(
            TelegramMessage(
                chat_id=-100,
                message_id=1,
                sender="Аня",
                text="Нужно отправить отчёт в пятницу",
                sent_at=now - timedelta(hours=1),
            )
        )
        await db.commit()

    async def fake_summarize(messages_text, open_tasks, calendar_text="", period_label=""):
        assert "Проект X" in messages_text and "отчёт" in messages_text
        return summarizer.SummaryResult(
            summary="Обсуждали отчёт.",
            highlights=["Отчёт в пятницу"],
            proposed_tasks=[
                {"title": "Отправить отчёт", "reason": "Аня просила", "priority": 1, "chat": "Проект X"}
            ],
        )

    monkeypatch.setattr(summarizer, "summarize", fake_summarize)

    resp = await client.post("/api/summaries?hours=24", headers=auth)
    assert resp.status_code == 200, resp.text
    summary = resp.json()
    assert "Отчёт в пятницу" in summary["text"]
    assert len(summary["proposed_tasks"]) == 1

    accepted = await client.post(f"/api/summaries/{summary['id']}/accept/0", headers=auth)
    assert accepted.status_code == 200
    assert accepted.json()["source"] == "ai" and accepted.json()["priority"] == 1

    chats = (await client.get("/api/telegram/chats", headers=auth)).json()
    assert chats[0]["title"] == "Проект X"


async def test_summary_without_messages_returns_null(client, auth):
    resp = await client.post("/api/summaries", headers=auth)
    assert resp.status_code == 200 and resp.json() is None


async def test_webhook_rejects_bad_secret(client):
    resp = await client.post(
        "/api/telegram/webhook", json={}, headers={"X-Telegram-Bot-Api-Secret-Token": "wrong"}
    )
    assert resp.status_code == 403
