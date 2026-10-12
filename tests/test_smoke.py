"""Startup tests for bot.py.

Importing bot must not read .env, build the Redis client or start the Discord
client; main(), which `python bot.py` runs, does all three. Client.run is patched
or refused wherever bot.py is imported or run, so even a regression cannot log in
to Discord. redis.asyncio.from_url only builds a client object, and the PING that
main() sends Redis before it starts the client is stubbed or goes to fakeredis, so
nothing here needs a network, a Redis server or a real Discord token.
"""

import asyncio
import functools
import importlib
import os
import pathlib
import runpy
import subprocess
import sys
from unittest import mock

import discord
import dotenv
import dotenv.main
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


def no_dotenv(bot):
    """A patch that makes bot's load_dotenv() a no-op. main() always calls it, and from bot.py
    python-dotenv looks for a .env in the repository and in every directory above it."""
    return mock.patch.object(bot, "load_dotenv")


def test_import_does_not_start_the_bot(monkeypatch):
    # Config is present. Before main() existed, importing bot here loaded .env, built the
    # Redis client and called client.run.
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
    # main() reads its config from the environment; load_dotenv() is a no-op here.
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

    run.assert_called_once_with(bot.client, FAKE_TOKEN, log_handler=None)
    load_dotenv.assert_called_once_with()
    assert isinstance(bot.redis_client, redis.asyncio.Redis)
    assert bot.redis_client.connection_pool.connection_kwargs["host"] == "localhost"


def test_main_loads_dotenv_before_reading_config(monkeypatch):
    # main() loads .env first and then reads the values it set.
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
    run.assert_called_once_with(bot.client, FAKE_TOKEN, log_handler=None)


def test_main_loads_dotenv_even_where_dyno_is_set(monkeypatch):
    # #35: main() skipped load_dotenv() when Heroku's DYNO was set. Without Heroku it always
    # calls it; python-dotenv never overrides a variable that is already set.
    monkeypatch.setenv("DYNO", "worker.1")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    bot, _ = import_fresh_bot(monkeypatch)

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        no_dotenv(bot) as load_dotenv,
        stub_redis_ping(),
    ):
        bot.main()

    load_dotenv.assert_called_once_with()
    run.assert_called_once_with(bot.client, FAKE_TOKEN, log_handler=None)


def test_main_keeps_the_environment_over_a_dotenv_file(monkeypatch, tmp_path):
    # #35: compose.yaml sets the container's environment (env_file, and REDIS_URL for its own
    # Redis). A variable already set there wins over the same one in a .env file, and one only
    # the file sets still comes from it. The real load_dotenv() reads tmp_path's .env here.
    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "DISCORD_TOKEN=token-from-the-file\nREDIS_URL=redis://from-the-file:6379/0\nDEV_GUILD_ID=300000000000019000\n"
    )
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    # Unset, so the file sets it; monkeypatch removes it again afterwards.
    monkeypatch.setenv("DEV_GUILD_ID", "")
    monkeypatch.delenv("DEV_GUILD_ID")
    bot, _ = import_fresh_bot(monkeypatch)
    load_this_file = functools.partial(dotenv.main.load_dotenv, dotenv_path=dotenv_file)

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        mock.patch.object(bot, "load_dotenv", side_effect=load_this_file) as load_dotenv,
        stub_redis_ping(),
    ):
        bot.main()

    if load_dotenv.call_count != 1 or os.environ.get("DEV_GUILD_ID") != "300000000000019000":
        pytest.fail(f"setup: main() should load the file once, got {load_dotenv.call_args_list!r}")
    run.assert_called_once_with(bot.client, FAKE_TOKEN, log_handler=None)
    assert bot.redis_client.connection_pool.connection_kwargs["host"] == "localhost"
    assert bot.DEV_GUILD_ID == 300000000000019000


def run_bot_py(monkeypatch, *flags):
    """Run bot.py as `python bot.py FLAGS...` does, with the startup PING stubbed.

    The caller patches Client.run. Returns the script's globals. The script binds dotenv's
    load_dotenv when it runs, so the patch is on the dotenv module.
    """
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    monkeypatch.delenv("DEV_GUILD_ID", raising=False)
    monkeypatch.setattr(sys, "argv", [str(REPO_ROOT / "bot.py"), *flags])

    with stub_redis_ping(), mock.patch.object(dotenv, "load_dotenv"):
        return runpy.run_path(str(REPO_ROOT / "bot.py"), run_name="__main__")


def test_running_bot_py_as_a_script_calls_main(monkeypatch):
    # The image starts the bot with `python bot.py`, which syncs no commands (#28).
    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        namespace = run_bot_py(monkeypatch)

    run.assert_called_once_with(namespace["client"], FAKE_TOKEN, log_handler=None)
    assert namespace["SYNC_COMMANDS"] is False


def test_bot_py_sync_turns_on_the_global_sync(monkeypatch):
    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        namespace = run_bot_py(monkeypatch, "--sync")

    run.assert_called_once_with(namespace["client"], FAKE_TOKEN, log_handler=None)
    assert namespace["SYNC_COMMANDS"] is True


def test_an_unknown_flag_stops_bot_py_before_it_logs_in(monkeypatch, capsys):
    with mock.patch.object(discord.Client, "run", autospec=True) as run, pytest.raises(SystemExit) as exited:
        run_bot_py(monkeypatch, "--snyc")

    run.assert_not_called()
    assert exited.value.code == 2  # argparse's usage error
    assert "unrecognized arguments: --snyc" in capsys.readouterr().err


@pytest.mark.parametrize(("value", "expected"), [(None, None), ("", None), ("300000000000019000", 300000000000019000)])
def test_main_reads_the_development_server_from_dev_guild_id(monkeypatch, value, expected):
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    if value is None:
        monkeypatch.delenv("DEV_GUILD_ID", raising=False)
    else:
        monkeypatch.setenv("DEV_GUILD_ID", value)
    bot, _ = import_fresh_bot(monkeypatch)

    with mock.patch.object(discord.Client, "run", autospec=True) as run, stub_redis_ping(), no_dotenv(bot):
        bot.main()

    run.assert_called_once_with(bot.client, FAKE_TOKEN, log_handler=None)
    assert expected == bot.DEV_GUILD_ID


def test_main_exits_with_a_clear_message_when_dev_guild_id_is_not_a_number(monkeypatch):
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    monkeypatch.setenv("DEV_GUILD_ID", "my-test-server")
    bot, _ = import_fresh_bot(monkeypatch)

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        no_dotenv(bot),
        pytest.raises(SystemExit) as exited,
    ):
        bot.main()

    run.assert_not_called()
    assert exited.value.code == "Cannot start: DEV_GUILD_ID must be a server ID, a number."


def exit_line_for_missing(names):
    """The line main() exits with when the variables called names are missing or empty."""
    return f"Cannot start: {names} must be set, in .env or in the environment (see .env.example)."


@pytest.mark.parametrize(
    ("env", "named"),
    [
        pytest.param({"REDIS_URL": LOCAL_REDIS_URL}, "DISCORD_TOKEN", id="DISCORD_TOKEN unset"),
        pytest.param({"DISCORD_TOKEN": "", "REDIS_URL": LOCAL_REDIS_URL}, "DISCORD_TOKEN", id="DISCORD_TOKEN empty"),
        pytest.param({"DISCORD_TOKEN": FAKE_TOKEN}, "REDIS_URL", id="REDIS_URL unset"),
        pytest.param({"DISCORD_TOKEN": FAKE_TOKEN, "REDIS_URL": ""}, "REDIS_URL", id="REDIS_URL empty"),
        pytest.param({}, "DISCORD_TOKEN and REDIS_URL", id="both unset"),
    ],
)
def test_main_names_each_missing_variable_and_stops_before_redis_and_discord(monkeypatch, env, named):
    # #18: a missing REDIS_URL used to crash inside redis-py with a traceback, and an empty
    # DISCORD_TOKEN reached Redis and then client.run(). load_dotenv() is a no-op here, so
    # main() reads the environment only.
    for name in ("DISCORD_TOKEN", "REDIS_URL", "DEV_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    bot, _ = import_fresh_bot(monkeypatch)

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        mock.patch.object(redis.asyncio, "from_url") as from_url,
        no_dotenv(bot),
        pytest.raises(SystemExit) as exited,
    ):
        bot.main()

    assert exited.value.code == exit_line_for_missing(named)
    # No Redis client was built, so nothing reached Redis, and nothing reached Discord.
    from_url.assert_not_called()
    assert bot.redis_client is None
    run.assert_not_called()


def test_a_env_copied_from_the_example_stops_at_the_empty_token(monkeypatch):
    # main() loads .env first. `cp .env.example .env` gives DISCORD_TOKEN= until the token is
    # filled in, and an empty value counts as missing.
    for name in ("DISCORD_TOKEN", "REDIS_URL", "DEV_GUILD_ID"):
        monkeypatch.delenv(name, raising=False)
    bot, _ = import_fresh_bot(monkeypatch)

    def load_a_copy_of_the_example():
        # Stands in for load_dotenv() on that .env, with .env.example's values; no real .env is read.
        monkeypatch.setenv("DISCORD_TOKEN", "")
        monkeypatch.setenv("REDIS_URL", "redis://127.0.0.1:6379/0")
        monkeypatch.setenv("DEV_GUILD_ID", "")

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        mock.patch.object(bot, "load_dotenv", side_effect=load_a_copy_of_the_example) as load_dotenv,
        mock.patch.object(redis.asyncio, "from_url") as from_url,
        pytest.raises(SystemExit) as exited,
    ):
        bot.main()

    load_dotenv.assert_called_once_with()
    assert exited.value.code == exit_line_for_missing("DISCORD_TOKEN")
    from_url.assert_not_called()
    assert bot.redis_client is None
    run.assert_not_called()


def test_main_exits_with_a_clear_message_when_redis_does_not_answer(monkeypatch):
    # #15: the bot used to start anyway, and then every save failed and every read said
    # that nothing was stored.
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    bot, _ = import_fresh_bot(monkeypatch)
    server = fakeredis.FakeServer()
    server.connected = False  # every command raises redis.ConnectionError
    monkeypatch.setattr(bot, "create_redis_client", lambda: fakeredis.FakeAsyncRedis(server=server))

    with (
        mock.patch.object(discord.Client, "run", autospec=True) as run,
        no_dotenv(bot),
        pytest.raises(SystemExit) as exited,
    ):
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
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)
    bot, _ = import_fresh_bot(monkeypatch)
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        bot, "create_redis_client", lambda: fakeredis.FakeAsyncRedis(server=server, decode_responses=True)
    )
    taken = []  # (pool, connection) for each connection taken, in order
    get_connection = redis.asyncio.ConnectionPool.get_connection

    async def get_connection_noting_it(pool, *args, **kwargs):
        connection = await get_connection(pool, *args, **kwargs)
        taken.append((pool, connection))
        return connection

    monkeypatch.setattr(redis.asyncio.ConnectionPool, "get_connection", get_connection_noting_it)
    at_run = {}

    def run(client, token, **kwargs):
        at_run["pools"] = [pool for pool, _ in taken]
        at_run["still connected"] = [connection.is_connected for _, connection in taken]
        # The bot's first Redis command, in a new event loop as client.run() would start.
        at_run["ping from the bot's loop"] = asyncio.run(bot.redis_client.ping())

    with mock.patch.object(discord.Client, "run", autospec=True, side_effect=run) as client_run, no_dotenv(bot):
        bot.main()

    client_run.assert_called_once_with(bot.client, FAKE_TOKEN, log_handler=None)
    assert len(at_run["pools"]) == 1  # the startup PING
    assert at_run["pools"][0] is not bot.redis_client.connection_pool
    assert at_run["still connected"] == [False]
    assert at_run["ping from the bot's loop"] is True


def test_import_without_config_from_an_empty_directory(tmp_path):
    # #24: `import bot` with no DISCORD_TOKEN or REDIS_URL, from a directory with no
    # .env, raised AttributeError while building the Redis client.
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "REDIS_URL")}
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


def test_import_loads_no_spacy(tmp_path):
    # #24 and #30: importing bot loaded spaCy and its English model, which only the @mention date
    # parser used; #30 measured 0.87 s more startup and peak memory up from 46 MB to 160 MB. Members
    # now type dates as slash command options. A fresh interpreter, so no earlier import here counts.
    script = IMPORT_BOT + "print(sorted(m for m in sys.modules if m.split('.')[0] in ('spacy', 'en_core_web_sm')))\n"
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "REDIS_URL")}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))

    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300
    )

    if result.returncode != 0 or len(result.stdout.splitlines()) < 2:
        pytest.fail(f"setup: importing bot should succeed and print the modules, got {result!r}")
    assert result.stdout.splitlines()[-1] == "[]"
