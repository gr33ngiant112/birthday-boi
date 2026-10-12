# ADR 0001: Set birthdays through slash commands only, and remove the @mention date parser

- Status: accepted, 2026-10-05. The audit's recommended option was applied as the default, and the owner may still change it (project board card #115).
- Issues: [#30](https://github.com/gr33ngiant112/birthday-boi/issues/30) (remove spaCy and the free-text parser), [#16](https://github.com/gr33ngiant112/birthday-boi/issues/16) (chat dates that fail to parse)

## Context

A member can set a birthday in two ways. One is `/set_birthday`, a slash command with a date option. The other is mentioning the bot in a server ("@Birthday Boi my birthday is ..."). For the mention, `on_message` in `bot.py` looks for a date with spaCy and then tries a fixed list of `strptime` formats. Since [#8](https://github.com/gr33ngiant112/birthday-boi/issues/8), a chat lookup only points to `/get_birthday` and shows no dates.

- The chat parser failed on about half of the everyday phrasings in the audit's test sets (#16, #30). It also accepted the year 2999, which the birth-year range check now refuses. In the audit's run, `dateutil.parser.parse(fuzzy=True)` scored 18/18 (#30).
- spaCy serves only this parser, but it constrains the whole build. spaCy 3.5 has no wheel for Python 3.12, so the bot stays on Python 3.11, and `pyproject.toml` has to pin `thinc`, `numpy` and `setuptools` around it. With spaCy the resolved dependencies come to about 44 packages; without it, about 14.
- In the audit's measurement, loading the spaCy model added about 0.9 s to startup and raised peak memory from 46 MB to 160 MB.

## Options

- A: delete the @mention set and lookup handler and spaCy. Birthdays are set and read only through slash commands.
- B: keep only "set" by chat, parsed with `dateutil`, in servers only, with the birth-year range check and a confirmation button before saving.
- Replacing the parser with a language model was ruled out: a deterministic parser measured more accurate here (#16).

## Decision

Option A, as #30 recommends.

## Consequences

- The handler and spaCy are removed in Phase 4 (board card #39, item 1). #16 closes when the handler is gone.
- Python can move to 3.12 or later, and the dependency lock ([#17](https://github.com/gr33ngiant112/birthday-boi/issues/17)) drops spaCy and the packages pinned around it.
- A member who used chat to set a birthday uses `/set_birthday`. Stored birthdays are not changed.
- If the owner picks B instead, this ADR is superseded by one that records B, and Phase 4 replaces the parser instead of removing it.
