"""YouGile REST API v2 client (https://yougile.com/api-v2).

Views offered to the UI: projects -> boards -> columns -> tasks (kanban), a flat list,
tasks grouped by deadline, and "my tasks" (assigned to the API-key owner)."""

import logging
import time
from typing import Any

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)
BASE = "https://yougile.com/api-v2"
_cache: dict[str, tuple[float, Any]] = {}
CACHE_TTL = 60


def _headers() -> dict[str, str]:
    key = get_settings().yougile_api_key
    if not key:
        raise RuntimeError("YOUGILE_API_KEY не задан")
    return {"Authorization": f"Bearer {key}"}


async def _get_all(path: str, params: dict | None = None) -> list[dict]:
    """Paginate a YouGile list endpoint ({paging, content})."""
    items: list[dict] = []
    offset = 0
    async with httpx.AsyncClient(base_url=BASE, headers=_headers(), timeout=30) as client:
        while True:
            resp = await client.get(path, params={**(params or {}), "limit": 1000, "offset": offset})
            resp.raise_for_status()
            data = resp.json()
            content = data.get("content", data if isinstance(data, list) else [])
            items.extend(content)
            paging = data.get("paging", {}) if isinstance(data, dict) else {}
            if not paging.get("next"):
                break
            offset += len(content)
            if not content:
                break
    return items


async def _cached(key: str, coro_factory):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < CACHE_TTL:
        return hit[1]
    value = await coro_factory()
    _cache[key] = (now, value)
    return value


def invalidate() -> None:
    _cache.clear()


async def me() -> dict:
    """The API-key owner."""

    async def _load():
        async with httpx.AsyncClient(base_url=BASE, headers=_headers(), timeout=30) as client:
            resp = await client.get("/users/me")
            resp.raise_for_status()
            return resp.json()

    return await _cached("me", _load)


async def users() -> list[dict]:
    return await _cached("users", lambda: _get_all("/users"))


async def projects() -> list[dict]:
    return await _cached("projects", lambda: _get_all("/projects"))


async def boards(project_id: str | None = None) -> list[dict]:
    params = {"projectId": project_id} if project_id else {}
    return await _cached(f"boards:{project_id}", lambda: _get_all("/boards", params))


async def columns(board_id: str | None = None) -> list[dict]:
    params = {"boardId": board_id} if board_id else {}
    return await _cached(f"columns:{board_id}", lambda: _get_all("/columns", params))


async def tasks(column_id: str | None = None, include_deleted: bool = False) -> list[dict]:
    params: dict[str, Any] = {"includeDeleted": str(include_deleted).lower()}
    if column_id:
        params["columnId"] = column_id

    async def _load():
        try:
            return await _get_all("/task-list", params)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code != 404:
                raise
            return await _get_all("/tasks", params)  # older API without /task-list

    return await _cached(f"tasks:{column_id}", _load)


async def overview() -> dict:
    """Everything the UI needs for the kanban/list/deadline views in one call."""
    prj, brd, cols, tsk, usr = await projects(), await boards(), await columns(), await tasks(), await users()
    user_map = {u["id"]: u.get("realName") or u.get("email", "") for u in usr}
    col_map = {c["id"]: c for c in cols}
    board_map = {b["id"]: b for b in brd}
    out_tasks = []
    for t in tsk:
        if t.get("deleted") or t.get("archived"):
            continue
        col = col_map.get(t.get("columnId"))
        board = board_map.get(col["boardId"]) if col else None
        deadline = (t.get("deadline") or {}).get("deadline")
        out_tasks.append(
            {
                "id": t["id"],
                "title": t.get("title", ""),
                "description": (t.get("description") or "")[:1000],
                "completed": bool(t.get("completed")),
                "column_id": t.get("columnId"),
                "column": col.get("title") if col else None,
                "board_id": board["id"] if board else None,
                "board": board.get("title") if board else None,
                "project_id": board.get("projectId") if board else None,
                "deadline": deadline,
                "assigned": [user_map.get(uid, uid) for uid in (t.get("assigned") or [])],
                "assigned_ids": t.get("assigned") or [],
                "updated": t.get("timestamp"),
            }
        )
    return {
        "projects": [{"id": p["id"], "title": p.get("title", "")} for p in prj if not p.get("deleted")],
        "boards": [
            {"id": b["id"], "title": b.get("title", ""), "project_id": b.get("projectId")}
            for b in brd
            if not b.get("deleted")
        ],
        "columns": [
            {
                "id": c["id"],
                "title": c.get("title", ""),
                "board_id": c.get("boardId"),
                "color": c.get("color"),
            }
            for c in cols
            if not c.get("deleted")
        ],
        "tasks": out_tasks,
        "users": [{"id": u["id"], "name": user_map[u["id"]]} for u in usr],
    }


async def task_log(task_id: str, limit: int = 20) -> list[dict]:
    """Newest-first history of a task: entries like {type: done|executor|move|add, userId, timestamp}."""
    async with httpx.AsyncClient(base_url=BASE, headers=_headers(), timeout=30) as client:
        resp = await client.get(f"/tasks/{task_id}/log", params={"limit": limit})
        resp.raise_for_status()
        data = resp.json()
    return data.get("content", []) if isinstance(data, dict) else data


async def update_task(task_id: str, patch: dict) -> dict:
    async with httpx.AsyncClient(base_url=BASE, headers=_headers(), timeout=30) as client:
        resp = await client.put(f"/tasks/{task_id}", json=patch)
        resp.raise_for_status()
    invalidate()
    return resp.json()


async def create_task(title: str, column_id: str, description: str = "") -> dict:
    async with httpx.AsyncClient(base_url=BASE, headers=_headers(), timeout=30) as client:
        resp = await client.post(
            "/tasks", json={"title": title, "columnId": column_id, "description": description}
        )
        resp.raise_for_status()
    invalidate()
    return resp.json()
