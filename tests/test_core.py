"""#32: properties of core.py, the bot's dates and rendering, checked with hypothesis.

The expected answers come from scanning the calendar day by day, the slow way, with only the
rule core.py documents: a Feb 29 birthday falls on Feb 28 in years without Feb 29. #13's leap-day
birthdays and #22's upcoming window go through the helpers tested here.
"""

import calendar
import datetime

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

import bot
import core

ONE_DAY = datetime.timedelta(days=1)
# Every month and day a birthday can have: the days of a leap year, so Feb 29 is one of them.
MONTH_DAYS = [(date.month, date.day) for date in (datetime.date(2000, 1, 1) + n * ONE_DAY for n in range(366))]
# Feb 29 about half the time, so most runs check the leap-day rule, not 1 draw in 366.
month_days = st.one_of(st.just((2, 29)), st.sampled_from(MONTH_DAYS))
# Today, anywhere from 1900 to 2100 (#32).
todays = st.dates(min_value=datetime.date(1900, 1, 1), max_value=datetime.date(2100, 12, 31))
# Display names a member controls: pings, a role mention, a code-block breakout, right-to-left and
# zero-width characters, emoji and a line break. Discord allows 32 characters.
HOSTILE_NAMES = [
    "@everyone",
    "@here",
    "<@&300000000000006500>",
    "```@here```",
    "\u202eesrever",
    "\u200b" * 32,
    "🎂" * 32,
    "line\nbreak",
    "**bold** ~~struck~~ ||spoiler||",
]
names = st.one_of(st.sampled_from(HOSTILE_NAMES), st.text(max_size=32))
# The headers of the two lists bot.py splits into messages.
UPCOMING_HEADER = "🎉 **Upcoming Birthdays:**"
SERVER_HEADER = "🎉 **Server Birthdays:**"
# Lines up to Discord's limit: a name, or a name repeated until the line is up to 2000 characters.
line_texts = st.one_of(
    names,
    st.builds(lambda name, length: (name * length)[:length], names.filter(bool), st.integers(1, core.MESSAGE_LIMIT)),
)


def falls_on(month, day, date):
    """Whether a birthday on this month and day falls on date: the same month and day, or Feb 28
    for a Feb 29 birthday in a year without Feb 29."""
    if (date.month, date.day) == (month, day):
        return True
    return (month, day) == (2, 29) and (date.month, date.day) == (2, 28) and not calendar.isleap(date.year)


def first_day_on_or_after(month, day, today, last):
    """The first date from today to last, both included, that the birthday falls on, or None."""
    date = today
    while date <= last:
        if falls_on(month, day, date):
            return date
        date += ONE_DAY
    return None


@settings(max_examples=300, deadline=None)
@given(month_day=month_days, today=todays)
# Feb 29 in 1900 and 2100, which have none, and in 2000, which has one; from Mar 1 to the next one.
@example(month_day=(2, 29), today=datetime.date(1900, 1, 1))
@example(month_day=(2, 29), today=datetime.date(2100, 1, 1))
@example(month_day=(2, 29), today=datetime.date(2000, 1, 1))
@example(month_day=(2, 29), today=datetime.date(2027, 3, 1))
@example(month_day=(1, 1), today=datetime.date(2100, 12, 31))
def test_next_occurrence_is_on_the_birthday_within_a_year_from_today(month_day, today):
    month, day = month_day

    occurrence = core.next_occurrence(month, day, today)

    assert today <= occurrence < today + 366 * ONE_DAY
    assert falls_on(month, day, occurrence)


@settings(max_examples=150, deadline=None)
@given(birthdays=st.lists(month_days, max_size=8), today=todays, days=st.integers(0, bot.UPCOMING_MAX_DAYS))
# Dec 25 is 90 days after Sep 26, 2026, and Dec 26 91; a birthday today with days=0; and Feb 28, 2027
# for Feb 29, one day away.
@example(birthdays=[(12, 25), (12, 26)], today=datetime.date(2026, 9, 26), days=90)
@example(birthdays=[(9, 26)], today=datetime.date(2026, 9, 26), days=0)
@example(birthdays=[(2, 29)], today=datetime.date(2027, 2, 27), days=1)
def test_upcoming_selection_equals_a_scan_of_the_next_days(birthdays, today, days):
    # /birthday upcoming lists each member whose birthday is 0 to `days` days away.
    selected = {}
    for member, (month, day) in enumerate(birthdays):
        occurrence = core.upcoming_occurrence(month, day, today, days)
        if occurrence is not None:
            selected[member] = occurrence

    scanned = {}
    for member, (month, day) in enumerate(birthdays):
        first = first_day_on_or_after(month, day, today, today + days * ONE_DAY)
        if first is not None:
            scanned[member] = first

    assert selected == scanned


@settings(max_examples=150, deadline=None)
@given(
    month_day=month_days,
    first=st.builds(lambda year, month: datetime.date(year, month, 1), st.integers(1900, 2100), st.integers(1, 12)),
)
# December's post reaches into the next year; Feb 29 falls on Feb 28 in 2027 and 2100.
@example(month_day=(2, 28), first=datetime.date(2026, 12, 1))
@example(month_day=(3, 1), first=datetime.date(2026, 12, 1))
@example(month_day=(2, 29), first=datetime.date(2027, 2, 1))
@example(month_day=(2, 29), first=datetime.date(2100, 1, 1))
def test_monthly_post_lists_the_birthdays_up_to_the_end_of_the_month_after_next(month_day, first):
    # The post goes out on the 1st and covers that month and the next two.
    month, day = month_day
    months = first.year * 12 + first.month - 1 + 3
    last = datetime.date(months // 12, months % 12 + 1, 1) - ONE_DAY

    assert core.is_monthly_post_day(first)
    assert core.monthly_post_occurrence(month, day, first) == first_day_on_or_after(month, day, first, last)


@settings(max_examples=150, deadline=None)
@given(header=st.sampled_from([UPCOMING_HEADER, SERVER_HEADER]), lines=st.lists(line_texts))
# A line that fills the header's message exactly, one that is a character too long for it, and two
# lines of the full 2000 characters.
@example(header=UPCOMING_HEADER, lines=["x" * (core.MESSAGE_LIMIT - len(UPCOMING_HEADER) - 1)])
@example(header=UPCOMING_HEADER, lines=["x" * (core.MESSAGE_LIMIT - len(UPCOMING_HEADER))])
@example(header=SERVER_HEADER, lines=["x" * core.MESSAGE_LIMIT, "y" * core.MESSAGE_LIMIT])
def test_every_chunk_fits_discords_limit_and_keeps_every_line_in_order(header, lines):
    chunks = core.chunk_messages(header, lines)

    assert [len(chunk) for chunk in chunks if len(chunk) > core.MESSAGE_LIMIT] == []
    assert "\n".join(chunks) == "\n".join([header, *lines])


# #37: Discord's limits on embeds (Embed Limits, https://docs.discord.com/developers/resources/message),
# written out here rather than read from core.py.
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
# All the embeds of a message together, titles and descriptions included.
EMBEDS_TOTAL_LIMIT = 6000
# The titles of the two lists bot.py sends as embeds.
EMBED_TITLES = ["🎉 Upcoming Birthdays", "🎉 Server Birthdays"]
# Lines up to the description's limit: a name, or a name repeated until the line is up to 4096 characters.
description_lines = st.one_of(
    names,
    st.builds(
        lambda name, length: (name * length)[:length], names.filter(bool), st.integers(1, EMBED_DESCRIPTION_LIMIT)
    ),
)


@settings(max_examples=150, deadline=None)
@given(title=st.sampled_from(EMBED_TITLES), lines=st.lists(description_lines))
# No lines; two lines that fill one description exactly (2047 + the newline + 2048 = 4096); two that
# are a character too long for one; and two full descriptions, 8192 characters, which no message holds.
@example(title=EMBED_TITLES[0], lines=[])
@example(title=EMBED_TITLES[0], lines=["x" * 2047, "y" * 2048])
@example(title=EMBED_TITLES[1], lines=["x" * 2048, "y" * 2048])
@example(title=EMBED_TITLES[1], lines=["x" * EMBED_DESCRIPTION_LIMIT, "y" * EMBED_DESCRIPTION_LIMIT])
def test_every_embed_fits_discords_limits_and_keeps_every_line_in_order(title, lines):
    embeds = core.chunk_embeds(title, lines)

    titles = [embed_title or "" for embed_title, _ in embeds]
    descriptions = [description for _, description in embeds]
    # One embed per message, so a message's embeds hold one title, or none, and one description.
    assert [len(text) for text in descriptions if len(text) > EMBED_DESCRIPTION_LIMIT] == []
    assert [len(text) for text in titles if len(text) > EMBED_TITLE_LIMIT] == []
    assert [
        len(t) + len(d) for t, d in zip(titles, descriptions, strict=True) if len(t) + len(d) > EMBEDS_TOTAL_LIMIT
    ] == []
    # The title on the first embed only (no lines, no embeds), and every line once, in order.
    assert titles == ([title] + [""] * (len(embeds) - 1) if lines else [])
    assert "\n".join(descriptions) == "\n".join(lines)


def test_an_embed_holds_lines_up_to_4096_characters_and_never_splits_one():
    # 2047 + the newline + 2048 is 4096 characters: one embed. A character more takes a second.
    assert core.chunk_embeds("T", ["x" * 2047, "y" * 2048]) == [("T", "x" * 2047 + "\n" + "y" * 2048)]
    assert core.chunk_embeds("T", ["x" * 2048, "y" * 2048]) == [("T", "x" * 2048), (None, "y" * 2048)]
    assert core.chunk_embeds("T", []) == []


@settings(max_examples=300, deadline=None)
@given(month=st.integers(1, 12), day=st.integers(1, 31), year=st.none() | st.integers(1900, 2100))
@example(month=2, day=29, year=None)
@example(month=2, day=29, year=1900)
@example(month=2, day=29, year=2000)
@example(month=4, day=31, year=None)
@example(month=2, day=30, year=None)
def test_a_set_date_is_stored_and_read_back_or_refused_if_it_does_not_exist(month, day, year):
    # /birthday set's options: Discord sends any day from 1 to 31 and an optional year.
    if day > calendar.monthrange(core.LEAP_YEAR if year is None else year, month)[1]:
        with pytest.raises(ValueError):
            core.birthday_as_date(month, day, year)
        return

    stored = core.stored_birthday(core.birthday_as_date(month, day, year), with_year=year is not None)

    assert core.parse_birthday(stored) == (month, day, year)


@settings(max_examples=300, deadline=None)
@given(month_day=month_days, date=todays)
# Feb 29 falls on Feb 28 in 2027 and 2100, which have no Feb 29, and not in 2028, which has one;
# the year's last and first days.
@example(month_day=(2, 29), date=datetime.date(2027, 2, 28))
@example(month_day=(2, 29), date=datetime.date(2100, 2, 28))
@example(month_day=(2, 29), date=datetime.date(2028, 2, 28))
@example(month_day=(2, 29), date=datetime.date(2028, 2, 29))
@example(month_day=(2, 28), date=datetime.date(2027, 2, 28))
@example(month_day=(12, 31), date=datetime.date(2026, 12, 31))
@example(month_day=(1, 1), date=datetime.date(2027, 1, 1))
def test_a_daily_greeting_falls_on_the_birthday_or_feb_28_for_feb_29(month_day, date):
    # #34: the day the daily greeting names a member.
    month, day = month_day

    assert core.falls_on(month, day, date) == falls_on(month, day, date)


@settings(max_examples=100, deadline=None)
@given(month_day=month_days, year=st.integers(1900, 2100))
@example(month_day=(2, 29), year=1900)
@example(month_day=(2, 29), year=2000)
@example(month_day=(2, 29), year=2027)
def test_every_birthday_gets_one_daily_greeting_a_year(month_day, year):
    month, day = month_day
    days_in_year = 366 if calendar.isleap(year) else 365
    dates = [datetime.date(year, 1, 1) + n * ONE_DAY for n in range(days_in_year)]

    assert len([date for date in dates if core.falls_on(month, day, date)]) == 1


def test_every_time_of_day_is_read_from_hh_mm_and_stored_as_it():
    # /birthday config's time option, with or without the hour's leading zero.
    for hour in range(24):
        for minute in range(60):
            for text in (f"{hour:02d}:{minute:02d}", f"{hour}:{minute:02d}"):
                time_of_day = core.parse_time_of_day(text)
                assert time_of_day == datetime.time(hour, minute), text
                assert core.format_time_of_day(time_of_day) == f"{hour:02d}:{minute:02d}"


@settings(max_examples=300, deadline=None)
@given(text=st.one_of(st.text(max_size=8), st.from_regex(r"\s?[0-9]{1,3}:[0-9]{1,3}\s?", fullmatch=True)))
@example(text="24:00")
@example(text="09:60")
@example(text="٠٩:٣٠")  # Arabic-Indic digits
@example(text="9:5")
def test_a_time_of_day_is_read_only_from_24_hour_hh_mm(text):
    time_of_day = core.parse_time_of_day(text)

    assert time_of_day is None or core.format_time_of_day(time_of_day) == text.strip().zfill(5)
