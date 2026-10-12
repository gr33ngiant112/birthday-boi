"""#37: /birthday list and /birthday upcoming reply with embeds that stay within Discord's limits,
whatever the members' display names are.

Each line is `<@id> - <date>`: Discord shows the member's name, which the bot never writes, and the
client's AllowedMentions.none() goes with every message, so nobody is pinged. Discord's limits
(Embed Limits and Create Message, https://docs.discord.com/developers/resources/message): a title
of at most 256 characters, a description of at most 4096, at most 10 embeds in a message, at most
6000 characters in all the embeds of a message together, and at most 2000 characters of content.

The payloads are those FakeInteraction builds with discord.py's own handle_message_parameters(),
as discord.py would send them.
"""

import asyncio
import datetime

import pytest

import bot

GUILD_ID = 300000000000021000
ASKER = (200000000000009001, "asker")
FIRST_MEMBER_ID = 200000000000010000
# Display names a member controls: pings, a role mention, code-block breakouts, right-to-left and
# zero-width characters, emoji, a line break, markdown, and Discord's longest name, 32 characters.
HOSTILE_NAMES = [
    "@everyone",
    "@here",
    "<@&300000000000006500>",
    "```@here```",
    "‮esrever",
    "​" * 32,
    "🎂" * 32,
    "line\nbreak",
    "**bold** ~~struck~~ ||spoiler||",
    "W" * 32,
]
TITLE_LIMIT = 256
DESCRIPTION_LIMIT = 4096
EMBEDS_PER_MESSAGE = 10
EMBEDS_TOTAL_LIMIT = 6000
CONTENT_LIMIT = 2000
# Mar 15 2026 is a Sunday, 14 days away. Every view below lists every member: /birthday upcoming
# runs with 366 days, and every month and day falls within 366 days of today.
TODAY = datetime.date(2026, 3, 1)
UPCOMING_DAYS = 366


def members(count):
    """count members, (user_id, display name), each with one of HOSTILE_NAMES."""
    return [(FIRST_MEMBER_ID + i, HOSTILE_NAMES[i % len(HOSTILE_NAMES)]) for i in range(count)]


def varied_birthday(i):
    """A birth date and show_age opt-in that differ from member to member, so the lines differ in
    length: the year shows in /birthday list, and the age in /birthday upcoming, for every other one."""
    return datetime.date(1950 + i % 60, i % 12 + 1, i % 28 + 1).isoformat(), i % 2 == 0


def run_view(view, gateway, fake_redis, guild_members, birthday_of):
    """Store each member's birthday, from birthday_of(i), then run the view as ASKER in the guild,
    where the members are cached under their display names. Returns the FakeInteraction."""
    guild = gateway.add_guild(GUILD_ID, "Guild", [ASKER, *guild_members])
    for i, (user_id, _) in enumerate(guild_members):
        birthday, show_age = birthday_of(i)
        fake_redis.seed_birthday(GUILD_ID, user_id, birthday, show_age=show_age)
    interaction = gateway.interaction(guild, guild.get_member(ASKER[0]))
    args = (UPCOMING_DAYS,) if view == "birthday_upcoming" else ()
    asyncio.run(getattr(bot, view).callback(interaction, *args))
    return interaction


def assert_within_discords_limits(payload):
    embeds = payload.get("embeds", [])
    assert len(payload.get("content") or "") <= CONTENT_LIMIT
    assert len(embeds) <= EMBEDS_PER_MESSAGE
    assert [embed for embed in embeds if len(embed.get("title", "")) > TITLE_LIMIT] == []
    assert [embed for embed in embeds if len(embed.get("description", "")) > DESCRIPTION_LIMIT] == []
    assert (
        sum(len(embed.get("title", "")) + len(embed.get("description", "")) for embed in embeds) <= EMBEDS_TOTAL_LIMIT
    )


def assert_pings_nobody(payload):
    # As tests/test_privacy.py checks every reply: without the field, Discord parses every mention.
    allowed = payload.get("allowed_mentions")
    assert allowed is not None
    assert allowed.get("parse") == []
    assert not allowed.get("users")
    assert not allowed.get("roles")
    assert not allowed.get("replied_user")


def snapshot(payload):
    """The parts of a payload the member sees, and who it may ping: content, the title and
    description of each embed, and allowed_mentions."""
    shown = {key: payload[key] for key in ("content", "allowed_mentions") if key in payload}
    if "embeds" in payload:
        shown["embeds"] = [
            {key: embed[key] for key in ("title", "description") if key in embed} for embed in payload["embeds"]
        ]
    return shown


TITLES = {"birthday_list": "🎉 Server Birthdays", "birthday_upcoming": "🎉 Upcoming Birthdays"}
NO_BIRTHDAYS = {
    "birthday_list": "❌ No birthdays have been set yet.",
    "birthday_upcoming": f"❌ No birthdays in the next {UPCOMING_DAYS} days.",
}


@pytest.mark.parametrize("count", [0, 1, 40, 500])
@pytest.mark.parametrize("view", ["birthday_list", "birthday_upcoming"])
def test_the_lists_stay_within_discords_limits_name_nobody_and_ping_nobody(
    view, count, fake_redis, freeze_today, gateway
):
    freeze_today(TODAY)
    guild_members = members(count)

    interaction = run_view(view, gateway, fake_redis, guild_members, varied_birthday)

    if not interaction.payloads:
        pytest.fail("setup: the view sent nothing")
    texts = interaction.texts()
    for payload in interaction.payloads:
        assert_within_discords_limits(payload)
        assert_pings_nobody(payload)
    # Every member once, by mention; no display name, anywhere in what the member reads.
    assert {user_id: "\n".join(texts).count(f"<@{user_id}>") for user_id, _ in guild_members} == {
        user_id: 1 for user_id, _ in guild_members
    }
    assert [name for name in HOSTILE_NAMES if any(name in text for text in texts)] == []
    assert [kwargs.get("ephemeral") for _, kwargs in interaction.sent] == [True] * len(interaction.sent)
    if count == 0:
        assert [snapshot(payload) for payload in interaction.payloads] == [
            {"content": NO_BIRTHDAYS[view], "allowed_mentions": {"parse": []}}
        ]
    else:
        # One embed per message, all from the bot's own text, the title on the first.
        embeds = [embed for payload in interaction.payloads for embed in payload["embeds"]]
        assert [len(payload["embeds"]) for payload in interaction.payloads] == [1] * len(interaction.payloads)
        assert [embed.get("title") for embed in embeds] == [TITLES[view]] + [None] * (len(embeds) - 1)
        assert [payload for payload in interaction.payloads if "content" in payload] == []


@pytest.mark.parametrize(
    ("view", "line"),
    [
        pytest.param(
            "birthday_list", f"<@{FIRST_MEMBER_ID}> - March 15 - next birthday Sunday, March 15 2026", id="list"
        ),
        pytest.param("birthday_upcoming", f"<@{FIRST_MEMBER_ID}> - Sunday, March 15", id="upcoming"),
    ],
)
def test_one_member_is_one_embed_under_the_title(view, line, fake_redis, freeze_today, gateway):
    freeze_today(TODAY)

    interaction = run_view(view, gateway, fake_redis, members(1), lambda i: ("1990-03-15", False))

    assert [snapshot(payload) for payload in interaction.payloads] == [
        {"embeds": [{"title": TITLES[view], "description": line}], "allowed_mentions": {"parse": []}}
    ]


@pytest.mark.parametrize(
    ("view", "line_length", "lines_per_message"),
    [
        # 70 characters a line: 57 lines and their newlines are 4046 characters, and 58 would be 4117.
        pytest.param("birthday_list", 70, [57] * 8 + [44], id="list"),
        # 40 characters a line: 99 lines are 4058 characters, and 100 would be 4099.
        pytest.param("birthday_upcoming", 40, [99] * 5 + [5], id="upcoming"),
    ],
)
def test_500_members_fill_each_embed_up_to_4096_characters(
    view, line_length, lines_per_message, fake_redis, freeze_today, gateway
):
    # The same birthday for everyone, so every line has the same length and the split is fixed,
    # whatever order Redis returns the members in.
    freeze_today(TODAY)
    guild_members = members(500)

    interaction = run_view(view, gateway, fake_redis, guild_members, lambda i: ("1990-03-15", False))

    descriptions = [embed["description"] for payload in interaction.payloads for embed in payload["embeds"]]
    lines = [line for description in descriptions for line in description.split("\n")]
    if {len(line) for line in lines} != {line_length}:
        pytest.fail(
            f"setup: every line should be {line_length} characters, got {sorted({len(line) for line in lines})}"
        )
    assert [len(payload["embeds"]) for payload in interaction.payloads] == [1] * len(lines_per_message)
    assert [len(description.split("\n")) for description in descriptions] == lines_per_message
    assert [len(description) for description in descriptions] == [n * (line_length + 1) - 1 for n in lines_per_message]
    assert {user_id: sum(f"<@{user_id}>" in line for line in lines) for user_id, _ in guild_members} == {
        user_id: 1 for user_id, _ in guild_members
    }
