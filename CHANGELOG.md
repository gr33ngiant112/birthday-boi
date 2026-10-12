# Changelog

All notable changes to birthday-boi are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-10-12

The first release. It replaces the Heroku-era bot with one that keeps each server's birthdays to
that server, asks before showing anyone's birth year or age, and runs from a Docker image.

### Added

- `/birthday` commands: `set` (month, day, an optional year and `show_age`), `show`, `list`,
  `upcoming` (the next 1 to 366 days, 30 by default) and `forget`. They work only in servers, and
  every reply is visible only to the member who asked (#62).
- `/birthday forget` deletes everything the bot stores about a member. When the bot leaves a
  server, it removes that server's list and the dates of members who were only there (#44, #62).
- PRIVACY.md: what the bot stores, who sees it and how to delete it (#44).
- A Docker image that runs the bot as an unprivileged user. `.env` files never enter the image
  (#55).
- `.env.example`, and a one-line error that names `DISCORD_TOKEN` or `REDIS_URL` when either is
  missing or empty, before the bot contacts Redis or Discord (#61).
- The bot checks Redis with a PING at startup and exits with the reason if Redis does not answer
  (#48).
- `DEV_GUILD_ID` copies the commands to a test server at every start, and `compose.dev.yaml` runs
  a local Redis for development (#52, #57).

### Changed

- The five commands `/set_birthday`, `/get_birthday`, `/list_birthdays`, `/forecast_birthdays`
  and `/forget_birthday` are now `/birthday set`, `show`, `list`, `upcoming` and `forget`. After
  deploying this release, run `python bot.py --sync` once so Discord replaces the old commands
  (#62).
- The birth year is optional, and the year and age are shown only to members who opted in with
  `show_age`. Dates read "December 25", or "December 25, 1990" with the opt-in (#62).
- Birthdays are kept per server: each server's lists, lookups and monthly post include only the
  members who set their birthday there (#43).
- The monthly post goes out on the 1st at 15:00 UTC, after the bot is ready, once per server per
  month (#50). It shows each member's month and day, never a year or age (#43).
- The bot syncs its commands to every server only when started with `--sync` (#52).
- The invite link asks for 3072 (View Channels and Send Messages) instead of 68608 (#62).
- Python 3.12, with every dependency pinned in `uv.lock` (#56, #62).

### Removed

- The @mention chat parser for setting and asking birthdays in chat, and with it spaCy (#62).
- Heroku's release script and the Procfile's release line (#39), and `set-env.sh` (#61).

### Fixed

- Replies no longer ping anyone, the commands no longer work in DMs, and no birth date appears
  in a public chat reply (#42).
- February 29 birthdays come once a year, on February 28 in other years, and birth years in the
  future or more than 120 years ago are refused (#47).
- A failed save or read is reported as such, instead of as saved or as no birthday set (#48).
- Plain `redis://` URLs work (#46).
- A restart near the 1st no longer skips or repeats that month's post (#50).

### Security

- `rediss://` connections check the Redis server's certificate and hostname, unless
  `REDIS_TLS_INSECURE=1` turns that off for a server with a self-signed certificate (#46).
- Dropping spaCy removed setuptools and its advisory (GHSA-h35f-9h28-mq5c) from the dependencies
  (#62).

[Unreleased]: https://github.com/gr33ngiant112/birthday-boi/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/gr33ngiant112/birthday-boi/releases/tag/v0.1.0
