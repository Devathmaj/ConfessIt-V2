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


def test_simulate_path_handles_partial_walks():
    """The admin preview may walk a path that stops before an ending; that is
    a valid partial path, not a crash on the missing ending id."""
    from app.services.plot_twist_generation import simulate_path

    partial = simulate_path(story_package(), ["c_loading"])
    assert partial["valid"] is True
    assert partial["ending_id"] is None
    assert partial["steps"][-1]["next_scene_id"] == "s_dock"
    # A full walk still reports its ending.
    full = simulate_path(story_package(), ["c_loading", "c_careful"])
    assert full["valid"] is True
    assert full["ending_id"] == "e_escape"


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
    stranded = [e for e in report.errors if e.code == "unreachable_scenes"]
    assert [e.scene_id for e in stranded] == ["s_lost"]  # per-scene: patchable


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


def test_blueprint_scale_budgets_are_enforced():
    """A blueprint whose main path or slot list dwarfs the configured decision
    count is refused: it would force Stage B far past the scene ceiling (a
    24-beat main path became a 29-scene package)."""
    long_path = dict(BLUEPRINT, main_path=[f"beat-{index}" for index in range(20)])
    assert validate_blueprint(long_path).ok  # budgets are opt-in
    report = validate_blueprint(long_path, decision_count=8, interchangeable_count=4)
    assert {error.code for error in report.errors} == {"blueprint_main_path_budget"}
    within = dict(BLUEPRINT, main_path=[f"beat-{index}" for index in range(10)])
    assert validate_blueprint(within, decision_count=8, interchangeable_count=4).ok
    many_slots = dict(
        BLUEPRINT,
        slots=[dict(BLUEPRINT["slots"][0], name=f"side-{index}") for index in range(10)],
    )
    report = validate_blueprint(many_slots, decision_count=8, interchangeable_count=4)
    assert {error.code for error in report.errors} == {"blueprint_slot_budget"}


def test_unreachable_alternatives_are_playable_through_module_selection():
    """Interchangeable scenes are entered through module selection: a
    transition into any scene of a slot resolves to the couple's alternative,
    so slot-mates of a reachable scene are not stranded; only scenes in
    unreached slots (or lost in core) are."""
    from types import SimpleNamespace

    from app.services.plot_twist_validation import _stranded_scene_ids

    by_scene = {
        "open": SimpleNamespace(slot="core"),
        "side-a": SimpleNamespace(slot="side"),
        "side-b": SimpleNamespace(slot="side"),
        "orphan": SimpleNamespace(slot="core"),
        "lonely": SimpleNamespace(slot="other"),
    }
    assert _stranded_scene_ids(by_scene, {"open", "side-a"}) == ["orphan", "lonely"]


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


def test_truncated_initial_package_answer_retries_once(db, monkeypatch):
    """A package answer cut off by the output-token budget is a stochastic
    overrun: the initial call retries once instead of failing the request."""
    from app.services import llm_client

    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="Locked In", difficulty="medium",
            config=GenerationConfig().model_dump(),
            blueprint=BLUEPRINT, status="approved",
            validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)

    calls = {"n": 0}

    def fake_generate(**kwargs):
        if kwargs.get("schema_name") != "plot_twist_package":
            return {"scenes": []}, {"strict_schema": True}
        calls["n"] += 1
        if calls["n"] == 1:
            raise llm_client.LLMError(
                "The model's answer was cut off by the output-token budget.",
                code="truncated",
            )
        return story_package(), {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    result = plot_twist_generation.generate_package(db, bp_id, created_by="adminuser")
    assert result["status"] == "ready_for_review"
    assert calls["n"] == 2


def test_truncated_package_repair_retries_within_the_cap(db, monkeypatch):
    """A cut-off repair answer spends the next attempt on the same repair
    instead of failing the whole generation with a 502."""
    from app.services import llm_client

    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="Locked In", difficulty="medium",
            config=GenerationConfig().model_dump(),
            blueprint=BLUEPRINT, status="approved",
            validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)

    calls = {"n": 0}

    def fake_generate(**kwargs):
        if kwargs.get("schema_name") != "plot_twist_package":
            return {"scenes": []}, {"strict_schema": True}
        calls["n"] += 1
        if calls["n"] == 1:
            # An opening scene that does not exist forces one repair.
            return dict(story_package(), opening_scene_id="missing"), {"strict_schema": True}
        if calls["n"] == 2:
            raise llm_client.LLMError(
                "The model's answer was cut off by the output-token budget.",
                code="truncated",
            )
        return story_package(), {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    result = plot_twist_generation.generate_package(db, bp_id, created_by="adminuser")
    assert result["status"] == "ready_for_review"
    assert calls["n"] == 3


def _package_with_wrong_actor():
    """``s_open`` (participant_one) leading into a participant_one ``s_dock``
    breaks alternation with exactly one scene-local error."""
    package = story_package()
    for scene in package["scenes"]:
        if scene["id"] == "s_dock":
            scene["actor"] = "participant_one"
    return package


def test_package_repair_patches_scenes_before_regenerating(db, monkeypatch):
    """Scene-local errors are fixed by a small patch request carrying only the
    implicated scenes -- not by regenerating the whole package."""
    from app.services import llm_client

    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="Locked In", difficulty="medium",
            config=GenerationConfig().model_dump(),
            blueprint=BLUEPRINT, status="approved",
            validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)

    calls = {"package": 0, "fix": 0}

    def fake_generate(**kwargs):
        name = kwargs.get("schema_name")
        if name == "plot_twist_package":
            calls["package"] += 1
            return _package_with_wrong_actor(), {"strict_schema": True}
        if name == "plot_twist_scene_fix":
            calls["fix"] += 1
            return {"slot": "core", "scenes": story_package()["scenes"]}, {"strict_schema": True}
        return {"scenes": []}, {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    result = plot_twist_generation.generate_package(db, bp_id, created_by="adminuser")
    assert result["status"] == "ready_for_review"
    assert calls == {"package": 1, "fix": 1}


def test_package_repair_falls_back_to_full_regeneration(db, monkeypatch):
    """When a patch cannot reduce the error count, the loop still regenerates
    the whole package rather than looping on patches forever."""
    from app.services import llm_client

    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="Locked In", difficulty="medium",
            config=GenerationConfig().model_dump(),
            blueprint=BLUEPRINT, status="approved",
            validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)

    calls = {"package": 0, "fix": 0}

    def fake_generate(**kwargs):
        name = kwargs.get("schema_name")
        if name == "plot_twist_package":
            calls["package"] += 1
            if calls["package"] == 1:
                return _package_with_wrong_actor(), {"strict_schema": True}
            return story_package(), {"strict_schema": True}
        if name == "plot_twist_scene_fix":
            calls["fix"] += 1
            # No improvement: the same broken scenes come straight back.
            return (
                {"slot": "core", "scenes": _package_with_wrong_actor()["scenes"]},
                {"strict_schema": True},
            )
        return {"scenes": []}, {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    result = plot_twist_generation.generate_package(db, bp_id, created_by="adminuser")
    assert result["status"] == "ready_for_review"
    assert calls == {"package": 2, "fix": 1}


def test_worse_regeneration_keeps_the_better_draft(db, monkeypatch):
    """A whole-package regeneration that adds errors is discarded: the stored
    package stays the best the loop reached, not whatever the last roll
    produced (the live run that ended at 14 errors after touching 5)."""
    from app.services import llm_client

    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="Locked In", difficulty="medium",
            config=GenerationConfig().model_dump(),
            blueprint=BLUEPRINT, status="approved",
            validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)

    def worse():
        package = _package_with_wrong_actor()
        for scene in package["scenes"]:
            if scene["id"] == "s_alarm":
                scene["actor"] = "participant_one"  # a second broken handoff
        return package

    calls = {"package": 0, "fix": 0}

    def fake_generate(**kwargs):
        name = kwargs.get("schema_name")
        if name == "plot_twist_package":
            calls["package"] += 1
            if calls["package"] == 1:
                return _package_with_wrong_actor(), {"strict_schema": True}
            return worse(), {"strict_schema": True}
        if name == "plot_twist_scene_fix":
            calls["fix"] += 1
            # The patch cannot improve anything.
            return (
                {"slot": "core", "scenes": _package_with_wrong_actor()["scenes"]},
                {"strict_schema": True},
            )
        return {"scenes": []}, {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    result = plot_twist_generation.generate_package(db, bp_id, created_by="adminuser")
    assert result["status"] == "validation_failed"
    assert len(result["validation_errors"]) == 1  # the better draft, not the worse one
    assert calls["package"] >= 2


def test_interchangeable_batches_respect_the_scene_ceiling(monkeypatch):
    """A batch append stops at the scene ceiling instead of overshooting it
    with a whole batch."""
    from app.services import llm_client
    from app.services.plot_twist_generation import _append_interchangeable_batches

    def fake_generate(**kwargs):
        return {
            "slot": "side",
            "scenes": [{"id": f"side-{index}", "slot": "side"} for index in range(8)],
        }, {"strict_schema": True}

    monkeypatch.setattr(llm_client, "generate_structured_json", fake_generate)
    payload = {"scenes": [{"id": "core-1", "slot": "core"}]}
    blueprint = {"slots": [{"name": "side", "accepts_tags": []}]}
    config = GenerationConfig(max_scenes=5, interchangeable_count=4)
    _append_interchangeable_batches(payload, blueprint, config)
    assert [scene["id"] for scene in payload["scenes"]] == [
        "core-1", "side-0", "side-1", "side-2", "side-3",
    ]


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
    # The documented spellings: foundry is the OpenAI-compatible path, and
    # the value is trimmed and lowercased before validation. An unset
    # provider is Groq, the provider the application shipped with.
    assert select_provider(LLMConfig(base_url="https://other/v1", model="m", provider="foundry")) == "foundry"
    assert select_provider(LLMConfig(base_url="https://other/v1", model="m", provider="  FOUNDry ")) == "foundry"
    assert select_provider(LLMConfig(base_url="https://other/v1", model="m")) == "groq"
    import pytest as _pytest

    with _pytest.raises(Exception):
        select_provider(LLMConfig(base_url="https://x/v1", model="m", provider="watson"))


def test_foundry_request_uses_openai_sdk_with_normalized_endpoint(monkeypatch):
    """foundry talks through the OpenAI SDK with the deployment name, a base
    URL stripped of any method path copied from the portal, and the token
    budget passed as max_completion_tokens (what reasoning models accept)."""
    import sys
    from types import SimpleNamespace

    from app.services import llm_client

    calls: dict = {}

    class _Message:
        content = '{"ok": true}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            calls.update(kwargs)
            return _Response()

    class _Chat:
        completions = _Completions()

    class _OpenAI:
        def __init__(self, **kwargs):
            calls["client_kwargs"] = kwargs

        chat = _Chat()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_OpenAI))
    config = llm_client.LLMConfig(
        base_url="https://confessit-model.services.ai.azure.com/openai/v1/responses",
        model="gpt-5-mini",
        provider="foundry",
        api_key="test-key",
    )
    payload, used_fallback = llm_client._generate_via_foundry(
        config, "system", "user", {"type": "object"}, "test_schema",
        "op-1", 30, 2048,
    )
    assert payload == {"ok": True}
    assert used_fallback is False
    assert calls["client_kwargs"] == {
        "base_url": "https://confessit-model.services.ai.azure.com/openai/v1",
        "api_key": "test-key",
    }
    assert calls["model"] == "gpt-5-mini"
    assert calls["max_completion_tokens"] == 2048
    assert calls["response_format"]["json_schema"]["name"] == "test_schema"
    assert calls["response_format"]["json_schema"]["strict"] is True
    # Reasoning deployments get the fixed low effort: hidden reasoning counts
    # against max_completion_tokens and once consumed a whole 16k budget.
    assert calls["reasoning_effort"] == "low"


def test_foundry_fallback_prompt_carries_the_schema(monkeypatch):
    """Without provider-enforced strict mode the schema still travels in the
    prompt; otherwise the model invents field names (live failure: state_vars
    came back as id/var_type/bounds instead of name/kind/min_value/max_value)."""
    import sys
    from types import SimpleNamespace

    from app.services import llm_client

    calls: list = []

    class _Message:
        content = '{"ok": true}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs["response_format"]["type"] == "json_schema":
                raise Exception(
                    "Invalid schema for response_format: schema must have a 'type' key."
                )
            return _Response()

    class _Chat:
        completions = _Completions()

    class _OpenAI:
        def __init__(self, **kwargs):
            pass

        chat = _Chat()

    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=_OpenAI))
    config = llm_client.LLMConfig(
        base_url="https://r.services.ai.azure.com/openai/v1",
        model="gpt-5-mini",
        provider="foundry",
        api_key="test-key",
    )
    schema = {
        "type": "object",
        "properties": {"state_vars": {"type": "array", "items": {"type": "object"}}},
        "required": ["state_vars"],
    }
    payload, used_fallback = llm_client._generate_via_foundry(
        config, "system prompt", "user prompt", schema, "plot_twist_blueprint",
        "op-fallback", 30, 2048,
    )
    assert payload == {"ok": True}
    assert used_fallback is True
    assert len(calls) == 2  # strict refused, JSON-object fallback used
    fallback_user = calls[1]["messages"][1]["content"]
    assert "user prompt" in fallback_user
    assert '"state_vars"' in fallback_user
    assert calls[1]["response_format"] == {"type": "json_object"}


def test_groq_fallback_prompt_carries_the_schema(monkeypatch):
    import sys
    from types import SimpleNamespace

    from app.services import llm_client

    calls: list = []

    class _Message:
        content = '{"ok": true}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            calls.append(kwargs)
            if kwargs["response_format"]["type"] == "json_schema":
                raise Exception("response_format json_schema is not supported.")
            return _Response()

    class _Chat:
        completions = _Completions()

    class _Groq:
        def __init__(self, **kwargs):
            pass

        chat = _Chat()

    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=_Groq))
    config = llm_client.LLMConfig(
        base_url="https://api.groq.com/openai/v1",
        model="qwen/qwen3.8-27b",
        provider="groq",
        api_key="test-key",
    )
    schema = {
        "type": "object",
        "properties": {"state_vars": {"type": "array", "items": {"type": "object"}}},
        "required": ["state_vars"],
    }
    payload, used_fallback = llm_client._generate_via_groq(
        config, "system prompt", "user prompt", schema, "plot_twist_blueprint",
        "op-fallback", 30, 2048,
    )
    assert payload == {"ok": True}
    assert used_fallback is True
    assert len(calls) == 2  # strict refused, JSON-object fallback used
    fallback_user = calls[1]["messages"][1]["content"]
    assert "user prompt" in fallback_user
    assert '"state_vars"' in fallback_user
    assert calls[1]["response_format"] == {"type": "json_object"}
    assert calls[1]["reasoning_effort"] == "default"  # sampling survives the fallback


def test_foundry_reasoning_effort_only_for_reasoning_deployments():
    from app.services.llm_client import _foundry_sampling_params

    assert _foundry_sampling_params("gpt-5-mini") == {"reasoning_effort": "low"}
    assert _foundry_sampling_params("GPT-5.1") == {"reasoning_effort": "low"}
    assert _foundry_sampling_params("o1") == {"reasoning_effort": "low"}
    assert _foundry_sampling_params("o3-mini") == {"reasoning_effort": "low"}
    # Non-reasoning and custom deployments reject the parameter.
    assert _foundry_sampling_params("gpt-4o-mini") == {}
    assert _foundry_sampling_params("my-custom-deployment") == {}
    assert _foundry_sampling_params("oasis-embed") == {}
    assert _foundry_sampling_params(None) == {}


def test_truncated_model_answer_names_the_token_budget_not_json():
    """A reasoning model that burns the whole output budget returns empty or
    partial content with finish_reason='length'; the admin must be told about
    the budget, not about invalid JSON."""
    from types import SimpleNamespace

    import pytest as _pytest

    from app.services import llm_client

    truncated = SimpleNamespace(
        finish_reason="length", message=SimpleNamespace(content="")
    )
    with _pytest.raises(llm_client.LLMError) as caught:
        llm_client._parse_completion(truncated)
    assert caught.value.code == "truncated"
    assert "cut off" in caught.value.detail
    # Complete JSON that merely reports the length limit is still accepted.
    complete = SimpleNamespace(
        finish_reason="length", message=SimpleNamespace(content='{"ok": true}')
    )
    assert llm_client._parse_completion(complete) == {"ok": True}


def test_foundry_base_url_normalization_keeps_custom_endpoints():
    from app.services.llm_client import _foundry_base_url

    for suffix in ("/responses", "/chat/completions", "/responses/", ""):
        assert (
            _foundry_base_url(f"https://r.services.ai.azure.com/openai/v1{suffix}")
            == "https://r.services.ai.azure.com/openai/v1"
        )
    # A custom proxy prefix is kept whole; nothing else is rewritten.
    assert _foundry_base_url("https://gw.example.com/llm-proxy/") == "https://gw.example.com/llm-proxy"


def test_provider_switch_routes_and_passes_the_configured_model(monkeypatch):
    """Changing LLM_PROVIDER changes the path taken; the configured model is
    what the selected provider receives. No code change is involved."""
    from app.services import llm_client

    seen: list = []

    def fake_groq(config, *args, **kwargs):
        seen.append((llm_client.select_provider(config), config.model))
        return {"ok": True}, False

    def fake_foundry(config, *args, **kwargs):
        seen.append((llm_client.select_provider(config), config.model))
        return {"ok": True}, True

    monkeypatch.setattr(llm_client, "_generate_via_groq", fake_groq)
    monkeypatch.setattr(llm_client, "_generate_via_foundry", fake_foundry)
    common = dict(
        system="s", user="u", json_schema={"type": "object"},
        schema_name="x", operation="op",
    )

    _, meta = llm_client.generate_structured_json(
        config=llm_client.LLMConfig(
            base_url="https://api.groq.com/openai/v1",
            model="openai/gpt-oss-120b", provider="groq", api_key="k",
        ),
        **common,
    )
    assert seen[-1] == ("groq", "openai/gpt-oss-120b")
    assert meta["provider"] == "groq" and meta["model"] == "openai/gpt-oss-120b"
    assert meta["strict_schema"] is True

    _, meta = llm_client.generate_structured_json(
        config=llm_client.LLMConfig(
            base_url="https://r.services.ai.azure.com/openai/v1",
            model="gpt-5-mini", provider="foundry", api_key="k",
        ),
        **common,
    )
    assert seen[-1] == ("foundry", "gpt-5-mini")
    assert meta["provider"] == "foundry" and meta["model"] == "gpt-5-mini"
    assert meta["strict_schema"] is False


def test_foundry_missing_configuration_errors_without_groq_fallback(monkeypatch):
    """A foundry selection with an incomplete endpoint/key/model reports a
    clear configuration error; it must never reroute to Groq."""
    from app.services import llm_client

    def boom(*args, **kwargs):
        raise AssertionError("must not fall back to another provider")

    monkeypatch.setattr(llm_client, "_generate_via_groq", boom)
    monkeypatch.setattr(llm_client, "_generate_via_foundry", boom)
    common = dict(
        system="s", user="u", json_schema={"type": "object"},
        schema_name="x", operation="op",
    )
    for config in (
        llm_client.LLMConfig(base_url=None, model="gpt-5-mini", provider="foundry", api_key="k"),
        llm_client.LLMConfig(base_url="https://r.services.ai.azure.com/openai/v1", model=None, provider="foundry", api_key="k"),
        llm_client.LLMConfig(base_url="https://r.services.ai.azure.com/openai/v1", model="gpt-5-mini", provider="foundry", api_key=None),
    ):
        with pytest.raises(llm_client.LLMError) as excinfo:
            llm_client.generate_structured_json(config=config, **common)
        assert excinfo.value.code == "missing_config"
        assert excinfo.value.detail


def test_unsupported_provider_is_a_clear_configuration_error():
    import pytest as _pytest

    from app.services import llm_client

    with _pytest.raises(llm_client.LLMError) as excinfo:
        llm_client.generate_structured_json(
            system="s", user="u", json_schema={"type": "object"},
            schema_name="x", operation="op",
            config=llm_client.LLMConfig(
                base_url="https://x/v1", model="m", provider="watson", api_key="k",
            ),
        )
    assert "LLM_PROVIDER" in excinfo.value.detail
    assert "watson" in excinfo.value.detail


def test_plot_twist_generation_runs_through_foundry_provider(db, monkeypatch):
    """The real blueprint/approval/package pipeline, with only the Foundry SDK
    call mocked: prompts, repair bounds and validation all stay live."""
    from app.services import llm_client

    with db.sessions.begin() as session:
        row = PlotTwistBlueprintRecord(
            title="T", difficulty="medium", config=GenerationConfig().model_dump(),
            blueprint={}, status="draft", validation_errors=[], created_by="adminuser",
        )
        session.add(row)
        session.flush()
        bp_id = str(row.id)

    calls: list = []

    def fake_foundry(config, system, user, json_schema, schema_name, operation_id, timeout_s, max_tokens):
        calls.append((llm_client.select_provider(config), config.model, schema_name))
        if schema_name == "plot_twist_blueprint":
            return dict(BLUEPRINT), False
        if schema_name == "plot_twist_package":
            return dict(story_package()), False
        return {"scenes": []}, False

    monkeypatch.setattr(llm_client, "_generate_via_foundry", fake_foundry)
    config = llm_client.LLMConfig(
        base_url="https://confessit-model.services.ai.azure.com/openai/v1",
        model="gpt-5-mini", provider="foundry", api_key="test-key",
    )

    blueprint = plot_twist_generation.generate_blueprint(db, bp_id, llm_config=config)
    assert blueprint["status"] == "ready_for_review"
    approved = plot_twist_generation.approve_blueprint(db, bp_id)
    assert approved["status"] == "approved"
    package = plot_twist_generation.generate_package(
        db, bp_id, created_by="adminuser", llm_config=config
    )
    assert package["status"] == "ready_for_review"
    scenes = package["package"]["scenes"]
    assert any(scene.get("choices") for scene in scenes)
    assert package["package"]["endings"]
    assert calls and all(provider == "foundry" for provider, _, _ in calls)
    assert all(model == "gpt-5-mini" for _, model, _ in calls)


def test_groq_sampling_params_follow_model():
    from app.services.llm_client import _groq_sampling_params

    assert _groq_sampling_params("qwen/qwen3.8-27b") == {
        "temperature": 0.6,
        "top_p": 0.95,
        "reasoning_effort": "default",
    }
    assert _groq_sampling_params("QWEN/qwen3.8-27b ") == {
        "temperature": 0.6,
        "top_p": 0.95,
        "reasoning_effort": "default",
    }
    assert _groq_sampling_params("openai/gpt-oss-20b") == {
        "reasoning_effort": "medium"
    }
    assert _groq_sampling_params(None) == {"reasoning_effort": "medium"}


def test_groq_qwen_create_uses_qwen_decoding_params(monkeypatch):
    import sys
    from types import SimpleNamespace

    from app.services import llm_client

    calls: dict = {}

    class _Message:
        content = '{"ok": true}'

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    class _Completions:
        def create(self, **kwargs):
            calls.update(kwargs)
            return _Response()

    class _Chat:
        completions = _Completions()

    class _Groq:
        def __init__(self, **kwargs):
            calls["client_kwargs"] = kwargs

        chat = _Chat()

    monkeypatch.setitem(sys.modules, "groq", SimpleNamespace(Groq=_Groq))
    config = llm_client.LLMConfig(
        base_url="https://api.groq.com/openai/v1",
        model="qwen/qwen3.8-27b",
        provider="groq",
        api_key="test-key",
    )
    payload, used_fallback = llm_client._generate_via_groq(
        config, "system", "user", {"type": "object"}, "test_schema",
        "op-1", 30, 2048,
    )
    assert payload == {"ok": True}
    assert used_fallback is False
    assert calls["model"] == "qwen/qwen3.8-27b"
    assert calls["temperature"] == 0.6
    assert calls["top_p"] == 0.95
    assert calls["reasoning_effort"] == "default"
    assert calls["max_completion_tokens"] == 2048


def test_strict_schema_transform_satisfies_strict_mode():
    """Every node carries a type, objects are closed and fully required, and
    the free-form maps strict mode cannot express are gone or replaced."""
    from app.services.plot_twist_schemas import (
        package_json_schema,
        scene_batch_json_schema,
        blueprint_json_schema,
    )

    def check(node):
        assert isinstance(node, dict), node
        assert (
            {"type", "anyOf", "oneOf", "$ref", "enum", "const"} & set(node)
        ), f"typeless node: {node}"
        # Free-form maps (additionalProperties true / a schema) break strict mode.
        assert node.get("additionalProperties") is not True, node
        assert not isinstance(node.get("additionalProperties"), dict), node
        for branch in node.get("anyOf") or []:
            check(branch)
        if isinstance(node.get("items"), dict):
            check(node["items"])
        properties = node.get("properties")
        if isinstance(properties, dict):
            assert set(node.get("required") or []) == set(properties), node
            assert node.get("additionalProperties") is False, node
            for value in properties.values():
                check(value)
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, node
        for value in (node.get("$defs") or {}).values():
            check(value)

    for schema_fn in (blueprint_json_schema, package_json_schema, scene_batch_json_schema):
        check(schema_fn())
    check(
        package_json_schema(
            {
                "state_vars": [
                    {"name": "trust", "kind": "int", "min_value": 0, "max_value": 10},
                    {"name": "discoveries", "kind": "flag"},
                ]
            }
        )
    )


def test_package_schema_injects_blueprint_state_vars():
    """``initial_state`` is a free-form map strict mode cannot express, so the
    blueprint's variables are injected as an explicit typed object (with the
    declared integer bounds); the provider then enforces coverage itself."""
    from app.services.plot_twist_schemas import package_json_schema

    schema = package_json_schema(
        {
            "state_vars": [
                {"name": "trust", "kind": "int", "min_value": 0, "max_value": 10},
                {"name": "discoveries", "kind": "flag"},
            ]
        }
    )
    init = schema["properties"]["initial_state"]
    assert init["type"] == "object"
    assert init["additionalProperties"] is False
    assert set(init["required"]) == {"trust", "discoveries"}
    assert init["properties"]["trust"] == {"type": "integer", "minimum": 0, "maximum": 10}
    assert init["properties"]["discoveries"] == {"type": "array", "items": {"type": "string"}}
    assert "initial_state" in schema["required"]
    # Without a blueprint the map cannot be expressed and is omitted.
    assert "initial_state" not in package_json_schema()["properties"]


def test_package_schema_caps_the_scene_count():
    """``max_scenes`` becomes ``maxItems`` at the provider, so an oversized
    package cannot be emitted at all; the model must merge beats instead."""
    from app.services.plot_twist_schemas import package_json_schema

    schema = package_json_schema(max_scenes=7)
    assert schema["properties"]["scenes"]["maxItems"] == 7
    assert "maxItems" not in package_json_schema()["properties"]["scenes"]


def test_optional_fields_stay_nullable_in_strict_schemas():
    """Nullability survives: collapsing it forced placeholder values such as
    ``ending_id: ""`` in live output."""
    from app.services.plot_twist_schemas import package_json_schema

    schema = package_json_schema()
    ending_id = schema["$defs"]["SceneDef"]["properties"]["ending_id"]
    assert {"type": "null"} in ending_id.get("anyOf") or []


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


def test_start_enters_a_pair_formed_before_the_event(http, db, players):
    # Pair first while no Plot Twist event exists, then open the event: the
    # couple reaches the story through /start instead of re-picking.
    seed_questionnaire(db)
    client_a = client_for(http, players["a"])
    client_b = client_for(http, players["b"])
    client_a.post("/games/plot-twist/selection", json={"selected_user_id": B_ID})
    client_b.post("/games/plot-twist/selection", json={"selected_user_id": A_ID})
    # The event opens after the pair formed (its own global, created after).
    seed_published(db)

    first = client_a.post("/games/plot-twist/start")
    assert first.status_code == 200, first.text
    assert first.json()["stage"] == "awaiting_you"
    assert first.json()["scene"]["scene_id"] == "s_open"
    # Idempotent: entering again fetches the same session.
    second = client_a.post("/games/plot-twist/start")
    assert second.status_code == 200, second.text
    assert second.json()["session_id"] == first.json()["session_id"]
    # Without a mutual pair there is nothing to start.
    lonely = client_for(http, players["c"]).post("/games/plot-twist/start")
    assert lonely.status_code == 400, lonely.text


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


def _stage_ready_version(db, seed, version):
    """One more validated package version waiting for review."""
    import copy
    from app.db_models import PlotTwistPackageRecord

    with db.sessions.begin() as session:
        row = session.get(PlotTwistPackageRecord, seed["package_id"])
        package = copy.deepcopy(dict(row.package))
        package["version"] = version
        nxt = PlotTwistPackageRecord(
            blueprint_id=str(row.blueprint_id), version=version,
            title=row.title, package=package, status="ready_for_review",
            validation_errors=[], created_by="adminuser",
        )
        session.add(nxt)
        session.flush()
        return str(nxt.id)


def test_publish_opens_the_event_immediately(db):
    from app.db_models import PlotTwistGameRecord

    seed = seed_published(db)
    v2_id = _stage_ready_version(db, seed, 2)
    # End the live event: publishing then opens a fresh period on v2.
    with db.sessions.begin() as session:
        game = session.scalar(
            select(PlotTwistGameRecord).where(
                PlotTwistGameRecord.status.in_(("active", "pending"))
            )
        )
        game.status = "ended"
    published = plot_twist_generation.publish_package(db, v2_id)
    assert published["status"] == "published"
    with db.sessions() as session:
        live = session.scalars(
            select(PlotTwistGameRecord).where(
                PlotTwistGameRecord.status.in_(("active", "pending"))
            )
        ).all()
        assert len(live) == 1
        assert str(live[0].story_package_id) == v2_id
    # Publishing v3 while the period is live rotates onto v3 immediately,
    # wordle-style: the v2 period ends and a fresh period pinned to v3 opens.
    v2_game_id = None
    with db.sessions() as session:
        v2_game_id = str(
            session.scalars(
                select(PlotTwistGameRecord).where(
                    PlotTwistGameRecord.status.in_(("active", "pending"))
                )
            ).one().id
        )
    v3_id = _stage_ready_version(db, seed, 3)
    plot_twist_generation.publish_package(db, v3_id)
    with db.sessions() as session:
        live = session.scalars(
            select(PlotTwistGameRecord).where(
                PlotTwistGameRecord.status.in_(("active", "pending"))
            )
        ).all()
        assert len(live) == 1
        assert str(live[0].story_package_id) == v3_id
        assert str(live[0].id) != v2_game_id
        assert session.get(PlotTwistGameRecord, v2_game_id).status == "ended"


def test_publish_rotates_mid_story_and_unfinished_scores_nothing(http, db, players):
    # A couple one decision into the story: publishing ends their period and
    # opens a fresh one on the new package. They start over; the abandoned
    # playthrough finalizes with zero awards instead of lingering.
    seed = seed_published(db)
    client_a, client_b = pair(db, http, players)
    state_a = client_a.get("/games/plot-twist").json()
    assert state_a["stage"] == "awaiting_you"
    moved = client_a.post(
        "/games/plot-twist/choices",
        json={"choice_id": "c_loading", "state_version": state_a["state_version"]},
    )
    assert moved.status_code == 200, moved.text
    with db.sessions() as session:
        old_game_id = str(
            session.scalars(
                select(PlotTwistGameRecord).where(
                    PlotTwistGameRecord.status.in_(("active", "pending"))
                )
            ).one().id
        )
    v2_id = _stage_ready_version(db, seed, 2)
    plot_twist_generation.publish_package(db, v2_id)
    with db.sessions() as session:
        assert session.get(PlotTwistGameRecord, old_game_id).status == "ended"
        live = session.scalars(
            select(PlotTwistGameRecord).where(
                PlotTwistGameRecord.status.in_(("active", "pending"))
            )
        ).all()
        assert len(live) == 1
        assert str(live[0].story_package_id) == v2_id
    # No session under the new period yet: the pair keeps each other and
    # re-enters through the normal start, with a clean history.
    fresh = client_a.get("/games/plot-twist").json()
    assert fresh["stage"] == "selecting_partner"
    assert fresh["partner"] is not None
    assert fresh.get("session_id") is None
    started = client_a.post("/games/plot-twist/start")
    assert started.status_code == 200, started.text
    assert started.json()["stage"] == "awaiting_you"
    assert started.json()["decisions_made"] == 0
    # The abandoned story settles with no awards: unfinished couples score 0.
    result = league_finalization.finalize_opportunity(db, "plot-twist", old_game_id)
    assert result["awards"] == 0
    with db.sessions() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(GameScoreAwardRecord).where(
                    GameScoreAwardRecord.game_id == "plot-twist",
                    GameScoreAwardRecord.opportunity_id == old_game_id,
                )
            )
            == 0
        )


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
