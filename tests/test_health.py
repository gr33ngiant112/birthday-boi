"""#35: the heartbeat file, and `python bot.py --healthcheck`, which the image's HEALTHCHECK runs.

A gateway bot listens on no port, so the HEALTHCHECK checks a file: the heartbeat task touches
it every HEARTBEAT_INTERVAL while the bot is ready and connected to Discord, and --healthcheck
fails once the file is HEARTBEAT_MAX_AGE old, or missing. Nothing here connects to Discord: the
tests set the client's state by hand. main()'s load_dotenv() is patched to a no-op, so no
developer's .env is read.
"""

import asyncio
import os
import time
import types
from unittest import mock

import discord
import pytest
import redis.asyncio

import bot

MAX_AGE = bot.HEARTBEAT_MAX_AGE.total_seconds()


def set_connection(monkeypatch, *, ready, websocket_open):
    """Put bot.client in a state: READY received or not, and a gateway websocket that is open,
    closed (as while discord.py reconnects), or None (never connected)."""
    event = asyncio.Event()
    if ready:
        event.set()
    monkeypatch.setattr(bot.client, "_ready", event)
    websocket = None if websocket_open is None else types.SimpleNamespace(open=websocket_open)
    monkeypatch.setattr(bot.client, "ws", websocket)


def aged(path, seconds):
    """Create path, or keep it, with a modification time that many seconds ago."""
    path.touch()
    then = time.time() - seconds
    os.utime(path, (then, then))
    return path


def age_of(path):
    return time.time() - path.stat().st_mtime


@pytest.mark.parametrize(
    ("ready", "websocket_open", "touched"),
    [
        pytest.param(True, True, True, id="ready and connected"),
        pytest.param(False, None, False, id="not connected yet"),
        pytest.param(False, True, False, id="connected, before READY"),
        pytest.param(True, False, False, id="ready, reconnecting"),
        pytest.param(True, None, False, id="ready, no websocket"),
    ],
)
def test_the_heartbeat_touches_the_file_only_while_ready_and_connected(
    ready, websocket_open, touched, monkeypatch, tmp_path
):
    heartbeat_file = aged(tmp_path / "heartbeat", 600)  # ten minutes old
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", str(heartbeat_file))
    set_connection(monkeypatch, ready=ready, websocket_open=websocket_open)

    asyncio.run(bot.heartbeat.coro())

    assert (age_of(heartbeat_file) < 10) is touched


def test_the_first_heartbeat_creates_the_file(monkeypatch, tmp_path):
    heartbeat_file = tmp_path / "heartbeat"
    monkeypatch.setattr(bot, "HEARTBEAT_FILE", str(heartbeat_file))
    set_connection(monkeypatch, ready=True, websocket_open=True)

    asyncio.run(bot.heartbeat.coro())

    assert heartbeat_file.exists()
    assert age_of(heartbeat_file) < 10


def run_healthcheck(monkeypatch, heartbeat_file=None):
    """Run `python bot.py --healthcheck` as main() does, with HEARTBEAT_FILE set to heartbeat_file,
    or unset when it is None. Returns (exit code, Client.run mock, redis.asyncio.from_url mock)."""
    if heartbeat_file is None:
        monkeypatch.delenv("HEARTBEAT_FILE", raising=False)
    else:
        monkeypatch.setenv("HEARTBEAT_FILE", str(heartbeat_file))
    # The check needs no token or Redis, and contacts neither.
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    # main() sets these module globals; monkeypatch puts them back afterwards.
    for name in ("SYNC_COMMANDS", "HEARTBEAT_FILE"):
        monkeypatch.setattr(bot, name, getattr(bot, name))

    with (
        mock.patch.object(bot, "load_dotenv"),
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        mock.patch.object(redis.asyncio, "from_url") as from_url,
        pytest.raises(SystemExit) as exited,
    ):
        bot.main(["--healthcheck"])
    return exited.value.code, run, from_url


@pytest.mark.parametrize(
    ("age", "code"),
    [
        pytest.param(0, 0, id="just touched"),
        pytest.param(MAX_AGE - 10, 0, id="10 s under the limit"),
        pytest.param(MAX_AGE + 10, 1, id="10 s over the limit"),
        pytest.param(3600, 1, id="an hour old"),
    ],
)
def test_healthcheck_exits_0_for_a_fresh_heartbeat_and_1_for_a_stale_one(age, code, monkeypatch, tmp_path, capsys):
    heartbeat_file = aged(tmp_path / "heartbeat", age)

    exit_code, run, from_url = run_healthcheck(monkeypatch, heartbeat_file)

    assert exit_code == code
    printed = capsys.readouterr()
    if code == 0:
        assert printed.out.startswith(f"Healthy: the heartbeat file {heartbeat_file} is ")
    else:
        assert printed.err.startswith(f"Unhealthy: the heartbeat file {heartbeat_file} is ")
        assert printed.err.endswith(f" s old, over the {MAX_AGE:.0f} s limit.\n")
    run.assert_not_called()
    from_url.assert_not_called()


def test_healthcheck_exits_1_when_there_is_no_heartbeat_file(monkeypatch, tmp_path, capsys):
    heartbeat_file = tmp_path / "heartbeat"

    exit_code, run, from_url = run_healthcheck(monkeypatch, heartbeat_file)

    assert exit_code == 1
    assert capsys.readouterr().err == f"Unhealthy: the heartbeat file {heartbeat_file} is missing or unreadable.\n"
    run.assert_not_called()
    from_url.assert_not_called()


@pytest.mark.parametrize("value", [None, ""], ids=["HEARTBEAT_FILE unset", "HEARTBEAT_FILE empty"])
def test_healthcheck_reads_the_default_file_when_heartbeat_file_is_unset(value, monkeypatch, tmp_path):
    default_file = aged(tmp_path / "default-heartbeat", 0)
    monkeypatch.setattr(bot, "DEFAULT_HEARTBEAT_FILE", str(default_file))

    exit_code, _, _ = run_healthcheck(monkeypatch, value)

    assert exit_code == 0
    assert str(default_file) == bot.HEARTBEAT_FILE
