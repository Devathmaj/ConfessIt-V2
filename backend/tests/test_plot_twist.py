"""Plot Twist end to end: validation, AI pipeline, gameplay and scoring.

The suite pins the properties a careless implementation would get quietly
wrong: generated packages validate deterministically, invalid packages never
publish, turns belong to their assignee, duplicate and stale submits cannot
corrupt state, and couple points stay couple points at exactly the published
reward values.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from app.accounts import AccountType
from app.db_models import (
    CoupleGameRecord,
    FeaturedGameRecord,
    GameScoreAwardRecord,
    PlotTwistBlueprintRecord,
    PlotTwistDecisionRecord,
    PlotTwistGameRecord,
    PlotTwistPackageRecord,
    PlotTwistSessionRecord,
)
from app.main import app
from app.models import UserDetails
from app.services import league_finalization, plot_twist_generation, plot_twist_gameplay
from app.services.auth_service import get_current_user
from app.services.league_adapters import competition_type_for
from app.services.league_global_scoring import global_base_for
from app.services.plot_twist_schemas import GenerationConfig
from app.services.plot_twist_validation import validate_blueprint, validate_package
from conftest import override_get_current_user

UTC = timezone.utc


def make_user(db, user_id, regno, name, gender="other"):
    from sqlalchemy import text

    with db.engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, regno, name, email, year, department, gender) "
                "VALUES (:id, :regno, :name, :email, 2, 'Class', :gender) "
                "ON CONFLICT (id) DO NOTHING"
            ),
            {"id": user_id, "regno": regno, "name": name, "email": f"{regno}@user.com", "gender": gender},
        )
    return UserDetails(
        id=user_id,
        regno=regno,
        name=name,
        email=f"{regno}@user.com",
        year=2,
        department="Class",
        gender=gender,
        account_type=AccountType.USERS.value,
    )


A_ID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
B_ID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
C_ID = "cccccccc-cccc-cccc-cccc-cccccccccccc"


@pytest.fixture
def players(db):
    from app.services.couple_service import seed_couple_questions

    seed_couple_questions(db)
    return {
        "a": make_user(db, A_ID, "player_a", "Alex", "male"),
        "b": make_user(db, B_ID, "player_b", "Sam", "female"),
        "c": make_user(db, C_ID, "player_c", "Jo", "other"),
    }


@pytest.fixture
def http():
    client = TestClient(app, follow_redirects=False)
    try:
        yield client
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


def client_for(http: TestClient, user: UserDetails):
    class _AsUser:
        def get(self, *args, **kwargs):
            app.dependency_overrides[get_current_user] = lambda: user
            return http.get(*args, **kwargs)

        def post(self, *args, **kwargs):
            app.dependency_overrides[get_current_user] = lambda: user
            return http.post(*args, **kwargs)

        def put(self, *args, **kwargs):
            app.dependency_overrides[get_current_user] = lambda: user
            return http.put(*args, **kwargs)

    return _AsUser()


def seed_questionnaire(db):
    day = datetime.now(UTC).date()
    game = CoupleGameRecord(
        game_date=day,
        created_by="adminuser",
        status="active",
        period_start_date=day,
        period_end_date=day + timedelta(days=1),
        started_at=datetime.now(UTC),
    )
    with db.sessions.begin() as session:
        session.add(game)
        session.flush()
        return str(game.id)


def story_package(version=1, **overrides):
    package = {
        "story_id": "auditorium-escape",
        "version": version,
        "title": "Locked In",
        "summary": "Locked in the college auditorium overnight.",
        "genre": "adventure",
        "tone": "playful",
        "difficulty": "medium",
        "blueprint_ref": "bp-1",
        "initial_state": {"courage": 5, "alarm": []},
        "state_vars": [
            {"name": "courage", "kind": "int", "min_value": 0, "max_value": 10, "initial": 5},
            {"name": "alarm", "kind": "flag", "initial": []},
        ],
        "opening_scene_id": "s_open",
        "scenes": [
            {
                "id": "s_open", "type": "opening", "title": "Locked In",
                "narrative_text": "The auditorium doors click shut behind you.",
                "actor": "participant_one", "slot": "core",
                "requires_tags": [], "provides_tags": ["indoors"], "forbidden_tags": [],
                "entry_conditions": [], "is_terminal": False,
                "choices": [
                    {"id": "c_loading", "text": "Sneak out through the loading area.",
                     "consequence_text": "You slip backstage.", "conditions": [],
                     "state_changes": [{"op": "increment", "variable": "courage", "value": 1}],
                     "next_scene_id": "s_dock", "reward_tier": None},
                    {"id": "c_lobby", "text": "Head for the main lobby.",
                     "consequence_text": "The lobby is dark.", "conditions": [],
                     "state_changes": [{"op": "decrement", "variable": "courage", "value": 1}],
                     "next_scene_id": "s_alarm", "reward_tier": None},
                ],
            },
            {
                "id": "s_dock", "type": "encounter", "title": "Loading Dock",
                "narrative_text": "The loading door has an alarm light blinking.",
                "actor": "participant_two", "slot": "core",
                "requires_tags": ["indoors"], "provides_tags": ["exit_available"],
                "forbidden_tags": [], "entry_conditions": [], "is_terminal": False,
                "choices": [
                    {"id": "c_careful", "text": "Prop the door and slip out quietly.",
                     "consequence_text": "Fresh air.", "conditions": [],
                     "state_changes": [{"op": "add_flag", "variable": "alarm", "value": "silent"}],
                     "next_scene_id": "s_free", "reward_tier": None},
                    {"id": "c_force", "text": "Shove the door open.",
                     "consequence_text": "The alarm screams.", "conditions": [],
                     "state_changes": [{"op": "add_flag", "variable": "alarm", "value": "loud"}],
                     "next_scene_id": "s_caught", "reward_tier": None},
                ],
            },
            {
                "id": "s_alarm", "type": "encounter", "title": "Lobby Alarm",
                "narrative_text": "A keypad glows beside the lobby doors.",
                "actor": "participant_two", "slot": "core",
                "requires_tags": ["indoors"], "provides_tags": ["security_alert"],
                "forbidden_tags": [], "entry_conditions": [], "is_terminal": False,
                "choices": [
                    {"id": "c_guess", "text": "Guess the code.",
                     "consequence_text": "Wrong, and now it beeps.", "conditions": [],
                     "state_changes": [{"op": "set", "variable": "courage", "value": 3}],
                     "next_scene_id": "s_caught", "reward_tier": None},
                ],
            },
            {
                "id": "s_free", "type": "ending", "title": "Out Into the Night",
                "narrative_text": "You made it out together.",
                "actor": "either", "slot": "core",
                "requires_tags": [], "provides_tags": [], "forbidden_tags": [],
                "entry_conditions": [], "is_terminal": True, "ending_id": "e_escape",
                "choices": [],
            },
            {
                "id": "s_caught", "type": "ending", "title": "Caught by Security",
                "narrative_text": "Flashlights converge on you both.",
                "actor": "either", "slot": "core",
                "requires_tags": [], "provides_tags": [], "forbidden_tags": [],
                "entry_conditions": [], "is_terminal": True, "ending_id": "e_fail",
                "choices": [],
            },
        ],
        "endings": [
            {"id": "e_escape", "type": "escape", "title": "Great Escape",
             "narrative": "Freedom tastes sweet.", "conditions": [], "priority": 10,
             "rewards": {}, "success": True, "reward_tier": "silver"},
            {"id": "e_fail", "type": "failure", "title": "Busted",
             "narrative": "Detention for two.", "conditions": [], "priority": 0,
             "rewards": {}, "success": False, "reward_tier": "participation"},
        ],
        "rewards": {"tiers": ["participation", "bronze", "silver", "gold"]},
    }
    package.update(overrides)
    return package


BLUEPRINT = {
    "title": "Locked In",
    "description": "A one-night auditorium escape.",
    "premise": "Two students locked in overnight.",
    "narrative_rules": ["Alternate decisions."],
    "characters": ["Alex", "Sam"],
    "world_details": ["College auditorium."],
    "state_vars": [{"name": "courage", "kind": "int", "min_value": 0, "max_value": 10, "initial": 5}],
    "main_path": ["locked in", "loading dock", "escape"],
    "slots": [{"name": "side", "accepts_tags": ["indoors"], "provides_tags": [],
               "max_alternatives": 2, "allow_repeat": False}],
    "required_modules": ["opening"],
    "encounter_categories": ["alarm"],
    "endings": [{"id": "e_escape", "type": "escape", "title": "Out",
                 "narrative": "Free.", "conditions": [], "priority": 10,
                 "rewards": {}, "success": True, "reward_tier": "silver"}],
    "reward_tiers": [{"tier": "silver", "points": 80}],
    "constraints": ["Stay indoors."],
    "difficulty": "medium",
}


def seed_published(db, package=None, blueprint=None):
    """A blueprint plus a published package version plus a live global event."""
    package = package or story_package()
    blueprint = blueprint or BLUEPRINT
    day = datetime.now(UTC).date()
    with db.sessions.begin() as session:
        bp = PlotTwistBlueprintRecord(
            title="Locked In", difficulty="medium",
            config=GenerationConfig().model_dump(),
            blueprint=blueprint, status="approved",
            validation_errors=[], created_by="adminuser",
        )
        session.add(bp)
        session.flush()
        bp_id = str(bp.id)
        pkg = PlotTwistPackageRecord(
            blueprint_id=bp_id, version=int(package.get("version", 1)),
            title=str(package.get("title")), package=package,
            status="published", validation_errors=[],
            published_at=datetime.now(UTC), created_by="adminuser",
        )
        session.add(pkg)
        session.flush()
        pkg_id = str(pkg.id)
        game = PlotTwistGameRecord(
            game_date=day, status="active",
            period_start_date=day, period_end_date=day + timedelta(days=1),
            story_package_id=pkg_id, seed="test-seed",
            started_at=datetime.now(UTC), created_by="scheduler",
        )
        session.add(game)
        session.flush()
        return {"blueprint_id": bp_id, "package_id": pkg_id, "game_id": str(game.id)}


def pair(db, http, players):
    """Mutual selection through the Plot Twist endpoint for A and B."""
    seed_questionnaire(db)
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])
    response = client_a.post("/games/plot-twist/selection", json={"selected_user_id": B_ID})
    assert response.status_code == 200, response.text
    response = client_b.post("/games/plot-twist/selection", json={"selected_user_id": A_ID})
    assert response.status_code == 200, response.text
    return client_a, client_b


# --- validation ---------------------------------------------------------------


def test_valid_branching_package_passes():
    report = validate_package(story_package())
    assert report.ok, [e.message for e in report.errors]


def test_unknown_transition_is_rejected():
    package = story_package()
    package["scenes"][0]["choices"][0]["next_scene_id"] = "s_missing"
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "unknown_transition" for e in report.errors)


def test_undefined_variable_is_rejected():
    package = story_package()
    package["scenes"][0]["choices"][0]["state_changes"] = [
        {"op": "increment", "variable": "bravery", "value": 1}
    ]
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "undefined_variable" for e in report.errors)


def test_dead_end_is_rejected():
    package = story_package()
    package["scenes"][1]["choices"] = []
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "dead_end" and e.scene_id == "s_dock" for e in report.errors)


def test_unreachable_scene_is_rejected():
    package = story_package()
    package["scenes"].append({
        "id": "s_lost", "type": "encounter", "title": "Lost",
        "narrative_text": "Nobody comes here.", "actor": "either", "slot": "core",
        "requires_tags": [], "provides_tags": [], "forbidden_tags": [],
        "entry_conditions": [], "is_terminal": False,
        "choices": [{"id": "c_loop", "text": "Wait.", "conditions": [],
                     "state_changes": [], "next_scene_id": "s_caught"}],
    })
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "unreachable_scenes" for e in report.errors)


def test_unsatisfiable_tags_are_rejected():
    package = story_package()
    package["scenes"][1]["slot"] = "side"
    package["scenes"][1]["requires_tags"] = ["underground"]
    report = validate_package(package, blueprint=BLUEPRINT)
    assert not report.ok
    assert any(e.code == "unsatisfiable_tags" for e in report.errors)


def test_duplicate_ids_are_rejected():
    package = story_package()
    package["scenes"].append(dict(package["scenes"][0]))
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "duplicate_scene_id" for e in report.errors)


def test_unknown_reward_tier_is_rejected():
    package = story_package()
    package["scenes"][0]["choices"][0]["reward_tier"] = "platinum"
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "unknown_reward_tier" for e in report.errors)


def test_out_of_range_set_is_rejected():
    package = story_package()
    package["scenes"][0]["choices"][0]["state_changes"] = [
        {"op": "set", "variable": "courage", "value": 99}
    ]
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "state_change_out_of_range" for e in report.errors)


def test_terminal_with_choices_is_rejected():
    package = story_package()
    package["scenes"][3]["choices"] = [package["scenes"][0]["choices"][0]]
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "terminal_with_choices" for e in report.errors)


def test_no_reachable_ending_is_rejected():
    package = story_package()
    for scene in package["scenes"]:
        if scene.get("is_terminal"):
            scene["is_terminal"] = False
            scene["choices"] = [{"id": "c_again", "text": "Keep going.",
                                 "conditions": [], "state_changes": [],
                                 "next_scene_id": "s_open"}]
            scene.pop("ending_id", None)
    package["endings"] = []
    report = validate_package(package)
    assert not report.ok
    assert any(e.code in ("no_reachable_ending", "no_endings") for e in report.errors)


def test_blueprint_requires_endings_and_unique_vars():
    bad = dict(BLUEPRINT, endings=[])
    assert not validate_blueprint(bad).ok
    bad_vars = dict(BLUEPRINT, state_vars=[
        {"name": "courage", "kind": "int"}, {"name": "courage", "kind": "flag"},
    ])
    report = validate_blueprint(bad_vars)
    assert not report.ok
    assert validate_blueprint(BLUEPRINT).ok


# --- AI pipeline (mocked: no network, no credentials) --------------------------


def _valid_blueprint_payload():
    return dict(BLUEPRINT)


def test_blueprint_generation_repairs_then_succeeds(db, monkeypatch):
    from app.services import llm_client

    seed = seed_published(db)  # live event exists; generation is independent of it
    _ = seed
    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="T", difficulty="medium", config=GenerationConfig().model_dump(),
            blueprint={}, status="draft", validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)
    calls = {"n": 0}

    def fake_generate(**kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return dict(_valid_blueprint_payload(), endings=[]), {"strict_schema": True}
        return _valid_blueprint_payload(), {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    result = plot_twist_generation.generate_blueprint(db, bp_id)
    assert result["status"] == "ready_for_review"
    assert calls["n"] == 2


def test_blueprint_generation_exhaustion_marks_failed(db, monkeypatch):
    from app.services import llm_client

    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="T", difficulty="medium", config=GenerationConfig().model_dump(),
            blueprint={}, status="draft", validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)

    def fake_generate(**kwargs):
        return {"title": "broken"}, {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    result = plot_twist_generation.generate_blueprint(db, bp_id)
    assert result["status"] == "generation_failed"
    assert result["validation_errors"]


def test_provider_failure_is_admin_safe_and_unpublished(db, monkeypatch):
    from app.services import llm_client

    def fake_generate(**kwargs):
        raise llm_client.LLMError("Could not reach the AI provider.", code="network")

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="T", difficulty="medium", config=GenerationConfig().model_dump(),
            blueprint={}, status="draft", validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)
    import pytest as _pytest

    with _pytest.raises(Exception) as excinfo:
        plot_twist_generation.generate_blueprint(db, bp_id)
    assert "provider" in str(excinfo.value.detail).lower()
    with db.sessions() as session:
        row = session.get(PlotTwistBlueprintRecord, bp_id)
        assert row.status == "generation_failed"
        assert "gsk" not in str(row.validation_errors).lower()


def test_provider_selection_is_config_only():
    from app.services.llm_client import LLMConfig, select_provider

    groq = LLMConfig(base_url="https://api.groq.com/openai/v1", model="m", provider="auto")
    assert select_provider(groq) == "groq"
    assert select_provider(LLMConfig(base_url="https://x.openai.azure.com/openai/v1", model="d", provider="auto")) == "openai"
    assert select_provider(LLMConfig(base_url="https://api.groq.com/openai/v1", model="m", provider="openai")) == "openai"
    assert select_provider(LLMConfig(base_url="https://other/v1", model="m", provider="groq")) == "groq"
    import pytest as _pytest

    with _pytest.raises(Exception):
        select_provider(LLMConfig(base_url="https://x/v1", model="m", provider="watson"))


def test_strict_schema_transform_satisfies_strict_mode():
    from app.services.plot_twist_schemas import (
        package_json_schema,
        scene_batch_json_schema,
        blueprint_json_schema,
    )

    def check(node):
        if isinstance(node, list):
            for item in node:
                check(item)
            return
        if not isinstance(node, dict):
            return
        for choice in node.get("anyOf") or []:
            assert not (isinstance(choice, dict) and choice.get("type") == "null"), node
        properties = node.get("properties")
        if isinstance(properties, dict):
            assert set(node.get("required") or []) >= set(properties), node
            for value in properties.values():
                check(value)
        if node.get("type") == "object" or "properties" in node:
            assert node.get("additionalProperties") is False, node
        for value in node.values():
            if isinstance(value, (dict, list)):
                check(value)

    for schema_fn in (blueprint_json_schema, package_json_schema, scene_batch_json_schema):
        check(schema_fn())


def test_rate_limit_detection_covers_groq_tpm_refusals():
    from app.services import llm_client

    class FakeResponse:
        headers = {"retry-after-ms": "1500"}

    class GroqTpmError(Exception):
        def __init__(self):
            super().__init__(
                "Error code: 413 - {'type': 'tokens', 'code': 'rate_limit_exceeded', "
                "'message': 'Request too large ... on tokens per minute (TPM): Limit 8000'}"
            )
            self.response = FakeResponse()

    assert llm_client._is_rate_limited(GroqTpmError()) is True
    assert llm_client._is_rate_limited(TimeoutError("timed out")) is False
    assert llm_client._retry_after_seconds(GroqTpmError()) == 1.5
    assert llm_client._retry_after_seconds(RuntimeError("nope")) is None
    classified = llm_client._classify_provider_error(GroqTpmError())
    assert classified.code == "rate_limited"
    assert "gsk" not in classified.detail.lower()


def test_minimized_blueprint_drops_prose_but_keeps_contract():
    from app.services.plot_twist_generation import _minimized_blueprint

    minimized = _minimized_blueprint(BLUEPRINT)
    assert minimized["title"] == BLUEPRINT["title"]
    assert minimized["state_vars"][0]["name"] == "courage"
    assert "min_value" in minimized["state_vars"][0]
    assert minimized["slots"][0]["name"] == "side"
    assert minimized["endings"][0]["id"] == "e_escape"
    dumped = str(minimized)
    assert "A one-night auditorium escape." not in dumped


def test_llm_error_classification():
    from app.services.llm_client import _classify_provider_error

    assert _classify_provider_error(TimeoutError("timed out")).code == "timeout"

    class RateLimit(Exception):
        pass

    RateLimit.__name__ = "RateLimitError"
    assert _classify_provider_error(RateLimit("429 rate limit")).code == "rate_limited"
    assert _classify_provider_error(ConnectionError("unreachable")).code == "network"
    err = _classify_provider_error(RuntimeError("401 auth bad Bearer gsk_secret"))
    assert err.code == "model_error"
    assert "gsk_secret" not in err.detail


def test_publish_refuses_invalid_package(db):
    import copy

    seed = seed_published(db)
    with db.sessions.begin() as session:
        row = session.get(PlotTwistPackageRecord, seed["package_id"])
        # Deep copy: a shallow copy shares nested objects with the loaded
        # snapshot, which defeats SQLAlchemy's JSON change detection.
        package = copy.deepcopy(dict(row.package))
        package["scenes"][0]["choices"][0]["next_scene_id"] = "s_missing"
        row.package = package
        row.status = "ready_for_review"
    import pytest as _pytest

    with _pytest.raises(Exception) as excinfo:
        plot_twist_generation.publish_package(db, seed["package_id"])
    assert excinfo.value.status_code == 422
    with db.sessions() as session:
        row = session.get(PlotTwistPackageRecord, seed["package_id"])
        assert row.status == "validation_failed"


# --- gameplay ------------------------------------------------------------------


def test_async_handoff_between_participants(http, db, players):
    seed_published(db)
    client_a, client_b = pair(db, http, players)

    state_a = client_a.get("/games/plot-twist").json()
    assert state_a["stage"] == "awaiting_you"
    assert state_a["scene"]["scene_id"] == "s_open"
    # The assignee sees choices; nothing leaks beyond the current scene.
    assert {c["choice_id"] for c in state_a["scene"]["choices"]} == {"c_loading", "c_lobby"}

    response = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    assert response.status_code == 200, response.text
    moved = response.json()
    assert moved["scene"]["scene_id"] == "s_dock"

    # Bob now holds the decision; Alice waits without seeing his choices.
    state_b = client_b.get("/games/plot-twist").json()
    assert state_b["stage"] == "awaiting_you"
    assert {c["choice_id"] for c in state_b["scene"]["choices"]} == {"c_careful", "c_force"}
    state_a2 = client_a.get("/games/plot-twist").json()
    assert state_a2["stage"] == "awaiting_partner"
    assert state_a2["scene"]["choices"] == []
    assert state_a2["scene"]["narrative_text"] == state_b["scene"]["narrative_text"]

    response = client_b.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_careful", "state_version": state_b["state_version"]},
    )
    assert response.status_code == 200, response.text
    done = response.json()
    assert done["stage"] == "completed"
    assert done["result"]["ending_id"] == "e_escape"
    assert done["result"]["success"] is True
    for viewer in (client_a, client_b):
        result = viewer.get("/games/plot-twist").json()
        assert result["stage"] == "completed"
        assert result["result"]["ending_title"] == "Great Escape"
        assert len(result["history"]) == 2


def test_turns_strictly_alternate_even_on_shared_scenes(http, db, players):
    package = story_package()
    # The dock becomes a shared moment: whoever's turn it is answers, and
    # the turn still passes to the other member afterwards.
    package["scenes"][1]["actor"] = "either"
    assert validate_package(package).ok
    seed_published(db, package=package)
    client_a, client_b = pair(db, http, players)
    state_a = client_a.get("/games/plot-twist").json()
    assert state_a["stage"] == "awaiting_you"
    moved = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    assert moved.status_code == 200, moved.text
    # Same member cannot answer twice running, even on a shared scene.
    again = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_careful", "state_version": moved.json()["state_version"]},
    )
    assert again.status_code == 403, again.text
    # The partner holds the shared turn and sees the choices.
    state_b = client_b.get("/games/plot-twist").json()
    assert state_b["stage"] == "awaiting_you"
    assert {c["choice_id"] for c in state_b["scene"]["choices"]} == {"c_careful", "c_force"}
    done = client_b.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_force", "state_version": state_b["state_version"]},
    )
    assert done.status_code == 200, done.text
    assert done.json()["stage"] == "completed"
    # Both sides read the same settled outcome.
    assert client_a.get("/games/plot-twist").json()["stage"] == "completed"


def test_repeated_loops_funnel_out_instead_of_repeating(http, db, players):
    package = story_package()
    # A visible loop with an exit: dock <-> side room, plus side -> caught.
    package["scenes"][1]["choices"].append(
        {"id": "c_back", "text": "Check the side room.", "conditions": [],
         "state_changes": [], "next_scene_id": "s_side"}
    )
    package["scenes"].append({
        "id": "s_side", "type": "encounter", "title": "Side Room",
        "narrative_text": "Dusty shelves.", "actor": "either", "slot": "core",
        "requires_tags": [], "provides_tags": [], "forbidden_tags": [],
        "entry_conditions": [], "is_terminal": False,
        "choices": [
            {"id": "c_return", "text": "Return to the dock.", "conditions": [],
             "state_changes": [], "next_scene_id": "s_dock"},
            {"id": "c_out", "text": "Climb out the window.", "conditions": [],
             "state_changes": [], "next_scene_id": "s_caught"},
        ],
    })
    assert validate_package(package).ok
    seed_published(db, package=package)
    client_a, client_b = pair(db, http, players)

    def act(client, choice_id):
        state = client.get("/games/plot-twist").json()
        assert state["stage"] == "awaiting_you", state
        response = client.post(
            "/games/plot-twist/choices",
            json={"choice_id": choice_id, "state_version": state["state_version"]},
        )
        assert response.status_code == 200, response.text
        return response.json()

    act(client_a, "c_loading")  # s_dock, visits: dock 1
    act(client_b, "c_back")  # s_side, visits: side 1
    act(client_a, "c_return")  # s_dock, visits: dock 2
    act(client_b, "c_back")  # s_side, visits: side 2
    # The side room has been seen twice: going back to the dock is withheld
    # and only the way out remains.
    state_a = client_a.get("/games/plot-twist").json()
    assert state_a["stage"] == "awaiting_you"
    assert {c["choice_id"] for c in state_a["scene"]["choices"]} == {"c_out"}
    done = act(client_a, "c_out")
    assert done["stage"] == "completed"


def test_decision_budget_concludes_runaway_stories(http, db, players):
    # A long straight corridor: no loops, so only the budget can end it.
    package = story_package(max_decisions=5)
    chain = ["s_open", "s_m1", "s_m2", "s_m3", "s_m4", "s_m5"]
    actors = ["participant_one", "participant_two", "participant_one",
              "participant_two", "participant_one", "participant_two"]
    scenes = []
    for index, scene_id in enumerate(chain):
        nxt = chain[index + 1] if index + 1 < len(chain) else "s_free"
        scenes.append({
            "id": scene_id, "type": "opening" if index == 0 else "encounter",
            "title": f"Step {index}", "narrative_text": "Onward.",
            "actor": actors[index], "slot": "core",
            "requires_tags": [], "provides_tags": [], "forbidden_tags": [],
            "entry_conditions": [], "is_terminal": False,
            "choices": [{"id": "c_on", "text": "Press on.", "conditions": [],
                         "state_changes": [], "next_scene_id": nxt}],
        })
    package["scenes"] = scenes + [
        s for s in package["scenes"] if s.get("id") == "s_free"
    ]
    package["opening_scene_id"] = "s_open"
    assert validate_package(package).ok
    seed_published(db, package=package)
    client_a, client_b = pair(db, http, players)

    def act(client):
        state = client.get("/games/plot-twist").json()
        assert state["stage"] == "awaiting_you", state
        response = client.post(
            "/games/plot-twist/choices",
            json={"choice_id": "c_on", "state_version": state["state_version"]},
        )
        assert response.status_code == 200, response.text
        return response.json()

    act(client_a)
    act(client_b)
    act(client_a)
    act(client_b)
    done = act(client_a)
    assert done["stage"] == "completed"
    assert done["result"]["decisions_made"] == 5
    # Best qualifying ending wins: silver escape over participation failure.
    assert done["result"]["ending_id"] == "e_escape"


def test_validation_refuses_unalternating_and_solo_packages():
    package = story_package()
    package["scenes"][1]["actor"] = "participant_one"
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "repeated_strict_actor" for e in report.errors)

    shared = story_package()
    for scene in shared["scenes"]:
        if not scene.get("is_terminal"):
            scene["actor"] = "joint"
    report = validate_package(shared)
    assert not report.ok
    assert any(e.code == "shared_scene_heavy" for e in report.errors)


def test_validation_refuses_inescapable_cycles():
    package = story_package()
    # Prune every exit: the dock and lobby only point at each other.
    package["scenes"][1]["choices"] = [
        {"id": "c_back", "text": "To the lobby.", "conditions": [],
         "state_changes": [], "next_scene_id": "s_alarm"}
    ]
    package["scenes"][2]["choices"] = [
        {"id": "c_return", "text": "To the dock.", "conditions": [],
         "state_changes": [], "next_scene_id": "s_dock"}
    ]
    report = validate_package(package)
    assert not report.ok
    assert any(e.code == "inescapable_cycle" for e in report.errors)


def test_wrong_participant_cannot_submit(http, db, players):
    seed_published(db)
    client_a, client_b = pair(db, http, players)
    state_a = client_a.get("/games/plot-twist").json()
    # s_open belongs to participant_one (Alex): Sam's submit is refused.
    response = client_b.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    assert response.status_code == 403, response.text


def test_invalid_and_stale_choices_are_rejected(http, db, players):
    seed_published(db)
    client_a, client_b = pair(db, http, players)
    state_a = client_a.get("/games/plot-twist").json()
    bad = client_a.post(
        "/games/plot-twist/choices", json={"choice_id": "nope", "state_version": 0}
    )
    assert bad.status_code == 400, bad.text
    moved = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    assert moved.status_code == 200, moved.text
    stale = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_careful", "state_version": state_a["state_version"]},
    )
    # Same version, different choice: stale, not a replay.
    assert stale.status_code == 409, stale.text


def test_duplicate_submit_answers_stored_outcome(http, db, players):
    seed_published(db)
    client_a, _ = pair(db, http, players)
    state_a = client_a.get("/games/plot-twist").json()
    first = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    assert first.status_code == 200, first.text
    second = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    assert second.status_code == 200, second.text
    assert second.json()["scene"]["scene_id"] == "s_dock"
    with db.sessions() as session:
        count = session.scalar(
            select(func.count()).select_from(PlotTwistDecisionRecord)
        )
        assert count == 1


def test_concurrent_submits_apply_once(db, players):
    from app.services.plot_twist_gameplay import submit_choice

    seed_published(db)
    seed_questionnaire(db)
    plot_twist_gameplay.choose_partner(db, players["a"], B_ID)
    plot_twist_gameplay.choose_partner(db, players["b"], A_ID)

    def attempt(choice_id):
        try:
            state = submit_choice(db, players["a"], choice_id, 0)
            return ("ok", state.scene.scene_id if state.scene else None)
        except Exception as exc:  # noqa: BLE001 - the code is the assertion
            return ("error", getattr(exc, "status_code", None))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ["c_loading", "c_lobby"]))
    assert sorted(r[0] for r in results) == ["error", "ok"]
    with db.sessions() as session:
        rows = session.scalars(select(PlotTwistDecisionRecord)).all()
        assert len(rows) == 1
        game = session.scalars(select(PlotTwistSessionRecord)).one()
        assert int(game.state_version) == 1


def test_completed_session_cannot_replay(http, db, players):
    seed_published(db)
    client_a, client_b = pair(db, http, players)
    state_a = client_a.get("/games/plot-twist").json()
    client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_lobby", "state_version": state_a["state_version"]},
    )
    state_b = client_b.get("/games/plot-twist").json()
    done = client_b.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_guess", "state_version": state_b["state_version"]},
    )
    assert done.json()["stage"] == "completed"
    replay = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": 99},
    )
    assert replay.status_code == 400, replay.text


def test_module_selection_is_deterministic_and_stable(http, db, players):
    package = story_package()
    package["scenes"].append({
        "id": "s_side_a", "type": "encounter", "title": "Side A",
        "narrative_text": "A.", "actor": "either", "slot": "side",
        "requires_tags": ["indoors"], "provides_tags": ["side_a"],
        "forbidden_tags": [], "entry_conditions": [], "is_terminal": False,
        "choices": [{"id": "c_back_a", "text": "Back.", "conditions": [],
                     "state_changes": [], "next_scene_id": "s_caught"}],
    })
    package["scenes"].append({
        "id": "s_side_b", "type": "encounter", "title": "Side B",
        "narrative_text": "B.", "actor": "either", "slot": "side",
        "requires_tags": ["indoors"], "provides_tags": ["side_b"],
        "forbidden_tags": [], "entry_conditions": [], "is_terminal": False,
        "choices": [{"id": "c_back_b", "text": "Back.", "conditions": [],
                     "state_changes": [], "next_scene_id": "s_caught"}],
    })
    # Reachable: the dock offers both detours, so validation passes and the
    # couple's persisted selection decides which variant they actually play.
    package["scenes"][1]["choices"].append(
        {"id": "c_detour", "text": "Check the side room.", "conditions": [],
         "state_changes": [], "next_scene_id": "s_side_a"}
    )
    package["scenes"][1]["choices"].append(
        {"id": "c_detour2", "text": "Check the other side room.", "conditions": [],
         "state_changes": [], "next_scene_id": "s_side_b"}
    )
    assert validate_package(package, blueprint=BLUEPRINT).ok
    seed_published(db, package=package)
    client_a, client_b = pair(db, http, players)
    with db.sessions() as session:
        row = session.scalars(select(PlotTwistSessionRecord)).one()
        first = dict(row.selected_scenes)
    assert set(first.get("slots", {}).get("side", [])) <= {"s_side_a", "s_side_b"}
    assert len(first.get("slots", {}).get("side", [])) == 1
    # Playing never reshuffles: the selection still matches after decisions.
    state_a = client_a.get("/games/plot-twist").json()
    client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    with db.sessions() as session:
        row = session.scalars(select(PlotTwistSessionRecord)).one()
        assert dict(row.selected_scenes) == first


# --- scoring -------------------------------------------------------------------


def _finish_escape(db, http, players):
    seed_published(db)
    client_a, client_b = pair(db, http, players)
    state_a = client_a.get("/games/plot-twist").json()
    client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    state_b = client_b.get("/games/plot-twist").json()
    done = client_b.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_careful", "state_version": state_b["state_version"]},
    )
    assert done.json()["stage"] == "completed"
    with db.sessions() as session:
        game = session.scalars(select(PlotTwistGameRecord)).one()
        return str(game.id)


def _end_game(db, game_id):
    with db.sessions.begin() as session:
        game = session.get(PlotTwistGameRecord, game_id)
        game.status = "ended"


def test_plot_twist_is_a_couple_game_with_bounded_base():
    assert competition_type_for("plot-twist") == "couple"
    assert int(global_base_for("plot-twist", {"base_points": 80})) == 80
    assert int(global_base_for("plot-twist", {"base_points": 400})) == 100


def test_finalize_awards_once_and_doubles_when_featured(db, http, players):
    game_id = _finish_escape(db, http, players)
    _end_game(db, game_id)
    first = league_finalization.finalize_opportunity(db, "plot-twist", game_id)
    assert first["action"] == "finalized"
    assert first["participants"] == 1
    with db.sessions() as session:
        award = session.scalars(
            select(GameScoreAwardRecord).where(
                GameScoreAwardRecord.game_id == "plot-twist",
                GameScoreAwardRecord.opportunity_id == game_id,
            )
        ).one()
        # silver (80) with no treasures: unfeatured base passes through.
        assert float(award.awarded_points) == 80.0
        assert award.member_a_id is not None and award.member_b_id is not None
        assert award.user_id is None
    # Idempotent: a second finalization settles on the surviving live row.
    second = league_finalization.finalize_opportunity(db, "plot-twist", game_id)
    assert second["awards"] == 0
    with db.sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(GameScoreAwardRecord).where(
                    GameScoreAwardRecord.game_id == "plot-twist",
                    GameScoreAwardRecord.opportunity_id == game_id,
                )
            )
            == 1
        )


def test_featured_game_earns_double(db, http, players):
    game_id = _finish_escape(db, http, players)
    with db.sessions.begin() as session:
        game = session.get(PlotTwistGameRecord, game_id)
        session.add(
            FeaturedGameRecord(
                game_date=game.period_start_date or game.game_date,
                game_id="plot-twist",
                created_by="adminuser",
            )
        )
    _end_game(db, game_id)
    league_finalization.finalize_opportunity(db, "plot-twist", game_id)
    with db.sessions() as session:
        award = session.scalars(
            select(GameScoreAwardRecord).where(
                GameScoreAwardRecord.game_id == "plot-twist",
                GameScoreAwardRecord.opportunity_id == game_id,
            )
        ).one()
        assert float(award.awarded_points) == 160.0


# --- admin API ------------------------------------------------------------------


def test_admin_blueprint_requires_approval_before_package(http, db, players):
    _ = players
    app.dependency_overrides[get_current_user] = override_get_current_user
    from conftest import override_get_admin_user

    app.dependency_overrides[get_current_user] = override_get_admin_user
    try:
        admin = http
        created = admin.post("/admin/plot-twist/blueprints", json={"difficulty": "easy"})
        assert created.status_code == 201, created.text
        bp_id = created.json()["id"]
        # Stage B before approval is refused.
        refused = admin.post(f"/admin/plot-twist/blueprints/{bp_id}/packages", json={})
        assert refused.status_code == 400, refused.text
    finally:
        app.dependency_overrides[get_current_user] = override_get_current_user


def test_admin_endpoints_require_staff(http, db, players):
    _ = (db, players)
    client = client_for(http, players["a"])
    assert client.get("/admin/plot-twist/blueprints").status_code == 403
    assert client.post("/admin/plot-twist/blueprints", json={"difficulty": "easy"}).status_code == 403
