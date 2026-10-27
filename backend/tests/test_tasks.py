async def test_task_crud(client, auth):
    created = await client.post("/api/tasks", json={"title": "Купить молоко", "priority": 1}, headers=auth)
    assert created.status_code == 201
    task = created.json()
    assert task["status"] == "todo" and task["source"] == "manual"

    listed = (await client.get("/api/tasks", headers=auth)).json()
    assert [t["id"] for t in listed] == [task["id"]]

    done = await client.patch(f"/api/tasks/{task['id']}", json={"status": "done"}, headers=auth)
    assert done.json()["completed_at"] is not None

    reopened = await client.patch(f"/api/tasks/{task['id']}", json={"status": "todo"}, headers=auth)
    assert reopened.json()["completed_at"] is None

    assert (await client.delete(f"/api/tasks/{task['id']}", headers=auth)).status_code == 204
    assert (await client.get("/api/tasks", headers=auth)).json() == []


async def test_due_date_can_be_cleared(client, auth):
    created = (await client.post("/api/tasks", json={"title": "x", "due_at": "2030-01-01T10:00:00Z"}, headers=auth)).json()
    assert created["due_at"]
    cleared = (await client.patch(f"/api/tasks/{created['id']}", json={"clear_due": True}, headers=auth)).json()
    assert cleared["due_at"] is None


async def test_validation(client, auth):
    assert (await client.post("/api/tasks", json={"title": ""}, headers=auth)).status_code == 422
    assert (await client.post("/api/tasks", json={"title": "x", "priority": 9}, headers=auth)).status_code == 422
