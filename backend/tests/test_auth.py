async def test_login_wrong_password(client):
    resp = await client.post("/api/auth/login", json={"password": "nope"})
    assert resp.status_code == 401


async def test_protected_requires_token(client):
    assert (await client.get("/api/tasks")).status_code == 401


async def test_status(client, auth):
    resp = await client.get("/api/status", headers=auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["telegram"] is False and body["push"] is False
