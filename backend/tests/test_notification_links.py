"""Absolute links attached to notification emails.

Participant links must match the routes the in-app inbox opens from the same
``target_kind`` / ``target_id`` pair. Staff links point at the review queue or
game console that can act on the notice. These tests pin the mappings without a
database, Functions host, or ACS.
"""

import pytest

from app.services import notification_links


@pytest.fixture(autouse=True)
def _fixed_origin(monkeypatch):
    monkeypatch.setattr(
        notification_links,
        "public_frontend_origin",
        lambda: "https://app.example.com",
    )


def test_conversation_target_opens_the_match_in_the_inbox():
    assert (
        notification_links.participant_notification_link(
            "conversation", "match-123"
        )
        == "https://app.example.com/inbox?match=match-123"
    )


def test_love_note_target_opens_the_note_in_the_reader_inbox():
    assert (
        notification_links.participant_notification_link("love_note", "note-123")
        == "https://app.example.com/love-notes?tab=inbox&note=note-123"
    )


def test_target_ids_are_quoted_as_one_url_value():
    assert (
        notification_links.participant_notification_link(
            "conversation", "match/1 ?"
        )
        == "https://app.example.com/inbox?match=match%2F1%20%3F"
    )


@pytest.mark.parametrize(
    ("kind", "target_id"),
    [(None, None), ("conversation", None), (None, "match-123"), ("report", "r1")],
)
def test_an_unknown_or_incomplete_target_has_no_link(kind, target_id):
    assert (
        notification_links.participant_notification_link(kind, target_id) is None
    )


@pytest.mark.parametrize(
    ("category", "path"),
    [
        ("report", "/admin/reports"),
        ("love_note", "/admin/love-notes"),
        ("game", "/admin/games"),
    ],
)
def test_staff_notices_open_their_review_queue(category, path):
    assert (
        notification_links.staff_notification_link(category)
        == f"https://app.example.com{path}"
    )


@pytest.mark.parametrize(
    ("game_id", "path"),
    [
        ("wordle", "/admin/games/manage/wordle"),
        ("anagram", "/admin/games/manage/anagram"),
        ("connections", "/admin/games/manage/connections"),
        ("featured", "/admin/games/manage/featured"),
        ("trivia-drop", "/admin/games/manage/trivia-drop"),
    ],
)
def test_game_notices_open_the_exact_manage_screen(game_id, path):
    assert (
        notification_links.staff_notification_link("game", game_id=game_id)
        == f"https://app.example.com{path}"
    )


def test_unknown_game_ids_are_quoted_on_a_manage_path():
    assert (
        notification_links.staff_notification_link(
            "game", game_id="custom/game"
        )
        == "https://app.example.com/admin/games/manage/custom%2Fgame"
    )


def test_an_unknown_staff_category_has_no_link():
    assert notification_links.staff_notification_link("other") is None
