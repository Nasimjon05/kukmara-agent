from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from assistant.claude_cli import (
    ClaudeAuthError,
    ClaudeCLI,
    ClaudeError,
    ClaudeLimitError,
    ClaudeTimeout,
    build_env,
    classify_failure,
    parse_result,
)
from tests.conftest import POSIX

TOKEN = "sk-ant-oat01-" + "A" * 60

FAKE_CLAUDE = r'''
import json, os, subprocess, sys, time

mode = os.environ.get("FAKE_MODE", "ok")
argv = sys.argv[1:]
stdin = sys.stdin.read()
prompt_file = argv[argv.index("--system-prompt-file") + 1]
with open(prompt_file, encoding="utf-8") as fh:
    system_prompt = fh.read()
record = {
    "argv": argv,
    "stdin": stdin,
    "env": dict(os.environ),
    "cwd": os.getcwd(),
    "listing": sorted(os.listdir(".")),
    "pid": os.getpid(),
    "prompt_file": prompt_file,
    "system_prompt": system_prompt,
    "prompt_mode": os.stat(prompt_file).st_mode & 0o777,
}
with open(os.environ["FAKE_RECORD"], "w", encoding="utf-8") as fh:
    json.dump(record, fh)


def result(text, is_error=False, status=None):
    print(json.dumps({"type": "result", "subtype": "success", "is_error": is_error,
                      "result": text, "api_error_status": status}))


if mode == "ok":
    if "stream-json" in argv:
        print(json.dumps({"type": "system", "subtype": "init", "tools": [], "mcp_servers": [],
                          "plugins": [], "skills": [], "apiKeySource": "none",
                          "apiProvider": "firstParty", "model": "fake-model"}))
    result("  OK reply  \n")
elif mode == "sleep":
    subprocess.Popen([sys.executable, "-c",
                      "import os, time; open(os.environ['FAKE_GRANDCHILD'], 'w').write(str(os.getpid())); time.sleep(60)"])
    time.sleep(60)
elif mode == "limit_epoch":
    result("Claude AI usage limit reached|1760000000", is_error=True)
elif mode == "limit_text":
    result("You've hit your limit · resets 3pm (Europe/Berlin)", is_error=True)
elif mode == "auth":
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    result("Invalid authentication credentials for OAuth token " + token, is_error=True, status=401)
elif mode == "crash":
    sys.stderr.write("boom: something broke\n")
    sys.exit(3)
elif mode == "empty":
    result("")
elif mode == "echo_token":
    result(os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "<none>"))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_claude.py"
    script.write_text(FAKE_CLAUDE, encoding="utf-8")
    if POSIX:
        binary = tmp_path / "claude"
        binary.write_text(f"#!{sys.executable}\n" + FAKE_CLAUDE, encoding="utf-8")
        binary.chmod(0o755)
    else:  # Windows local development: a .bat wrapper stands in for the shebang
        binary = tmp_path / "claude.bat"
        binary.write_text(f'@"{sys.executable}" "{script}" %*\n', encoding="utf-8")
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_RECORD", str(record))
    monkeypatch.setenv("FAKE_MODE", "ok")
    monkeypatch.setenv("FAKE_GRANDCHILD", str(tmp_path / "grandchild.pid"))

    class Fake:
        workdir = tmp_path / "data" / "claude-run"

        def __init__(self):
            self.binary = str(binary)
            self.tmp_path = tmp_path

        def cli(self, **kwargs):
            kwargs.setdefault("workdir", self.workdir)
            kwargs.setdefault("timeout", 30)
            return ClaudeCLI(self.binary, **kwargs)

        def mode(self, value):
            monkeypatch.setenv("FAKE_MODE", value)

        def record(self):
            return json.loads(record.read_text(encoding="utf-8"))

    return Fake()


def pid_alive(pid: int) -> bool:
    if POSIX:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


# ----------------------------------------------------------------- happy path


async def test_reply_is_stripped(fake):
    assert await fake.cli().generate("system", "hello") == "OK reply"


async def test_message_only_via_stdin_and_profile_only_in_private_file(fake):
    await fake.cli().generate("PROFILE-SECRET system", "MESSAGE-SECRET text")
    rec = fake.record()
    assert rec["stdin"] == "MESSAGE-SECRET text"
    assert not any("MESSAGE-SECRET" in a or "PROFILE-SECRET" in a for a in rec["argv"])
    assert rec["system_prompt"] == "PROFILE-SECRET system"
    if POSIX:
        assert rec["prompt_mode"] == 0o600
    assert not Path(rec["prompt_file"]).exists()


async def test_prompt_file_removed_on_error(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError):
        await fake.cli().generate("system", "hi")
    assert not Path(fake.record()["prompt_file"]).exists()
    assert list(fake.workdir.iterdir()) == []


async def test_no_shell_is_used(fake, tmp_path):
    marker = tmp_path / "pwned"
    text = f"$(touch {marker}) `touch {marker}` ; touch {marker} && echo hi"
    await fake.cli().generate("system", text)
    assert not marker.exists()
    assert fake.record()["stdin"] == text


async def test_isolation_flags(fake):
    await fake.cli(effort="medium", model="sonnet").generate("system", "hi")
    argv = fake.record()["argv"]
    assert argv[0] == "-p"
    expected_order = [
        "--safe-mode", "--settings", "--tools", "--strict-mcp-config", "--setting-sources",
        "--disable-slash-commands", "--no-session-persistence", "--system-prompt-file",
        "--output-format", "--effort",
    ]
    positions = [argv.index(flag) for flag in expected_order]
    assert positions == sorted(positions)
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert argv[argv.index("--output-format") + 1] == "json"
    assert argv[argv.index("--effort") + 1] == "medium"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--mcp-config" not in argv and "--bare" not in argv and "--verbose" not in argv
    settings = json.loads(argv[argv.index("--settings") + 1])
    assert settings["disableAllHooks"] is True
    assert settings["enabledPlugins"] and not any(settings["enabledPlugins"].values())


async def test_child_environment_is_clean(fake, monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api-should-not-pass")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://evil")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:ABC")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-parent-token")
    monkeypatch.setenv("CLAUDE_CODE_SIMPLE", "1")
    for name in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE",
                 "CLAUDE_CODE_ENTRYPOINT"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    await fake.cli().generate("system", "hi")
    env = {k.upper(): v for k, v in fake.record()["env"].items()}
    assert not any(k.startswith("ANTHROPIC_") for k in env)
    for name in ("TELEGRAM_BOT_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_SIMPLE", "CLAUDE_CODE_USE_BEDROCK",
                 "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY", "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT"):
        assert name not in env
    assert env["HOME"] == str(tmp_path / "home")
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"


async def test_configured_token_reaches_only_child_env(fake):
    cli = fake.cli(oauth_token=TOKEN)
    assert await cli.generate("system", "hi") == "OK reply"
    rec = fake.record()
    assert {k.upper(): v for k, v in rec["env"].items()}["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN
    assert TOKEN not in json.dumps(rec["argv"]) and TOKEN not in rec["stdin"] and TOKEN not in rec["system_prompt"]
    assert TOKEN not in repr(cli)


async def test_no_token_configured_means_none_in_child(fake, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", TOKEN)
    fake.mode("echo_token")
    assert await fake.cli().generate("system", "hi") == "<none>"


async def test_token_is_redacted_in_output(fake):
    fake.mode("echo_token")
    assert await fake.cli(oauth_token=TOKEN).generate("system", "hi") == "[REDACTED]"


async def test_rejected_token_message(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli(oauth_token=TOKEN).generate("system", "hi")
    message = str(info.value)
    assert "claude setup-token" in message and "CLI said:" in message
    assert "[REDACTED]" in message
    assert TOKEN not in message and TOKEN not in info.value.detail


async def test_inspect_reports_auth(fake):
    report = await fake.cli(oauth_token=TOKEN, auth_label="subscription token from .env").inspect()
    assert report["auth"] == "subscription token from .env"
    assert report["init"]["plugins"] == [] and report["init"]["apiProvider"] == "firstParty"
    assert report["reply"] == "OK reply"
    argv = fake.record()["argv"]
    assert argv[argv.index("--output-format") + 1] == "stream-json" and "--verbose" in argv
    assert fake.record()["stdin"] == "ping"

    report = await fake.cli().inspect()
    assert report["auth"] == "this machine's claude login"


async def test_runs_in_private_empty_workdir(fake):
    await fake.cli().generate("system", "hi")
    rec = fake.record()
    assert Path(rec["cwd"]).resolve() == fake.workdir.resolve()
    assert len(rec["listing"]) == 1 and rec["listing"][0].startswith("system-")
    if POSIX:
        assert stat.S_IMODE(os.stat(fake.workdir).st_mode) == 0o700


async def test_timeout_kills_process_group(fake):
    fake.mode("sleep")
    started = time.monotonic()
    with pytest.raises(ClaudeTimeout):
        await fake.cli(timeout=2.5).generate("system", "hi")
    assert time.monotonic() - started < 5
    grandchild_file = fake.tmp_path / "grandchild.pid"
    assert grandchild_file.exists()
    grandchild = int(grandchild_file.read_text())
    deadline = time.monotonic() + 2
    while pid_alive(grandchild) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not pid_alive(grandchild)
    assert not pid_alive(fake.record()["pid"])


# ----------------------------------------------------------------- failures


async def test_limit_with_epoch(fake):
    fake.mode("limit_epoch")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate("system", "hi")
    assert info.value.resets_at == 1760000000
    assert "|1760000000" not in info.value.detail
    assert str(info.value).startswith("Claude subscription usage limit reached (resets ")


async def test_limit_with_text(fake):
    fake.mode("limit_text")
    with pytest.raises(ClaudeLimitError) as info:
        await fake.cli().generate("system", "hi")
    assert info.value.resets_at is None
    assert info.value.reset_text.startswith("3pm")


async def test_auth_failure_without_token(fake):
    fake.mode("auth")
    with pytest.raises(ClaudeAuthError) as info:
        await fake.cli().generate("system", "hi")
    assert "/login" in str(info.value)


async def test_crash(fake):
    fake.mode("crash")
    with pytest.raises(ClaudeError) as info:
        await fake.cli().generate("system", "hi")
    assert "boom" in str(info.value)
    assert not isinstance(info.value, (ClaudeAuthError, ClaudeLimitError))


async def test_empty_reply(fake):
    fake.mode("empty")
    with pytest.raises(ClaudeError, match="empty"):
        await fake.cli().generate("system", "hi")


async def test_binary_not_found(tmp_path):
    cli = ClaudeCLI(str(tmp_path / "missing-claude"), workdir=tmp_path / "run")
    with pytest.raises(ClaudeError, match="not found"):
        await cli.generate("system", "hi")


# ------------------------------------------------------ pure function tests


def test_classify_failure():
    limit = classify_failure("Claude usage limit reached|1760000000")
    assert isinstance(limit, ClaudeLimitError) and limit.resets_at == 1760000000
    assert isinstance(classify_failure("anything", 429), ClaudeLimitError)
    assert isinstance(classify_failure("Rate-limit exceeded"), ClaudeLimitError)
    assert isinstance(classify_failure("You are out of extra usage"), ClaudeLimitError)
    text = classify_failure("You've hit your usage limit · resets 3pm (Asia/Tashkent)")
    assert isinstance(text, ClaudeLimitError) and text.reset_text == "3pm (Asia/Tashkent)"

    for message in ("Not logged in · Please run /login", "Invalid API key", "OAuth token has expired",
                    "Unauthorized"):
        error = classify_failure(message)
        assert isinstance(error, ClaudeAuthError), message
        assert "/login" in str(error)
    assert isinstance(classify_failure("whatever", 401), ClaudeAuthError)
    assert isinstance(classify_failure("whatever", 403), ClaudeAuthError)
    assert len(classify_failure("Unauthorized " + "x" * 1000).detail) == 300

    generic = classify_failure("y" * 2000)
    assert type(generic) is ClaudeError and len(str(generic)) < 600


def test_parse_result():
    ok = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": " hi "})
    assert parse_result(0, "noise\n" + ok + "\n\n", "") == "hi"

    err = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "Not logged in · Please run /login"})
    with pytest.raises(ClaudeAuthError):
        parse_result(1, err, "")

    sub = json.dumps({"type": "result", "subtype": "error_max_turns", "is_error": False, "result": "x"})
    with pytest.raises(ClaudeError):
        parse_result(0, sub, "")

    limited = json.dumps({"type": "result", "subtype": "success", "is_error": True, "result": "x", "api_error_status": 429})
    with pytest.raises(ClaudeLimitError):
        parse_result(1, limited, "")

    empty = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "   "})
    with pytest.raises(ClaudeError, match="empty"):
        parse_result(0, empty, "")

    with pytest.raises(ClaudeError, match="exit code 7"):
        parse_result(7, "", "")
    with pytest.raises(ClaudeError, match="kaboom"):
        parse_result(1, "not json", "kaboom")


def test_build_env_with_explicit_base():
    env = build_env("sk-ant-oat01-x", base={"PATH": "/bin", "ANTHROPIC_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "t"})
    assert env == {
        "PATH": "/bin",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
        "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-x",
    }
