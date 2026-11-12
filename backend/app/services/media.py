"""Audio pipeline for the bot: get the audio (a Telegram file or a link via yt-dlp), cut it into
small mono mp3 chunks with ffmpeg and transcribe the chunks with Polza.AI speech-to-text.

Russian recordings go to a diarizing model (GigaAM) that also labels speakers; the rest to Whisper.
A 30-second sample through a cheap model decides which. Speaker labels are per chunk ("2:S0" is
speaker S0 of chunk 2): the summary step later maps them to names, merging the same person across
chunks. Segments are [start, end, text] or [start, end, text, label].

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
# speakers are labelled per chunk, so diarized chunks are longer: 30 min is ~7 MB (~10 MB as base64)
DIARIZE_CHUNK_SECONDS = 1800
MAX_SECONDS = 4 * 3600
PARALLEL = 3
VERBOSE_MODELS = {"openai/whisper-1"}  # the only Whisper on Polza that returns segments with timecodes
SUBTITLE_CHARS = 110  # diarized turns are 15-25 s long: split them into subtitle-sized lines
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
    segments: list[list]  # [start, end, text] or [start, end, text, speaker label]
    model: str
    cost_rub: float

    @property
    def has_speakers(self) -> bool:
        return any(len(sg) > 3 for sg in self.segments)


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


async def to_chunks(src: Path, workdir: Path, seconds: int | None = None) -> list[tuple[Path, float, float]]:
    """-> [(chunk path, offset seconds, chunk duration)]"""
    seconds = seconds or CHUNK_SECONDS
    duration = await probe_duration(src)
    if duration > MAX_SECONDS + 60:
        raise MediaError(f"запись длиннее {MAX_SECONDS // 3600} часов")
    pattern = workdir / "chunk_%03d.mp3"
    rc, _, err = await _run(
        [
            "nice", "-n", "10", "ffmpeg", "-nostdin", "-v", "error", "-i", str(src),
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k",
            "-f", "segment", "-segment_time", str(seconds), "-reset_timestamps", "1",
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
        length = await probe_duration(path) or seconds
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
    fmt = "json"
    if model in VERBOSE_MODELS:
        fmt = "verbose_json"
    elif model == get_settings().stt_diarize_model:
        fmt = "diarized_json"
    body = {"model": model, "file": data_url, "response_format": fmt}
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


def _data_url(path: Path) -> str:
    return "data:audio/mpeg;base64," + base64.b64encode(path.read_bytes()).decode()


_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")


def split_turn(start: float, end: float, text: str, label: str | None) -> list[list]:
    """A diarized turn is 15-25 s of speech: cut it at sentence ends into subtitle-sized lines and
    share the time out by length (the model gives no word timings)."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(text.strip()):
        if pieces and len(pieces[-1]) + len(sentence) < SUBTITLE_CHARS:
            pieces[-1] += " " + sentence
        else:
            pieces.append(sentence)
    pieces = [p for p in pieces if p.strip()] or [text.strip()]
    total = sum(len(p) for p in pieces) or 1
    out, t = [], start
    for i, p in enumerate(pieces):
        t_end = end if i == len(pieces) - 1 else t + (end - start) * len(p) / total
        seg = [round(t, 2), round(t_end, 2), p.strip()]
        if label:
            seg.append(label)
        out.append(seg)
        t = t_end
    return out


async def _transcribe_chunk(
    client: httpx.AsyncClient, path: Path, offset: float, length: float, index: int = 0, diarize: bool = False
) -> tuple[list[list], str, float, str]:
    s = get_settings()
    data_url = _data_url(path)
    models = [m for m in ((s.stt_diarize_model if diarize else ""), s.stt_model, s.stt_fallback_model) if m]
    models = list(dict.fromkeys(models))
    for i, model in enumerate(models):
        try:
            data = await _stt_request(client, data_url, model)
            break
        except MediaError:
            if i == len(models) - 1:
                raise
            log.warning("stt %s failed for %s, trying %s", model, path.name, models[i + 1])
    cost = float((data.get("usage") or {}).get("cost_rub") or 0)
    if not cost:  # Polza does not always report usage: estimate from the published per-minute price
        cost = float(data.get("duration") or length) / 60 * await _price_per_minute(client, model)
    segs: list[list] = []
    for sg in data.get("segments") or []:
        text = (sg.get("text") or "").strip()
        if not text:
            continue
        start, end = offset + float(sg["start"]), offset + float(sg["end"])
        if sg.get("speaker") is not None:
            segs += split_turn(start, end, text, f"{index + 1}:{sg['speaker']}")
        else:
            segs.append([round(start, 2), round(end, 2), text])
    if not segs and (data.get("text") or "").strip():
        segs = [[round(offset, 2), round(offset + length, 2), data["text"].strip()]]
    return segs, _iso_lang(data.get("language")), cost, model


async def transcribe(
    chunks: list[tuple[Path, float, float]],
    progress: Progress | None = None,
    diarize: bool = False,
    language: str = "",
    extra_cost: float = 0.0,
) -> Transcript:
    s = get_settings()
    if not s.stt_key:
        raise MediaError("не задан ключ распознавания (STT_API_KEY или LLM_PROVIDER=polza с LLM_API_KEY)")
    sem = asyncio.Semaphore(PARALLEL)
    done = 0

    async with _client() as client:

        async def one(index: int, chunk: tuple[Path, float, float]):
            nonlocal done
            async with sem:
                res = await _transcribe_chunk(client, *chunk, index=index, diarize=diarize)
            done += 1
            if progress:
                await progress(done, len(chunks))
            return res

        results = await asyncio.gather(*(one(i, c) for i, c in enumerate(chunks)))

    segments = sorted((sg for r in results for sg in r[0]), key=lambda sg: sg[0])
    langs = Counter(r[1] for r in results if r[1])
    models = Counter(r[3] for r in results)
    return Transcript(
        text=" ".join(sg[2] for sg in segments),
        language=langs.most_common(1)[0][0] if langs else language,
        duration=sum(c[2] for c in chunks),
        segments=segments,
        model=models.most_common(1)[0][0],
        cost_rub=round(sum(r[2] for r in results) + extra_cost, 2),
    )


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=300, headers={"Authorization": f"Bearer {get_settings().stt_key}"})


_NOT_RU = set("іїєґўәғқңөұүһ")  # Ukrainian, Belarusian, Kazakh letters


def looks_russian(text: str) -> bool:
    letters = [c for c in text.lower() if c.isalpha()]
    if len(letters) < 10:
        return False
    cyr = sum("а" <= c <= "я" or c == "ё" or c in _NOT_RU for c in letters)
    return cyr / len(letters) > 0.7 and not _NOT_RU & set(letters)


async def detect_russian(src: Path, workdir: Path, duration: float) -> tuple[bool, float]:
    """30 seconds from the first third of the recording through a cheap model. -> (russian?, cost)"""
    s = get_settings()
    if not s.stt_diarize_model or not s.stt_probe_model or not s.stt_key:
        return False, 0.0
    sample = workdir / "probe.mp3"
    offset = min(60.0, duration / 3) if duration > 45 else 0.0
    rc, _, _ = await _run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{offset:.1f}", "-i", str(src), "-t", "30",
            "-vn", "-ac", "1", "-ar", "16000", "-c:a", "libmp3lame", "-b:a", "32k", str(sample),
        ],
        300,
    )  # fmt: skip
    if rc != 0 or not sample.exists():
        return False, 0.0
    try:
        async with _client() as client:
            data = await _stt_request(client, _data_url(sample), s.stt_probe_model)
            cost = 0.5 * await _price_per_minute(client, s.stt_probe_model)
    except MediaError as exc:
        log.warning("language probe failed: %s", exc)
        return False, 0.0
    finally:
        sample.unlink(missing_ok=True)
    return looks_russian(data.get("text") or ""), cost


async def prepare_and_transcribe(src: Path, workdir: Path, progress: Progress | None = None) -> Transcript:
    duration = await probe_duration(src)
    russian, probe_cost = await detect_russian(src, workdir, duration)
    chunks = await to_chunks(src, workdir, DIARIZE_CHUNK_SECONDS if russian else CHUNK_SECONDS)
    src.unlink(missing_ok=True)  # free disk early, the chunks are all we need
    return await transcribe(
        chunks, progress, diarize=russian, language="ru" if russian else "", extra_cost=probe_cost
    )


# ---------- formatting ----------


def ts(seconds: float) -> str:
    s = int(seconds)
    h, m, sec = s // 3600, s % 3600 // 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


def has_timecodes(model: str) -> bool:
    return model in VERBOSE_MODELS or model == get_settings().stt_diarize_model


def speaker_of(seg: list) -> str | None:
    return seg[3] if len(seg) > 3 else None


def srt(segments: list[list], texts: list[str] | None = None, names: dict[str, str] | None = None) -> str:
    """With speaker names, a line that starts a new turn gets "Имя: " in front."""

    def stamp(t: float) -> str:
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d},{ms % 1000:03d}"

    blocks, prev = [], None
    for i, seg in enumerate(segments):
        start, end, text = seg[:3]
        line = texts[i] if texts is not None and i < len(texts) else text
        who = speaker_of(seg)
        if names and who and who != prev and names.get(who):
            line = f"{names[who]}: {line}"
        prev = who
        blocks.append(f"{i + 1}\n{stamp(start)} --> {stamp(end)}\n{line}\n")
    return "\n".join(blocks)


def paragraphs(
    segments: list[list],
    texts: list[str] | None = None,
    every: float = 60,
    names: dict[str, str] | None = None,
) -> str:
    """Readable transcript: a paragraph with a [mm:ss] mark roughly every minute, split at a sentence end.
    With speaker names (label -> name), every change of speaker starts a "**Имя** [mm:ss]" paragraph."""
    out: list[str] = []
    current: list[str] = []
    start, who = 0.0, None
    for i, seg in enumerate(segments):
        s0, text = seg[0], seg[2]
        line = texts[i] if texts is not None and i < len(texts) else text
        speaker = speaker_of(seg) if names is not None else None
        if current:
            elapsed = s0 - start
            if (
                speaker != who
                or (elapsed >= every and current[-1].rstrip().endswith((".", "!", "?", "…")))
                or elapsed >= every * 3
            ):
                out.append(_para(start, who, current, names))
                current = []
        if not current:
            start, who = s0, speaker
        current.append(line)
    if current:
        out.append(_para(start, who, current, names))
    return "\n\n".join(out)


def _para(start: float, who: str | None, lines: list[str], names: dict[str, str] | None) -> str:
    name = (names or {}).get(who or "") or who
    return (f"**{name}** " if name else "") + f"[{ts(start)}] " + " ".join(lines)
