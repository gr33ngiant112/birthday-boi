# birthday-boi

## A Discord bot that remembers your birthday and everyone elses.

### Commands

The commands work in servers only, never in DMs, and only the member who runs one sees the answer.

- `/birthday set month day [year] [show_age]`: set your birthday and share it with this server. Pick the month from the list and type the day; the year is optional. Others see only the month and day, unless you give the year and turn on `show_age`.
- `/birthday show member`: a member's birthday, if they shared it with this server. For a member who gave the year and turned on `show_age`, it also shows the year and the age they turn next.
- `/birthday list`: the birthdays shared with this server, with the year for members who gave it and turned on `show_age`.
- `/birthday upcoming [days]`: the birthdays from today to 30 days ahead, or to `days` days ahead (1 to 366), with the age they turn for members who gave the year and turned on `show_age`.
- `/birthday forget`: delete your birthday from every server.
- `/birthday config [channel] [time] [timezone] [off]`, for members with the Manage Server permission: turn on the server's daily birthday greetings, or change them, with all three of `channel`, `time` (24-hour `HH:MM`, such as `09:00`) and `timezone` (a name from the tz database, such as `Europe/Berlin` or `America/New_York`). `off: True` turns them off. Discord cannot hide a subcommand from members without a permission, so every member sees this one, and the bot refuses it to members without Manage Server.

On the 1st of each month at 15:00 UTC, the bot posts the birthdays of that month and the next two in each server's #general, with the month and day only.

The daily greetings are off until someone with Manage Server runs `/birthday config` in the server. Then, every day at the server's time in its time zone, the bot posts "🎂 Happy birthday, @member!" in the server's channel for each member who shared their birthday with the server and has it that day, and pings those members and nobody else. The greeting shows no year or age. A Feb 29 birthday is greeted on Feb 28 in years without Feb 29. The bot checks every 5 minutes, so a greeting goes out up to 5 minutes after its time, and a bot that was down then greets later that day. A server gets one greeting a day, however often the bot restarts. When the bot leaves a server, it deletes the server's greeting settings.

### Developing

1. Install [uv](https://docs.astral.sh/uv/getting-started/installation/), and [Docker](https://docs.docker.com/engine/install/) with the Compose plugin for the local Redis. Python comes with uv: it uses Python 3.12 (`.python-version`) and downloads it if this machine doesn't have it.
2. In your clone of this repository, create the virtual environment `.venv` with the packages that `uv.lock` pins, the dev tools included, and activate it:

```bash
uv sync --locked
source .venv/bin/activate
```

3. Run `pre-commit install`. Before each commit, the hooks run `ruff check`, `ruff format --check` and the command registry test with this venv's ruff and pytest, so commit with the venv active. `ruff format .` fixes what the format check finds.
4. Run the tests with `pytest -q`. They need no network, Redis or Discord token.

### Building and testing the Docker image

The `Dockerfile` builds the bot's image. CI builds it and runs the lint and the tests inside it. To do the same locally:

```bash
docker build --tag birthday-boi .
docker build --target test --tag birthday-boi:test .
docker run --rm --network none birthday-boi:test
```

The first command builds the bot's image. The second builds the test image: the bot's image plus the dev tools and the tests. The third runs `ruff check`, `ruff format --check` and the tests in a container with no network, and exits with a non-zero status if any of them fails. `.dockerignore` keeps `.env` files out of the build context, so they never reach an image.

### Installing

1. Create an application in the Discord [Developer Portal](https://discord.com/developers/applications). For development, use a separate application, as "Running against a test server" explains.
2. On the application's Bot page, choose Reset Token and copy the token: it goes in `DISCORD_TOKEN` (see "Running"). Leave the privileged intents off.
3. Invite the bot to a server, as below.

### Inviting the bot

Invite the bot with only the access it uses. Replace `<CLIENT_ID>` with your application's client ID from the Developer Portal:

```
https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot%20applications.commands&permissions=3072
```

- Scopes: `bot` and `applications.commands` (the slash commands).
- Permissions: View Channels and Send Messages (`3072`), for the monthly list in #general and the daily greetings in the channel a server picks. The answers to slash commands need no channel permission.
- Never grant Administrator or Mention Everyone; the bot needs neither.
- On the Developer Portal's Bot page, leave the Message Content and Server Members privileged intents off. `bot.py` uses `discord.Intents.default()`, which requests neither.

### Running

`python bot.py` reads its settings from the environment, after adding those in a `.env` file if there is one; a variable already set in the environment wins over `.env`. `cp .env.example .env` gives you one to fill in:

- `DISCORD_TOKEN`, required: the bot's token, from the application's Bot page.
- `REDIS_URL`, required: where Redis listens. `redis://127.0.0.1:6379/0` is the local Redis from `compose.dev.yaml`.
- `DEV_GUILD_ID`, optional: a test server's ID (see "Running against a test server").
- `REDIS_TLS_INSECURE`, optional, for `rediss://` URLs only: the bot checks the server's certificate unless this is `1`. A server with a self-signed certificate, such as Heroku's, needs `REDIS_TLS_INSECURE=1`, or the certificate check fails and the bot does not start.
- `HEARTBEAT_FILE`, optional: the file the bot touches every minute while it is connected to Discord, which `python bot.py --healthcheck` checks (see "Deploying"). `/tmp/birthday-boi.heartbeat` when unset.

If `DISCORD_TOKEN` or `REDIS_URL` is missing or empty, the bot exits with status 1 and a line that names it, before it contacts Redis or Discord. Before it logs in to Discord, the bot sends Redis a PING. If Redis does not answer, the bot exits with status 1 and prints the reason, so start Redis first and check `REDIS_URL`.

### Running against a test server

Use a separate Discord application for development, and keep its token as the only token in your local `.env`. The bot loads `.env` itself, so a local run, a test or a tool can use any token in it. Never put the production token there.

1. Run `cp .env.example .env`. In `.env`, set `DISCORD_TOKEN` to the development application's token and `DEV_GUILD_ID` to your test server's ID; `REDIS_URL` already points at the Redis of the next step. To copy the ID, turn on Developer Mode in Discord's Advanced settings, right-click the server and choose Copy Server ID.
2. Start a local Redis with `docker compose -f compose.dev.yaml up -d`. It listens on 127.0.0.1:6379 only. To use another host port, set `REDIS_PORT` in `.env`, where Compose reads it too, and use the same port in `REDIS_URL`.
3. Run `python bot.py` with the venv active. The bot copies its slash commands to the `DEV_GUILD_ID` server and syncs them there, where they show up at once.

`docker compose -f compose.dev.yaml down` stops Redis; add `-v` to delete its data.

### Deploying

`compose.yaml` runs the bot and its own Redis on one host, as the Compose project `birthday-boi-prod`, apart from `compose.dev.yaml`'s development Redis. On the host, in a clone of this repository used only for deploying (a development clone's `.env` holds the development token):

1. Run `cp .env.example .env` and set `DISCORD_TOKEN` to the production application's token. Compose sets `REDIS_URL` to the stack's Redis, which wins over the one in `.env`. `.env` stays on the host: `.dockerignore` keeps it out of the image.
2. Run `docker compose up -d --build`. The bot starts once Redis answers its healthcheck, and both restart unless you stop them. Redis keeps its data in the `birthday-boi-prod_redis-data` volume and publishes no port on the host.
3. `docker compose ps` shows the bot's health in its status: `health: starting` until its first heartbeat, then `healthy`. The bot touches a heartbeat file every minute while it is connected to Discord. The image's HEALTHCHECK, `python bot.py --healthcheck`, fails when that file is missing or 3 minutes old, and after 3 failed checks in a row the status is `unhealthy`. `docker inspect --format '{{json .State.Health}}' birthday-boi-prod-bot-1` shows the latest checks' output, and `docker compose logs bot` the bot's log.
4. After a release that changes the slash commands, sync them once (see "Updating the slash commands"): run `docker compose stop bot`, then `docker compose run --rm bot python bot.py --sync`. When its log shows `Slash commands synced globally`, stop it with Ctrl+C and run `docker compose start bot`.

To update the bot, pull the new version and run `docker compose up -d --build` again. `docker compose down` stops the stack and keeps the data; `docker compose down -v` deletes it.

### Updating the slash commands

Without `--sync`, a start changes the slash commands only in the `DEV_GUILD_ID` server, if one is set. After a release that adds, removes or changes a slash command, start the bot once with `python bot.py --sync`: it replaces the commands in every server with this build's, then runs as usual. A command missing from the build disappears from every server, so check the command list before you sync. Don't use `--sync` with the development token: the test server would then list each command twice, once from the global sync and once from `DEV_GUILD_ID`.

The `/birthday` group renamed every command: it replaced `/set_birthday`, `/get_birthday`, `/list_birthdays`, `/forecast_birthdays` and `/forget_birthday`. So after deploying the first version with `/birthday`, start the bot once with `python bot.py --sync`. Until then, every server keeps offering the old commands, which the bot no longer answers, and no server offers `/birthday`.

### Privacy

[PRIVACY.md](PRIVACY.md) describes what the bot stores, who can see it, and how to delete it. To delete your birthday, run `/birthday forget` in any server the bot shares with you.
