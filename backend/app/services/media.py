"""Audio pipeline for the bot: get the audio (a Telegram file or a link via yt-dlp), cut it into
small mono mp3 chunks with ffmpeg and transcribe the chunks with Polza.AI speech-to-text.

yt-dlp runs as a subprocess so it can be upgraded in place when YouTube changes something
(see _self_update) without restarting the backend."""

import asyncio
import base64
import logging
import re
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)

STT_URL = "https://polza.ai/api/v1/audio/transcriptions"
CHUNK_SECONDS = 600  # 10 min of 32 kbit/s mono mp3 is ~2.4 MB, well under Polza's ~15 MB body limit
MAX_SECONDS = 4 * 3600
PARALLEL = 3
VERBOSE_MODELS = {"openai/whisper-1"}  # the only Polza STT model that returns segments with timecodes
URL_RE = re.compile(r"https?://[^\s<>\"']+")

# whisper-1 reports the language as an English name
_LANG_NAMES = {
    "russian": "ru", "english": "en", "german": "de", "french": "fr", "spanish": "es",
    "italian": "it", "portuguese": "pt", "ukrainian": "uk", "chinese": "zh", "japanese": "ja",
    "korean": "ko", "turkish": "tr", "polish": "pl", "dutch": "nl", "arabic": "ar", "hindi": "hi",
    "kazakh": "kk", "belarusian": "be", "hebrew": "he", "czech": "cs",
}  # fmt: skip


class MediaError(RuntimeError):
    """Message for the user (Russian)."""


@dataclass
class Audio:
    path: Path
    title: str
    duration: float


@dataclass
class Transcript:
    text: str
    language: str
    duration: float
    segments: list[list]  # [start, end, text]
    model: str
    cost_rub: float


Progress = Callable[[int, int], Awaitable[None]]


async def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise MediaError("слишком долго, остановил") from None
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


async def probe_duration(path: Path) -> float:
    rc, out, _ = await _run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)], 60
    )
    try:
        return float(out.strip()) if rc == 0 else 0.0
    except ValueError:
        return 0.0


# ---------- download ----------

_last_update = 0.0


async def _self_update() -> bool:
    """YouTube breaks old yt-dlp versions every few weeks: upgrade at most once per 6 hours."""
    global _last_update
    if time.monotonic() - _last_update < 6 * 3600 and _last_update:
        return False
    _last_update = time.monotonic()
    rc, _, err = await _run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "-q",
            "--disable-pip-version-check",
            "-U",
            "yt-dlp[default]",
        ],
        300,
    )
    log.info("yt-dlp self-update rc=%s %s", rc, err[-300:])
    return rc == 0


def _ytdlp_error(stderr: str) -> str:
    lines = [ln for ln in stderr.strip().splitlines() if "ERROR" in ln] or stderr.strip().splitlines()[-1:]
    msg = (lines[-1] if lines else "неизвестная ошибка").replace("ERROR: ", "")
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return "YouTube просит подтвердить, что это не бот. Пришлите файл или другую ссылку."
    if "Unsupported URL" in msg:
        return "по этой ссылке не нашёл ни видео, ни аудио"
    if "does not pass filter" in msg:
        return f"запись длиннее {MAX_SECONDS // 3600} часов"
    if "File is larger than max-filesize" in msg:
        return "файл больше 1 ГБ"
    return msg[:300]


async def download_url(url: str, workdir: Path) -> Audio:
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/best",
        "--no-playlist", "--no-progress", "--no-warnings",
        "--match-filter", f"duration <? {MAX_SECONDS}",
        "--max-filesize", "1G",
        "-o", str(workdir / "src.%(ext)s"),
        "--print", "after_move:%(duration)s\t%(title)s",
        url,
    ]  # fmt: skip
    rc, out, err = await _run(cmd, 1200)
    if rc != 0 and "Unsupported URL" not in err and "does not pass filter" not in err:
        if await _self_update():
            rc, out, err = await _run(cmd, 1200)
    if rc != 0:
        raise MediaError(f"не удалось скачать: {_ytdlp_error(err)}")
    files = sorted(p for p in workdir.glob("src.*") if not p.name.endswith(".part"))
    if not files:
        raise MediaError("не удалось скачать: файл не появился")
    duration_s, _, title = (out.strip().splitlines() or [""])[-1].partition("\t")
    try:
        duration = float(duration_s)
    except ValueError:
        duration = await probe_duration(files[0])
    return Audio(path=files[0], title=title.strip()[:300], duration=duration)


# ---------- chunks ----------


async def to_chunks(src: Path, workdir: Path) -> list[tuple[Path, float, float]]:
    """-> [(chunk path, offset seconds, chunk duration)]"""
    duration = await probe_duration(src)
    if duration > MAX_SECONDS + 60:
        raise MediaError(f"запись длиннее {MAX_SECONDS // 3600} часов")
    pattern = workdir / "chunk_%03d.mp3"
    rc, _, err = await _run(
        [
            "nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
            "-f", "segment", "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
            str(pattern),
        ],
        1800,
    )  # fmt: skip
    chunks = sorted(workdir.glob("chunk_*.mp3"))
    if rc != 0 or not chunks:
        raise MediaError(
            "в файле не нашлось звуковой дорожки" if "no streams" in err.lower() or not chunks else err[-300:]
        )
    result, offset = [], 0.0
    for path in chunks:
        length = await probe_duration(path) or CHUNK_SECONDS
        result.append((path, offset, length))
        offset += length
    return result


# ---------- speech-to-text ----------


def _iso_lang(value: str | None) -> str:
    v = (value or "").strip().lower()
    return _LANG_NAMES.get(v, v[:5])


_prices: dict[str, float] = {}


async def _price_per_minute(client: httpx.AsyncClient, model: str) -> float:
    if not _prices:
        try:
            resp = await client.get(STT_URL.replace("/audio/transcriptions", "/models"), timeout=30)
            items = resp.json().get("data", [])
            for m in items:
                price = ((m.get("top_provider") or {}).get("pricing") or {}).get("stt_per_minute")
                if price:
                    _prices[m["id"]] = float(price)
        except Exception as exc:  # the cost line is informational only
            log.debug("stt prices unavailable: %s", exc)
    return _prices.get(model, 0.0)


async def _stt_request(client: httpx.AsyncClient, data_url: str, model: str) -> dict:
    body = {
        "model": model,
        "file": data_url,
        "response_format": "verbose_json" if model in VERBOSE_MODELS else "json",
    }
    last = ""
    for attempt in range(3):
        try:
            resp = await client.post(STT_URL, json=body)
        except httpx.HTTPError as exc:
            last = f"нет соединения ({exc.__class__.__name__})"
        else:
            if resp.status_code == 200:
                return resp.json()
            last = f"{resp.status_code}: {resp.text[:200]}"
            if resp.status_code < 500 and resp.status_code != 429:
                break
        await asyncio.sleep(3 * (attempt + 1))
    raise MediaError(f"распознавание ({model}) не удалось: {last}")


async def _transcribe_chunk(
    client: httpx.AsyncClient, path: Path, offset: float, length: float
) -> tuple[list[list], str, float, str]:
    s = get_settings()
    data_url = "data:audio/mpeg;base64," + base64.b64encode(path.read_bytes()).decode()
    model = s.stt_model
    try:
        data = await _stt_request(client, data_url, model)
    except MediaError:
        if not s.stt_fallback_model or s.stt_fallback_model == model:
            raise
        log.warning("stt %s failed for %s, trying %s", model, path.name, s.stt_fallback_model)
        model = s.stt_fallback_model
        data = await _stt_request(client, data_url, model)
    cost = float((data.get("usage") or {}).get("cost_rub") or 0)
    if not cost:  # Polza does not always report usage: estimate from the published per-minute price
        cost = float(data.get("duration") or length) / 60 * await _price_per_minute(client, model)
    segs = [
        [round(offset + float(sg["start"]), 2), round(offset + float(sg["end"]), 2), sg["text"].strip()]
        for sg in data.get("segments") or []
        if sg.get("text", "").strip()
    ]
    if not segs and (data.get("text") or "").strip():
        segs = [[round(offset, 2), round(offset + length, 2), data["text"].strip()]]
    return segs, _iso_lang(data.get("language")), cost, model


async def transcribe(chunks: list[tuple[Path, float, float]], progress: Progress | None = None) -> Transcript:
    s = get_settings()
    if not s.stt_key:
        raise MediaError("не задан ключ распознавания (STT_API_KEY или LLM_PROVIDER=polza с LLM_API_KEY)")
    sem = asyncio.Semaphore(PARALLEL)
    done = 0

    async with httpx.AsyncClient(timeout=300, headers={"Authorization": f"Bearer {s.stt_key}"}) as client:

        async def one(chunk: tuple[Path, float, float]):
            nonlocal done
            async with sem:
                res = await _transcribe_chunk(client, *chunk)
            done += 1
            if progress:
                await progress(done, len(chunks))
            return res

        results = await asyncio.gather(*(one(c) for c in chunks))

    segments = sorted((sg for r in results for sg in r[0]), key=lambda sg: sg[0])
    langs = Counter(r[1] for r in results if r[1])
    models = Counter(r[3] for r in results)
    return Transcript(
        text=" ".join(sg[2] for sg in segments),
        language=langs.most_common(1)[0][0] if langs else "",
        duration=sum(c[2] for c in chunks),
        segments=segments,
        model=models.most_common(1)[0][0],
        cost_rub=round(sum(r[2] for r in results), 2),
    )


async def prepare_and_transcribe(src: Path, workdir: Path, progress: Progress | None = None) -> Transcript:
    chunks = await to_chunks(src, workdir)
    src.unlink(missing_ok=True)  # free disk early, the chunks are all we need
    return await transcribe(chunks, progress)


# ---------- formatting ----------


def ts(seconds: float) -> str:
    s = int(seconds)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def srt(segments: list[list], texts: list[str] | None = None) -> str:
    def stamp(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"

    blocks = []
    for i, (start, end, text) in enumerate(segments):
        line = texts[i] if texts is not None and i < len(texts) else text
        blocks.append(f"{i + 1}\n{stamp(start)} --> {stamp(end)}\n{line}\n")
    return "\n".join(blocks)


def paragraphs(segments: list[list], texts: list[str] | None = None, every: float = 60) -> str:
    """Readable transcript: a paragraph with a [mm:ss] mark roughly every minute, split at a sentence end."""
    out: list[str] = []
    current: list[str] = []
    start = 0.0
    for i, (s0, _, text) in enumerate(segments):
        line = texts[i] if texts is not None and i < len(texts) else text
        if current:
            elapsed = s0 - start
            if (
                elapsed >= every and current[-1].rstrip().endswith((".", "!", "?", "…"))
            ) or elapsed >= every * 3:
                out.append(f"[{ts(start)}] " + " ".join(current))
                current = []
        if not current:
            start = s0
        current.append(line)
    if current:
        out.append(f"[{ts(start)}] " + " ".join(current))
    return "\n\n".join(out)
