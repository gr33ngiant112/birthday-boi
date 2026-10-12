# AGENTS.md

## Commands

- Set up with uv, as CI does: `uv sync --locked --no-build && . .venv/bin/activate && pre-commit install`. uv creates `.venv` with Python 3.11 (`.python-version`; spaCy 3.5.0 has wheels only up to 3.11) and installs the packages `uv.lock` pins, dev tools included, from wheels only.
- After a dependency change in `pyproject.toml`, run `uv lock` and commit `uv.lock` with it. The Docker build runs `uv sync --locked`, which fails on a stale `uv.lock`.
- Lint: `ruff check .` and `ruff format --check .`, with the settings in `pyproject.toml`. `ruff format .` fixes the format.
- Test: `pytest -q`. Before each commit, pre-commit runs the lint and `tests/test_registry.py`.
- Run against a test server: `docker compose -f compose.dev.yaml up -d`, then `python bot.py`. The bot reads `DISCORD_TOKEN` (the development application's token), `REDIS_URL=redis://127.0.0.1:6379/0` and `DEV_GUILD_ID` (the test server's ID) from the owner's `.env`.

## Rules

- Never read, print or edit `.env`. It holds the development bot token, and `python bot.py` loads it.
- Make targeted edits to `bot.py`. Never regenerate or re-paste it whole: that is how 91b142b and 7a98e91 deleted slash commands and the monthly task, and how 79c6556 left a second copy of the file that would not parse.
- A global command sync replaces the commands in every server, so a build missing a command deletes it everywhere. Develop with `DEV_GUILD_ID`. `python bot.py --sync` is only for a release that changes the commands.
- If a change adds, removes or renames a slash command, task loop or event handler, update the expected sets in `tests/test_registry.py` in the same change.
- `tests/` enforces the privacy, mention and date rules. Keep it green, and don't edit or delete a test to make a change pass. A PR that leaves fewer tests than its base fails the Test count check unless the owner adds the `tests-removed` label.
- Ask before adding a dependency, turning on a privileged intent or editing `.github/workflows/`.
- A commit an assistant helped write names it in a trailer. Keep the tool's own (`Co-Authored-By:` in Claude Code); other tools add `Assisted-by: <tool and model>`.
