"""#23: slash commands offered in DMs, and Discord's 2000-character message limit."""

import asyncio
import datetime

import pytest
from discord import app_commands

import bot

GUILD_ID = 300000000000005000
DM_CONTEXTS = {app_commands.AppCommandContext.DM_CHANNEL, app_commands.AppCommandContext.PRIVATE_CHANNEL}
DISCORD_MESSAGE_LIMIT = 2000


def offered_in_dms(payload):
    """Whether Discord offers a global command registered with this payload in DMs."""
    contexts = payload.get("contexts")
    if contexts is not None:
        return bool(DM_CONTEXTS & set(contexts))
    # Without contexts, Discord falls back to the deprecated dm_permission (default true).
    return payload.get("dm_permission", True)


def test_slash_commands_are_not_offered_in_dms():
    tree = bot.client.tree
    # The payloads tree.sync() sends to Discord.
    payloads = {command.name: command.to_dict(tree) for command in tree.get_commands()}
    if not {"set_birthday", "get_birthday", "list_birthdays", "forecast_birthdays"} <= payloads.keys():
        pytest.fail(f"setup: expected the four slash commands, got {sorted(payloads)}")
    assert [name for name, payload in payloads.items() if offered_in_dms(payload)] == []


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="#23: /list_birthdays sends the whole table as one message, with no length limit",
)
def test_list_birthdays_with_100_users_stays_within_discord_message_limit(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    members = [(200000000000002000 + i, f"member-{i:03d}") for i in range(100)]
    guild = gateway.add_guild(GUILD_ID, "Guild", members)
    for i, (user_id, _) in enumerate(members):
        fake_redis.set(f"user:{user_id}:birthday", f"1990-{i % 12 + 1:02d}-{i % 28 + 1:02d}")
    interaction = gateway.interaction(guild, guild.get_member(members[0][0]))

    asyncio.run(bot.list_birthdays.callback(interaction))

    lengths = [len(content or "") for content, _ in interaction.sent]
    if not lengths:
        pytest.fail("setup: /list_birthdays sent nothing")
    assert max(lengths) <= DISCORD_MESSAGE_LIMIT
