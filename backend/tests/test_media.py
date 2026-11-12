import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db import SessionLocal
from app.models import MediaJob, Task
from app.services import media, media_text, telegram_media

SEGMENTS = [
    [0.0, 4.0, "Привет, это тест."],
    [4.0, 70.0, "Обсудили бюджет."],
    [70.0, 75.0, "Иван пришлёт смету до пятницы."],
]


def test_formatting():
    assert media.ts(75) == "01:15"
    assert media.ts(3725) == "1:02:05"
    srt = media.srt(SEGMENTS[:1], ["Hi, this is a test."])
    assert srt.startswith("1\n00:00:00,000 --> 00:00:04,000\nHi, this is a test.")
    text = media.paragraphs(SEGMENTS)
    assert text.startswith("[00:00] Привет, это тест. Обсудили бюджет.") and "\n\n[01:10] Иван" in text


def test_translation_request_parsing():
    assert media_text.wants_translation("сделай выжимку") is None
    assert media_text.wants_translation("переведи на английский") == "en"
    assert media_text.wants_translation("Перевод") == ""
    assert media_text.default_target("ru") == "en" and media_text.default_target("en") == "ru"


async def test_transcribe_merges_chunks_and_falls_back(monkeypatch, tmp_path):
    monkeypatch.setenv("STT_API_KEY", "k")
    from app.config import get_settings

    get_settings.cache_clear()
    calls = []

    async def fake_request(client, data_url, model):
        calls.append(model)
        if model == "openai/whisper-1" and len(calls) == 1:
            raise media.MediaError("502")
        if model == "openai/whisper-1":
            return {"language": "russian", "segments": [{"start": 1, "end": 3, "text": " второй "}],
                    "usage": {"cost_rub": 0.5}}  # fmt: skip
        return {"text": "первый", "usage": {"cost_rub": 0.1}}

    monkeypatch.setattr(media, "_stt_request", fake_request)
    monkeypatch.setattr(media, "_prices", {"x": 1.0})  # no price lookup over the network
    chunks = []
    for i in range(2):
        p = tmp_path / f"chunk_{i}.mp3"
        p.write_bytes(b"x")
        chunks.append((p, i * 600.0, 600.0))
    media.PARALLEL = 1  # deterministic order for the fallback check
    try:
        tr = await media.transcribe(chunks)
    finally:
        media.PARALLEL = 3
        get_settings.cache_clear()
    assert calls == ["openai/whisper-1", "openai/whisper-large-v3-turbo", "openai/whisper-1"]
    assert tr.segments == [[0.0, 600.0, "первый"], [601.0, 603.0, "второй"]]
    assert tr.text == "первый второй" and tr.cost_rub == 0.6 and tr.duration == 1200


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="no ffmpeg")
async def test_to_chunks_cuts_audio(monkeypatch, tmp_path):
    src = tmp_path / "tone.wav"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=12", str(src)], check=True
    )
    monkeypatch.setattr(media, "CHUNK_SECONDS", 5)
    chunks = await media.to_chunks(src, tmp_path)
    assert len(chunks) == 3
    assert chunks[1][1] == pytest.approx(5, abs=0.2) and sum(c[2] for c in chunks) == pytest.approx(
        12, abs=0.3
    )


async def test_translate_halves_batch_when_model_merges_lines(monkeypatch):
    class FakeLLM:
        provider, model = "fake", "fake"

        async def complete_json(self, system, user, schema):
            lines = [ln.split(". ", 1)[1] for ln in user.splitlines()]
            if len(lines) > 1 and len(lines) % 2:  # odd batches come back merged
                return {"lines": [" ".join(lines)]}
            return {"lines": [f"EN {ln}" for ln in lines]}

    monkeypatch.setattr(media_text, "get_llm", lambda: FakeLLM())
    out = await media_text.translate(SEGMENTS, "en")
    assert out == [f"EN {s[2]}" for s in SEGMENTS]


class FakeMessage:
    def __init__(self, bot, text=""):
        self.bot, self.text = bot, text
        self.message_id = len(bot.sent) + 1

    async def edit_text(self, text):
        self.bot.edits.append(text)

    async def delete(self):
        pass


class FakeBot:
    def __init__(self):
        self.sent, self.docs, self.edits = [], [], []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((text, reply_markup))
        return FakeMessage(self, text)

    async def send_document(self, chat_id, document, caption=None):
        self.docs.append(document.filename)

    async def download(self, file_id, destination):
        Path(destination).write_bytes(b"audio")


async def test_bot_pipeline_summary_tasks_and_translation(monkeypatch):
    async def fake_transcribe(src, work, progress=None):
        assert src.read_bytes() == b"audio"
        return media.Transcript("…", "ru", 75, SEGMENTS, "openai/whisper-1", 0.9)

    async def fake_summary(text, title, duration, language, instruction=""):
        assert "[00:00]" in text and instruction == "переведи на английский"
        return media_text.Digest(
            title="Планёрка", summary="Обсудили бюджет.", key_points=["Смета до пятницы"],
            proposed_tasks=[media_text.Proposal(title="Получить смету от Ивана", reason="обещал")],
        )  # fmt: skip

    async def fake_translate(segments, target, progress=None):
        assert target == "en"
        return [f"EN {s[2]}" for s in segments]

    monkeypatch.setattr(media, "prepare_and_transcribe", fake_transcribe)
    monkeypatch.setattr(media_text, "summarize", fake_summary)
    monkeypatch.setattr(media_text, "translate", fake_translate)

    bot = FakeBot()
    status = telegram_media.Status(FakeMessage(bot, "⏳"))
    await asyncio.wait_for(
        telegram_media._process(
            bot, 42, status, source="telegram:voice", title="Голосовое", file_id="f", file_name="",
            instruction="переведи на английский",
        ),
        5,
    )  # fmt: skip

    async with SessionLocal() as db:
        job = (await db.scalars(select(MediaJob))).one()
    assert (
        job.title == "Планёрка"
        and job.language == "ru"
        and job.proposed_tasks[0]["title"].startswith("Получить")
    )
    text, keyboard = bot.sent[0]
    assert "🎙 Планёрка" in text and "Смета до пятницы" in text and "💸 0,90 ₽" in text
    datas = [b.callback_data for row in keyboard.inline_keyboard for b in row]
    assert datas == [f"mtask:{job.id}:0", f"mtr:{job.id}:en", f"msrt:{job.id}"]
    assert bot.docs == ["Планёрка.md", "Планёрка.en.md", "Планёрка.en.srt"]

    class Query:
        data = f"mtask:{job.id}:0"
        from_user = type("U", (), {"id": 1})()
        answers: list = []

        async def answer(self, text=None, show_alert=False):
            self.answers.append(text)

    q = Query()
    await telegram_media.cb_task(q)
    async with SessionLocal() as db:
        task = (await db.scalars(select(Task))).one()
    assert task.title == "Получить смету от Ивана" and task.source_ref == f"media:{job.id}"


async def test_download_error_is_reported(monkeypatch):
    async def fail(url, work):
        raise media.MediaError("не удалось скачать: по этой ссылке не нашёл ни видео, ни аудио")

    monkeypatch.setattr(media, "download_url", fail)
    bot = FakeBot()
    await telegram_media._process(
        bot, 1, telegram_media.Status(FakeMessage(bot, "⏳")), source="https://x", url="https://x"
    )
    assert bot.edits[-1].startswith("❌ Не получилось: не удалось скачать")
