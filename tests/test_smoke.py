"""Import smoke test for bot.py.

bot.py does its startup work at import time: it loads the spaCy model, builds
the Redis client and calls client.run(TOKEN). The test imports it with
discord.Client.run patched, so it needs no network, no Redis server and no
real Discord token.
"""

import importlib
import sys
from unittest import mock

import discord

FAKE_TOKEN = "not-a-real-token"


def test_import_runs_client_with_token_from_env(monkeypatch):
    # With DYNO set, bot.py reads its config from the environment and skips
    # load_dotenv(), so a developer's local .env file is never read.
    monkeypatch.setenv("DYNO", "pytest")
    monkeypatch.setenv("DISCORD_TOKEN", FAKE_TOKEN)
    # redis.from_url() only builds the client; nothing connects at import.
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.delitem(sys.modules, "bot", raising=False)

    with mock.patch.object(discord.Client, "run", autospec=True) as run:
        try:
            # Runs bot.py top to bottom, including spacy.load("en_core_web_sm").
            bot = importlib.import_module("bot")
        finally:
            sys.modules.pop("bot", None)

    run.assert_called_once_with(bot.client, FAKE_TOKEN)
