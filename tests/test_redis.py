"""#20: create_redis_client() with a plain redis:// URL (local runs, CI service containers)."""

import pytest
import redis

import bot


@pytest.mark.xfail(
    strict=True,
    raises=TypeError,
    reason="#20: ssl_cert_reqs is passed for every URL, and a plain redis:// Connection rejects it",
)
def test_plain_redis_url_builds_a_connection(monkeypatch):
    monkeypatch.setattr(bot, "REDIS_URL", "redis://localhost:6379/0")
    client = bot.create_redis_client()

    # Builds the connection object only; nothing connects.
    connection = client.connection_pool.make_connection()

    assert isinstance(connection, redis.connection.Connection)
    assert not isinstance(connection, redis.connection.SSLConnection)
    assert (connection.host, connection.port, connection.db) == ("localhost", 6379, 0)
