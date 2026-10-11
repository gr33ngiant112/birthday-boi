# Three stages:
#   runtime  the bot's image: its dependencies and bot.py, run as an unprivileged user.
#   test     runtime plus the dev tools and the tests; `docker run` lints and runs the tests.
#   (last)   runtime again, so a plain `docker build .` builds the bot's image, not the test image.
# The README's "Building and testing the Docker image" has the commands.

# python:3.11-slim (Python 3.11.17 on Debian 13 when pinned), pinned to its digest. spaCy 3.5.0,
# which requirements.txt pins, has wheels for CPython 3.11 at the newest, built for glibc.
FROM python:3.11-slim@sha256:e88e9763f943ec1834f992a4b51e0f24500486803e8bc534e5767af9ea65f6ce AS runtime

# print() output reaches `docker logs` at once instead of waiting in a buffer.
ENV PYTHONUNBUFFERED=1

WORKDIR /app

# The bot runs as this unprivileged user (uid 10001). /app and its files stay owned by root, so
# the bot cannot change its own code.
RUN useradd --uid 10001 --no-log-init bot

# --only-binary :all: as in CI: no source distributions from PyPI. pip still builds spaCy's
# English model from the release URL in requirements.txt. The dependencies come before bot.py,
# so a code change reuses this layer.
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir --only-binary :all: -r requirements.txt

COPY bot.py ./
USER 10001
CMD ["python", "bot.py"]

# The tests run against the runtime stage's bot.py and packages. .github/scripts/ is here so the
# lint covers every Python file CI linted on the host.
FROM runtime AS test
USER 0
COPY requirements-dev.txt ./
RUN python -m pip install --no-cache-dir --only-binary :all: -r requirements-dev.txt
COPY pytest.ini ./
COPY tests/ tests/
COPY .github/scripts/ .github/scripts/
USER 10001
# The bot user cannot write to /app, so ruff and pytest run without their caches.
CMD ["sh", "-c", "ruff check --no-cache --select E9,F63,F7,F82 . && exec pytest -q -p no:cacheprovider"]

FROM runtime
