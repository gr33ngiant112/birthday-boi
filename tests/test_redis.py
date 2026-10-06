"""#20: create_redis_client() with a plain redis:// URL (local runs, CI service containers),
and with a rediss:// URL, whose server certificate is checked unless the operator opts out
with REDIS_TLS_INSECURE=1."""

import ssl

import pytest
import redis.asyncio

import bot

TLS_URL = "rediss://redis.example.test:6380/0"


def tls_settings(connection):
    """(verify_mode, check_hostname) of the SSLContext the connection would open with."""
    context = connection.ssl_context.get()
    return context.verify_mode, context.check_hostname


def test_plain_redis_url_builds_a_connection(monkeypatch):
    monkeypatch.setattr(bot, "REDIS_URL", "redis://localhost:6379/0")
    client = bot.create_redis_client()

    # Builds the connection object only; nothing connects.
    connection = client.connection_pool.make_connection()

    assert isinstance(connection, redis.asyncio.connection.Connection)
    assert not isinstance(connection, redis.asyncio.connection.SSLConnection)
    assert (connection.host, connection.port, connection.db) == ("localhost", 6379, 0)


def test_plain_redis_url_ignores_the_tls_opt_out(monkeypatch):
    monkeypatch.setenv("REDIS_TLS_INSECURE", "1")
    monkeypatch.setattr(bot, "REDIS_URL", "redis://localhost:6379/0")

    connection = bot.create_redis_client().connection_pool.make_connection()

    assert not isinstance(connection, redis.asyncio.connection.SSLConnection)


@pytest.mark.parametrize("opt_out", [None, "", "0", "true"])
def test_tls_url_checks_the_server_certificate(opt_out, monkeypatch):
    if opt_out is None:
        monkeypatch.delenv("REDIS_TLS_INSECURE", raising=False)
    else:
        monkeypatch.setenv("REDIS_TLS_INSECURE", opt_out)
    monkeypatch.setattr(bot, "REDIS_URL", TLS_URL)

    connection = bot.create_redis_client().connection_pool.make_connection()

    if not isinstance(connection, redis.asyncio.connection.SSLConnection):
        pytest.fail(f"setup: a rediss:// URL should build a TLS connection, got {type(connection).__name__}")
    assert tls_settings(connection) == (ssl.CERT_REQUIRED, True)


def test_tls_url_skips_the_certificate_check_only_with_the_opt_out(monkeypatch):
    monkeypatch.setenv("REDIS_TLS_INSECURE", "1")
    monkeypatch.setattr(bot, "REDIS_URL", TLS_URL)

    connection = bot.create_redis_client().connection_pool.make_connection()

    if not isinstance(connection, redis.asyncio.connection.SSLConnection):
        pytest.fail(f"setup: a rediss:// URL should build a TLS connection, got {type(connection).__name__}")
    assert tls_settings(connection) == (ssl.CERT_NONE, False)
