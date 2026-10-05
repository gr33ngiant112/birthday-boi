"""#23: slash commands offered in DMs, Discord's 2000-character message limit, and
answering an interaction before any Redis call."""

import asyncio
import datetime

import pytest
from discord import app_commands

import bot

GUILD_ID = 300000000000005000
MEMBER = (200000000000002201, "member")
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
    if not {"set_birthday", "get_birthday", "list_birthdays", "forecast_birthdays", "forget_birthday"} <= payloads.keys():
        pytest.fail(f"setup: expected the five slash commands, got {sorted(payloads)}")
    assert [name for name, payload in payloads.items() if offered_in_dms(payload)] == []


def test_list_birthdays_with_100_users_stays_within_discord_message_limit(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    members = [(200000000000002000 + i, f"member-{i:03d}") for i in range(100)]
    guild = gateway.add_guild(GUILD_ID, "Guild", members)
    for i, (user_id, _) in enumerate(members):
        fake_redis.seed_birthday(GUILD_ID, user_id, f"1990-{i % 12 + 1:02d}-{i % 28 + 1:02d}")
    interaction = gateway.interaction(guild, guild.get_member(members[0][0]))

    asyncio.run(bot.list_birthdays.callback(interaction))

    lengths = [len(content or "") for content, _ in interaction.sent]
    if not lengths:
        pytest.fail("setup: /list_birthdays sent nothing")
    assert max(lengths) <= DISCORD_MESSAGE_LIMIT
    # Split between messages, never dropped or repeated.
    text = "\n".join(content or "" for content, _ in interaction.sent)
    assert {user_id: text.count(f"<@{user_id}>") for user_id, _ in members} == {user_id: 1 for user_id, _ in members}


@pytest.mark.parametrize(
    "command", ["set_birthday", "get_birthday", "list_birthdays", "forecast_birthdays", "forget_birthday"]
)
def test_command_answers_the_interaction_before_any_redis_call(command, fake_redis, freeze_today, gateway, monkeypatch):
    freeze_today(datetime.date(2026, 9, 29))
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER])
    fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "1990-10-15")
    member = guild.get_member(MEMBER[0])
    interaction = gateway.interaction(guild, member)
    # Whether the interaction was answered each time the command took a Redis
    # connection, which every command and pipeline does before it talks to Redis.
    answered = []
    pool = bot.redis_client.connection_pool
    get_connection = pool.get_connection

    async def get_connection_checking_the_answer(*args, **kwargs):
        answered.append(interaction.response.is_done())
        return await get_connection(*args, **kwargs)

    monkeypatch.setattr(pool, "get_connection", get_connection_checking_the_answer)
    args = {"set_birthday": ("10-15-1990",), "get_birthday": (member,)}.get(command, ())

    asyncio.run(getattr(bot, command).callback(interaction, *args))

    if not answered or not interaction.sent:
        pytest.fail(f"setup: expected Redis calls and a reply, got {answered!r} and {interaction.sent!r}")
    assert answered[0] is True
    assert interaction.deferred is not None
    assert interaction.deferred.get("ephemeral") is True
    assert [kwargs.get("ephemeral") for _, kwargs in interaction.sent] == [True] * len(interaction.sent)
