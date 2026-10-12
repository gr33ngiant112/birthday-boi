"""Dates and rendering for bot.py (#32). Every date the bot computes comes from here: the next
birthday, the windows of /birthday upcoming and the monthly post, ages, the birth-year range,
the stored forms of a birthday and how replies show dates; and the splitting of long replies
into messages and embeds (#37).

Plain functions of their arguments: this module imports neither discord nor redis and never
reads the clock. bot.py reads today's date and passes it in.
"""

import calendar
import datetime

# Discord rejects messages longer than this.
MESSAGE_LIMIT = 2000

# Discord rejects an embed whose description is longer than this, and a message whose embeds hold
# more than 6000 characters together (https://docs.discord.com/developers/resources/message,
# "Embed Limits"). Two full descriptions exceed 6000, so a reply sends one embed per message: its
# title (Discord allows 256 characters) and description stay far below 6000.
EMBED_DESCRIPTION_LIMIT = 4096

# A birth year may be at most this many years before the current year.
MAX_AGE_YEARS = 120

# The year that stands in for a birthday given without one: a leap year, so Feb 29 is a date.
LEAP_YEAR = 2000


# Join lines, one per row, into chunks of at most `limit` characters, never splitting a line.
# Every line is a mention and a date, far below the limits.
def chunk_lines(lines, limit):
    chunks = []
    for line in lines:
        if chunks and len(chunks[-1]) + 1 + len(line) <= limit:
            chunks[-1] += "\n" + line
        else:
            chunks.append(line)
    return chunks


# Split a header and lines into messages of at most MESSAGE_LIMIT characters,
# never splitting a line.
def chunk_messages(header, lines):
    return chunk_lines([header, *lines], MESSAGE_LIMIT)


# Split a title and lines into the embeds of a reply, one per message: (title, description) pairs,
# with the title on the first only and descriptions of at most EMBED_DESCRIPTION_LIMIT characters,
# never splitting a line. No lines, no embeds.
def chunk_embeds(title, lines):
    descriptions = chunk_lines(lines, EMBED_DESCRIPTION_LIMIT)
    return [(title if index == 0 else None, description) for index, description in enumerate(descriptions)]


# The first date on or after today with this month and day. A Feb 29 birthday falls
# on Feb 28 in years without Feb 29, so it still comes once a year.
def next_occurrence(month, day, today):
    # This year's date if it has not passed yet, otherwise next year's.
    year = today.year if (month, day) >= (today.month, today.day) else today.year + 1
    if (month, day) == (2, 29) and not calendar.isleap(year):
        day = 28
    return datetime.date(year, month, day)


# The next birthday with this month and day if it is at most `days` days after today, the
# window of /birthday upcoming; otherwise None.
def upcoming_occurrence(month, day, today, days):
    occurrence = next_occurrence(month, day, today)
    # next_occurrence() is today or later, so this keeps 0 to `days` days away.
    return occurrence if (occurrence - today).days <= days else None


# The monthly post goes out on the 1st of each month.
def is_monthly_post_day(today):
    return today.day == 1


# The next birthday with this month and day if the post of `today` lists it, that is if it
# falls in today's month or one of the next two; otherwise None.
def monthly_post_occurrence(month, day, today):
    occurrence = next_occurrence(month, day, today)
    next_month = (today.month % 12) + 1
    month_after = ((today.month + 1) % 12) + 1
    return occurrence if occurrence.month in [today.month, next_month, month_after] else None


# The age a member born in `year` turns on `occurrence`, one of their birthdays.
def age_on(year, occurrence):
    return occurrence.year - year


# Why a date cannot be a birth date, or None if it can.
def birth_date_error(birthday_date, today):
    if birthday_date > today:
        return "❌ That date is in the future. Please check the year and try again."
    if birthday_date.year < today.year - MAX_AGE_YEARS:
        return f"❌ That date is more than {MAX_AGE_YEARS} years ago. Please check the year and try again."
    return None


# The date of a birthday given as a month, a day and an optional year. Without a year,
# LEAP_YEAR stands in, so Feb 29 is a date. Raises ValueError when the month has no such day,
# or the year no Feb 29.
def birthday_as_date(month, day, year=None):
    return datetime.date(LEAP_YEAR if year is None else year, month, day)


# How a birthday is stored: "YYYY-MM-DD", or ISO 8601's date without a year, "--MM-DD", when
# the member gave none. parse_birthday() reads both.
def stored_birthday(birthday_date, with_year):
    return birthday_date.isoformat() if with_year else birthday_date.strftime("--%m-%d")


# The (month, day, year) of a stored birthday, "YYYY-MM-DD" or ISO 8601's year-less "--MM-DD",
# whose year is None. Raises ValueError for anything else.
def parse_birthday(birthday):
    if birthday.startswith("--"):
        date = datetime.date.fromisoformat(f"{LEAP_YEAR}{birthday[1:]}")
        return date.month, date.day, None
    date = datetime.date.fromisoformat(birthday)
    return date.month, date.day, date.year


# A birthday as the replies show it: "December 25", or "December 25, 1990" with a year, which
# callers pass only when the member opted in with show_age.
def format_birthday(month, day, year=None):
    month_and_day = datetime.date(LEAP_YEAR, month, day).strftime("%B %d")
    return month_and_day if year is None else f"{month_and_day}, {year}"


# A date a birthday falls on, as the replies show it: "Friday, December 25", or with its year,
# "Friday, December 25 2026".
def format_occurrence(occurrence, with_year=False):
    return occurrence.strftime("%A, %B %d %Y" if with_year else "%A, %B %d")


# The month a monthly post's marker in Redis names: "YYYY-MM".
def month_key(today):
    return today.strftime("%Y-%m")
