from __future__ import annotations

import asyncio
import inspect
import itertools
import os
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import GetBusinessConnection, SendMessage
from aiogram.types import BusinessConnection, Chat, Message, Update

from assistant.config import load_settings
from assistant.handlers import build_router
from assistant.service import build_assistant

POSIX = os.name == "posix"
OWNER_ID = 111
CONN_ID = "conn-1"
BOT_TOKEN = "123456:TEST-token"

FILLED_PROFILE = """<!-- secret comment -->
# Men haqimda
- Ism: Nasim
- Shahar: Toshkent
## Yozish uslubi
- Xabar uzunligi: qisqa
"""


class RecordingSession(BaseSession):
    """Fake Telegram API: records every request, never touches the network."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: List[Any] = []
        self.connections: Dict[str, BusinessConnection] = {}
        self._ids = itertools.count(5000)

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, GetBusinessConnection):
            connection = self.connections.get(method.business_connection_id)
            if connection is None:
                raise TelegramBadRequest(method=method, message="Bad Request: business connection not found")
            return connection
        if isinstance(method, SendMessage):
            return Message(
                message_id=next(self._ids),
                date=datetime.now(),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=method.text,
                business_connection_id=method.business_connection_id,
            )
        return True

    async def stream_content(self, *args, **kwargs):
        raise AssertionError("stream_content must not be called in tests")

    async def close(self) -> None:
        pass

    def of(self, kind) -> List[Any]:
        return [r for r in self.requests if isinstance(r, kind)]


class FakeGenerator:
    """Stands in for ClaudeCLI: returns queued replies or raises queued exceptions."""

    def __init__(self) -> None:
        self.responses: deque = deque()
        self.calls: List[tuple] = []
        self.default = "Salom! Keyinroq yozaman."
        self.hook: Optional[Callable[[], Any]] = None

    def queue(self, *items: Any) -> None:
        self.responses.extend(items)

    async def generate(self, system_prompt: str, user_prompt: str) -> str:
        self.calls.append((system_prompt, user_prompt))
        if self.hook is not None:
            result = self.hook()
            if inspect.isawaitable(result):
                await result
        item = self.responses.popleft() if self.responses else self.default
        if isinstance(item, BaseException):
            raise item
        return item


class Clock:
    def __init__(self, now: float = 1_800_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def connection_payload(
    connection_id: str = CONN_ID, user_id: int = OWNER_ID, enabled: bool = True, can_reply: bool = True
) -> dict:
    return {
        "id": connection_id,
        "user": {"id": user_id, "is_bot": False, "first_name": "Owner"},
        "user_chat_id": user_id,
        "date": int(time.time()),
        "is_enabled": enabled,
        "rights": {"can_reply": can_reply},
        "can_reply": can_reply,
    }


class Harness:
    def __init__(self, tmp_path: Path, **overrides: str) -> None:
        self.tmp_path = tmp_path
        env_file = tmp_path / ".env"
        if not env_file.exists():
            env_file.write_text("", encoding="utf-8")
        profile = tmp_path / "profile.md"
        if not profile.exists():
            profile.write_text(FILLED_PROFILE, encoding="utf-8")
        values = {
            "TELEGRAM_BOT_TOKEN": BOT_TOKEN,
            "OWNER_ID": str(OWNER_ID),
            "DATA_DIR": str(tmp_path / "data"),
            "PROFILE_PATH": str(profile),
        }
        values.update(overrides)
        self.settings = load_settings(env_file, overrides=values)
        self.session = RecordingSession()
        self.bot = Bot(BOT_TOKEN, session=self.session)
        self.generator = FakeGenerator()
        self.clock = Clock()
        self.previews: List[Any] = []
        self._update_ids = itertools.count(1)
        self._message_ids = itertools.count(1)
        self._build()

    def _build(self) -> None:
        self.assistant = build_assistant(
            self.settings, generator=self.generator, clock=self.clock, preview_sink=self.previews.append
        )
        self.dp = Dispatcher()
        self.dp.include_router(build_router(self.assistant))

    def restart(self) -> None:
        """A fresh Assistant on the same data directory (simulates a process restart)."""
        self._build()

    @property
    def outcome(self):
        return self.assistant.outcomes[-1] if self.assistant.outcomes else None

    async def feed(self, payload: dict) -> None:
        data = {"update_id": next(self._update_ids), **payload}
        update = Update.model_validate(data, context={"bot": self.bot})
        await self.dp.feed_update(self.bot, update)

    def set_connection(self, **kwargs: Any) -> BusinessConnection:
        """Change what Telegram answers to getBusinessConnection, without sending an update."""
        connection = BusinessConnection.model_validate(connection_payload(**kwargs))
        self.session.connections[connection.id] = connection
        return connection

    async def connect(self, **kwargs: Any) -> None:
        payload = connection_payload(**kwargs)
        self.set_connection(**kwargs)
        await self.feed({"business_connection": payload})

    async def business_message(
        self,
        text: Optional[str] = "Salom, qalesan?",
        *,
        from_id: int = 222,
        chat_id: Optional[int] = None,
        message_id: Optional[int] = None,
        connection_id: str = CONN_ID,
        is_bot: bool = False,
        via_business_bot: bool = False,
        name: str = "Ali",
        extra: Optional[dict] = None,
    ):
        message: Dict[str, Any] = {
            "message_id": message_id if message_id is not None else next(self._message_ids),
            "date": int(time.time()),
            "chat": {"id": chat_id if chat_id is not None else from_id, "type": "private", "first_name": name},
            "from": {"id": from_id, "is_bot": is_bot, "first_name": name},
            "business_connection_id": connection_id,
        }
        if text is not None:
            message["text"] = text
        if via_business_bot:
            message["sender_business_bot"] = {"id": 999, "is_bot": True, "first_name": "Bot", "username": "my_bot"}
        if extra:
            message.update(extra)
        await self.feed({"business_message": message})
        return self.outcome

    async def owner_message(self, text: str = "Ha, keyin gaplashamiz", *, chat_id: int = 222, **kwargs: Any):
        return await self.business_message(text, from_id=OWNER_ID, chat_id=chat_id, name="Owner", **kwargs)

    async def direct_message(self, text: str, *, from_id: int = OWNER_ID) -> None:
        message: Dict[str, Any] = {
            "message_id": next(self._message_ids),
            "date": int(time.time()),
            "chat": {"id": from_id, "type": "private", "first_name": "User"},
            "from": {"id": from_id, "is_bot": False, "first_name": "User"},
            "text": text,
        }
        if text.startswith("/"):
            message["entities"] = [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
        await self.feed({"message": message})

    def sent(self) -> List[SendMessage]:
        return self.session.of(SendMessage)


@pytest.fixture
def make_harness(tmp_path):
    def factory(**overrides: str) -> Harness:
        return Harness(tmp_path, **overrides)

    return factory
