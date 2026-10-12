# birthday-boi

## A Discord bot that remembers your birthday and everyone elses.

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
https://discord.com/oauth2/authorize?client_id=<CLIENT_ID>&scope=bot%20applications.commands&permissions=68608
```

- Scopes: `bot` and `applications.commands` (the slash commands).
- Permissions: View Channels, Send Messages and Read Message History (`68608`). The bot replies to mentions and posts the monthly list in #general, and Discord requires Read Message History to reply to a message.
- Never grant Administrator or Mention Everyone; the bot needs neither.
- On the Developer Portal's Bot page, leave the Message Content and Server Members privileged intents off. `bot.py` uses `discord.Intents.default()`, which requests neither.

### Running

`python bot.py`, the Procfile's `worker` command, reads its settings from the environment; when `DYNO` is not set (a local run), it loads them from a `.env` file first. `cp .env.example .env` gives you one to fill in:

- `DISCORD_TOKEN`, required: the bot's token, from the application's Bot page.
- `REDIS_URL`, required: where Redis listens. `redis://127.0.0.1:6379/0` is the local Redis from `compose.dev.yaml`.
- `DEV_GUILD_ID`, optional: a test server's ID (see "Running against a test server").
- `REDIS_TLS_INSECURE`, optional, for `rediss://` URLs only: the bot checks the server's certificate unless this is `1`. A server with a self-signed certificate, such as Heroku's, needs `REDIS_TLS_INSECURE=1`, or the certificate check fails and the bot does not start.

If `DISCORD_TOKEN` or `REDIS_URL` is missing or empty, the bot exits with status 1 and a line that names it, before it contacts Redis or Discord. Before it logs in to Discord, the bot sends Redis a PING. If Redis does not answer, the bot exits with status 1 and prints the reason, so start Redis first and check `REDIS_URL`.

### Running against a test server

Use a separate Discord application for development, and keep its token as the only token in your local `.env`. The bot loads `.env` itself, so a local run, a test or a tool can use any token in it. Never put the production token there.

1. Run `cp .env.example .env`. In `.env`, set `DISCORD_TOKEN` to the development application's token and `DEV_GUILD_ID` to your test server's ID; `REDIS_URL` already points at the Redis of the next step. To copy the ID, turn on Developer Mode in Discord's Advanced settings, right-click the server and choose Copy Server ID.
2. Start a local Redis with `docker compose -f compose.dev.yaml up -d`. It listens on 127.0.0.1:6379 only. To use another host port, set `REDIS_PORT` in `.env`, where Compose reads it too, and use the same port in `REDIS_URL`.
3. Run `python bot.py` with the venv active. The bot copies its slash commands to the `DEV_GUILD_ID` server and syncs them there, where they show up at once.

`docker compose -f compose.dev.yaml down` stops Redis; add `-v` to delete its data.

### Updating the slash commands

Without `--sync`, a start changes the slash commands only in the `DEV_GUILD_ID` server, if one is set. After a release that adds, removes or changes a slash command, start the bot once with `python bot.py --sync`: it replaces the commands in every server with this build's, then runs as usual. A command missing from the build disappears from every server, so check the command list before you sync. Don't use `--sync` with the development token: the test server would then list each command twice, once from the global sync and once from `DEV_GUILD_ID`.

### Privacy

[PRIVACY.md](PRIVACY.md) describes what the bot stores, who can see it, and how to delete it. To delete your birthday, run `/forget_birthday` in any server the bot shares with you.
