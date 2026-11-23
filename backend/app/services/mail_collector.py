"""Request collector, mail: new letters from a user's suppliers go to their bot chat.

The mailbox is read over IMAP strictly read-only (EXAMINE + BODY.PEEK), so nothing gets marked as
read. Progress is the last seen UID; on the first run (or when the server resets UIDVALIDITY) only the
position is remembered, old mail is never forwarded."""

import asyncio
import email
import html
import imaplib
import logging
import re
from datetime import datetime, timezone
from email.message import EmailMessage
from email.policy import default as default_policy
from email.utils import getaddresses

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal
from app.models import MailAccount, MailRule
from app.services import bot_access, inbox

log = logging.getLogger(__name__)

TIMEOUT = 30
BATCH = 20  # letters fetched per pass
TEXT_LIMIT = 3500
ALERT_AFTER = 3  # failures in a row before the user hears about it


class MailError(Exception):
    pass


# ---------- matching ----------


def normalize_patterns(raw: list[str] | str) -> list[str]:
    """ "Ivan@X.ru, supplier.ru" -> ["ivan@x.ru", "@supplier.ru"]."""
    items = re.split(r"[\s,;]+", raw) if isinstance(raw, str) else raw
    out: list[str] = []
    for p in items:
        p = (p or "").strip().lower().strip("<>")
        if not p:
            continue
        if "@" not in p:
            p = "@" + p
        if p not in out:
            out.append(p)
    return out


def matches(address: str, patterns: list[str]) -> bool:
    address = address.strip().lower()
    if "@" not in address:
        return False
    domain = address.rsplit("@", 1)[1]
    for p in patterns:
        if p.startswith("@"):
            d = p[1:]
            if domain == d or domain.endswith("." + d):
                return True
        elif address == p:
            return True
    return False


def find_rule(addresses: list[str], rules: list[MailRule]) -> MailRule | None:
    for rule in rules:
        if rule.enabled and any(matches(a, rule.patterns or []) for a in addresses):
            return rule
    return None


# ---------- parsing ----------

_QUOTE_MARKERS = re.compile(
    r"^\s*(-{2,}\s*(Original Message|Исходное сообщение|Пересылаемое сообщение|Forwarded message)\s*-{2,}"
    r"|On .{4,200} wrote:\s*$"
    r"|.{4,200}(пишет|написал\(а\)|написал):\s*$"
    r"|(От|From):\s.+$)",
    re.IGNORECASE,
)
_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/div|/tr|/li|/h\d)\b[^>]*>", re.IGNORECASE)
_DROP = re.compile(r"<\s*(script|style|head)\b.*?<\s*/\s*\1\s*>", re.IGNORECASE | re.DOTALL)


def html_to_text(raw: str) -> str:
    raw = _DROP.sub("", raw)
    raw = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    raw = _BLOCK_TAGS.sub("\n", raw)
    raw = re.sub(r"<[^>]+>", "", raw)
    return html.unescape(raw).replace("\xa0", " ")


def strip_quotes(text: str) -> str:
    """Drops the quoted thread: everything from the first "On … wrote:" / "От: …" line, and "> " lines."""
    lines: list[str] = []
    for line in text.splitlines():
        if _QUOTE_MARKERS.match(line) and any(s.strip() for s in lines):
            break
        if line.lstrip().startswith(">"):
            continue
        lines.append(line.rstrip())
    out = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", out).strip()


def _part_text(part: EmailMessage) -> str:
    try:
        return part.get_content()
    except Exception:  # unknown charset and the like
        payload = part.get_payload(decode=True) or b""
        return payload.decode("utf-8", errors="replace")


def parse(raw: bytes) -> dict:
    msg: EmailMessage = email.message_from_bytes(raw, policy=default_policy)  # type: ignore[assignment]
    plain = html_part = None
    attachments: list[str] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename()
        disposition = part.get_content_disposition()
        if filename or disposition == "attachment":
            attachments.append(filename or "без имени")
            continue
        ctype = part.get_content_type()
        if ctype == "text/plain" and plain is None:
            plain = _part_text(part)
        elif ctype == "text/html" and html_part is None:
            html_part = _part_text(part)
    body = plain if plain and plain.strip() else html_to_text(html_part or "")
    body = strip_quotes(body)
    if len(body) > TEXT_LIMIT:
        body = body[:TEXT_LIMIT].rstrip() + "…"
    senders = [a for _, a in getaddresses([str(msg.get("From", "")), str(msg.get("Sender", ""))]) if a]
    return {
        "message_id": str(msg.get("Message-ID", "")).strip(),
        "from": str(msg.get("From", "")).strip(),
        "addresses": senders,
        "subject": " ".join(str(msg.get("Subject", "")).split()),
        "text": body,
        "attachments": attachments,
    }


def bot_text(supplier: str, letter: dict) -> str:
    parts = [f"📧 В «{supplier}» пришло письмо"]
    if letter["from"]:
        parts.append(f"От: {letter['from']}")
    parts.append(f"Тема: {letter['subject'] or '(без темы)'}")
    if letter["text"]:
        parts.append("")
        parts.append(letter["text"])
    if letter["attachments"]:
        parts.append("")
        parts.append("📎 " + ", ".join(letter["attachments"]))
    return "\n".join(parts)


# ---------- IMAP (blocking, run in a thread) ----------


def _connect(host: str, port: int, login: str, password: str) -> imaplib.IMAP4_SSL:
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=TIMEOUT)
    except OSError as exc:
        raise MailError(f"Не удалось подключиться к {host}:{port}: {exc}") from exc
    try:
        conn.login(login, password)
    except imaplib.IMAP4.error as exc:
        try:
            conn.logout()
        except Exception:
            pass
        raise MailError(
            "Почта не приняла логин или пароль. Для Mail.ru нужен пароль для внешнего приложения"
        ) from exc
    return conn


def _examine(conn: imaplib.IMAP4_SSL, folder: str) -> int:
    typ, _ = conn.select(_quote(folder), readonly=True)
    if typ != "OK":
        raise MailError(f"Нет папки {folder}")
    typ, data = conn.response("UIDVALIDITY")
    try:
        return int(data[0])
    except (TypeError, ValueError, IndexError):
        return 0


def _quote(folder: str) -> str:
    return folder if folder.isalnum() else '"' + folder.replace('"', '\\"') + '"'


def check_login(host: str, port: int, login: str, password: str, folder: str = "INBOX") -> None:
    conn = _connect(host, port, login, password)
    try:
        _examine(conn, folder)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _uids(conn: imaplib.IMAP4_SSL, query: str) -> list[int]:
    typ, data = conn.uid("SEARCH", None, query)
    if typ != "OK":
        raise MailError("Почта не ответила на поиск писем")
    return sorted(int(x) for x in (data[0] or b"").split())


def fetch_new(
    host: str,
    port: int,
    login: str,
    password: str,
    folder: str,
    uidvalidity: int | None,
    last_uid: int | None,
) -> tuple[int, int, list[tuple[int, bytes]]]:
    """-> (uidvalidity, new last_uid, [(uid, raw letter), ...]); the letters only when we had a position."""
    conn = _connect(host, port, login, password)
    try:
        validity = _examine(conn, folder)
        if last_uid is None or (uidvalidity and validity != uidvalidity):
            uids = _uids(conn, "ALL")
            return validity, (uids[-1] if uids else 0), []
        uids = [u for u in _uids(conn, f"UID {last_uid + 1}:*") if u > last_uid][:BATCH]
        letters: list[tuple[int, bytes]] = []
        for uid in uids:
            typ, data = conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            raw = next((d[1] for d in data or [] if isinstance(d, tuple) and len(d) > 1), None)
            if typ == "OK" and raw:
                letters.append((uid, raw))
        return validity, (uids[-1] if uids else last_uid), letters
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------- one pass ----------


async def collect_account(db: AsyncSession, account: MailAccount) -> int:
    """Checks one mailbox; returns how many letters were forwarded."""
    acc = bot_access.get(account.user_id)
    if acc is None or not acc.has_workspace:
        return 0
    rules = list((await db.scalars(select(MailRule).where(MailRule.user_id == account.user_id))).all())
    try:
        validity, last_uid, letters = await asyncio.to_thread(
            fetch_new, account.host, account.port, account.login, account.password, account.folder,
            account.uidvalidity, account.last_uid,
        )  # fmt: skip
    except Exception as exc:
        account.errors = (account.errors or 0) + 1
        account.last_error = str(exc) if isinstance(exc, MailError) else f"Ошибка почты: {exc}"
        account.last_check_at = datetime.now(timezone.utc)
        await db.commit()
        log.warning("mail collector user=%s: %s", account.user_id, account.last_error)
        if account.errors == ALERT_AFTER:
            await inbox.deliver(
                account.user_id, f"📧 Почта {account.login} не подключается: {account.last_error}"
            )
        return 0

    sent = 0
    for uid, raw in letters:
        try:
            letter = parse(raw)
        except Exception as exc:
            log.warning("mail collector user=%s uid=%s: unparsable letter: %s", account.user_id, uid, exc)
            continue
        rule = find_rule(letter["addresses"], rules)
        if rule is None:
            continue
        item = await inbox.store(
            db, account.user_id, "mail", letter["message_id"] or f"{validity}:{uid}", rule.name,
            letter["subject"], letter["text"],
        )  # fmt: skip
        if item is None:
            continue
        await inbox.deliver(account.user_id, bot_text(rule.name, letter), item.id)
        sent += 1

    account.uidvalidity = validity
    account.last_uid = last_uid
    account.errors = 0
    account.last_error = ""
    account.last_check_at = datetime.now(timezone.utc)
    await db.commit()
    return sent


async def collect_all() -> None:
    async with SessionLocal() as db:
        accounts = (await db.scalars(select(MailAccount).where(MailAccount.enabled.is_(True)))).all()
        for account in accounts:
            try:
                await collect_account(db, account)
            except Exception:
                log.exception("mail collector user=%s failed", account.user_id)
                await db.rollback()
