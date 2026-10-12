# birthday-boi

## A Discord bot that remembers your birthday and everyone elses.

### Developing

1. Install pyenv

```bash
git clone https://github.com/pyenv/pyenv.git ~/.pyenv
echo 'export PATH="$HOME/.pyenv/bin:$PATH"' >> ~/.bashrc
echo 'eval "$(pyenv init --path)"' >> ~/.bashrc
source ~/.bashrc
```

2. Install python 3.11 and set it globally. Make sure you have glibc and dependent tooling available to build a Python distribution. If you have trouble building Python, You can find solutions to common issues with installing these tools [here](https://github.com/pyenv/pyenv/wiki/Common-build-problems)

```bash
# If you're on ubuntu, you'll need these packages
sudo apt-get update
sudo apt-get install \
    build-essential \
    libssl-dev \
    zlib1g-dev \
    libbz2-dev \
    libreadline-dev \
    libsqlite3-dev \
    libffi-dev \
    liblzma-dev \
    tk-dev \
    libgdbm-dev \
    libncurses5-dev \
    libncursesw5-dev \
    xz-utils \
    libexpat1-dev \
    libdb5.3-dev \
    libmpdec-dev \
    libtinfo-dev \
    uuid-dev \
    libgmp-dev

# Install python3 and set it globally
pyenv install 3.11
pyenv global 3.11
exec zsh # or bash
```

3. Install [uv](https://docs.astral.sh/uv/getting-started/installation/). Then create the virtual environment `.venv` with the packages that `uv.lock` pins, the dev tools included, and activate it:

```bash
uv sync --locked
source .venv/bin/activate
```

4. Run `pre-commit install`. Before each commit, the hooks run `ruff check`, `ruff format --check` and the command registry test with this venv's ruff and pytest, so commit with the venv active. `ruff format .` fixes what the format check finds.

### Building and testing the Docker image

The `Dockerfile` builds the bot's image. CI builds it and runs the lint and the tests inside it. To do the same locally:

```bash
docker build --tag birthday-boi .
docker build --target test --tag birthday-boi:test .
docker run --rm --network none birthday-boi:test
```

The first command builds the bot's image. The second builds the test image: the bot's image plus the dev tools and the tests. The third runs `ruff check`, `ruff format --check` and the tests in a container with no network, and exits with a non-zero status if any of them fails. `.dockerignore` keeps `.env` files out of the build context, so they never reach an image.

### Installing

1. Register the bot as a discord app [here](https://discord.com/developers/applications). Remember to _save the token_.

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

`python bot.py`, the Procfile's `worker` command, reads `DISCORD_TOKEN` and `REDIS_URL` from the environment; when `DYNO` is not set (a local run), it loads them from a `.env` file first. Before it logs in to Discord, the bot sends Redis a PING. If Redis does not answer, the bot exits with status 1 and prints the reason, so start Redis first and check `REDIS_URL`. For a `rediss://` server with a self-signed certificate, such as Heroku's, set `REDIS_TLS_INSECURE=1`, or the certificate check fails and the bot does not start.

### Running against a test server

Use a separate Discord application for development, and keep its token as the only token in your local `.env`. The bot loads `.env` itself, so a local run, a test or a tool can use any token in it. Never put the production token there.

1. Start a local Redis with `docker compose -f compose.dev.yaml up -d`. It listens on 127.0.0.1:6379 only. To use another host port, set `REDIS_PORT` and use the same port in `REDIS_URL`.
2. In `.env`, set `DISCORD_TOKEN` to the development application's token, `REDIS_URL=redis://127.0.0.1:6379/0`, and `DEV_GUILD_ID` to your test server's ID. To copy the ID, turn on Developer Mode in Discord's Advanced settings, right-click the server and choose Copy Server ID.
3. Run `python bot.py`. The bot copies its slash commands to the `DEV_GUILD_ID` server and syncs them there, where they show up at once.

`docker compose -f compose.dev.yaml down` stops Redis; add `-v` to delete its data.

### Updating the slash commands

Without `--sync`, a start changes the slash commands only in the `DEV_GUILD_ID` server, if one is set. After a release that adds, removes or changes a slash command, start the bot once with `python bot.py --sync`: it replaces the commands in every server with this build's, then runs as usual. A command missing from the build disappears from every server, so check the command list before you sync. Don't use `--sync` with the development token: the test server would then list each command twice, once from the global sync and once from `DEV_GUILD_ID`.

### Privacy

[PRIVACY.md](PRIVACY.md) describes what the bot stores, who can see it, and how to delete it. To delete your birthday, run `/forget_birthday` in any server the bot shares with you.
