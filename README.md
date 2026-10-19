# Telegram Business personal assistant

A bot that connects to your personal Telegram account through **Telegram Business → Chatbots**
and drafts replies to your private chats in your name and in your style. Facts about you and
your writing style come from [`profile.md`](profile.md).

Replies are written by the **Claude Code CLI** (`claude -p`) installed next to the bot, using
your **Claude subscription** (a `claude setup-token` token). An Anthropic API key is never used.

## What it does

- **PREVIEW mode is the default** (`AUTO_SEND=false`): replies are only printed to the logs
  (`PREVIEW, not sent`) and appended to `data/previews.jsonl`. Nothing is sent until you set
  `AUTO_SEND=true`.
- **Which chats the bot sees is decided by Telegram only** — in *Settings → Telegram Business →
  Chatbots* you choose the chats. There is no allowlist in the code.
- Runs with long polling: no domain, webhook or open port needed.

## Behaviour

| Situation | What happens |
|---|---|
| Business connection of another account | Ignored (`foreign_connection`), Claude is not called, warning in the log |
| Which chats | Whatever you selected in *Business → Chatbots*; every message Telegram delivers is handled |
| You write in a chat yourself | Saved to the history, never answered; the bot stays quiet in that chat for `OWNER_TAKEOVER_MINUTES` |
| Messages from bots (incl. this bot's own replies) | Ignored |
| Duplicate update | Processed once, also across restarts (`data/seen.json`) |
| Service messages (pins, title changes…) | Ignored; media is described as `[photo] caption`, `[voice message]`, `[sticker] 😀`… |
| `/pause` | No replies at all until `/resume`; survives restarts |
| Subscription usage limit | No retries until the reset time (or `LIMIT_COOLDOWN_MINUTES`); survives restarts; owner is notified in AUTO-SEND mode |
| Timeout / login error | Nothing is sent to the chat; owner is notified in AUTO-SEND mode, at most once per 10 minutes |
| Claude answers `[SKIP]` | Nothing is shown or sent |
| Before every auto-send | Pause/limit/takeover are re-checked and the business connection is fetched fresh from Telegram (owner, enabled, `can_reply`) |

Owner commands (only in your private chat with the bot): `/pause`, `/resume`, `/status`.
`/start` shows you an Uzbek how-to (mode, state, how to connect, commands). Anyone else gets a
short Uzbek note that this is a personal bot; Claude is not called and nothing about you is shared.
Other direct messages to the bot are ignored.

## How Claude is called

Every reply is one `claude -p` process started without a shell:

```
claude -p --safe-mode --settings '{"disableAllHooks":true,"enabledPlugins":{...: false}}' \
  --tools "" --strict-mcp-config --setting-sources "" --disable-slash-commands \
  --no-session-persistence --system-prompt-file <tmp file> --output-format json --effort low
```

- No tools, MCP servers, plugins, skills, hooks, CLAUDE.md or auto-memory.
- `--bare` is **not** used: it ignores the OAuth login, so the subscription would stop working.
- The conversation goes through **stdin only**; the system prompt (with your profile) is written to a
  private 0600 temp file in `data/claude-run` and deleted afterwards, so neither appears in `ps`.
- The child environment drops `ANTHROPIC_*`, `TELEGRAM_BOT_TOKEN`, Bedrock/Vertex/Foundry switches,
  `CLAUDE_CODE_SIMPLE` and nested-session variables; `CLAUDE_CODE_OAUTH_TOKEN` is added only from your settings.
- The token is redacted (`[REDACTED]`) from CLI output; a rejected token produces a message with
  `CLI said: ...` so you can see the reason without seeing the token.
- `python bot.py --check-claude` verifies all of this against the real CLI.

## Deploy to Railway

1. On a computer with a browser run `claude setup-token` and copy the `sk-ant-oat...` token (valid for 1 year).
2. In @BotFather: `/newbot`, then *Bot Settings → Business Mode* → enable. Get your numeric ID from @userinfobot.
3. Create a **private** GitHub repository and push this code (`profile.md` is baked into the image).
4. Railway → *New Project* → *GitHub Repository* → select the repo.
5. Service → *Variables* → *Raw Editor*:
   ```
   TELEGRAM_BOT_TOKEN=...
   OWNER_ID=...
   CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat...
   AUTO_SEND=false
   ```
   Optional: `CLAUDE_MODEL`, `CLAUDE_EFFORT`, `CLAUDE_TIMEOUT_SECONDS`, `LIMIT_COOLDOWN_MINUTES`,
   `OWNER_TAKEOVER_MINUTES`, `HISTORY_LIMIT`. Not needed: `CLAUDE_BIN`, `DATA_DIR`, `PROFILE_PATH`.
6. Volume: right-click the canvas (or Cmd/Ctrl+K) → *Volume* → select the service → mount path exactly
   `/app/data` → *Deploy*. Without a volume, history, pause and limit state are lost on every deploy.
7. Logs should show `Bot @... is running`, `Mode: PREVIEW` and
   `Claude auth: subscription token from environment variables`. Red lines in Railway are just stderr, not errors.
8. Send `/start` to the bot, then in Telegram *Settings → Telegram Business → Chatbots* (Telegram Premium
   required): enter the bot username, choose the chats, allow replying. The log shows
   `Business connection ...: user=<id> enabled=True`.
9. When someone writes to you, the log shows a `PREVIEW, not sent` block. When the replies look good,
   set `AUTO_SEND=true`.

Notes:
- Run the bot in **one place only**: two pollers with the same bot token interfere with each other
  (do not run `python bot.py` locally while Railway is running).
- Every push redeploys; changing `profile.md` also means commit + push.
- Token expired or leaked: run `claude setup-token` again and update the Railway variable.
  Bot token leaked: `/revoke` in @BotFather.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env && chmod 600 .env   # then put your token into .env with an editor
.venv/bin/python bot.py --check-claude
.venv/bin/python bot.py --try "Salom, qalesan?"
```

If `CLAUDE_CODE_OAUTH_TOKEN` is empty, this machine's own `claude` login is used.
On Windows use `.venv\Scripts\python`.

## Commands

| Command | Purpose | Exit codes |
|---|---|---|
| `python bot.py` | Run the bot with settings from `.env` | 0 normal stop, 2 config error or bot token rejected |
| `python bot.py --settings-from-env` | Container mode: settings from environment variables (removed from the process environment after reading) | same |
| `python bot.py --check-claude` | Check the Claude CLI setup without Telegram | 0 OK, 1 check failed, 2 config error |
| `python bot.py --try "text"` | Show the reply to a made-up message without Telegram | 0 OK, 1 Claude error, 2 config error |

`--check-claude` and `--try` do not need Telegram settings and also work with `--settings-from-env`.

## Tests

```bash
.venv/bin/python -m pytest
```

All tests run offline: a fake Telegram session and a fake `claude` executable are used.

## Files

| Path | Purpose |
|---|---|
| `bot.py` | Entry point and CLI flags |
| `assistant/config.py` | Settings from `.env` or the environment |
| `assistant/claude_cli.py` | Isolated `claude -p` runner, error classification |
| `assistant/language.py` | uz / ru / en language hint |
| `assistant/prompts.py` | System and user prompts |
| `assistant/store.py` | History, duplicates and runtime state in `data/` |
| `assistant/service.py` | Reply logic |
| `assistant/handlers.py` | aiogram router |
| `profile.md` | Facts about you and your writing style |
| `Dockerfile`, `railway.json`, `.dockerignore` | Railway deployment |
| `tests/` | Offline tests |
