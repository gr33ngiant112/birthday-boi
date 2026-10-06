"""#9: /forget_birthday deletes a member's birthday from every guild, and when the bot
leaves a guild it deletes that guild's set and any birthday no other guild can see.

Setting a birthday also adds the guild to the member's list of guilds,
user:<id>:guilds, so neither path needs KEYS or SCAN to find a member's guilds.
"""

import asyncio
import datetime

import pytest

import bot

GUILD_A = 300000000000010000
GUILD_B = 300000000000011000
GUILD_C = 300000000000012000
SETTER = (200000000000004001, "setter")
SETTER_BIRTHDAY = datetime.date(1990, 12, 25)
CAROL = (200000000000004002, "carol")
CAROL_BIRTHDAY = datetime.date(1988, 3, 15)
ALICE = (200000000000004003, "alice-only-in-a")
BOB = (200000000000004004, "bob-only-in-b")
DELETED = "✅ Your birthday has been deleted from every server."
NOTHING_STORED = "✅ You had no birthday stored, so there was nothing to delete."
NOT_DELETED = "❌ Your birthday could not be deleted. Please try again later."


def run_command(gateway, guild, user, command, *args):
    """Run a slash command as user, a (user_id, name) member of guild; return the FakeInteraction."""
    interaction = gateway.interaction(guild, guild.get_member(user[0]))
    asyncio.run(getattr(bot, command).callback(interaction, *args))
    return interaction


def replies(interaction):
    """(content, ephemeral) for each response and follow-up, in order."""
    return [(content, kwargs.get("ephemeral")) for content, kwargs in interaction.sent]


def text_of(interaction):
    return "\n".join(content or "" for content, _ in interaction.sent)


def guilds_of(user):
    return f"user:{user[0]}:guilds"


def shown_in(gateway, fake_redis, guild, asker, member):
    """The text of each view in guild: /get_birthday about member, /list_birthdays and
    /forecast_birthdays run by asker, and the monthly post."""
    shown = {
        command: text_of(run_command(gateway, guild, asker, command, *args))
        for command, args in [
            ("get_birthday", (guild.get_member(member[0]),)),
            ("list_birthdays", ()),
            ("forecast_birthdays", ()),
        ]
    }
    gateway.sent.clear()
    # The task posts once a month in each guild (#14): without the markers of earlier runs,
    # it posts as the first run of the month does.
    fake_redis.clear_monthly_post_markers()
    asyncio.run(bot.check_upcoming_birthdays())
    shown["monthly post"] = "\n".join(payload.get("content") or "" for payload in gateway.sent_to(gateway.general(guild)))
    return shown


def change_after_the_bot_reads(monkeypatch, key, change):
    """Call change() once, as soon as bot.redis_client has the reply to SMEMBERS key.

    It stands for another command, handled at the same time, whose write lands between
    the bot's read of key and the bot's own write. Returns a list that holds True once
    change() has run.
    """
    ran = []
    pool = bot.redis_client.connection_pool
    get_connection = pool.get_connection

    async def get_connection_watching_for_the_read(*args, **kwargs):
        connection = await get_connection(*args, **kwargs)
        if "read_response" not in vars(connection):  # wrap each connection once
            send_command, read_response = connection.send_command, connection.read_response
            last_sent = []

            async def send_and_note(*command, **options):
                last_sent[:] = [command]
                return await send_command(*command, **options)

            async def read_then_change(*args, **kwargs):
                response = await read_response(*args, **kwargs)
                if last_sent == [("SMEMBERS", key)] and not ran:
                    ran.append(True)
                    change()
                last_sent.clear()
                return response

            connection.send_command = send_and_note
            connection.read_response = read_then_change
        return connection

    monkeypatch.setattr(pool, "get_connection", get_connection_watching_for_the_read)
    return ran


def test_setting_a_birthday_adds_the_guild_to_the_members_guilds(fake_redis, gateway):
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [SETTER])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [SETTER])

    set_in_a = run_command(gateway, guild_a, SETTER, "set_birthday", "12-25-1990")
    message = gateway.message(
        guild_b, SETTER, f"<@{gateway.bot_user.id}> my birthday is December 25 1990", mentions=[gateway.mention_bot()]
    )
    asyncio.run(bot.on_message(message))

    chat_replies = [payload.get("content") for payload in gateway.sent_to(gateway.general(guild_b))]
    if text_of(set_in_a) != "✅ Your birthday has been set to 12-25-1990.":
        pytest.fail(f"setup: /set_birthday should set the birthday, got {set_in_a.sent!r}")
    if chat_replies != ["✅ Your birthday has been updated to 12-25-1990."]:
        pytest.fail(f"setup: the chat path should set the birthday, got {chat_replies!r}")
    assert fake_redis.smembers(guilds_of(SETTER)) == {str(GUILD_A), str(GUILD_B)}
    # The other tests seed with FakeStore.seed_birthday, which must write exactly this.
    written = fake_redis.contents()
    fake_redis.flushall()
    for guild_id in (GUILD_A, GUILD_B):
        fake_redis.seed_birthday(guild_id, SETTER[0], SETTER_BIRTHDAY.isoformat())
    assert fake_redis.contents() == written


def test_forget_birthday_deletes_the_date_and_every_guild_membership(fake_redis, gateway):
    gateway.add_guild(GUILD_A, "Guild A", [SETTER, CAROL])
    gateway.add_guild(GUILD_B, "Guild B", [SETTER, CAROL])
    # SETTER never set the birthday in guild C: /forget_birthday there still reaches every guild.
    guild_c = gateway.add_guild(GUILD_C, "Guild C", [SETTER])
    for guild_id in (GUILD_A, GUILD_B):
        fake_redis.seed_birthday(guild_id, CAROL[0], CAROL_BIRTHDAY.isoformat())
    without_setter = fake_redis.contents()
    for guild_id in (GUILD_A, GUILD_B):
        fake_redis.seed_birthday(guild_id, SETTER[0], SETTER_BIRTHDAY.isoformat())

    interaction = run_command(gateway, guild_c, SETTER, "forget_birthday")

    assert replies(interaction) == [(DELETED, True)]
    # Nothing of SETTER's is left, and CAROL's birthday is untouched.
    assert fake_redis.contents() == without_setter


def test_no_view_shows_the_birthday_after_forget_birthday(fake_redis, freeze_today, gateway):
    freeze_today(datetime.date(2026, 10, 1))  # the 1st, so the monthly task posts; Dec 25 is 85 days away
    guilds = [gateway.add_guild(guild_id, "Guild", [SETTER, CAROL]) for guild_id in (GUILD_A, GUILD_B)]
    for guild in guilds:
        fake_redis.seed_birthday(guild.id, SETTER[0], SETTER_BIRTHDAY.isoformat())
    month_and_day = SETTER_BIRTHDAY.strftime("%B %d")  # every view shows it

    before = {guild.id: shown_in(gateway, fake_redis, guild, CAROL, SETTER) for guild in guilds}
    run_command(gateway, guilds[0], SETTER, "forget_birthday")
    after = {guild.id: shown_in(gateway, fake_redis, guild, CAROL, SETTER) for guild in guilds}

    if any(month_and_day not in text for shown in before.values() for text in shown.values()):
        pytest.fail(f"setup: every view should show the birthday before /forget_birthday, got {before!r}")
    assert [(guild_id, view) for guild_id, shown in after.items() for view, text in shown.items() if month_and_day in text] == []


def test_forget_birthday_with_nothing_stored_says_so(fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [SETTER, CAROL])
    fake_redis.seed_birthday(GUILD_A, CAROL[0], CAROL_BIRTHDAY.isoformat())
    before = fake_redis.contents()

    interaction = run_command(gateway, guild, SETTER, "forget_birthday")

    assert replies(interaction) == [(NOTHING_STORED, True)]
    assert fake_redis.contents() == before


def test_forget_birthday_says_so_when_it_cannot_reach_the_store(fake_redis, gateway):
    guild = gateway.add_guild(GUILD_A, "Guild A", [SETTER])
    fake_redis.seed_birthday(GUILD_A, SETTER[0], SETTER_BIRTHDAY.isoformat())
    server = fake_redis.connection_pool.connection_kwargs["server"]

    server.connected = False  # every command now raises redis.ConnectionError
    interaction = run_command(gateway, guild, SETTER, "forget_birthday")
    server.connected = True

    if fake_redis.get(f"user:{SETTER[0]}:birthday") != SETTER_BIRTHDAY.isoformat():
        pytest.fail("setup: nothing should be deleted while the store is unreachable")
    assert replies(interaction) == [(NOT_DELETED, True)]


def test_forget_birthday_also_clears_a_guild_added_while_it_runs(fake_redis, gateway, monkeypatch):
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [SETTER])
    fake_redis.seed_birthday(GUILD_A, SETTER[0], SETTER_BIRTHDAY.isoformat())
    # SETTER's /set_birthday in guild B, handled at the same time, lands after
    # /forget_birthday has read SETTER's guilds and before it deletes anything.
    ran = change_after_the_bot_reads(
        monkeypatch,
        guilds_of(SETTER),
        lambda: fake_redis.seed_birthday(GUILD_B, SETTER[0], SETTER_BIRTHDAY.isoformat()),
    )

    interaction = run_command(gateway, guild_a, SETTER, "forget_birthday")

    if not ran:
        pytest.fail("setup: the set in guild B should land after /forget_birthday reads SETTER's guilds")
    assert replies(interaction) == [(DELETED, True)]
    # Otherwise guild B's set keeps SETTER, and would show any birthday SETTER sets later, anywhere.
    assert fake_redis.contents() == {}


def test_leaving_a_guild_deletes_its_set_and_the_birthdays_no_other_guild_can_see(fake_redis, gateway):
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [SETTER, ALICE])
    guild_b = gateway.add_guild(GUILD_B, "Guild B", [SETTER, BOB])
    fake_redis.seed_birthday(GUILD_B, SETTER[0], SETTER_BIRTHDAY.isoformat())
    fake_redis.seed_birthday(GUILD_B, BOB[0], "1992-11-20")
    shared_with_b_only = fake_redis.contents()
    fake_redis.seed_birthday(GUILD_A, SETTER[0], SETTER_BIRTHDAY.isoformat())
    fake_redis.seed_birthday(GUILD_A, ALICE[0], "1990-10-15")
    if fake_redis.smembers(f"guild:{GUILD_A}:birthdays") != {str(SETTER[0]), str(ALICE[0])}:
        pytest.fail("setup: guild A's set should list SETTER and ALICE")

    # Through the client, where discord.py looks up the handler when Discord reports
    # that the bot was removed from a guild (GUILD_DELETE).
    asyncio.run(bot.client.on_guild_remove(guild_a))

    # Guild A's set is gone, and so are ALICE's date and list of guilds: no guild can
    # see them. SETTER's date stays, shared with guild B only.
    assert fake_redis.contents() == shared_with_b_only
    get_in_b = run_command(gateway, guild_b, BOB, "get_birthday", guild_b.get_member(SETTER[0]))
    assert SETTER_BIRTHDAY.strftime("%m-%d-%Y") in text_of(get_in_b)


def test_leaving_a_guild_keeps_a_birthday_set_in_another_guild_meanwhile(fake_redis, gateway, monkeypatch):
    guild_a = gateway.add_guild(GUILD_A, "Guild A", [SETTER])
    fake_redis.seed_birthday(GUILD_A, SETTER[0], SETTER_BIRTHDAY.isoformat())
    # SETTER's /set_birthday in guild B lands after the cleanup has read SETTER's
    # guilds (only A, so far) and before it deletes anything.
    ran = change_after_the_bot_reads(
        monkeypatch,
        guilds_of(SETTER),
        lambda: fake_redis.seed_birthday(GUILD_B, SETTER[0], SETTER_BIRTHDAY.isoformat()),
    )

    asyncio.run(bot.client.on_guild_remove(guild_a))

    if not ran:
        pytest.fail("setup: the set in guild B should land after the cleanup reads SETTER's guilds")
    assert fake_redis.contents() == {
        f"user:{SETTER[0]}:birthday": SETTER_BIRTHDAY.isoformat(),
        f"guild:{GUILD_B}:birthdays": {str(SETTER[0])},
        guilds_of(SETTER): {str(GUILD_B)},
    }
