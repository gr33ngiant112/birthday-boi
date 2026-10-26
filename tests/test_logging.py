"""#37: the bot logs with the logging module instead of print(), its excepts catch only the errors
they expect, and a slash command that fails anyway gets one log line and an error reply.

A log line names members and servers by ID and errors by type, never with a birth date or a name.
The print() lines did both: a ValueError from a stored date that cannot be read quotes the date,
and a failed monthly post printed the server's name.
"""

import ast
import asyncio
import datetime
import logging
import os
import pathlib
import re
import subprocess
import sys
import types

import aiohttp
import discord
import pytest
from discord import app_commands

import bot

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
GUILD_ID = 300000000000022000
OTHER_GUILD_ID = 300000000000023000
ASKER = (200000000000011001, "asker")
READABLE = (200000000000011002, "readable")
UNREADABLE = (200000000000011003, "unreadable")
# A stored birthday core.parse_birthday() cannot read: month, day and year, as the bot's replies
# once wrote dates. Its ValueError quotes it: "Invalid isoformat string: '12-25-1990'".
UNREADABLE_BIRTHDAY = "12-25-1990"
# How that date could reach the log or the output.
UNREADABLE_FORMS = ["12-25-1990", "1990-12-25", "12-25", "December 25", "1990"]
COMMAND_FAILED = "❌ Something went wrong. Please try again later."


def log_lines(caplog):
    return [(record.levelno, record.getMessage()) for record in caplog.records]


def test_bot_py_and_core_py_call_no_print():
    calls = [
        f"{name}:{node.lineno}"
        for name in ("bot.py", "core.py")
        for node in ast.walk(ast.parse((REPO_ROOT / name).read_text(encoding="utf-8")))
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"
    ]

    assert calls == []


# `python bot.py --healthcheck` in a fresh interpreter, where nothing has set up logging, as in the
# image. load_dotenv() is replaced first: it reads the .env beside the repository's bot.py, a
# developer's .env. Client.run() is refused, as in tests/test_smoke.py.
HEALTHCHECK = """
import runpy
import sys

import discord
import dotenv

dotenv.load_dotenv = lambda *args, **kwargs: False


def refuse_to_run(*args, **kwargs):
    sys.exit("client.run() was called")


discord.Client.run = refuse_to_run
sys.argv = [sys.argv[1], "--healthcheck"]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def test_python_bot_py_logs_to_stderr_with_the_time_the_level_and_the_logger(tmp_path):
    heartbeat_file = tmp_path / "heartbeat"
    heartbeat_file.touch()
    env = {k: v for k, v in os.environ.items() if k not in ("DISCORD_TOKEN", "REDIS_URL", "DEV_GUILD_ID")}
    env["HEARTBEAT_FILE"] = str(heartbeat_file)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH")]))

    result = subprocess.run(
        [sys.executable, "-c", HEALTHCHECK, str(REPO_ROOT / "bot.py")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    assert (result.returncode, result.stdout) == (0, ""), result.stderr
    # discord.py's format: [time] [level, padded to 8] logger: message. INFO lines are written too.
    assert re.fullmatch(
        r"\[\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\] \[INFO    \] birthday_boi: "
        rf"Healthy: the heartbeat file {re.escape(str(heartbeat_file))} is \d+ s old\.\n",
        result.stderr,
    ), result.stderr


def view_text(view, gateway, guild, asker):
    """Run a view that lists the guild's birthdays and return the text it sent there."""
    if view == "monthly post":
        asyncio.run(bot.check_upcoming_birthdays())
        return "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    interaction = gateway.interaction(guild, guild.get_member(asker[0]))
    asyncio.run(getattr(bot, view).callback(interaction, *((90,) if view == "birthday_upcoming" else ())))
    return "\n".join(interaction.texts())


SKIPPED_IN = {
    "birthday_list": f"/birthday list in guild {GUILD_ID}",
    "birthday_upcoming": f"/birthday upcoming in guild {GUILD_ID}",
    "monthly post": f"the monthly post of guild {GUILD_ID}",
}


@pytest.mark.parametrize("view", ["birthday_list", "birthday_upcoming", "monthly post"])
def test_a_stored_birthday_that_cannot_be_read_is_skipped_and_logged_without_the_date(
    view, fake_redis, freeze_today, gateway, caplog, capsys
):
    freeze_today(datetime.date(2026, 10, 1))  # the 1st, so the monthly task posts; Nov 3 is 33 days away
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER, READABLE, UNREADABLE])
    fake_redis.seed_birthday(GUILD_ID, READABLE[0], "1991-11-03")
    fake_redis.seed_birthday(GUILD_ID, UNREADABLE[0], UNREADABLE_BIRTHDAY)

    text = view_text(view, gateway, guild, ASKER)

    # The narrowed except still skips only that member: the others are listed.
    if f"<@{READABLE[0]}>" not in text:
        pytest.fail(f"setup: the {view} should list the member whose birthday can be read, got {text!r}")
    assert f"<@{UNREADABLE[0]}>" not in text
    assert log_lines(caplog) == [
        (
            logging.ERROR,
            f"Skipped user {UNREADABLE[0]} in {SKIPPED_IN[view]}: unreadable birthday (ValueError)",
        )
    ]
    printed = capsys.readouterr()
    assert [form for form in UNREADABLE_FORMS if form in caplog.text + printed.out + printed.err] == []


def test_birthday_show_of_a_stored_birthday_that_cannot_be_read_says_so_and_logs_without_the_date(
    fake_redis, freeze_today, gateway, caplog, capsys
):
    freeze_today(datetime.date(2026, 10, 1))
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER, UNREADABLE])
    fake_redis.seed_birthday(GUILD_ID, UNREADABLE[0], UNREADABLE_BIRTHDAY)
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))

    asyncio.run(bot.birthday_show.callback(interaction, guild.get_member(UNREADABLE[0])))

    assert interaction.sent == [("❌ An error occurred while retrieving the birthday.", {"ephemeral": True})]
    assert log_lines(caplog) == [
        (
            logging.ERROR,
            f"Unreadable birthday of user {UNREADABLE[0]} in /birthday show in guild {GUILD_ID} (ValueError)",
        )
    ]
    printed = capsys.readouterr()
    assert [form for form in UNREADABLE_FORMS if form in caplog.text + printed.out + printed.err] == []


def forbidden():
    return discord.Forbidden(
        types.SimpleNamespace(status=403, reason="Forbidden"), {"code": 50013, "message": "Missing Permissions"}
    )


@pytest.mark.parametrize(
    "error",
    [
        # Discord refuses it, as without permission to write in #general.
        pytest.param(forbidden(), id="discord.Forbidden"),
        # The connection fails: discord.py retries neither of these, and aiohttp's is no OSError.
        pytest.param(aiohttp.ServerDisconnectedError(), id="aiohttp.ServerDisconnectedError"),
        pytest.param(ConnectionResetError(104, "Connection reset by peer"), id="ConnectionResetError"),
    ],
)
def test_a_monthly_post_that_fails_to_send_is_logged_and_the_other_guilds_get_theirs(
    error, fake_redis, freeze_today, gateway, monkeypatch, caplog
):
    freeze_today(datetime.date(2026, 10, 1))
    failing = gateway.add_guild(GUILD_ID, "Guild A", [READABLE])
    other = gateway.add_guild(OTHER_GUILD_ID, "Guild B", [ASKER])
    fake_redis.seed_birthday(GUILD_ID, READABLE[0], "1991-11-03")
    fake_redis.seed_birthday(OTHER_GUILD_ID, ASKER[0], "1992-12-15")
    if [guild.id for guild in bot.client.guilds] != [GUILD_ID, OTHER_GUILD_ID]:
        pytest.fail("setup: the task should reach the failing guild first")
    send_message = gateway.send_message

    async def fail_in_guild_a(channel_id, *, params):
        if int(channel_id) == gateway.general(failing).id:
            raise error
        return await send_message(channel_id, params=params)

    monkeypatch.setattr(bot.client._connection.http, "send_message", fail_in_guild_a)

    asyncio.run(bot.check_upcoming_birthdays())

    assert [
        f"<@{ASKER[0]}>" in (payload.get("content") or "") for payload in gateway.sent_to(gateway.general(other))
    ] == [True]
    assert log_lines(caplog) == [
        (logging.ERROR, f"The monthly post failed to send in guild {GUILD_ID} ({type(error).__name__})")
    ]


def test_birthday_forget_logs_a_storage_failure_by_its_type(fake_redis, gateway, caplog):
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER])
    fake_redis.seed_birthday(GUILD_ID, ASKER[0], "1990-12-25")
    fake_redis.connection_pool.connection_kwargs["server"].connected = False  # every command fails
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))

    asyncio.run(bot.birthday_forget.callback(interaction))

    assert interaction.sent == [("❌ Your birthday could not be deleted. Please try again later.", {"ephemeral": True})]
    assert log_lines(caplog) == [(logging.ERROR, "Storage unavailable in /birthday forget (ConnectionError)")]


def run_as_the_tree_does(command, interaction, *args):
    """Run a slash command as discord.py's CommandTree does: an error from the command's callback
    reaches the tree's error handler as a CommandInvokeError (Command._do_call, CommandTree._call)."""
    interaction.command = command

    async def run():
        try:
            await command.callback(interaction, *args)
        except Exception as e:  # noqa: BLE001 - discord.py hands every error to the tree's handler
            await bot.client.tree.on_error(interaction, app_commands.CommandInvokeError(command, e))

    asyncio.run(run())


async def event_loop_is_closed(*args, **kwargs):
    # An error the commands do not handle: redis.asyncio raises it for a connection opened in
    # another event loop.
    raise RuntimeError("Event loop is closed")


def test_a_command_that_fails_after_deferring_logs_one_line_and_tells_the_member(
    fake_redis, gateway, monkeypatch, caplog
):
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER])
    monkeypatch.setattr(bot, "get_guild_birthdays_redis", event_loop_is_closed)
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))

    run_as_the_tree_does(bot.birthday_list, interaction)

    # One line, without the error's message or a traceback.
    assert log_lines(caplog) == [
        (logging.ERROR, f"Command failed: command='birthday list' guild={GUILD_ID} error=RuntimeError")
    ]
    assert [record.exc_info for record in caplog.records] == [None]
    # /birthday list deferred first, so the error reply is a follow-up, which only the member sees.
    assert interaction.deferred == {"ephemeral": True}
    assert interaction.sent == [(COMMAND_FAILED, {"ephemeral": True})]


@pytest.mark.parametrize(
    ("command", "error", "logged_command"),
    [
        pytest.param(
            bot.birthday_show,
            app_commands.CommandSignatureMismatch(bot.birthday_show),
            "'birthday show'",
            id="a command changed since the last sync",
        ),
        pytest.param(None, app_commands.CommandNotFound("birthday", []), "None", id="a command the bot does not have"),
    ],
)
def test_an_error_before_any_reply_is_answered_with_the_interaction_response(
    command, error, logged_command, gateway, caplog
):
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER])
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))
    interaction.command = command

    asyncio.run(bot.client.tree.on_error(interaction, error))

    assert log_lines(caplog) == [
        (logging.ERROR, f"Command failed: command={logged_command} guild={GUILD_ID} error={type(error).__name__}")
    ]
    # Nothing was sent before, so the reply is the interaction's response: a follow-up cannot come
    # first (FakeFollowup refuses it, as Discord does).
    assert interaction.deferred is None
    assert interaction.sent == [(COMMAND_FAILED, {"ephemeral": True})]


def test_an_error_reply_that_fails_is_logged_and_not_raised(fake_redis, gateway, monkeypatch, caplog):
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER])
    monkeypatch.setattr(bot, "get_guild_birthdays_redis", event_loop_is_closed)
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))

    # The follow-up fails too, as once the interaction's 15 minutes are over.
    async def unknown_webhook(*args, **kwargs):
        raise discord.NotFound(
            types.SimpleNamespace(status=404, reason="Not Found"), {"code": 10015, "message": "Unknown Webhook"}
        )

    monkeypatch.setattr(interaction.followup, "send", unknown_webhook)

    run_as_the_tree_does(bot.birthday_list, interaction)

    assert log_lines(caplog) == [
        (logging.ERROR, f"Command failed: command='birthday list' guild={GUILD_ID} error=RuntimeError"),
        (logging.ERROR, f"Error reply failed: command='birthday list' guild={GUILD_ID} error=NotFound"),
    ]
