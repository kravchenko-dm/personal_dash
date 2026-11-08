"""Mirror of the restic and rclone binaries for dash-agent.

Some VPS cannot reach GitHub (or get a proxy's 504 on api.github.com), so the agent falls back to
`GET /api/agent/tools/{name}?arch=…` on the dashboard. The dashboard downloads the release once,
keeps it in a cache directory and streams the raw (decompressed) binary to agents.
"""

import asyncio
import bz2
import io
import logging
import os
import zipfile
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

TOOLS = ("restic", "rclone")
ARCHES = ("amd64", "arm64", "arm")
CACHE_DIR = Path(os.environ.get("DASH_TOOLS_CACHE", "/var/cache/dash-tools"))
_locks: dict[str, asyncio.Lock] = {}


async def fetch_restic(arch: str) -> bytes:
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        meta = (
            (await client.get("https://api.github.com/repos/restic/restic/releases/latest"))
            .raise_for_status()
            .json()
        )
        want = f"_linux_{arch}.bz2"
        asset = next((a for a in meta.get("assets", []) if a["name"].endswith(want)), None)
        if not asset:
            raise RuntimeError(f"no restic build for {arch}")
        data = (await client.get(asset["browser_download_url"])).raise_for_status().content
    return bz2.decompress(data)


async def fetch_rclone(arch: str) -> bytes:
    async with httpx.AsyncClient(timeout=300, follow_redirects=True) as client:
        data = (
            (await client.get(f"https://downloads.rclone.org/rclone-current-linux-{arch}.zip"))
            .raise_for_status()
            .content
        )
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        member = next(n for n in zf.namelist() if n.endswith("/rclone"))
        return zf.read(member)


FETCHERS = {"restic": fetch_restic, "rclone": fetch_rclone}


async def ensure_tool(name: str, arch: str) -> Path:
    """Path to the cached binary, downloading it on first request. Raises on unknown tool/arch."""
    if name not in TOOLS or arch not in ARCHES:
        raise KeyError(f"{name}/{arch}")
    path = CACHE_DIR / f"{name}_{arch}"
    if path.is_file() and path.stat().st_size > 0:
        return path
    lock = _locks.setdefault(f"{name}_{arch}", asyncio.Lock())
    async with lock:
        if path.is_file() and path.stat().st_size > 0:
            return path
        log.info("tool mirror: downloading %s for %s", name, arch)
        data = await FETCHERS[name](arch)
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".part")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        log.info("tool mirror: cached %s (%d bytes)", path.name, len(data))
    return path
