"""Magic-link redemption tolerates roaming without giving up the tripwire.

Strict IP equality turned every carrier-NAT move, campus egress rotation and
IPv6-to-IPv4 transition into a failed login. Redemption now proceeds when the mint
and redeem addresses share a /24 (IPv4) or /64 (IPv6) subnet -- after IPv4-mapped
IPv6 normalisation -- or when the User-Agent matches the one recorded at mint. The
unguessable token remains the real gate; these only decide roam versus refuse.

What refusal still means is unchanged: an unrelated network counts (bounded, so spam
cannot overflow the counter) and gets a 403, and the exact addresses stay in the log
for abuse review. The counter column is a plain Integer, widened at boot on databases
that predate the change.
"""

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.accounts import AccountType
from app.db_models import LoginTokenRecord, UserRecord, find_account_in_table
from app.main import app
from app.services.auth_service import _normalize_client_ip, _redemption_continuity

VERIFY_ENDPOINT = "/auth/login/magic/verify"


class _FromHost:
    """Presents every request as arriving from one fixed address."""

    def __init__(self, host="127.0.0.1", port=51234):
        self.host = host
        self.port = port

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = {**scope, "client": (self.host, self.port)}
        await app(scope, receive, send)


def _browser(host="127.0.0.1"):
    """One browser arriving from ``host``: a fresh client, so no cookie is shared."""
    return TestClient(_FromHost(host))


def _seed_user(db, regno):
    with db.sessions.begin() as session:
        session.add(UserRecord(
            regno=regno,
            name=regno.title(),
            email=f"{regno}@example.com",
            year=2,
            department="Test_Class",
            gender="other",
        ))


def _mint_token_for(db, regno, *, host="127.0.0.1", user_agent="Test Agent"):
    from app.create_token import create_and_store_magic_token

    with db.sessions() as session:
        found = find_account_in_table(session, AccountType.USERS.value, regno)
    assert found is not None, f"no users account with regno {regno!r}"
    resolved_type, row = found

    class _Request:
        client = type("C", (), {"host": host})()
        headers = {"user-agent": user_agent}

    token, _ = create_and_store_magic_token(db, str(row.id), resolved_type, _Request())
    return token


def _attempts(db, plaintext):
    from app.services.auth_service import TOKEN_HASH_DOMAIN_MAGIC, hash_token

    with db.sessions() as session:
        row = session.scalar(
            select(LoginTokenRecord).where(
                LoginTokenRecord.token_hash == hash_token(plaintext, domain=TOKEN_HASH_DOMAIN_MAGIC)
            )
        )
        return row.attempt_count


def test_roam_inside_the_subnet_redeems(db):
    _seed_user(db, "roamer")
    plaintext = _mint_token_for(db, "roamer", host="127.0.0.1")

    # Same /24, different address: the NAT-pool move that strict equality refused.
    # The counter still records the redemption itself (the consume path counts every
    # presentation); what must not happen is a refusal.
    response = _browser(host="127.0.0.2").get(f"{VERIFY_ENDPOINT}?token={plaintext}")

    assert response.status_code == 200, response.text
    assert _attempts(db, plaintext) == 1


def test_unrelated_network_is_refused_and_counted(db):
    _seed_user(db, "traveller")
    plaintext = _mint_token_for(db, "traveller", host="127.0.0.1")

    first = _browser(host="10.9.9.9").get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert first.status_code == 403, first.text
    assert _attempts(db, plaintext) == 1

    second = _browser(host="10.9.9.9").get(f"{VERIFY_ENDPOINT}?token={plaintext}")
    assert second.status_code == 403, second.text
    assert _attempts(db, plaintext) == 2


def test_matching_user_agent_redeems_across_networks(db):
    _seed_user(db, "commuter")
    plaintext = _mint_token_for(
        db, "commuter", host="127.0.0.1", user_agent="RoamAgent/1.0"
    )

    # A different network entirely, but the same browser that minted the link.
    response = _browser(host="10.9.9.9").get(
        f"{VERIFY_ENDPOINT}?token={plaintext}",
        headers={"user-agent": "RoamAgent/1.0"},
    )

    assert response.status_code == 200, response.text
    assert _attempts(db, plaintext) == 1


def test_attempt_counter_saturates_instead_of_overflowing(db):
    """The bound is exercised at the edge: a maxed counter stays maxed, no 500."""
    _seed_user(db, "hammered")
    plaintext = _mint_token_for(db, "hammered", host="127.0.0.1")

    from app.services.auth_service import TOKEN_HASH_DOMAIN_MAGIC, hash_token

    with db.sessions.begin() as session:
        session.execute(
            text(
                "UPDATE login_tokens SET attempt_count = 2147483647 "
                "WHERE token_hash = :hash"
            ),
            {"hash": hash_token(plaintext, domain=TOKEN_HASH_DOMAIN_MAGIC)},
        )

    refused = _browser(host="10.9.9.9").get(f"{VERIFY_ENDPOINT}?token={plaintext}")

    assert refused.status_code == 403, refused.text
    assert _attempts(db, plaintext) == 2147483647


def test_normalisation_unwraps_mapped_ipv6_and_rejects_garbage():
    assert _normalize_client_ip("::ffff:127.0.0.1") == _normalize_client_ip("127.0.0.1")
    assert _normalize_client_ip("not-an-address") is None
    assert _normalize_client_ip("") is None


def test_continuity_matrix():
    same24 = dict(minted_ip="192.0.2.10", minted_user_agent="A", remote_ip="192.0.2.200", remote_user_agent="B")
    assert _redemption_continuity(**same24) is True
    assert (
        _redemption_continuity(
            minted_ip="192.0.2.10", minted_user_agent="A",
            remote_ip="198.51.100.7", remote_user_agent="B",
        )
        is False
    )
    assert (
        _redemption_continuity(
            minted_ip="192.0.2.10", minted_user_agent="A",
            remote_ip="198.51.100.7", remote_user_agent="A",
        )
        is True
    )
    assert (
        _redemption_continuity(
            minted_ip="2001:db8:abcd:0012::1", minted_user_agent="A",
            remote_ip="2001:db8:abcd:0012::9", remote_user_agent="B",
        )
        is True
    )
    assert (
        _redemption_continuity(
            minted_ip="2001:db8:abcd:0012::1", minted_user_agent="A",
            remote_ip="2001:db8:abcd:0013::1", remote_user_agent="B",
        )
        is False
    )
    # Versions never mix, and garbage never admits.
    assert (
        _redemption_continuity(
            minted_ip="127.0.0.1", minted_user_agent="A",
            remote_ip="::1", remote_user_agent="B",
        )
        is False
    )
    assert (
        _redemption_continuity(
            minted_ip="???", minted_user_agent="",
            remote_ip="???", remote_user_agent="B",
        )
        is False
    )


def test_widening_converges_a_deployed_smallint_counter(db):
    """The boot migration, run against a table in its old shape: smallint in, integer
    out, idempotent on the second run."""
    with db.engine.begin() as connection:
        connection.execute(text("ALTER TABLE login_tokens ALTER COLUMN attempt_count TYPE smallint"))
        before = connection.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'login_tokens' "
                "AND column_name = 'attempt_count'"
            )
        ).scalar()
    assert before == "smallint"

    db.apply_widened_columns()

    with db.engine.begin() as connection:
        after = connection.execute(
            text(
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'login_tokens' "
                "AND column_name = 'attempt_count'"
            )
        ).scalar()
    assert after == "integer"

    db.apply_widened_columns()
