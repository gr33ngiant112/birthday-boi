"""Startup tests for bot.py.

Importing bot must not read .env, build the Redis client or start the Discord
client; main(), which `python bot.py` runs, does all three. Client.run is patched
or refused wherever bot.py is imported or run, so even a regression cannot log in
to Discord. redis.asyncio.from_url only builds a client object, and the PING that
main() sends Redis before it starts the client is stubbed or goes to fakeredis, so
nothing here needs a network, a Redis server or a real Discord token.
"""

import asyncio
import importlib
import os
import pathlib
import runpy
import subprocess
import sys
from unittest import mock

import discord
import dotenv
import fakeredis
import pytest
import redis
import redis.asyncio

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
FAKE_TOKEN = "not-a-real-token"
LOCAL_REDIS_URL = "redis://localhost:6379/0"

# Runs in a fresh interpreter: fail instead of logging in if import calls client.run.
IMPORT_BOT = """
import sys
import discord

def refuse_to_run(*args, **kwargs):
    sys.exit("client.run() was called during import")

discord.Client.run = refuse_to_run
import bot
print(bot.__file__)
"""


def import_fresh_bot(monkeypatch):
    """Import bot.py as a new module object, with Client.run patched during the import.

    Returns (module, the run mock). The shared bot module is restored afterwards.
    """
    monkeypatch.delitem(sys.modules, "bot", raising=False)
    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        try:
            return importlib.import_module("bot"), run
        finally:
            sys.modules.pop("bot", None)


def stub_redis_ping():
    """A patch that answers main()'s startup PING without a connection."""
    return mock.patch.object(redis.asyncio.Redis, "ping", new=mock.AsyncMock(return_value=True))


def test_import_does_not_start_the_bot(monkeypatch):
    # Config is present and DYNO is unset (a local run). Before main() existed,
    # importing bot here loaded .env, built the Redis client and called client.run.
    monkeypatch.delenv("DYNO", raising=False)
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)

    with (
        mock.patch.object(dotenv, "load_dotenv") as load_dotenv,
        mock.patch.object(redis, "from_url") as from_url,
        mock.patch.object(redis.asyncio, "from_url") as async_from_url,
    ):
        bot, run = import_fresh_bot(monkeypatch)

    run.assert_not_called()
    load_dotenv.assert_not_called()
    from_url.assert_not_called()
    async_from_url.assert_not_called()
    assert bot.TOKEN is None
    assert bot.redis_client is None


def test_main_runs_client_with_token_from_env(monkeypatch):
    # With DYNO set (Heroku), main() reads its config from the environment only.
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    # redis.asyncio.from_url() only builds the client; nothing connects.
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    bot, _ = import_fresh_bot(monkeypatch)

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        mock.patch.object(bot, "load_dotenv") as load_dotenv,
        stub_redis_ping(),
    ):
        bot.main()

    run.assert_called_once_with(bot.client, FAKE_TOKEN)
    load_dotenv.assert_not_called()
    assert isinstance(bot.redis_client, redis.asyncio.Redis)
    assert bot.redis_client.connection_pool.connection_kwargs["host"] == "localhost"


def test_main_loads_dotenv_before_reading_config(monkeypatch):
    # Without DYNO, main() loads .env first and then reads the values it set.
    monkeypatch.delenv("DYNO", raising=False)
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)
    monkeypatch.delenv("REDIS_URL", raising=False)
    bot, _ = import_fresh_bot(monkeypatch)

    def fake_load_dotenv():
        # Stands in for a developer's .env file; no real .env is read.
        monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
        monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        mock.patch.object(bot, "load_dotenv", side_effect=fake_load_dotenv) as load_dotenv,
        stub_redis_ping(),
    ):
        bot.main()

    load_dotenv.assert_called_once_with()
    run.assert_called_once_with(bot.client, FAKE_TOKEN)


def run_bot_py(monkeypatch, *flags):
    """Run bot.py as `python bot.py FLAGS...` does, with the startup PING stubbed.

    The caller patches Client.run. Returns the script's globals.
    """
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    monkeypatch.delenv("DEV_GUILD_ID", raising=False)
    monkeypatch.setattr(sys, "argv", [str(REPO_ROOT / "bot.py"), *flags])

    with stub_redis_ping():
        return runpy.run_path(str(REPO_ROOT / "bot.py"), run_name="__main__")


def test_running_bot_py_as_a_script_calls_main(monkeypatch):
    # The Procfile starts the bot with `python bot.py`, which syncs no commands (#28).
    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        namespace = run_bot_py(monkeypatch)

    run.assert_called_once_with(namespace["client"], FAKE_TOKEN)
    assert namespace["SYNC_COMMANDS"] is False


def test_bot_py_sync_turns_on_the_global_sync(monkeypatch):
    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        namespace = run_bot_py(monkeypatch, "--sync")

    run.assert_called_once_with(namespace["client"], FAKE_TOKEN)
    assert namespace["SYNC_COMMANDS"] is True


def test_an_unknown_flag_stops_bot_py_before_it_logs_in(monkeypatch, capsys):
    with mock.patch.object(discord.Client, "run", autospec=True) as run, pytest.raises(SystemExit) as exited:
        run_bot_py(monkeypatch, "--snyc")

    run.assert_not_called()
    assert exited.value.code == 2  # argparse's usage error
    assert "unrecognized arguments: --snyc" in capsys.readouterr().err


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ("300000000000019000", 300000000000019000)])
def test_main_reads_the_development_server_from_dev_guild_id(monkeypatch, value, expected):
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    if value is None:
        monkeypatch.delenv("DEV_GUILD_ID", raising=False)
    else:
        monkeypatch.setenv("DEV_GUILD_ID", value)
    bot, _ = import_fresh_bot(monkeypatch)

    with mock.patch.object(discord.Client, "run", autospec=True) as run, stub_redis_ping():
        bot.main()

    run.assert_called_once_with(bot.client, FAKE_TOKEN)
    assert bot.DEV_GUILD_ID == expected


def test_main_exits_with_a_clear_message_when_dev_guild_id_is_not_a_number(monkeypatch):
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    monkeypatch.setenv("DEV_GUILD_ID", "my-test-server")
    bot, _ = import_fresh_bot(monkeypatch)

    with mock.patch.object(discord.Client, "run", autospec=True) as run, pytest.raises(SystemExit) as exited:
        bot.main()

    run.assert_not_called()
    assert exited.value.code == "Cannot start: DEV_GUILD_ID must be a server ID, a number."


def test_main_exits_with_a_clear_message_when_redis_does_not_answer(monkeypatch):
    # #15: the bot used to start anyway, and then every save failed and every read said
    # that nothing was stored.
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    bot, _ = import_fresh_bot(monkeypatch)
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises redis.ConnectionError
    monkeypatch.setattr(bot, "create_redis_client", lambda: fakeredis.FakeAsyncRedis(server=server))

    with mock.patch.object(discord.Client, "run", autospec=True) as run, pytest.raises(SystemExit) as exited:
        bot.main()

    run.assert_not_called()
    # A string exit code is printed to stderr, and the process exits with status 1.
    assert exited.value.code == (
        "Cannot start: Redis did not answer a PING. Check REDIS_URL and that Redis is running. "
        "ConnectionError: FakeRedis is emulating a connection error."
    )


def test_main_pings_redis_with_a_client_of_its_own(monkeypatch):
    # redis.asyncio connections belong to the event loop that opened them, and client.run()
    # starts a new loop for the bot. A PING through the bot's own client would leave that
    # client a connection on the closed startup loop, and its first command in the bot's
    # loop would raise "RuntimeError: Event loop is closed". fakeredis connections work in
    # any loop, so this checks the cause: when client.run() is called, the PING has been
    # sent, not through the bot's client, and its connection is closed.
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    bot, _ = import_fresh_bot(monkeypatch)
    server = fakeredis.FakeServer()
    monkeypatch.setattr(bot, "create_redis_client", lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True))
    taken = []  # (pool, connection) for each connection taken, in order
    get_connection = redis.asyncio.ConnectionPool.get_connection

    async def get_connection_noting_it(pool, *args, **kwargs):
        connection = await get_connection(pool, *args, **kwargs)
        taken.append((pool, connection))
        return connection

    monkeypatch.setattr(redis.asyncio.ConnectionPool, "get_connection", get_connection_noting_it)
    at_run = {}

    def run(client, token):
        at_run["pools"] = [pool for pool, _ in taken]
        at_run["still connected"] = [connection.is_connected for _, connection in taken]
        # The bot's first Redis command, in a new event loop as client.run() would start.
        at_run["ping from the bot's loop"] = asyncio.run(bot.redis_client.ping())

    with mock.patch.object(discord.Client, "run", autospec=True, side_effect=run) as client_run:
        bot.main()

    client_run.assert_called_once_with(bot.client, FAKE_TOKEN)
    assert len(at_run["pools"]) == 1  # the startup PING
    assert at_run["pools"][0] is not bot.redis_client.connection_pool
    assert at_run["still connected"] == [False]
    assert at_run["ping from the bot's loop"] is True


def test_import_without_config_from_an_empty_directory(tmp_path):
    # #24: `import bot` with no DISCORD_TOKEN or REDIS_URL, from a directory with no
    # .env, raised AttributeError while building the Redis client. DYNO is removed
    # too, so this is the local-run path that used to call load_dotenv().
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "REDIS_URL", "DYNO")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))

    result = subprocess.run(
        [sys.executable, "-c", IMPORT_BOT],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert result.returncode == 0, result.stderr
    assert pathlib.Path(result.stdout.splitlines()[-1]) == REPO_ROOT / "bot.py"
