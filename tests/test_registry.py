"""#27: the bot's slash commands, task loops and event handlers, checked against sets written here.

Edits have dropped commands and the monthly task before (7a98e91 removed every slash command
and the task), and a global tree.sync() then removes a dropped command from every server. These
tests read what the running bot would register, from the objects bot.py builds on import, not
from its source text, so moving code into other modules keeps them passing:

- slash commands: bot.client.tree.get_commands(), with each group's subcommands named "group sub";
- task loops: the tasks.Loop objects at module level in the bot's own modules, by the name of the
  coroutine each one runs;
- event handlers: the on_* coroutines that @client.event sets on bot.client.

Adding, removing, renaming or regrouping any of them fails here until the set below is updated.
"""

import asyncio
import inspect
import pathlib
import sys
from unittest import mock

from discord import app_commands
from discord.ext import tasks

import bot

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent

EXPECTED_COMMANDS = {"set_birthday", "get_birthday", "list_birthdays", "forecast_birthdays", "forget_birthday"}
EXPECTED_LOOPS = {"check_upcoming_birthdays"}
EXPECTED_EVENT_HANDLERS = {"on_ready", "on_guild_remove"}


def assert_same(kind, found, expected):
    missing, extra = sorted(expected - found), sorted(found - expected)
    assert not missing and not extra, (
        f"{kind} differ from the expected set: missing {missing}, extra {extra}. "
        "If the change is intended, update the expected set in tests/test_registry.py."
    )


def command_names(commands):
    """The qualified name of each command, with a group replaced by its subcommands: "group sub"."""
    names = set()
    for command in commands:
        if isinstance(command, app_commands.Group):
            names |= command_names(command.commands)
        else:
            names.add(command.qualified_name)
    return names


def bot_modules():
    """The imported modules whose file is in this repository, outside tests/ and outside any
    installed packages (a virtualenv may live in the repository)."""
    modules = []
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if not file:
            continue
        path = pathlib.Path(file).resolve()
        if (
            path.is_relative_to(REPO_ROOT)
            and not path.is_relative_to(REPO_ROOT / "tests")
            and not {"site-packages", "dist-packages"} & set(path.parts)
        ):
            modules.append(module)
    return modules


def test_slash_commands():
    assert_same("Slash commands", command_names(bot.client.tree.get_commands()), EXPECTED_COMMANDS)


def test_task_loops():
    loops = {
        value.coro.__name__
        for module in bot_modules()
        for value in vars(module).values()
        if isinstance(value, tasks.Loop)
    }
    assert_same("Task loops", loops, EXPECTED_LOOPS)


def test_event_handlers():
    # Client.event() sets each handler on the client itself, so the handlers discord.Client
    # defines for every client, such as on_error, are not in vars().
    handlers = {
        name
        for name, value in vars(bot.client).items()
        if name.startswith("on_") and inspect.iscoroutinefunction(value)
    }
    assert_same("Event handlers", handlers, EXPECTED_EVENT_HANDLERS)


def test_setup_hook_starts_the_task_loops(monkeypatch):
    # setup_hook() runs at login. Loop.start() only records the loop here, and CommandTree.sync
    # is faked, so nothing reaches Discord whether setup_hook() syncs globally, to one guild or not at all.
    started = []
    monkeypatch.setattr(tasks.Loop, "start", lambda loop, *args, **kwargs: started.append(loop.coro.__name__))
    monkeypatch.setattr(bot.client.tree, "sync", mock.AsyncMock(return_value=[]))

    asyncio.run(bot.client.setup_hook())

    assert_same("Task loops started by setup_hook()", set(started), EXPECTED_LOOPS)


def test_subcommands_are_named_with_their_groups():
    # bot.py has no groups yet, so this checks command_names() on commands in nested groups.
    group = app_commands.Group(name="birthday", description="Birthdays")
    admin = app_commands.Group(name="admin", description="Admin", parent=group)

    @group.command(name="set", description="Set")
    async def set_command(interaction):
        """Never called: only its name is read."""

    @admin.command(name="reset", description="Reset")
    async def reset_command(interaction):
        """Never called: only its name is read."""

    @app_commands.command(name="ping", description="Ping")
    async def ping_command(interaction):
        """Never called: only its name is read."""

    assert command_names([group, ping_command]) == {"birthday set", "birthday admin reset", "ping"}
