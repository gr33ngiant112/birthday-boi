"""Startup tests for bot.py.

Importing bot must not read .env, build the Redis client or start the Discord
client; main(), which `python bot.py` runs, does all three. Client.run is patched
or refused wherever bot.py is imported or run, so even a regression cannot log in
to Discord. redis.asyncio.from_url only builds a client object, so nothing here
needs a network, a Redis server or a real Discord token.
"""

import importlib
import os
import pathlib
import runpy
import subprocess
import sys
from unittest import mock

import discord
import dotenv
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
    ):
        bot.main()

    load_dotenv.assert_called_once_with()
    run.assert_called_once_with(bot.client, FAKE_TOKEN)


def test_running_bot_py_as_a_script_calls_main(monkeypatch):
    # The Procfile starts the bot with `python bot.py`.
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    monkeypatch.setenv("REDIS_URL", LOCAL_REDIS_URL)

    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        namespace = runpy.run_path(str(REPO_ROOT / "bot.py"), run_name="__main__")

    run.assert_called_once_with(namespace["client"], FAKE_TOKEN)


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
