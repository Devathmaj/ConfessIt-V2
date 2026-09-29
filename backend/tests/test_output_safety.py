"""Error and delivery-URL shapes that keep reflected content inert.

User content legitimately contains quotes and markup characters -- names with
apostrophes, confessions quoting HTML -- so the server does not strip them (React
escapes by default, which is the correct layer; server-side escaping would
double-escape). What the server *does* guarantee, pinned here:

* error responses are ``application/json`` with ``nosniff``: a 422 echoing the
  submitted value can never be sniffed into HTML and executed;
* avatar delivery URLs are pinned to the CDN host the frontend allowlist knows, so a
  misconfigured cloud name fails closed instead of handing out an address the client
  would refuse to render -- or worse, one pointing somewhere else.
"""

import pytest
from fastapi import HTTPException

from app.services.storage_service import storage_service


def test_validation_errors_are_json_not_sniffable(client):
    """A 422 echoing attacker input must not be interpretable as HTML."""
    # The conftest client signs in as a participant override; an empty body fails
    # request validation before any database read, which is exactly the echo path:
    # the refused payload comes straight back in `detail`.
    response = client.post("/love-notes/send", json={})

    assert response.status_code == 422, response.text
    assert response.headers["content-type"] == "application/json"
    assert response.headers["x-content-type-options"] == "nosniff"


def test_profile_delivery_url_is_pinned_to_the_cdn_host(monkeypatch):
    """The SDK builds the URL from configuration; a wrong host fails closed here."""
    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    url = storage_service.profile_delivery_url(
        "profile_pics/22CS045/AbCdEfGhIjKlMnOpQrStUv/profile", "png"
    )
    assert url.startswith("https://res.cloudinary.com/")


def test_profile_delivery_url_refuses_a_non_cdn_host(monkeypatch):
    """A tampered builder output is a 503, never an address handed to a browser."""
    from app.services import storage_service as storage_module

    monkeypatch.setattr(storage_service, "enabled", True, raising=False)
    monkeypatch.setattr(
        storage_module,
        "cloudinary_url",
        lambda *args, **kwargs: ("https://evil.example.com/pic.png", {}),
    )
    with pytest.raises(HTTPException) as exc_info:
        storage_service.profile_delivery_url(
            "profile_pics/22CS045/AbCdEfGhIjKlMnOpQrStUv/profile", "png"
        )
    assert exc_info.value.status_code == 503
