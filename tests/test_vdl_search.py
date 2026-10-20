from __future__ import annotations

import json

import httpx
import pytest

from assistant.claude_cli import build_env
from assistant.config import ConfigError, load_settings
from assistant.prompts import build_system_prompt, parse_agent_reply
from assistant.service import Outcome, card_caption
from assistant.vdl_search import VdlSearch, make_vdl_search
from tests.conftest import CONN_ID

from datetime import datetime


def make_item(n: int, **extra) -> dict:
    item = {
        "id": f"p{n}",
        "type": "market",
        "name": f"Muzlatgich {n}",
        "price": 3500000 + n,
        "old_price": None,
        "discount_percent": None,
        "image": f"https://img.test/{n}.jpg",
        "link": f"https://t.me/vdl_market_bot/app?startapp=p{n}",
        "seller": {"name": "Kukmara Vodil"},
    }
    item.update(extra)
    return item


def items_response(items):
    return lambda request: httpx.Response(200, json={"items": items})


def search_json(q="muzlatgich", type_="market", reply="Mana, bor variantlar:", max_price=None):
    return json.dumps({"reply": reply, "search": {"q": q, "type": type_, "max_price": max_price}})


# ---------------------------------------------------------------- VdlSearch


async def test_search_sends_params_and_auth():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"items": [make_item(1)]})

    search = VdlSearch("https://vdl.test/search", "secret-key", transport=httpx.MockTransport(handler))
    items = await search.search("lavash", "food", 3, 25000)
    assert [i["name"] for i in items] == ["Muzlatgich 1"]
    request = seen[0]
    assert request.headers["Authorization"] == "Bearer secret-key"
    assert dict(request.url.params) == {"q": "lavash", "type": "food", "limit": "3", "max_price": "25000"}
    assert "secret-key" not in repr(search)


async def test_search_clamps_limit_and_type():
    seen = []
    search = VdlSearch(
        "https://vdl.test/search", "k",
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={"items": []})),
    )
    await search.search("x", "weapons", 50)
    assert seen[0].url.params["limit"] == "10"
    assert seen[0].url.params["type"] == "market"
    assert "max_price" not in seen[0].url.params


@pytest.mark.parametrize(
    "handler",
    [
        lambda r: httpx.Response(500, text="boom"),
        lambda r: httpx.Response(401, json={"error": "bad key"}),
        lambda r: httpx.Response(200, text="<html>not json</html>"),
        lambda r: httpx.Response(200, json={"no_items": True}),
        lambda r: httpx.Response(200, json={"items": "oops"}),
    ],
)
async def test_search_errors_return_empty_list(handler):
    search = VdlSearch("https://vdl.test/search", "k", transport=httpx.MockTransport(handler))
    assert await search.search("x") == []


async def test_search_network_error_returns_empty_list():
    def handler(request):
        raise httpx.ConnectError("no network", request=request)

    search = VdlSearch("https://vdl.test/search", "k", transport=httpx.MockTransport(handler))
    assert await search.search("x") == []


def test_settings_and_factory(tmp_path):
    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=1:a\nOWNER_ID=1\n", encoding="utf-8")
    s = load_settings(env)
    assert s.vdl_search_enabled is False and make_vdl_search(s) is None

    env.write_text("TELEGRAM_BOT_TOKEN=1:a\nOWNER_ID=1\nVDL_API_KEY=vdl_secret_123\n", encoding="utf-8")
    s = load_settings(env)
    assert s.vdl_search_enabled and "vdl_secret_123" not in repr(s)
    assert s.vdl_api_url.endswith("/api/public/agent/search")
    assert isinstance(make_vdl_search(s), VdlSearch)

    env.write_text("TELEGRAM_BOT_TOKEN=1:a\nOWNER_ID=1\nVDL_API_URL=http://insecure\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="https"):
        load_settings(env)


def test_vdl_key_not_passed_to_claude():
    assert "VDL_API_KEY" not in build_env(None, base={"VDL_API_KEY": "x", "PATH": "/bin"})


# ------------------------------------------------------------ prompt & parse


def test_prompt_mentions_search_only_when_enabled():
    now = datetime(2026, 10, 20, 12, 0)
    assert '"type": "food"' in build_system_prompt("p", now, search_enabled=True)
    assert '"search" must always be null' in build_system_prompt("p", now, search_enabled=False)


def test_parse_agent_reply():
    assert parse_agent_reply("plain text") == ("plain text", None)
    reply, search = parse_agent_reply(search_json("lavash", "food", max_price=25000))
    assert reply == "Mana, bor variantlar:"
    assert search == {"q": "lavash", "type": "food", "max_price": 25000}
    assert parse_agent_reply('```json\n{"reply": "Hi", "search": null}\n```') == ("Hi", None)
    assert parse_agent_reply('{"reply": "Hi", "search": {"q": "x", "type": "guns"}}')[1]["type"] == "market"
    assert parse_agent_reply('{"reply": "Hi", "search": {"q": "  "}}') == ("Hi", None)


def test_parse_generic_requests_browse_everything():
    browse = parse_agent_reply('{"reply": "Hi", "search": {"q": "", "type": "food", "max_price": 10000}}')[1]
    assert browse == {"q": "", "type": "food", "max_price": 10000}
    assert parse_agent_reply('{"reply": "Hi", "search": {"q": "Taom", "type": "food"}}')[1]["q"] == ""
    assert parse_agent_reply('{"reply": "Hi", "search": {"q": "taom"}}')[1] is None
    assert parse_agent_reply('{"reply": "Hi", "search": {"q": "", "type": "toys"}}')[1] is None


async def test_search_with_empty_query_is_sent():
    seen = []
    search = VdlSearch(
        "https://vdl.test/search", "k",
        transport=httpx.MockTransport(lambda r: seen.append(r) or httpx.Response(200, json={"items": []})),
    )
    await search.search("", "food", 3, 10000)
    assert dict(seen[0].url.params) == {"q": "", "type": "food", "limit": "3", "max_price": "10000"}


def test_card_caption_escapes_html():
    caption = card_caption(make_item(1, name="<script>TV</script>", seller={"name": "A & B"},
                                     old_price=4000000, discount_percent=12))
    assert "&lt;script&gt;" in caption and "A &amp; B" in caption
    assert "3 500 001 so'm (−12%)" in caption
    assert "Bo'lib to'lash" not in caption


def test_card_caption_installment():
    caption = card_caption(make_item(1, installment={"monthly": 40000, "months": 2}))
    assert "📆 Bo'lib to'lash: 40 000 so'm × 2 oy" in caption
    assert "Bo'lib" not in card_caption(make_item(1, installment={"monthly": None, "months": 2}))


# ------------------------------------------------------------ assistant flow


async def test_auto_send_reply_then_at_most_three_cards(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    requests = h.use_search(items_response([make_item(i) for i in range(1, 6)]))
    h.generator.queue(search_json())
    assert await h.business_message("Muzlatgich bormi?") == Outcome.SENT

    assert requests[0].url.params["limit"] == "3"
    texts = h.sent()
    assert texts[0].text == "Mana, bor variantlar:" and texts[0].business_connection_id == CONN_ID
    photos = h.photos()
    assert len(photos) == 3
    assert all(p.business_connection_id == CONN_ID and p.parse_mode == "HTML" for p in photos)
    button = photos[0].reply_markup.inline_keyboard[0][0]
    assert button.text == "🛍️ Ochish" and button.url == make_item(1)["link"]
    assert "Muzlatgich 1" in photos[0].caption
    # Reply first, then the cards.
    kinds = [type(r).__name__ for r in h.session.requests]
    assert kinds == ["GetBusinessConnection", "SendMessage", "SendPhoto", "SendPhoto", "SendPhoto"]
    history = h.assistant.history.get(CONN_ID, 222)
    assert "sent VDL Market cards" in history[-1]["text"]


async def test_cards_without_image_or_with_broken_image(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.use_search(items_response([make_item(1, image=None, type="food"), make_item(2)]))
    h.session.fail_photos = True
    h.generator.queue(search_json("lavash", "food"))
    assert await h.business_message("Lavash bormi?") == Outcome.SENT
    cards = [m for m in h.sent() if m.parse_mode == "HTML"]
    assert len(cards) == 2
    assert cards[0].reply_markup.inline_keyboard[0][0].text == "🍔 Ochish"


async def test_nothing_found_message(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.use_search(items_response([]))
    h.generator.queue(search_json())
    assert await h.business_message("Muzlatgich bormi?") == Outcome.SENT
    assert h.sent()[-1].text == "Hozircha topilmadi, boshqa so'z bilan qidirib ko'ring"
    assert h.photos() == []


async def test_search_error_behaves_like_nothing_found(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    h.use_search(lambda r: httpx.Response(503))
    h.generator.queue(search_json())
    assert await h.business_message("Muzlatgich bormi?") == Outcome.SENT
    assert h.photos() == []
    assert "topilmadi" in h.sent()[-1].text


async def test_preview_mode_shows_cards_and_sends_nothing(make_harness):
    h = make_harness()
    await h.connect()
    h.use_search(items_response([make_item(i) for i in range(1, 6)]))
    h.generator.queue(search_json())
    assert await h.business_message("Muzlatgich bormi?") == Outcome.PREVIEWED
    assert h.session.requests == []
    preview = h.previews[-1]
    assert preview.search["q"] == "muzlatgich" and len(preview.cards) == 5


async def test_search_ignored_when_not_configured(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    assert h.assistant.searcher is None
    h.generator.queue(search_json())
    assert await h.business_message("Muzlatgich bormi?") == Outcome.SENT
    assert [m.text for m in h.sent()] == ["Mana, bor variantlar:"]
    assert '"search" must always be null' in h.generator.calls[0][0]


async def test_clarifying_question_without_search(make_harness):
    h = make_harness(AUTO_SEND="true")
    await h.connect()
    requests = h.use_search(items_response([make_item(1)]))
    h.generator.queue(json.dumps({"reply": "Qanaqa texnika kerak?", "search": None}))
    assert await h.business_message("Bir narsa kerak edi") == Outcome.SENT
    assert requests == [] and h.photos() == []
    assert [m.text for m in h.sent()] == ["Qanaqa texnika kerak?"]
