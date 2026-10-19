from __future__ import annotations

import asyncio
import json
import os
import stat

from aiogram.methods import GetBusinessConnection, SendMessage

from assistant.claude_cli import ClaudeAuthError, ClaudeLimitError, ClaudeTimeout
from assistant.service import Outcome
from tests.conftest import CONN_ID, OWNER_ID, POSIX


# ------------------------------------------------------------------ preview mode


async def test_preview_mode_shows_reply_and_sends_nothing(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue("Yaxshi, o'zingchi?")
    outcome = await h.business_message("Salom, qalesan?")

    assert outcome == Outcome.PREVIEWED
    assert h.sent() == []
    assert len(h.previews) == 1
    assert h.previews[0].reply == "Yaxshi, o'zingchi?"
    assert h.previews[0].language == "uz"

    path = h.settings.data_dir / "previews.jsonl"
    record = json.loads(path.read_text(encoding="utf-8").strip())
    assert record["reply"] == "Yaxshi, o'zingchi?"
    if POSIX:
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    # The unsent reply is not part of the history.
    history = h.assistant.history.get(CONN_ID, 222)
    assert [item["role"] for item in history] == ["other"]


async def test_known_connection_makes_no_telegram_requests_in_preview(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Hi")
    await h.business_message("How are you?")
    assert h.session.requests == []


async def test_unknown_connection_is_looked_up_once_then_cached(make_harness):
    h = make_harness()
    h.set_connection()
    assert await h.business_message("Hi") == Outcome.PREVIEWED
    assert await h.business_message("Hello again") == Outcome.PREVIEWED
    assert len(h.session.of(GetBusinessConnection)) == 1
    assert h.sent() == []


async def test_connection_lookup_failure(make_harness):
    h = make_harness()
    assert await h.business_message("Hi") == Outcome.NO_CONNECTION
    assert h.generator.calls == []


async def test_foreign_and_disabled_connections_do_not_call_claude(make_harness):
    h = make_harness()
    await h.connect(connection_id="foreign", user_id=999)
    assert await h.business_message("Hi", connection_id="foreign") == Outcome.FOREIGN_CONNECTION
    await h.connect(connection_id="off", enabled=False)
    assert await h.business_message("Hi", connection_id="off") == Outcome.CONNECTION_DISABLED
    assert h.generator.calls == []
    assert h.sent() == []


async def test_any_chat_gets_a_reply_without_allowlist(make_harness):
    h = make_harness()
    await h.connect()
    for chat in (1, 22, 333, 4444):
        assert await h.business_message("Hi there", from_id=chat) == Outcome.PREVIEWED


# --------------------------------------------------------------- owner & bots


async def test_owner_message_is_recorded_and_triggers_takeover(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.owner_message("Hozir bandman") == Outcome.OWNER_MESSAGE
    assert h.assistant.history.get(CONN_ID, 222)[-1]["role"] == "owner"
    assert h.generator.calls == []

    assert await h.business_message("Salom") == Outcome.OWNER_ACTIVE
    assert h.generator.calls == []

    h.clock.advance(15 * 60 + 1)
    assert await h.business_message("Salom?") == Outcome.PREVIEWED


async def test_bot_messages_are_ignored(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message("auto reply", via_business_bot=True, from_id=OWNER_ID) == Outcome.IGNORED_BOT
    assert await h.business_message("I am a bot", is_bot=True) == Outcome.IGNORED_BOT
    assert h.generator.calls == []


# ----------------------------------------------------------------- duplicates


async def test_duplicates_are_processed_once(make_harness):
    h = make_harness()
    await h.connect()
    await asyncio.gather(
        h.business_message("Hi", message_id=7),
        h.business_message("Hi", message_id=7),
    )
    assert list(h.assistant.outcomes).count(Outcome.PREVIEWED) == 1
    assert list(h.assistant.outcomes).count(Outcome.DUPLICATE) == 1

    assert await h.business_message("Hi", message_id=7) == Outcome.DUPLICATE
    h.restart()
    assert await h.business_message("Hi", message_id=7) == Outcome.DUPLICATE
    assert len(h.generator.calls) == 1


# ---------------------------------------------------------------- content


async def test_service_messages_and_media(make_harness):
    h = make_harness()
    await h.connect()
    assert await h.business_message(None, extra={"delete_chat_photo": True}) == Outcome.UNSUPPORTED

    sticker = {
        "file_id": "s", "file_unique_id": "su", "type": "regular", "width": 512, "height": 512,
        "is_animated": False, "is_video": False, "emoji": "😀",
    }
    await h.business_message(None, extra={"sticker": sticker})
    voice = {"file_id": "v", "file_unique_id": "vu", "duration": 3}
    await h.business_message(None, extra={"voice": voice})
    texts = [item["text"] for item in h.assistant.history.get(CONN_ID, 222)]
    assert texts == ["[sticker] 😀", "[voice message]"]


async def test_history_is_separate_per_connection_and_chat(make_harness):
    h = make_harness()
    await h.connect()
    await h.connect(connection_id="conn-2")
    await h.business_message("secret for chat A", from_id=1, name="Alice")
    await h.business_message("message for chat B", from_id=2, name="Bob")
    await h.business_message("same chat, other connection", from_id=1, connection_id="conn-2")

    prompt_b = h.generator.calls[1][1]
    assert "message for chat B" in prompt_b and "secret for chat A" not in prompt_b
    prompt_c = h.generator.calls[2][1]
    assert "secret for chat A" not in prompt_c
    assert h.assistant.history.chat_count() == 3


async def test_language_hint(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Привет, как дела?", from_id=1)
    await h.business_message("Hi, are you free tonight?", from_id=2)
    await h.business_message("Bugun bo'shmisan?", from_id=3)
    await h.business_message("👍", from_id=1)
    prompts = [call[1] for call in h.generator.calls]
    assert prompts[0].endswith("Their latest message is in Russian.")
    assert prompts[1].endswith("Their latest message is in English.")
    assert prompts[2].endswith("Their latest message is in Uzbek.")
    assert prompts[3].endswith("Their latest message is in Russian.")


async def test_skip_is_not_shown(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue("[SKIP]")
    assert await h.business_message("ok") == Outcome.SKIPPED
    assert h.previews == []


async def test_profile_comments_are_not_sent_to_claude(make_harness):
    h = make_harness()
    await h.connect()
    await h.business_message("Hi")
    system_prompt = h.generator.calls[0][0]
    assert "Nasim" in system_prompt
    assert "secret comment" not in system_prompt


# ------------------------------------------------------------------ auto-send


async def test_auto_send_rechecks_connection_and_sends(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue("Yaxshi rahmat")
    assert await h.business_message("Salom") == Outcome.SENT

    kinds = [type(r) for r in h.session.requests]
    assert kinds == [GetBusinessConnection, SendMessage]
    sent = h.sent()[0]
    assert sent.business_connection_id == CONN_ID
    assert sent.chat_id == 222 and sent.text == "Yaxshi rahmat"
    last = h.assistant.history.get(CONN_ID, 222)[-1]
    assert (last["role"], last["text"]) == ("owner", "Yaxshi rahmat")


async def test_auto_send_splits_long_replies(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue("x" * 9000)
    assert await h.business_message("Tell me everything") == Outcome.SENT
    assert [len(m.text) for m in h.sent()] == [4000, 4000, 1000]


async def test_auto_send_refuses_without_permission(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.set_connection(can_reply=False)
    assert await h.business_message("Hi") == Outcome.PERMISSION_DENIED
    assert h.sent() == []
    assert "permission" in h.previews[-1].note

    h.set_connection(enabled=False)
    assert await h.business_message("Hi again") == Outcome.PERMISSION_DENIED
    assert h.sent() == []
    assert "disabled" in h.previews[-1].note


async def test_auto_send_stops_when_owner_writes_during_generation(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.hook = lambda: h.owner_message("I'll answer myself")
    await h.business_message("Salom")
    assert h.assistant.outcomes[-1] == Outcome.OWNER_ACTIVE
    assert h.sent() == []
    assert "owner_active" in h.previews[-1].note


# --------------------------------------------------------------- owner commands


async def test_pause_and_resume_owner_only_and_persistent(make_harness):
    h = make_harness()
    await h.connect()
    await h.direct_message("/pause", from_id=777)
    assert h.assistant.state.paused is False
    assert h.sent() == []

    await h.direct_message("/pause")
    assert h.assistant.state.paused is True
    assert await h.business_message("Hi") == Outcome.PAUSED

    h.restart()
    assert h.assistant.state.paused is True
    assert await h.business_message("Hi?") == Outcome.PAUSED

    await h.direct_message("/resume", from_id=777)
    assert h.assistant.state.paused is True
    await h.direct_message("/resume")
    assert h.assistant.state.paused is False
    assert await h.business_message("Hi!!") == Outcome.PREVIEWED


async def test_strangers_get_nothing_for_other_messages(make_harness):
    h = make_harness()
    await h.direct_message("hello bot", from_id=777)
    await h.direct_message("/status", from_id=777)
    assert h.sent() == []
    assert h.generator.calls == []


async def test_status_for_owner(make_harness):
    h = make_harness()
    await h.direct_message("/status")
    assert "PREVIEW" in h.sent()[0].text


async def test_start_for_owner(make_harness):
    h = make_harness()
    await h.direct_message("/start")
    text = h.sent()[-1].text
    assert "PREVIEW" in text and "ishlayapti" in text
    assert "Chatbots" in text and "/pause" in text and "/resume" in text and "/status" in text
    assert "AUTO_SEND=true" in text

    h2 = make_harness(AUTO_SEND="true")
    h2.assistant.pause()
    await h2.direct_message("/start")
    text = h2.sent()[-1].text
    assert "AUTO-SEND" in text and "pauzada" in text
    assert "AUTO_SEND=true" not in text


async def test_start_for_stranger(make_harness):
    h = make_harness()
    await h.direct_message("/start", from_id=777)
    text = h.sent()[-1].text
    assert h.sent()[-1].chat_id == 777
    assert "egasi" in text
    assert "Nasim" not in text and "Toshkent" not in text
    assert h.generator.calls == []


# ------------------------------------------------------------------ failures


async def test_usage_limit_waits_until_reset(make_harness):
    h = make_harness()
    await h.connect()
    reset = h.clock.now + 3600
    h.generator.queue(ClaudeLimitError("limit", resets_at=reset))
    assert await h.business_message("Hi", from_id=1) == Outcome.LIMITED
    assert await h.business_message("Hi?", from_id=2) == Outcome.LIMITED
    assert len(h.generator.calls) == 1

    h.restart()
    assert await h.business_message("Hi again", from_id=3) == Outcome.LIMITED

    h.clock.advance(3601)
    assert await h.business_message("Now?", from_id=4) == Outcome.PREVIEWED


async def test_usage_limit_without_reset_uses_cooldown(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue(ClaudeLimitError("limit"))
    assert await h.business_message("Hi", from_id=1) == Outcome.LIMITED
    h.clock.advance(29 * 60)
    assert await h.business_message("Hi?", from_id=1) == Outcome.LIMITED
    h.clock.advance(2 * 60)
    assert await h.business_message("Hi!", from_id=1) == Outcome.PREVIEWED


async def test_usage_limit_notifies_owner_in_auto_send_and_resume_clears(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue(ClaudeLimitError("limit"))
    assert await h.business_message("Hi") == Outcome.LIMITED
    alerts = [m for m in h.sent() if m.chat_id == OWNER_ID]
    assert len(alerts) == 1 and "limit" in alerts[0].text
    assert all(m.business_connection_id is None for m in h.sent())

    await h.direct_message("/resume")
    assert h.assistant.state.limit_until is None
    assert await h.business_message("Hi again") == Outcome.SENT


async def test_timeout_and_auth_errors_send_nothing_to_chat(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.generator.queue(ClaudeTimeout("slow"), ClaudeAuthError("login", "x"), ClaudeTimeout("slow"))
    assert await h.business_message("one", from_id=1) == Outcome.ERROR
    assert await h.business_message("two", from_id=2) == Outcome.ERROR
    assert await h.business_message("three", from_id=3) == Outcome.ERROR
    assert all(m.chat_id == OWNER_ID for m in h.sent())
    assert len(h.sent()) == 1  # the owner alert is not repeated


async def test_errors_in_preview_mode_send_nothing(make_harness):
    h = make_harness()
    await h.connect()
    h.generator.queue(ClaudeTimeout("slow"))
    assert await h.business_message("one") == Outcome.ERROR
    assert h.session.requests == []
