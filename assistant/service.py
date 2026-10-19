"""Core logic: decides whether and how to answer a business message."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Callable, Deque, Dict, Optional, Union

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import BusinessConnection, Message

from .claude_cli import ClaudeCLI, ClaudeError, ClaudeLimitError
from .config import Settings
from .language import detect_language
from .prompts import SKIP_TOKEN, build_system_prompt, build_user_prompt
from .store import HistoryStore, RuntimeState, SeenMessages, ensure_private_dir

log = logging.getLogger("assistant")

TELEGRAM_CHUNK = 4000
OWNER_ALERT_INTERVAL = 600.0


class Outcome(str, Enum):
    PREVIEWED = "previewed"
    SENT = "sent"
    DUPLICATE = "duplicate"
    IGNORED_BOT = "ignored_bot"
    NO_CONNECTION = "no_connection"
    FOREIGN_CONNECTION = "foreign_connection"
    CONNECTION_DISABLED = "connection_disabled"
    UNSUPPORTED = "unsupported"
    OWNER_MESSAGE = "owner_message"
    OWNER_ACTIVE = "owner_active"
    PAUSED = "paused"
    LIMITED = "limited"
    SKIPPED = "skipped"
    PERMISSION_DENIED = "permission_denied"
    ERROR = "error"


@dataclass
class Preview:
    connection_id: str
    chat_id: int
    chat_name: str
    language: Optional[str]
    incoming: str
    reply: str
    note: Optional[str] = None


def print_preview(preview: Preview) -> None:
    lines = [
        "=" * 60,
        f"PREVIEW, not sent | chat {preview.chat_id} ({preview.chat_name}) | lang={preview.language or '?'}",
        f"  them: {preview.incoming}",
        f"  reply: {preview.reply}",
    ]
    if preview.note:
        lines.append(f"  note: {preview.note}")
    lines.append("=" * 60)
    log.info("\n".join(lines))


_MEDIA_LABELS = (
    ("photo", "[photo]"),
    ("video", "[video]"),
    ("video_note", "[video message]"),
    ("voice", "[voice message]"),
    ("audio", "[audio]"),
    ("animation", "[GIF]"),
    ("document", "[file]"),
    ("location", "[location]"),
    ("venue", "[location]"),
    ("contact", "[contact]"),
    ("poll", "[poll]"),
)


def describe(message: Message) -> Optional[str]:
    """Readable text for a message, or None for service messages."""
    if message.text:
        return message.text
    caption = (message.caption or "").strip()
    if message.sticker is not None:
        label = f"[sticker] {message.sticker.emoji or ''}".strip()
    elif message.dice is not None:
        label = f"[dice] {message.dice.emoji}"
    else:
        label = next((text for attr, text in _MEDIA_LABELS if getattr(message, attr, None) is not None), None)
    if label is None:
        return None
    return f"{label} {caption}".strip()


def can_reply(connection: BusinessConnection) -> bool:
    rights = getattr(connection, "rights", None)
    if rights is not None:
        return bool(getattr(rights, "can_reply", False))
    return bool(getattr(connection, "can_reply", False))


_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def read_profile(path: Union[str, Path]) -> str:
    try:
        text = Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""
    return _COMMENT_RE.sub("", text).strip()


def profile_is_filled(profile: str) -> bool:
    for line in profile.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if re.fullmatch(r"-\s*[^:]+:\s*", line):
            continue
        return True
    return False


def _sender_name(message: Message) -> str:
    if message.from_user is not None:
        return message.from_user.full_name
    return message.chat.full_name or "Them"


def _chunks(text: str, size: int = TELEGRAM_CHUNK):
    return [text[i : i + size] for i in range(0, len(text), size)] or [text]


class Assistant:
    def __init__(
        self,
        settings: Settings,
        generator,
        *,
        clock: Callable[[], float] = time.time,
        preview_sink: Callable[[Preview], None] = print_preview,
    ):
        self.settings = settings
        self.generator = generator
        self.clock = clock
        self.preview_sink = preview_sink
        data_dir = ensure_private_dir(settings.data_dir)
        self.history = HistoryStore(data_dir / "history.json", settings.history_limit)
        self.seen = SeenMessages(data_dir / "seen.json")
        self.state = RuntimeState(data_dir / "state.json")
        self.previews_path = data_dir / "previews.jsonl"
        self.connections: Dict[str, BusinessConnection] = {}
        self.outcomes: Deque[Outcome] = deque(maxlen=100)
        self._takeover_until: Dict[str, float] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._last_owner_alert = 0.0

    # ----------------------------------------------------------------- updates

    async def on_business_connection(self, connection: BusinessConnection) -> None:
        self.connections[connection.id] = connection
        if connection.user.id != self.settings.owner_id:
            log.warning(
                "Business connection %s belongs to user %s, not OWNER_ID; it will be ignored",
                connection.id,
                connection.user.id,
            )
            return
        log.info(
            "Business connection %s: user=%s enabled=%s can_reply=%s",
            connection.id,
            connection.user.id,
            connection.is_enabled,
            can_reply(connection),
        )

    async def on_business_message(self, bot: Bot, message: Message) -> Outcome:
        outcome = await self._handle(bot, message)
        self.outcomes.append(outcome)
        return outcome

    async def _handle(self, bot: Bot, message: Message) -> Outcome:
        conn_id = message.business_connection_id
        if not conn_id:
            return Outcome.NO_CONNECTION
        chat_id = message.chat.id
        # Marked before any await so concurrent duplicates are caught too.
        if not self.seen.first_time(f"{conn_id}:{chat_id}:{message.message_id}"):
            return Outcome.DUPLICATE
        if message.sender_business_bot is not None or (message.from_user is not None and message.from_user.is_bot):
            return Outcome.IGNORED_BOT

        connection = self.connections.get(conn_id)
        if connection is None:
            try:
                connection = await bot.get_business_connection(business_connection_id=conn_id)
            except TelegramAPIError as exc:
                log.warning("Could not look up business connection %s: %s", conn_id, exc)
                return Outcome.NO_CONNECTION
            self.connections[conn_id] = connection
        if connection.user.id != self.settings.owner_id:
            return Outcome.FOREIGN_CONNECTION
        if not connection.is_enabled:
            return Outcome.CONNECTION_DISABLED

        text = describe(message)
        if text is None:
            return Outcome.UNSUPPORTED

        key = HistoryStore.key(conn_id, chat_id)
        now = self.clock()
        if message.from_user is not None and message.from_user.id == self.settings.owner_id:
            self.history.add(conn_id, chat_id, "owner", "You", text, None, now)
            if self.settings.owner_takeover_minutes > 0:
                self._takeover_until[key] = now + self.settings.owner_takeover_minutes * 60
            return Outcome.OWNER_MESSAGE

        language = detect_language(text) or self.history.last_language(conn_id, chat_id)
        chat_name = _sender_name(message)
        self.history.add(conn_id, chat_id, "other", chat_name, text, language, now)

        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            blocked = self._blocked(key)
            if blocked is not None:
                return blocked

            reply_or_outcome = await self._generate(bot, conn_id, chat_id, language)
            if isinstance(reply_or_outcome, Outcome):
                return reply_or_outcome
            reply = reply_or_outcome.strip()
            if not reply or reply == SKIP_TOKEN or reply.startswith(SKIP_TOKEN):
                return Outcome.SKIPPED

            preview = Preview(conn_id, chat_id, chat_name, language, text, reply)
            if not self.settings.auto_send:
                self._preview(preview)
                return Outcome.PREVIEWED
            return await self._send(bot, preview, key)

    # ---------------------------------------------------------------- helpers

    def _blocked(self, key: str) -> Optional[Outcome]:
        now = self.clock()
        if self.state.paused:
            return Outcome.PAUSED
        limit_until = self.state.limit_until
        if limit_until and limit_until > now:
            return Outcome.LIMITED
        if self._takeover_until.get(key, 0) > now:
            return Outcome.OWNER_ACTIVE
        return None

    async def _generate(self, bot: Bot, conn_id: str, chat_id: int, language: Optional[str]):
        system_prompt = build_system_prompt(
            read_profile(self.settings.profile_path), datetime.fromtimestamp(self.clock()).astimezone()
        )
        user_prompt = build_user_prompt(self.history.get(conn_id, chat_id), language)
        try:
            return await self.generator.generate(system_prompt, user_prompt)
        except ClaudeLimitError as exc:
            now = self.clock()
            if exc.resets_at and exc.resets_at > now:
                until = exc.resets_at
            else:
                until = now + self.settings.limit_cooldown_minutes * 60
            self.state.limit_until = until
            log.warning(
                "%s. Pausing replies until %s.",
                exc,
                datetime.fromtimestamp(until).astimezone().strftime("%Y-%m-%d %H:%M %Z"),
            )
            if self.settings.auto_send:
                await self._notify_owner(
                    bot, f"⚠️ {exc}. Replies are paused until the limit resets (or /resume).", force=True
                )
            return Outcome.LIMITED
        except ClaudeError as exc:
            log.error("Claude failed for chat %s: %s", chat_id, exc)
            if self.settings.auto_send:
                await self._notify_owner(bot, f"⚠️ Assistant could not reply: {exc}")
            return Outcome.ERROR

    def _preview(self, preview: Preview) -> None:
        self.preview_sink(preview)
        record = dict(asdict(preview), ts=self.clock())
        fd = os.open(self.previews_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.chmod(self.previews_path, 0o600)

    async def _send(self, bot: Bot, preview: Preview, key: str) -> Outcome:
        blocked = self._blocked(key)
        if blocked is not None:
            preview.note = f"not sent: {blocked.value} (state changed while the reply was being written)"
            self._preview(preview)
            return blocked

        # Fresh lookup (never the cache): the owner may have revoked access meanwhile.
        try:
            connection = await bot.get_business_connection(business_connection_id=preview.connection_id)
        except TelegramAPIError as exc:
            preview.note = f"not sent: could not verify the business connection ({exc})"
            self._preview(preview)
            return Outcome.PERMISSION_DENIED
        self.connections[preview.connection_id] = connection
        reason = None
        if connection.user.id != self.settings.owner_id:
            reason = "connection belongs to another account"
        elif not connection.is_enabled:
            reason = "connection is disabled"
        elif not can_reply(connection):
            reason = "the bot has no permission to reply in Telegram Business settings"
        if reason:
            preview.note = f"not sent: {reason}"
            self._preview(preview)
            return Outcome.PERMISSION_DENIED

        try:
            for chunk in _chunks(preview.reply):
                await bot.send_message(
                    chat_id=preview.chat_id, text=chunk, business_connection_id=preview.connection_id
                )
        except TelegramAPIError as exc:
            log.error("Sending reply to chat %s failed: %s", preview.chat_id, exc)
            return Outcome.ERROR
        self.history.add(
            preview.connection_id, preview.chat_id, "owner", "You", preview.reply, None, self.clock()
        )
        log.info("Reply sent to chat %s (%s)", preview.chat_id, preview.chat_name)
        return Outcome.SENT

    async def _notify_owner(self, bot: Bot, text: str, *, force: bool = False) -> None:
        now = self.clock()
        if not force and now - self._last_owner_alert < OWNER_ALERT_INTERVAL:
            return
        self._last_owner_alert = now
        try:
            await bot.send_message(chat_id=self.settings.owner_id, text=text)
        except TelegramAPIError as exc:
            log.warning("Could not notify the owner: %s", exc)

    # ------------------------------------------------------------ owner tools

    def pause(self) -> None:
        self.state.paused = True

    def resume(self) -> None:
        self.state.paused = False
        self.state.limit_until = None
        self._takeover_until.clear()

    def status_text(self) -> str:
        mode = "AUTO-SEND" if self.settings.auto_send else "PREVIEW: replies are only shown in the logs"
        lines = [f"Mode: {mode}", f"State: {'paused' if self.state.paused else 'running'}"]
        limit_until = self.state.limit_until
        if limit_until and limit_until > self.clock():
            lines.append(
                "Usage limit: until " + datetime.fromtimestamp(limit_until).astimezone().strftime("%Y-%m-%d %H:%M %Z")
            )
        else:
            lines.append("Usage limit: none")
        lines.append(f"Chats in history: {self.history.chat_count()}")
        return "\n".join(lines)

    def start_text(self) -> str:
        mode = (
            "AUTO-SEND — javoblar sizning nomingizdan avtomatik yuboriladi"
            if self.settings.auto_send
            else "PREVIEW — javoblar yuborilmaydi, faqat logda ko'rsatiladi"
        )
        state = "pauzada ⏸" if self.state.paused else "ishlayapti ✅"
        minutes = self.settings.owner_takeover_minutes
        lines = [
            "Salom! Men sizning shaxsiy yordamchingizman 🤖",
            "Sizga kimdir Telegramda yozsa, profile.md dagi ma'lumotlar asosida sizning uslubingizda javob tayyorlayman.",
            "",
            f"Rejim: {mode}",
            f"Holat: {state}",
            "",
            "Qanday ulanadi:",
            "1. Telegram → Settings → Telegram Business → Chatbots",
            "2. Shu botning username'ini kiriting",
            "3. Bot qaysi chatlarni ko'rishini tanlang",
            "4. Javob berish ruxsatini yoqing",
            "(Telegram Premium kerak)",
            "",
            "Buyruqlar:",
            "/pause — javob berishni to'xtatish",
            "/resume — davom ettirish",
            "/status — holat",
            "",
            "Bilib qo'ying:",
        ]
        if minutes > 0:
            lines.append(f"• Chatda o'zingiz yozsangiz, o'sha chatda {minutes} daqiqa jim turaman.")
        lines += [
            "• Pul, qarz, uchrashuv va rejalarga rozilik bermayman, va'da qilmayman.",
            "• profile.md ni to'ldirish kerak — javoblar shunga qarab yoziladi.",
        ]
        if not self.settings.auto_send:
            lines.append("• Javoblar haqiqatan yuborilishi uchun AUTO_SEND=true qiling.")
        return "\n".join(lines)


def make_claude_cli(settings: Settings) -> ClaudeCLI:
    return ClaudeCLI(
        settings.claude_bin,
        workdir=settings.claude_workdir,
        model=settings.claude_model,
        effort=settings.claude_effort,
        timeout=settings.claude_timeout,
        oauth_token=settings.claude_oauth_token,
        auth_label=settings.claude_auth_mode,
    )


def build_assistant(settings: Settings, **kwargs) -> Assistant:
    generator = kwargs.pop("generator", None) or make_claude_cli(settings)
    return Assistant(settings, generator, **kwargs)
