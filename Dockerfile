# Stages:
#   uv       uv's own image. Its /uv binary is mounted only while packages install, so uv is not in
#            the bot's image.
#   runtime  the bot's image: the packages uv.lock pins, bot.py and core.py, run as an unprivileged
#            user.
#   test     runtime plus the dev tools and the tests; `docker run` lints, checks the format and
#            runs the tests.
#   (last)   runtime again, so a plain `docker build .` builds the bot's image, not the test image.
# The README's "Building and testing the Docker image" has the commands.

# uv 0.12.23 from Astral's official image, pinned to its digest. uv comes from a FROM line of its
# own because Dependabot's docker updates read only FROM lines, so they bump this one too.
FROM ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 AS uv

# python:3.12-slim (Python 3.12.15 on Debian 13 when pinned), pinned to its digest. pyproject.toml's
# requires-python and .python-version name the same Python version.
FROM python:3.12-slim@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1 AS runtime

# print() output reaches `docker logs` at once instead of waiting in a buffer.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# The bot runs as this unprivileged user (uid 10001). /app and its files stay owned by root, so
# the bot cannot change its own code.
RUN useradd --uid 10001 --no-log-init bot

# uv installs the packages into /app/.venv with this image's Python and never downloads one. It
# compiles them to bytecode, because the bot cannot write .pyc files under /app.
ENV UV_PYTHON=/usr/local/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_NO_CACHE=1

# --locked fails the build when uv.lock does not match pyproject.toml. --no-build fails it when a
# package has no wheel, so nothing is built from source. The dependencies come before bot.py, so
# a code change reuses this layer.
COPY pyproject.toml uv.lock ./
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --locked --no-build --no-dev
# `python` is the virtualenv's Python, which has the packages.
ENV PATH=/app/.venv/bin:$PATH

COPY bot.py core.py ./
USER 10001
CMD ["python", "bot.py"]

# The tests run against the runtime stage's bot.py, core.py and packages. .github/scripts/ is
# here so the lint covers every Python file CI linted on the host.
FROM runtime AS test
USER 0
# Adds pyproject.toml's dev group (ruff, pytest, hypothesis, fakeredis, pre-commit) to the same
# virtualenv.
RUN --mount=from=uv,source=/uv,target=/bin/uv \
    uv sync --locked --no-build --group dev
COPY pytest.ini ./
COPY tests/ tests/
COPY .github/scripts/ .github/scripts/
USER 10001
# `ruff check`, then `ruff format --check`, which also fails on a CRLF file, then the tests; ruff
# reads its settings from pyproject.toml. The bot user cannot write to /app, so ruff and pytest run
# without their caches, and hypothesis keeps its example database in /tmp, which `docker run --rm`
# discards; in /app it would warn and fall back to memory.
ENV HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hypothesis
CMD ["sh", "-c", "ruff check --no-cache . && ruff format --no-cache --check . && exec pytest -q -p no:cacheprovider"]

FROM runtime
