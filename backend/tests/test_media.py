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

    async def fake_translate_summary(title, text, target):
        return {"title": "Planning", "text": "We discussed the budget."}

    monkeypatch.setattr(media_text, "translate_summary", fake_translate_summary)

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
    assert datas == [f"mtask:{job.id}:0", f"mtm:{job.id}:en", f"msrt:{job.id}"]
    assert bot.docs == ["Планёрка.md", "Планёрка.en.md", "Планёрка.en.srt"]
    assert bot.sent[2][0].startswith("🌐 Planning")  # "переведи на английский" translates the summary too

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


async def test_translation_menu_parts_and_cache(monkeypatch):
    calls = []

    async def fake_translate(segments, target, progress=None):
        calls.append(("lines", target))
        return [f"RU {s[2]}" for s in segments]

    async def fake_translate_summary(title, text, target):
        calls.append(("summary", target))
        return {"title": "Planning", "text": "Budget."}

    monkeypatch.setattr(media_text, "translate", fake_translate)
    monkeypatch.setattr(media_text, "translate_summary", fake_translate_summary)
    async with SessionLocal() as db:
        job = MediaJob(
            chat_id=1, source="https://youtu.be/x", title="ГОТУЙТЕСЯ", summary_title="Готовьтесь к худшему",
            summary="Суть", language="uk", segments=SEGMENTS, proposed_tasks=[], translations={},
        )  # fmt: skip
        db.add(job)
        await db.commit()
        await db.refresh(job)

    # the summary is already Russian: no "Выжимку" for Russian, all four for English
    def menu(target):
        return [b.callback_data for row in telegram_media._menu(job, target).inline_keyboard for b in row]

    assert menu("ru") == [f"mtr:{job.id}:ru:srt", f"mtr:{job.id}:ru:text", f"mtr:{job.id}:ru:all",
                          f"mtm:{job.id}:en", f"mback:{job.id}"]  # fmt: skip
    assert menu("en")[:4] == [f"mtr:{job.id}:en:{p}" for p in ("summary", "srt", "text", "all")]
    assert telegram_media._digest_message(job).startswith("🎙 Готовьтесь к худшему\n▶️ ГОТУЙТЕСЯ")

    bot = FakeBot()
    await telegram_media._translate(bot, 1, job.id, "ru", "srt")
    await telegram_media._translate(bot, 1, job.id, "ru", "text")
    await telegram_media._translate(bot, 1, job.id, "en", "summary")
    await telegram_media._translate(bot, 1, job.id, "en", "summary")
    assert calls == [("lines", "ru"), ("summary", "en")]  # paid once per language
    assert bot.docs == ["ГОТУЙТЕСЯ.ru.srt", "ГОТУЙТЕСЯ.ru.md"]
    assert [t for t, _ in bot.sent if t.startswith("🌐 Planning")] == ["🌐 Planning\n\nBudget."] * 2


def test_russian_detection():
    assert media.looks_russian("Я считаю, что мы как парламентарии будем говорить")
    assert not media.looks_russian("Я вважаю, що ми як парламентарі будемо говорити і їсти")  # Ukrainian
    assert not media.looks_russian("I think we as parliamentarians will talk")
    assert not media.looks_russian("ну")


def test_split_turn_shares_time_by_length():
    out = media.split_turn(10, 30, "Первое предложение тут. " + "Второе длинное предложение. " * 6, "1:S0")
    assert len(out) > 1 and out[0][0] == 10 and out[-1][1] == 30 and all(sg[3] == "1:S0" for sg in out)
    assert all(a[1] == b[0] for a, b in zip(out, out[1:], strict=False))
    assert media.split_turn(0, 5, "Коротко.", None) == [[0, 5, "Коротко."]]


async def test_diarized_chunk_labels_and_whisper_fallback(monkeypatch, tmp_path):
    from app.config import get_settings

    calls = []

    async def fake_request(client, data_url, model):
        calls.append(model)
        if model == "ai-sage/gigaam-v3-diarize" and len(calls) > 1:
            raise media.MediaError("503")
        if model == "ai-sage/gigaam-v3-diarize":
            return {"duration": 20, "segments": [
                {"start": 0, "end": 5, "text": "Здравствуйте.", "speaker": "S0"},
                {"start": 5, "end": 9, "text": "Добрый день.", "speaker": "S1"}]}  # fmt: skip
        return {"language": "russian", "segments": [{"start": 0, "end": 2, "text": "Без меток."}]}

    monkeypatch.setattr(media, "_stt_request", fake_request)
    monkeypatch.setattr(media, "_prices", {"x": 1.0})
    get_settings.cache_clear()
    path = tmp_path / "c.mp3"
    path.write_bytes(b"x")
    segs, _, _, model = await media._transcribe_chunk(None, path, 1800, 20, index=1, diarize=True)
    assert segs == [[1800, 1805, "Здравствуйте.", "2:S0"], [1805, 1809, "Добрый день.", "2:S1"]]
    segs, lang, _, model = await media._transcribe_chunk(None, path, 0, 20, index=0, diarize=True)
    assert model == "openai/whisper-1" and segs == [[0, 2, "Без меток."]] and lang == "ru"


async def test_prepare_picks_diarization_for_russian(monkeypatch, tmp_path):
    seen = {}

    async def fake_detect(src, work, duration):
        return True, 0.02

    async def fake_chunks(src, work, seconds=None):
        seen["seconds"] = seconds
        return [(tmp_path / "c.mp3", 0.0, 60.0)]

    async def fake_transcribe(chunks, progress=None, diarize=False, language="", extra_cost=0.0):
        seen.update(diarize=diarize, language=language, extra=extra_cost)

    async def fake_probe(path):
        return 60.0

    monkeypatch.setattr(media, "detect_russian", fake_detect)
    monkeypatch.setattr(media, "to_chunks", fake_chunks)
    monkeypatch.setattr(media, "transcribe", fake_transcribe)
    monkeypatch.setattr(media, "probe_duration", fake_probe)
    await media.prepare_and_transcribe(tmp_path / "src.mp3", tmp_path)
    assert seen == {"seconds": media.DIARIZE_CHUNK_SECONDS, "diarize": True, "language": "ru", "extra": 0.02}


DIALOG = [
    [0.0, 4.0, "Добрый день, начнём.", "1:S0"],
    [4.0, 8.0, "Да, все на связи.", "1:S1"],
    [8.0, 12.0, "Тогда по бюджету.", "1:S0"],
]


def test_speaker_formatting():
    names = {"1:S0": "Ведущий", "1:S1": "Иван"}
    text = media.paragraphs(DIALOG, names=names)
    assert text.split("\n\n") == [
        "**Ведущий** [00:00] Добрый день, начнём.",
        "**Иван** [00:04] Да, все на связи.",
        "**Ведущий** [00:08] Тогда по бюджету.",
    ]
    assert media.paragraphs(DIALOG, names={}).startswith("**1:S0** [00:00]")
    assert media.paragraphs(DIALOG).startswith("[00:00] Добрый день, начнём. Да, все")  # no speakers mode
    assert "\nИван: Да, все на связи.\n" in media.srt(DIALOG, names=names)
    assert media_text.speaker_names(DIALOG + [[12, 13, "Ещё.", "2:S0"]], {"1:S0": "Ведущий"}) == {
        "1:S0": "Ведущий", "1:S1": "Спикер 2", "2:S0": "Спикер 3"}  # fmt: skip


async def test_bot_pipeline_names_speakers(monkeypatch):
    async def fake_transcribe(src, work, progress=None):
        return media.Transcript("…", "ru", 12, DIALOG, "ai-sage/gigaam-v3-diarize", 0.1)

    async def fake_summary(text, title, duration, language, instruction=""):
        assert "**1:S1** [00:04]" in text
        return media_text.Digest(
            title="Созвон", summary="Обсудили бюджет.",
            speakers=[media_text.Speaker(label="1:S0", name="Ведущий"), media_text.Speaker(label="1:S1", name="Иван")],
        )  # fmt: skip

    async def fake_translate(segments, target, progress=None):
        return [f"EN {s[2]}" for s in segments]

    async def fake_translate_summary(title, text, target):
        return {"title": "Call", "text": "Budget."}

    monkeypatch.setattr(media, "prepare_and_transcribe", fake_transcribe)
    monkeypatch.setattr(media_text, "summarize", fake_summary)
    monkeypatch.setattr(media_text, "translate", fake_translate)
    monkeypatch.setattr(media_text, "translate_summary", fake_translate_summary)
    bot = FakeBot()
    sent_docs = []

    async def keep_doc(chat_id, document, caption=None):
        sent_docs.append((document.filename, document.data.decode()))

    bot.send_document = keep_doc
    await telegram_media._process(bot, 42, telegram_media.Status(FakeMessage(bot, "⏳")), source="telegram:voice",
                                  title="Голосовое", file_id="f")  # fmt: skip
    async with SessionLocal() as db:
        job = (await db.scalars(select(MediaJob))).one()
    assert job.speakers == {"1:S0": "Ведущий", "1:S1": "Иван"}
    assert "👥 Ведущий, Иван" in bot.sent[0][0]
    assert "**Иван** [00:04] Да, все на связи." in sent_docs[0][1]

    await telegram_media._translate(bot, 42, job.id, "en", "all")
    md = dict(sent_docs)["Созвон.en.md"]
    srt = dict(sent_docs)["Созвон.en.srt"]
    assert "**EN Иван** [00:04] EN Да, все на связи." in md and "EN Ведущий: EN Тогда по бюджету." in srt
