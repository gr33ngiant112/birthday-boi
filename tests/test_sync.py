"""#28: the slash commands go to a development server, and to every server only when asked.

A global sync replaces the bot's commands in every server, so a build that is missing a
command deletes it everywhere. The client's setup_hook() runs as at login, with
CommandTree.sync faked so that nothing reaches Discord and the task loops not started.
"""

import asyncio
from unittest import mock

import discord
import pytest

import bot

DEV_GUILD_ID = 300000000000019000
# The top-level commands: the /birthday group, whose subcommands go with it.
COMMANDS = {"birthday"}


@pytest.fixture
def setup_hook(monkeypatch):
    """Return (run, sync, start). run(dev_guild_id, sync_commands) runs setup_hook() with the
    settings main() would have set; sync is the faked CommandTree.sync, start the task's start().
    sync.sent lists, for each sync, the names of the commands it would send to Discord: the tree's
    commands for that guild (or the global ones) at the time of the call."""

    def send(*, guild=None):
        sync.sent.append({command.name for command in bot.client.tree.get_commands(guild=guild)})
        return []

    sync = mock.AsyncMock(side_effect=send)
    sync.sent = []
    monkeypatch.setattr(bot.client.tree, "sync", sync)

    def run(dev_guild_id=None, sync_commands=False):
        monkeypatch.setattr(bot, "DEV_GUILD_ID", dev_guild_id)
        monkeypatch.setattr(bot, "SYNC_COMMANDS", sync_commands)
        asyncio.run(bot.client.setup_hook())

    # mock.patch, not monkeypatch: on the way out it deletes the loops' own start attribute, so
    # Loop.start is the class's again (monkeypatch would leave a copy on the loop behind).
    with (
        mock.patch.object(bot.check_upcoming_birthdays, "start") as start,
        mock.patch.object(bot.heartbeat, "start"),
        mock.patch.object(bot.send_birthday_greetings, "start"),
    ):
        yield run, sync, start
    # The copies copy_global_to() made for the development server.
    bot.client.tree.clear_commands(guild=discord.Object(id=DEV_GUILD_ID))


def global_commands():
    return {command.name for command in bot.client.tree.get_commands()}


def test_a_start_syncs_no_commands_unless_asked(setup_hook):
    run, sync, start = setup_hook

    run()

    sync.assert_not_called()
    start.assert_called_once_with()  # the monthly task still starts


def test_sync_replaces_the_commands_in_every_server(setup_hook):
    run, sync, start = setup_hook

    run(sync_commands=True)

    assert sync.await_args_list == [mock.call()]  # no guild: a global sync
    start.assert_called_once_with()


def test_a_development_server_gets_every_command_and_nothing_is_synced_globally(setup_hook):
    run, sync, start = setup_hook
    if global_commands() != COMMANDS:
        pytest.fail(f"setup: expected the birthday group, got {sorted(global_commands())}")

    run(dev_guild_id=DEV_GUILD_ID)

    assert [(call.args, call.kwargs["guild"].id) for call in sync.await_args_list] == [((), DEV_GUILD_ID)]
    assert sync.sent == [COMMANDS]  # copied to the server before its sync, not after
    dev_guild = discord.Object(id=DEV_GUILD_ID)
    assert {command.name for command in bot.client.tree.get_commands(guild=dev_guild)} == COMMANDS
    start.assert_called_once_with()
