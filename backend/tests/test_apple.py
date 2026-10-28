from app.services import apple_reminders
from app.services.apple_reminders import ReminderIn

SNAPSHOT = {
    "items": [
        {
            "title": "Купить корм коту",
            "list": "Семья",
            "due": "28.10.2026 18:00",
            "priority": "High",
            "completed": "No",
        },
        {
            "title": "Позвонить маме",
            "list": "Личное",
            "due": "",
            "priority": "",
            "completed": False,
            "notes": "после 19",
        },
        {"title": "Сделано", "list": "Личное", "completed": "Yes"},
        {
            "title": "Купить корм коту",
            "list": "Семья",
            "due": "2026-10-28T18:00:00+03:00",
        },  # dup (different tz parse)
    ]
}


def test_reminder_parsing():
    r = ReminderIn.model_validate(SNAPSHOT["items"][0])
    assert r.priority == 1 and r.completed is False and r.due.strftime("%d.%m %H:%M") == "28.10 18:00"
    r2 = ReminderIn.model_validate(SNAPSHOT["items"][1])
    assert r2.due is None and r2.priority == 0 and r2.notes == "после 19"
    assert ReminderIn.model_validate({"title": "x", "due": "2026-01-02T03:04:05Z"}).due is not None


async def test_sync_list_and_convert(client, auth):
    # token in the query string works too (Shortcuts-friendly)
    token = auth["Authorization"].split()[1]
    resp = await client.post(f"/api/apple/reminders/sync?token={token}", json=SNAPSHOT)
    assert resp.status_code == 200, resp.text
    assert resp.json()["stored"] == 3  # completed one dropped, naive/aware dup kept as two keys

    listed = (await client.get("/api/apple/reminders", headers=auth)).json()
    assert listed["last_sync"]["count"] == 3
    titles = [i["title"] for i in listed["items"]]
    assert "Сделано" not in titles and titles[0] == "Купить корм коту"  # due first

    first = listed["items"][0]
    task = await client.post(f"/api/apple/reminders/{first['id']}/to-task", headers=auth)
    assert task.status_code == 200 and task.json()["source"] == "apple" and task.json()["priority"] == 1
    assert "Список Apple: Семья" in task.json()["notes"]
    again = await client.post(f"/api/apple/reminders/{first['id']}/to-task", headers=auth)
    assert again.status_code == 409

    # second sync without "Позвонить маме" removes it, keeps task link on the surviving one
    smaller = {"items": [SNAPSHOT["items"][0]]}
    await client.post("/api/apple/reminders/sync", json=smaller, headers=auth)
    listed = (await client.get("/api/apple/reminders", headers=auth)).json()
    assert [i["title"] for i in listed["items"]] == ["Купить корм коту"]
    assert listed["items"][0]["imported_task_id"] == task.json()["id"]


async def test_sync_accepts_single_dict_and_rejects_unauth(client, auth):
    assert (
        await client.post("/api/apple/reminders/sync", json={"items": {"title": "одно"}}, headers=auth)
    ).json()["stored"] == 1
    assert (await client.post("/api/apple/reminders/sync", json={"items": []})).status_code == 401


def test_reminders_text_formatting():
    from app.models import AppleReminder

    rows = [
        AppleReminder(title="A", list_name="Семья", due_at=None),
        AppleReminder(title="B", list_name="", due_at=None),
    ]
    text = apple_reminders.reminders_text(rows)
    assert text == "• [Семья] A\n• B"
