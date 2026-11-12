"""What the LLM does with a transcript: a Plaud-style summary (with proposed tasks) and translation
that keeps the segment timecodes, so it can also be saved as subtitles."""

import asyncio
import logging

from pydantic import BaseModel, Field, ValidationError

from app.services import media
from app.services.llm import LLMError, get_llm

log = logging.getLogger(__name__)

MAX_CHARS = 350_000  # ~4-5 hours of speech; longer transcripts are cut for the summary
BATCH_CHARS = 6000
PARALLEL = 3

LANGS = {
    "ru": ("русский", "на русский"), "en": ("английский", "на английский"), "de": ("немецкий", "на немецкий"),
    "fr": ("французский", "на французский"), "es": ("испанский", "на испанский"),
    "it": ("итальянский", "на итальянский"), "pt": ("португальский", "на португальский"),
    "uk": ("украинский", "на украинский"), "zh": ("китайский", "на китайский"),
    "ja": ("японский", "на японский"), "ko": ("корейский", "на корейский"), "tr": ("турецкий", "на турецкий"),
    "pl": ("польский", "на польский"), "kk": ("казахский", "на казахский"),
}  # fmt: skip
_LANG_STEMS = {
    "русск": "ru", "англ": "en", "немец": "de", "франц": "fr", "испан": "es", "итал": "it",
    "португ": "pt", "украин": "uk", "китай": "zh", "япон": "ja", "корей": "ko", "турец": "tr",
    "польск": "pl", "казах": "kk", "english": "en", "russian": "ru",
}  # fmt: skip


SUMMARY_LANG = "ru"  # summaries are always written in Russian


def lang_name(code: str) -> str:
    return LANGS.get(code, (code or "?", f"на {code}"))[0]


def wants_translation(instruction: str) -> str | None:
    """'переведи на английский' -> 'en'; 'перевод' -> '' (pick automatically); no request -> None."""
    low = instruction.lower()
    if "перев" not in low and "translat" not in low:
        return None
    for stem, code in _LANG_STEMS.items():
        if stem in low:
            return code
    return ""


def default_target(source_lang: str) -> str:
    return "en" if source_lang == "ru" else "ru"


# ---------- summary ----------

SUMMARY_PROMPT = """Ты делаешь конспект аудиозаписи или видео по расшифровке (как Plaud или Otter).
Расшифровка автоматическая: в ней бывают ошибки распознавания, повторы и слова-паразиты — молча
исправляй очевидное, не выдумывай того, чего нет.

ВСЕ поля ответа (title, summary, key_points, decisions, chapters, proposed_tasks) пиши ТОЛЬКО на
русском языке, даже если запись на украинском, английском или любом другом языке: переводи смысл,
имена собственные передавай в русском написании.

Определи жанр (встреча, звонок, лекция, интервью, видеоролик, голосовая заметка) и пиши под него:
- summary: суть в 3–8 предложениях — о чём запись и к чему пришли;
- key_points: главные мысли и факты, с цифрами и именами, если они есть;
- decisions: принятые решения и договорённости (для лекций и роликов обычно пусто);
- chapters: оглавление по темам с таймкодом начала из расшифровки (формат мм:сс или ч:мм:сс),
  для записей короче 5 минут — пусто;
- proposed_tasks: конкретные действия для владельца, которые следуют из записи (кто что обещал
  сделать, что нужно проверить или подготовить). Если поводов нет — пустой список.

Если пользователь дал пожелание — учти его в первую очередь."""

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "description": "Короткое название записи, до 80 символов"},
        "summary": {"type": "string"},
        "key_points": {"type": "array", "items": {"type": "string"}},
        "decisions": {"type": "array", "items": {"type": "string"}},
        "chapters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"time": {"type": "string"}, "title": {"type": "string"}},
                "required": ["time", "title"],
                "additionalProperties": False,
            },
        },
        "proposed_tasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "reason": {"type": "string", "description": "Откуда задача, 1 фраза"},
                    "priority": {"type": "integer", "description": "1 высокий, 2 обычный, 3 низкий"},
                },
                "required": ["title", "reason", "priority"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["title", "summary", "key_points", "decisions", "chapters", "proposed_tasks"],
    "additionalProperties": False,
}


class Chapter(BaseModel):
    time: str
    title: str


class Proposal(BaseModel):
    title: str
    reason: str = ""
    priority: int = Field(default=2, ge=1, le=3)


class Digest(BaseModel):
    title: str = ""
    summary: str
    key_points: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    chapters: list[Chapter] = Field(default_factory=list)
    proposed_tasks: list[Proposal] = Field(default_factory=list)

    def as_text(self) -> str:
        parts = [self.summary.strip()]
        if self.key_points:
            parts.append("Главное:\n" + "\n".join(f"• {p}" for p in self.key_points))
        if self.decisions:
            parts.append("Решения:\n" + "\n".join(f"• {d}" for d in self.decisions))
        if self.chapters:
            parts.append("Содержание:\n" + "\n".join(f"{c.time} {c.title}" for c in self.chapters))
        return "\n\n".join(parts)


async def summarize(
    transcript_text: str, title: str, duration: float, language: str, instruction: str = ""
) -> Digest:
    text = transcript_text
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS] + "\n…(дальше обрезано)…"
    user = (
        f"Источник: {title or 'без названия'}\nДлительность: {media.ts(duration)}\n"
        f"Язык записи: {lang_name(language)}\n"
        + (f"Пожелание пользователя: {instruction}\n" if instruction else "")
        + f"\n## Расшифровка\n{text}"
    )
    user += "\n\n(Напоминание: весь ответ — на русском языке.)"
    llm = get_llm()
    data = await llm.complete_json(SUMMARY_PROMPT, user, SUMMARY_SCHEMA)
    try:
        return Digest.model_validate(data)
    except ValidationError as exc:
        raise LLMError(f"{llm.provider}/{llm.model}: ответ не по схеме: {exc.errors()[0]['msg']}") from exc


# ---------- translation ----------

TRANSLATE_PROMPT = """Ты переводчик. Тебе дают пронумерованные строки автоматической расшифровки речи.
Переведи каждую строку {target}, естественно и разговорно, как субтитры. Очевидные ошибки распознавания
исправляй по смыслу. Строки не объединяй и не дели: в lines должно быть ровно {n} элементов, в том же
порядке, без номеров."""

TRANSLATE_SCHEMA = {
    "type": "object",
    "properties": {"lines": {"type": "array", "items": {"type": "string"}}},
    "required": ["lines"],
    "additionalProperties": False,
}


def _batches(lines: list[str]) -> list[tuple[int, int]]:
    out, start, size = [], 0, 0
    for i, line in enumerate(lines):
        if size and size + len(line) > BATCH_CHARS:
            out.append((start, i))
            start, size = i, 0
        size += len(line) + 8
    if start < len(lines):
        out.append((start, len(lines)))
    return out


async def _translate_batch(llm, lines: list[str], target: str) -> list[str]:
    numbered = "\n".join(f"{i + 1}. {line}" for i, line in enumerate(lines))
    system = TRANSLATE_PROMPT.format(target=LANGS.get(target, ("", f"на язык {target}"))[1], n=len(lines))
    for _ in range(2):
        data = await llm.complete_json(system, numbered, TRANSLATE_SCHEMA)
        out = [str(x).strip() for x in data.get("lines") or []]
        if len(out) == len(lines):
            return out
    if len(lines) > 1:  # the model merged or split lines: halve the batch until counts match
        mid = len(lines) // 2
        return await _translate_batch(llm, lines[:mid], target) + await _translate_batch(
            llm, lines[mid:], target
        )
    return out[:1] or lines


async def translate(segments: list[list], target: str, progress: media.Progress | None = None) -> list[str]:
    lines = [sg[2] for sg in segments]
    batches = _batches(lines)
    llm = get_llm()
    sem = asyncio.Semaphore(PARALLEL)
    done = 0

    async def one(a: int, b: int) -> list[str]:
        nonlocal done
        async with sem:
            out = await _translate_batch(llm, lines[a:b], target)
        done += 1
        if progress:
            await progress(done, len(batches))
        return out

    parts = await asyncio.gather(*(one(a, b) for a, b in batches))
    return [line for part in parts for line in part]


SUMMARY_TR_PROMPT = """Переведи заголовок и конспект записи {target}. Сохрани структуру: абзацы, заголовки
разделов, маркеры «•», таймкоды и переносы строк. Ничего не добавляй и не сокращай."""

SUMMARY_TR_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "text": {"type": "string"}},
    "required": ["title", "text"],
    "additionalProperties": False,
}


async def translate_summary(title: str, text: str, target: str) -> dict:
    llm = get_llm()
    data = await llm.complete_json(
        SUMMARY_TR_PROMPT.format(target=LANGS.get(target, ("", f"на язык {target}"))[1]),
        f"Заголовок: {title}\n\nКонспект:\n{text}",
        SUMMARY_TR_SCHEMA,
    )
    if not str(data.get("text") or "").strip():
        raise LLMError("пустой перевод выжимки")
    return {"title": str(data.get("title") or title).strip(), "text": str(data["text"]).strip()}
