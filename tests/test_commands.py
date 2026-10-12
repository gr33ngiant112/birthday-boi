"""#23 and #33: slash commands offered in DMs, Discord's 2000-character message limit (#37: and
its embed limits), answering an interaction before any Redis call, and replies that only the
caller sees."""

import asyncio
import datetime

import pytest
from discord import app_commands

import bot

GUILD_ID = 300000000000005000
MEMBER = (200000000000002201, "member")
DM_CONTEXTS = {app_commands.AppCommandContext.DM_CHANNEL, app_commands.AppCommandContext.PRIVATE_CHANNEL}
DISCORD_MESSAGE_LIMIT = 2000
DISCORD_EMBED_DESCRIPTION_LIMIT = 4096
SUBCOMMANDS = ["birthday_set", "birthday_show", "birthday_list", "birthday_upcoming", "birthday_forget"]


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
    if "birthday" not in payloads:
        pytest.fail(f"setup: expected the birthday group, got {sorted(payloads)}")
    assert [name for name, payload in payloads.items() if offered_in_dms(payload)] == []


def test_the_birthday_group_is_guild_only():
    # #33. Discord reads contexts and dm_permission only on a top-level command, so the group's
    # apply to every subcommand. These are the values @app_commands.guild_only() gave the five
    # flat commands the group replaced.
    payload = bot.birthday_group.to_dict(bot.client.tree)

    if sorted(option["name"] for option in payload["options"]) != ["forget", "list", "set", "show", "upcoming"]:
        pytest.fail(f"setup: expected the five subcommands, got {payload['options']!r}")
    assert payload["contexts"] == [app_commands.AppCommandContext.GUILD]
    assert payload["dm_permission"] is False


def test_birthday_list_with_100_users_stays_within_discord_message_limit(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 9, 29))
    members = [(200000000000002000 + i, f"member-{i:03d}") for i in range(100)]
    guild = gateway.add_guild(GUILD_ID, "Guild", members)
    for i, (user_id, _) in enumerate(members):
        fake_redis.seed_birthday(GUILD_ID, user_id, f"1990-{i % 12 + 1:02d}-{i % 28 + 1:02d}")
    interaction = gateway.interaction(guild, guild.get_member(members[0][0]))

    asyncio.run(bot.birthday_list.callback(interaction))

    lengths = [len(content or "") for content, _ in interaction.sent]
    # #37: the list is sent as embeds.
    description_lengths = [len(embed["description"]) for payload in interaction.payloads for embed in payload["embeds"]]
    if not lengths:
        pytest.fail("setup: /birthday list sent nothing")
    assert max(lengths) <= DISCORD_MESSAGE_LIMIT
    assert max(description_lengths) <= DISCORD_EMBED_DESCRIPTION_LIMIT
    # Split between messages, never dropped or repeated.
    text = "\n".join(interaction.texts())
    assert {user_id: text.count(f"<@{user_id}>") for user_id, _ in members} == {user_id: 1 for user_id, _ in members}


@pytest.mark.parametrize("command", SUBCOMMANDS)
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
    args = {"birthday_set": (10, 15, 1990), "birthday_show": (member,)}.get(command, ())

    asyncio.run(getattr(bot, command).callback(interaction, *args))

    if not answered or not interaction.sent:
        pytest.fail(f"setup: expected Redis calls and a reply, got {answered!r} and {interaction.sent!r}")
    assert answered[0] is True
    assert interaction.deferred is not None
    assert interaction.deferred.get("ephemeral") is True
    assert [kwargs.get("ephemeral") for _, kwargs in interaction.sent] == [True] * len(interaction.sent)


# Each kind of reply of each subcommand: (subcommand, whether the store holds MEMBER's
# birthday or is down, the arguments, how the first reply starts).
EVERY_REPLY = [
    pytest.param("birthday_set", "stored", (10, 15, 1990), "✅ Your birthday has been set", id="set"),
    pytest.param("birthday_set", "stored", (2, 30), "❌ That date does not exist", id="set, no such date"),
    pytest.param("birthday_set", "stored", (1, 1, 2099), "❌ That date is in the future", id="set, in the future"),
    pytest.param("birthday_set", "down", (10, 15, 1990), "⚠️ Storage is unavailable, so", id="set, storage down"),
    pytest.param("birthday_show", "stored", "member", "🎂", id="show"),
    pytest.param("birthday_show", "empty", "member", f"❌ <@{MEMBER[0]}> has not set", id="show, not set"),
    pytest.param("birthday_show", "down", "member", "⚠️ Storage is unavailable.", id="show, storage down"),
    pytest.param("birthday_list", "stored", (), "🎉 Server Birthdays", id="list"),
    pytest.param("birthday_list", "empty", (), "❌ No birthdays have been set", id="list, none"),
    pytest.param("birthday_list", "down", (), "⚠️ Storage is unavailable.", id="list, storage down"),
    pytest.param("birthday_upcoming", "stored", (), "🎉 Upcoming Birthdays", id="upcoming"),
    pytest.param("birthday_upcoming", "empty", (), "❌ No birthdays in the next 30 days", id="upcoming, none"),
    pytest.param("birthday_upcoming", "down", (), "⚠️ Storage is unavailable.", id="upcoming, storage down"),
    pytest.param("birthday_forget", "stored", (), "✅ Your birthday has been deleted", id="forget"),
    pytest.param("birthday_forget", "empty", (), "✅ You had no birthday stored", id="forget, nothing stored"),
    pytest.param("birthday_forget", "down", (), "❌ Your birthday could not be deleted", id="forget, storage down"),
]


@pytest.mark.parametrize(("command", "store", "args", "reply"), EVERY_REPLY)
def test_every_reply_is_ephemeral(command, store, args, reply, fake_redis, freeze_today, gateway):
    # #33: whatever a subcommand answers, only the member who ran it sees the answer.
    freeze_today(datetime.date(2026, 9, 29))  # MEMBER's birthday, Oct 15, is 16 days away
    guild = gateway.add_guild(GUILD_ID, "Guild", [MEMBER])
    if store != "empty":
        fake_redis.seed_birthday(GUILD_ID, MEMBER[0], "1990-10-15", show_age=True)
    if store == "down":
        fake_redis.connection_pool.connection_kwargs["server"].connected = False  # every command fails
    member = guild.get_member(MEMBER[0])
    interaction = gateway.interaction(guild, member)

    asyncio.run(getattr(bot, command).callback(interaction, *((member,) if args == "member" else args)))

    if not interaction.sent or not interaction.texts()[0].startswith(reply):
        pytest.fail(f"setup: expected a reply starting {reply!r}, got {interaction.sent!r}")
    assert interaction.deferred is not None
    assert interaction.deferred.get("ephemeral") is True
    assert [kwargs.get("ephemeral") for _, kwargs in interaction.sent] == [True] * len(interaction.sent)
