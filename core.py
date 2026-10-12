"""Dates and rendering for bot.py (#32). Every date the bot computes comes from here: the next
birthday, the windows of /birthday upcoming and the monthly post, ages, the birth-year range,
the stored forms of a birthday and how replies show dates; the times, time zones and local dates
of the daily greetings (#34); and the splitting of long replies.

Plain functions of their arguments: this module imports neither discord nor redis and never
reads the clock. bot.py reads today's date, or the time, and passes it in.
"""

import calendar
import datetime
import re
import zoneinfo

# Discord rejects messages longer than this.
MESSAGE_LIMIT = 2000

# A birth year may be at most this many years before the current year.
MAX_AGE_YEARS = 120

# The year that stands in for a birthday given without one: a leap year, so Feb 29 is a date.
LEAP_YEAR = 2000


# Split a header and lines into messages of at most MESSAGE_LIMIT characters,
# never splitting a line. Every line is a mention and a date, far below the limit.
def chunk_messages(header, lines):
    chunks = [header]
    for line in lines:
        if len(chunks[-1]) + 1 + len(line) > MESSAGE_LIMIT:
            chunks.append(line)
        else:
            chunks[-1] += "\n" + line
    return chunks


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


# A time of day as /birthday config takes it: 24-hour "HH:MM", such as "09:00" or "9:00". None for
# anything else.
def parse_time_of_day(text):
    match = re.fullmatch(r"([01]?[0-9]|2[0-3]):([0-5][0-9])", text.strip())
    return None if match is None else datetime.time(int(match[1]), int(match[2]))


# A time of day as the bot stores and shows it: "09:00".
def format_time_of_day(time_of_day):
    return time_of_day.strftime("%H:%M")


# The time zone with this IANA name, such as "Europe/Berlin", or None if the host's time zone
# database has none by that name.
def time_zone(name):
    try:
        return zoneinfo.ZoneInfo(name.strip())
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return None


# The date in `zone` at `now`, an aware datetime, once the clocks there show `greeting_time` or
# later that day; None before that. The clocks are read as they show: on a day they skip
# `greeting_time` (springing forward from 02:00 to 03:00 skips 02:30), it comes when they show
# 03:00; on a day they show it twice (falling back), it comes the first time.
def greeting_date(now, greeting_time, zone):
    local = now.astimezone(zone)
    return local.date() if local.time() >= greeting_time else None


# Whether a birthday with this month and day falls on `date`: next_occurrence()'s rule, so a
# Feb 29 birthday falls on Feb 28 in years without Feb 29.
def falls_on(month, day, date):
    return next_occurrence(month, day, date) == date


# The date a daily greeting's marker in Redis names: "YYYY-MM-DD".
def day_key(date):
    return date.isoformat()
