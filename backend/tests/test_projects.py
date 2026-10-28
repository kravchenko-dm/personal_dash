from datetime import datetime, timedelta, timezone

from app.db import SessionLocal
from app.models import TelegramChat, TelegramMessage
from app.services import summarizer
from app.services.projects import slugify


def test_slugify():
    assert slugify("Мой Сервис 2") == "moj-servis-2"
    assert slugify("!!!") == "project"


async def test_project_crud_and_slug_uniqueness(client, auth):
    a = await client.post("/api/projects", json={"name": "Shop API", "stage": "dev"}, headers=auth)
    assert a.status_code == 201, a.text
    b = await client.post("/api/projects", json={"name": "Shop API"}, headers=auth)
    assert a.json()["slug"] == "shop-api" and b.json()["slug"] == "shop-api-2"

    listed = (await client.get("/api/projects", headers=auth)).json()
    assert [p["name"] for p in listed] == ["Shop API", "Shop API"]
    assert listed[0]["feature_counts"]["idea"] == 0

    pid = a.json()["id"]
    upd = await client.put(
        f"/api/projects/{pid}",
        json={"name": "Shop API v2", "stage": "beta", "site_url": "https://x"},
        headers=auth,
    )
    assert upd.json()["slug"] == "shop-api-v2" and upd.json()["stage"] == "beta"

    st = await client.patch(f"/api/projects/{pid}/status", json={"status_note": "Чиним оплату"}, headers=auth)
    assert st.json()["status_note"] == "Чиним оплату" and st.json()["status_note_at"]

    assert (
        await client.put(f"/api/projects/{pid}", json={"name": "x", "stage": "nope"}, headers=auth)
    ).status_code == 422
    assert (await client.delete(f"/api/projects/{pid}", headers=auth)).status_code == 204
    assert (await client.get(f"/api/projects/{pid}", headers=auth)).status_code == 404


async def test_feature_board_moves(client, auth):
    pid = (await client.post("/api/projects", json={"name": "P"}, headers=auth)).json()["id"]
    ids = []
    for title in ("A", "B", "C"):
        r = await client.post(f"/api/projects/{pid}/features", json={"title": title}, headers=auth)
        assert r.status_code == 201
        ids.append(r.json()["id"])
    feats = (await client.get(f"/api/projects/{pid}/features", headers=auth)).json()
    assert [f["position"] for f in feats] == [1, 2, 3]

    # move C to planned
    moved = (
        await client.post(
            f"/api/projects/{pid}/features/{ids[2]}/move", json={"status": "planned"}, headers=auth
        )
    ).json()
    assert moved["status"] == "planned" and moved["position"] == 1

    # move A to the end of idea column -> B becomes 1, A 2
    await client.post(
        f"/api/projects/{pid}/features/{ids[0]}/move", json={"status": "idea", "index": 5}, headers=auth
    )
    feats = (await client.get(f"/api/projects/{pid}/features", headers=auth)).json()
    idea = [(f["title"], f["position"]) for f in feats if f["status"] == "idea"]
    assert idea == [("B", 1), ("A", 2)]

    done = (
        await client.post(
            f"/api/projects/{pid}/features/{ids[1]}/move", json={"status": "done"}, headers=auth
        )
    ).json()
    assert done["done_at"] is not None
    back = (
        await client.post(
            f"/api/projects/{pid}/features/{ids[1]}/move", json={"status": "testing"}, headers=auth
        )
    ).json()
    assert back["done_at"] is None

    patched = (
        await client.patch(
            f"/api/projects/{pid}/features/{ids[0]}",
            json={"priority": 1, "eta": "2026-12-01T00:00:00Z"},
            headers=auth,
        )
    ).json()
    assert patched["priority"] == 1 and patched["eta"]
    cleared = (
        await client.patch(f"/api/projects/{pid}/features/{ids[0]}", json={"clear_eta": True}, headers=auth)
    ).json()
    assert cleared["eta"] is None

    counts = (await client.get("/api/projects", headers=auth)).json()[0]["feature_counts"]
    assert counts == {"idea": 1, "planned": 1, "in_progress": 0, "testing": 1, "done": 0}

    # feature of another project is not reachable through this one
    other = (await client.post("/api/projects", json={"name": "Q"}, headers=auth)).json()["id"]
    assert (await client.delete(f"/api/projects/{other}/features/{ids[0]}", headers=auth)).status_code == 404
    assert (await client.delete(f"/api/projects/{pid}/features/{ids[0]}", headers=auth)).status_code == 204


async def test_notes_and_detail(client, auth):
    pid = (await client.post("/api/projects", json={"name": "P"}, headers=auth)).json()["id"]
    n1 = (
        await client.post(
            f"/api/projects/{pid}/notes", json={"title": "Окружения", "body": "prod: https://x"}, headers=auth
        )
    ).json()
    n2 = (
        await client.post(
            f"/api/projects/{pid}/notes",
            json={"title": "Чеклист релиза", "body": "- [ ] миграции", "pinned": True},
            headers=auth,
        )
    ).json()
    detail = (await client.get(f"/api/projects/{pid}", headers=auth)).json()
    assert [n["title"] for n in detail["notes"]] == ["Чеклист релиза", "Окружения"]  # pinned first
    upd = (
        await client.put(
            f"/api/projects/{pid}/notes/{n1['id']}",
            json={"title": "Окружения", "body": "prod: https://y", "pinned": False},
            headers=auth,
        )
    ).json()
    assert upd["body"] == "prod: https://y"
    assert (await client.delete(f"/api/projects/{pid}/notes/{n2['id']}", headers=auth)).status_code == 204
    assert detail["project"]["name"] == "P" and detail["features"] == [] and detail["chats"] == []


async def test_project_chat_binding_and_project_summary(client, auth, monkeypatch):
    pid = (
        await client.post("/api/projects", json={"name": "Shop", "description": "Магазин"}, headers=auth)
    ).json()["id"]
    await client.post(
        f"/api/projects/{pid}/features", json={"title": "Оплата СБП", "status": "in_progress"}, headers=auth
    )
    now = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        db.add(TelegramChat(chat_id=-1, title="Shop dev", kind="supergroup"))
        db.add(TelegramChat(chat_id=-2, title="Другое", kind="supergroup"))
        db.add(
            TelegramMessage(
                chat_id=-1,
                message_id=1,
                sender="Петя",
                text="Надо добавить промокоды",
                sent_at=now - timedelta(hours=1),
            )
        )
        db.add(
            TelegramMessage(
                chat_id=-2,
                message_id=2,
                sender="Вася",
                text="Посторонний чат",
                sent_at=now - timedelta(hours=1),
            )
        )
        await db.commit()
    chats = (await client.get("/api/telegram/chats", headers=auth)).json()
    shop_chat = next(c for c in chats if c["title"] == "Shop dev")
    attached = (await client.put(f"/api/projects/{pid}/chats/{shop_chat['id']}", headers=auth)).json()
    assert attached["project_id"] == pid

    seen: dict = {}

    async def fake_summarize(messages_text, open_tasks, calendar_text="", period_label=""):
        seen["messages"] = messages_text
        seen["tasks"] = open_tasks
        return summarizer.SummaryResult(
            summary="Обсуждали промокоды.",
            proposed_tasks=[
                {"title": "Промокоды в корзине", "reason": "просил Петя", "priority": 2, "chat": "Shop dev"}
            ],
        )

    monkeypatch.setattr(summarizer, "summarize", fake_summarize)
    s = (await client.post(f"/api/summaries?project_id={pid}", headers=auth)).json()
    assert s["project_id"] == pid
    assert "промокоды" in seen["messages"] and "Посторонний чат" not in seen["messages"]
    assert "Проект: Shop" in seen["tasks"] and "Оплата СБП" in seen["tasks"]

    # summary of the bound chat alone also picks up the project
    s2 = (await client.post("/api/summaries?chat_id=-1", headers=auth)).json()
    assert s2["project_id"] == pid

    feat = await client.post(f"/api/summaries/{s['id']}/accept-feature/0", headers=auth)
    assert feat.status_code == 200, feat.text
    assert (
        feat.json()["source"] == "ai" and feat.json()["status"] == "idea" and feat.json()["project_id"] == pid
    )

    detail = (await client.get(f"/api/projects/{pid}", headers=auth)).json()
    assert [c["title"] for c in detail["chats"]] == ["Shop dev"]
    assert len(detail["summaries"]) == 2
    assert len(detail["features"]) == 2

    detached = (await client.delete(f"/api/projects/{pid}/chats/{shop_chat['id']}", headers=auth)).json()
    assert detached["project_id"] is None
    # project with no chats -> no messages -> null summary
    assert (await client.post(f"/api/summaries?project_id={pid}", headers=auth)).json() is None
