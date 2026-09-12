"""``GET /api/time`` is what makes the server, not the browser, the authority on time.

The browser measures its own drift against this endpoint and re-bases its clock. That only
works if the reply is unambiguous, uncached, and internally consistent, so each of those is
pinned here.
"""

from datetime import datetime, timedelta, timezone


class TestPayloadShape:
    def test_reports_now_with_an_explicit_offset(self, client):
        response = client.get("/api/time")
        assert response.status_code == 200

        body = response.json()
        parsed = datetime.fromisoformat(body["now"])
        assert parsed.tzinfo is not None, "client cannot place an unzoned timestamp"

    def test_now_is_utc(self, client):
        body = client.get("/api/time").json()
        parsed = datetime.fromisoformat(body["now"])

        assert parsed.utcoffset() == timedelta(0)
        assert parsed == parsed.astimezone(timezone.utc)

    def test_epoch_ms_agrees_with_now(self, client):
        """The two fields are redundant on purpose: a client can use either, so a
        divergence between them would be a silent corruption of whatever read the other."""
        body = client.get("/api/time").json()

        from_iso = datetime.fromisoformat(body["now"]).timestamp() * 1000

        assert abs(body["epoch_ms"] - from_iso) < 1000

    def test_agrees_with_the_database_clock(self, client, db):
        """Both sides run in the same process against the same host, so they must not
        disagree. A gap here would mean one of them is reading a different clock."""
        from sqlalchemy import text

        with db.engine.connect() as connection:
            db_now = connection.execute(text("SELECT now()")).scalar()
        served = datetime.fromisoformat(client.get("/api/time").json()["now"])

        assert abs((db_now - served).total_seconds()) < 5

    def test_the_db_session_reads_back_in_utc(self, db):
        """The connection pool pins its session to UTC rather than inheriting whatever the
        database image defaults to, so a value written as UTC is also read as UTC."""
        from sqlalchemy import text

        with db.engine.connect() as connection:
            assert connection.execute(text("SHOW timezone")).scalar() == "UTC"


class TestNotCacheable:
    def test_is_marked_no_store(self, client):
        """A proxy or the browser returning a stale "now" would make a correct client
        believe it is on the wrong time, which is the exact failure this endpoint exists
        to prevent."""

        assert client.get("/api/time").headers.get("cache-control") == "no-store"


class TestPubliclyReadable:
    def test_needs_no_authentication(self, client):
        """The clock has to be readable before a session exists, and a 401/redirect here
        would leave every visitor permanently on the device clock."""

        assert client.get("/api/time").status_code == 200


class TestMonotonicAcrossCalls:
    def test_never_goes_backwards(self, client):
        first = client.get("/api/time").json()["epoch_ms"]
        second = client.get("/api/time").json()["epoch_ms"]

        assert second >= first, "server reported time moved backwards"
