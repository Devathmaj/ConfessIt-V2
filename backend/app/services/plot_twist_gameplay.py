"""Playing Plot Twist: the participant-facing half of the feature.

The pairing half belongs to the Couple Questionnaire and is reused, not
reimplemented: a couple here is a mutual-selection session in
``couple_game_sessions`` for the current global questionnaire period, read
through ``couple_gameplay``'s own lookup. This module never decides who
forms a couple and never creates a pairing.

The story half belongs to a published, versioned story package pinned by
the current global Plot Twist period. Gameplay executes that package
deterministically: choices are validated against the current scene and the
assigned participant, state changes use only the closed operator set, and
the scene sequence per couple is selected once from a persisted seed and
never reshuffled.

Rules holding at the bottom of everything here.

**Reads never create gameplay rows.** Partner selection reuses the
questionnaire's storage; the Plot Twist session row is created when a
mutual pair enters an active event (on selection, or on the first choice
submit for pairs formed earlier).

**One couple, one session per global game.** The pair is stored in
canonical order with a unique constraint. Creation runs under the
per-game advisory lock first and the pair lock second (ENG-001 order),
so concurrent enters serialize into exactly one session.

**Turns strictly alternate between the two members.** ``participant_one``
means user_a and ``participant_two`` means user_b; ``either``/``joint``
scenes resolve to whoever's turn it is, so they never let one side answer
twice in a row either. Anything else is a 403: a participant answers only
on their own turn, and only after their partner has answered theirs.

**Loops cannot run forever.** A choice leading back to a scene the couple
has already seen twice is no longer offered -- the least-visited way
forward always remains, so the story funnels out instead of stranding.
Past the package's decision budget (default 24), a non-terminal scene
concludes the story at the best qualifying ending rather than playing on.

**Submits are atomic and idempotent.** The session row is locked
``FOR UPDATE``; the choice is re-validated inside the transaction;
``state_version`` is optimistic concurrency (a stale version is a 409);
and a repeated submit of an already-recorded turn answers the stored
outcome instead of applying consequences twice.

**Scores settle once.** Completion stores deterministic ``base_points``
from the ending tier plus treasure bonuses (capped at 100). League
finalization reads those rows; retries cannot award twice because the
ledger is written once per opportunity.
"""

import random
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import HTTPException, status
from sqlalchemy import or_, select, text
from sqlalchemy.exc import IntegrityError

from ..db_models import (
    CoupleGameSelectionRecord,
    CoupleGameSessionRecord,
    PlotTwistDecisionRecord,
    PlotTwistGameRecord,
    PlotTwistPackageRecord,
    PlotTwistSessionRecord,
    UserRecord,
)
from ..limits import (
    PLOT_TWIST_DEFAULT_MAX_DECISIONS,
    PLOT_TWIST_MAX_SCENE_VISITS,
    PLOT_TWIST_REWARD_TIERS,
    PLOT_TWIST_SESSION_LOCK_KEY,
    PLOT_TWIST_TREASURE_BONUS_EACH,
    PLOT_TWIST_TREASURE_BONUS_MAX,
)
from ..models import (
    CouplePartnerCandidate,
    CouplePartnerView,
    CoupleSelectionView,
    PlotTwistCurrentState,
    PlotTwistDecisionView,
    PlotTwistResultView,
    PlotTwistSceneView,
    UserDetails,
)
from ..postgres import Database, is_valid_id

GAME_ID = "plot-twist"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today():
    return datetime.now(timezone.utc).date()


# --- pure state helpers (also used by the admin simulator) --------------------


def check_conditions(conditions: list[dict], state: dict) -> bool:
    """Whether every machine-evaluable condition holds against ``state``."""
    for cond in conditions or []:
        variable = cond.get("variable")
        op = cond.get("op")
        value = cond.get("value")
        current = state.get(variable)
        if op == "has_flag":
            flags = current if isinstance(current, list) else []
            if value not in flags:
                return False
        elif op == "missing_flag":
            flags = current if isinstance(current, list) else []
            if value in flags:
                return False
        elif op == "eq":
            if current != value:
                return False
        elif op == "gte":
            if not isinstance(current, (int, float)) or current < value:
                return False
        elif op == "lte":
            if not isinstance(current, (int, float)) or current > value:
                return False
        else:
            return False
    return True


def apply_state_changes(
    var_map: dict[str, dict], state: dict, changes: list[dict]
) -> dict:
    """Apply the closed operator set to a copy of ``state``, clamped to ranges."""
    result = dict(state)
    for change in changes or []:
        op = change.get("op")
        variable = change.get("variable")
        value = change.get("value")
        spec = var_map.get(variable) or {}
        kind = spec.get("kind", "int")
        if op == "set":
            if kind == "flag" and not isinstance(value, bool):
                continue
            if kind == "int" and not isinstance(value, int):
                continue
            result[variable] = _clamp(spec, value)
        elif op == "increment" and kind == "int" and isinstance(value, int):
            result[variable] = _clamp(spec, int(result.get(variable) or 0) + value)
        elif op == "decrement" and kind == "int" and isinstance(value, int):
            result[variable] = _clamp(spec, int(result.get(variable) or 0) - value)
        elif op == "add_flag" and kind == "flag":
            flags = list(result.get(variable) or [])
            if value not in flags:
                flags.append(value)
            result[variable] = flags
        elif op == "remove_flag" and kind == "flag":
            result[variable] = [f for f in (result.get(variable) or []) if f != value]
    return result


def _clamp(spec: dict, value: Any) -> Any:
    if not isinstance(value, int) or isinstance(value, bool):
        return value
    minimum = spec.get("min_value")
    maximum = spec.get("max_value")
    if isinstance(minimum, int) and value < minimum:
        return minimum
    if isinstance(maximum, int) and value > maximum:
        return maximum
    return value


def score_for_outcome(
    ending: Optional[dict], treasures: list[str], decisions_made: int
) -> int:
    """Deterministic 0-100 base for one completed session.

    The ending's reward tier sets the base; each treasure adds a bounded
    bonus. AI values can never bypass the cap: the result is clamped.
    """
    tier = (ending or {}).get("reward_tier") or "participation"
    base = PLOT_TWIST_REWARD_TIERS.get(str(tier).lower(), PLOT_TWIST_REWARD_TIERS["participation"])
    bonus = min(len(treasures) * PLOT_TWIST_TREASURE_BONUS_EACH, PLOT_TWIST_TREASURE_BONUS_MAX)
    _ = decisions_made
    return max(0, min(100, int(base) + int(bonus)))


# --- reading -----------------------------------------------------------------


def _require_playable(session) -> None:
    from .game_configs import check_playable

    check_playable(session, GAME_ID)


def _latest_global_game(session) -> Optional[PlotTwistGameRecord]:
    """The current Plot Twist period with a pinned package, if playable."""
    from . import game_periods

    game = session.scalar(
        select(PlotTwistGameRecord)
        .where(PlotTwistGameRecord.status.in_(("active", "pending")))
        .limit(1)
    )
    if game is None or game.status != "active":
        return None
    if game.story_package_id is None:
        return None
    start = game.period_start_date or game.game_date
    if start is not None and start > _today():
        return None
    if game_periods.is_reset_due(game.period_end_date, _today()):
        return None
    return game


def _my_couple_session(session, user_id: str):
    """This caller's mutual-selection session from the questionnaire's store."""
    from . import couple_gameplay

    questionnaire = couple_gameplay._latest_playable_game(session)
    if questionnaire is None:
        return None
    return session.scalar(
        select(CoupleGameSessionRecord).where(
            CoupleGameSessionRecord.couple_game_id == questionnaire.id,
            or_(
                CoupleGameSessionRecord.user_a_id == user_id,
                CoupleGameSessionRecord.user_b_id == user_id,
            ),
        )
    )


def _my_selection(session, user: UserDetails) -> Optional[CoupleSelectionView]:
    selection = session.scalar(
        select(CoupleGameSelectionRecord).where(
            CoupleGameSelectionRecord.user_id == user.id
        )
    )
    if selection is None:
        return None
    chosen = session.get(UserRecord, str(selection.selected_user_id))
    if chosen is None:
        return None
    return CoupleSelectionView(
        selected_user_id=str(chosen.id), selected_user_name=chosen.name
    )


def _package_for(session, story_package_id: str) -> dict:
    row = session.get(PlotTwistPackageRecord, story_package_id)
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This story version is no longer available.",
        )
    return dict(row.package or {})


def _member_for_role(row: PlotTwistSessionRecord, role: str) -> str:
    """The member id holding a ``user_a``/``user_b`` turn role."""
    return str(row.user_a_id) if role == "user_a" else str(row.user_b_id)


def _role_for_actor(
    actor: Optional[str],
    user_a_id: str,
    last_actor_id: Optional[str],
) -> str:
    """Whose turn a scene is, as a ``user_a``/``user_b`` role.

    Strict actors name their member; shared (``either``/``joint``) scenes
    resolve to whoever's turn it is -- the member who did *not* act last
    (user_a opens). The alternation guard below never lets one side answer
    twice running: a strict scene naming the member who just acted flips to
    the other member instead. Validation refuses such packages at publish
    time, so the flip only ever rescues stories published before the rule;
    stranding the couple instead would be worse.
    """
    role = "user_a"
    if actor == "participant_one":
        role = "user_a"
    elif actor == "participant_two":
        role = "user_b"
    elif last_actor_id is not None:
        role = "user_b" if str(last_actor_id) == str(user_a_id) else "user_a"
    if last_actor_id is not None:
        last_role = "user_a" if str(last_actor_id) == str(user_a_id) else "user_b"
        if role == last_role:
            role = "user_b" if role == "user_a" else "user_a"
    return role


def _assignee_member(
    row: PlotTwistSessionRecord, scene: dict, last_actor_id: Optional[str]
) -> str:
    """The member id whose turn the current scene is.

    New rows store the resolved role directly; rows written before roles
    existed (``assigned_actor == "either"``) resolve through the same rule
    so in-flight stories keep alternating without a migration.
    """
    role = row.assigned_actor if row.assigned_actor in ("user_a", "user_b") else None
    if role is None:
        role = _role_for_actor(scene.get("actor"), str(row.user_a_id), last_actor_id)
    return _member_for_role(row, role)


def _scene_view(
    scene: dict,
    viewer_id: Optional[str],
    assignee: Optional[str],
    completed: bool,
    offered: list[dict],
    visited_count: int,
) -> PlotTwistSceneView:
    show_choices = completed or (viewer_id is not None and (assignee is None or assignee == viewer_id))
    return PlotTwistSceneView(
        scene_id=str(scene.get("id")),
        scene_type=str(scene.get("type")),
        title=str(scene.get("title")),
        narrative_text=str(scene.get("narrative_text")),
        actor=str(scene.get("actor") or "either"),
        choices=offered if show_choices else [],
        is_terminal=bool(scene.get("is_terminal")),
        visited_count=visited_count,
    )


def _assemble_state(session, user: UserDetails) -> PlotTwistCurrentState:
    """Where the caller stands, derived from stored rows and nothing else."""
    _require_playable(session)
    game = _latest_global_game(session)
    my_selection = _my_selection(session, user)
    if game is None:
        return PlotTwistCurrentState(stage="no_game", my_selection=my_selection)
    couple = _my_couple_session(session, user.id)
    if couple is None:
        return PlotTwistCurrentState(
            stage="selecting_partner",
            game_id=str(game.id),
            my_selection=my_selection,
        )
    user_a_id, user_b_id = str(couple.user_a_id), str(couple.user_b_id)
    partner_id = user_b_id if user_a_id == user.id else user_a_id
    partner_row = session.get(UserRecord, partner_id)
    partner = CouplePartnerView(
        id=partner_id, name=partner_row.name if partner_row is not None else "Partner"
    )
    row = session.scalar(
        select(PlotTwistSessionRecord).where(
            PlotTwistSessionRecord.plot_twist_game_id == game.id,
            PlotTwistSessionRecord.user_a_id == user_a_id,
            PlotTwistSessionRecord.user_b_id == user_b_id,
        )
    )
    if row is None:
        # Entered the event but the session row is created on entering or on
        # the first submit; until then the couple picks their opening.
        return PlotTwistCurrentState(
            stage="selecting_partner",
            game_id=str(game.id),
            my_selection=my_selection,
            partner=partner,
        )
    package = _package_for(session, str(row.story_package_id))
    scenes = {s.get("id"): s for s in (package.get("scenes") or [])}
    endings = {e.get("id"): e for e in (package.get("endings") or [])}
    scene = scenes.get(row.current_scene_id, {})
    decisions = session.scalars(
        select(PlotTwistDecisionRecord)
        .where(PlotTwistDecisionRecord.session_id == row.id)
        .order_by(PlotTwistDecisionRecord.seq.asc())
    ).all()
    history = [
        PlotTwistDecisionView(
            seq=int(d.seq),
            scene_id=str(d.scene_id),
            scene_title=str((scenes.get(d.scene_id) or {}).get("title") or d.scene_id),
            actor_user_id=str(d.actor_user_id),
            actor_name=_actor_name(session, str(d.actor_user_id), user, partner),
            choice_id=str(d.choice_id),
            choice_text=_choice_text(scenes.get(d.scene_id) or {}, str(d.choice_id)),
            consequence_text=_choice_consequence(scenes.get(d.scene_id) or {}, str(d.choice_id)),
            created_at=d.created_at,
        )
        for d in decisions
    ]
    base = dict(
        game_id=str(game.id),
        session_id=str(row.id),
        my_selection=my_selection,
        partner=partner,
        state_version=int(row.state_version),
        decisions_made=len(decisions),
        history=history,
    )
    last_actor_id = str(decisions[-1].actor_user_id) if decisions else None
    if row.status == "completed":
        ending = endings.get(row.ending_id or "")
        return PlotTwistCurrentState(
            stage="completed",
            scene=_scene_view(scene, user.id, None, True, _offered_choices(scene, dict(row.state or {})), len(decisions)),
            result=PlotTwistResultView(
                ending_id=row.ending_id,
                ending_type=(ending or {}).get("type"),
                ending_title=(ending or {}).get("title"),
                narrative=(ending or {}).get("narrative"),
                success=bool((ending or {}).get("success", False)),
                treasures=list(row.treasures or []),
                decisions_made=len(decisions),
                completed_at=row.completed_at,
            ),
            **base,
        )
    # Strict alternation: exactly one member holds this turn, so the other
    # side always waits. Shared scenes resolve to whoever's turn it is.
    assignee = _assignee_member(row, scene, last_actor_id)
    stage = "awaiting_you" if _may_submit(row, assignee, user.id) else "awaiting_partner"
    targets = _choice_targets(package, dict(row.selected_scenes or {}), scene)
    offered = _offered_choices(scene, dict(row.state or {}), targets, _visited_counts(row))
    visited = sum(1 for v in (row.visited_scenes or []) if v == row.current_scene_id)
    return PlotTwistCurrentState(
        stage=stage,  # type: ignore[arg-type]
        scene=_scene_view(scene, user.id, assignee, False, offered, visited),
        **base,
    )


def _actor_name(session, actor_id: str, user: UserDetails, partner: CouplePartnerView) -> str:
    if actor_id == user.id:
        return user.name
    if actor_id == partner.id:
        return partner.name
    row = session.get(UserRecord, actor_id)
    return row.name if row is not None else "Partner"


def _choice_text(scene: dict, choice_id: str) -> Optional[str]:
    for choice in scene.get("choices") or []:
        if choice.get("id") == choice_id:
            return choice.get("text")
    return None


def _choice_consequence(scene: dict, choice_id: str) -> Optional[str]:
    for choice in scene.get("choices") or []:
        if choice.get("id") == choice_id:
            return choice.get("consequence_text")
    return None


def _offered_choices(
    scene: dict,
    state: dict,
    targets: Optional[dict[str, Optional[str]]] = None,
    visited_counts: Optional[dict[str, int]] = None,
) -> list[dict]:
    """Choices the engine offers: conditions held, loops funnelled out.

    ``targets`` maps each choice id to its resolved next scene (through the
    couple's module selection) and ``visited_counts`` counts prior visits.
    A choice leading back to a scene seen ``PLOT_TWIST_MAX_SCENE_VISITS``
    times or more is withheld -- replaying the same loop stops being an
    option -- while the least-visited way forward always remains, so
    filtering can never strand the couple on a choiceless scene.
    """
    eligible = []
    for choice in scene.get("choices") or []:
        if not check_conditions(choice.get("conditions") or [], state):
            continue
        eligible.append(choice)
    if targets is not None and visited_counts is not None and eligible:
        fresh = [
            choice
            for choice in eligible
            if visited_counts.get(targets.get(str(choice.get("id")), "") or "", 0)
            < PLOT_TWIST_MAX_SCENE_VISITS
        ]
        if fresh:
            eligible = fresh
        else:
            eligible = [
                min(
                    eligible,
                    key=lambda c: (
                        visited_counts.get(targets.get(str(c.get("id")), "") or "", 0),
                        str(c.get("id")),
                    ),
                )
            ]
    return [
        {
            "choice_id": str(choice.get("id")),
            "text": str(choice.get("text")),
            "consequence_text": choice.get("consequence_text"),
            "reward_tier": choice.get("reward_tier"),
        }
        for choice in eligible
    ]


def _choice_targets(
    package: dict, selection: dict, scene: dict
) -> dict[str, Optional[str]]:
    """Each choice id to its resolved next scene for this couple."""
    targets: dict[str, Optional[str]] = {}
    for choice in scene.get("choices") or []:
        resolved = _resolve_scene(package, selection, choice.get("next_scene_id"))
        targets[str(choice.get("id"))] = (
            str(resolved.get("id")) if resolved else None
        )
    return targets


def _visited_counts(row: PlotTwistSessionRecord) -> dict[str, int]:
    counts: dict[str, int] = {}
    for scene_id in row.visited_scenes or []:
        counts[str(scene_id)] = counts.get(str(scene_id), 0) + 1
    return counts


def _may_submit(row: PlotTwistSessionRecord, assignee_id: str, user_id: str) -> bool:
    """Only the member holding this turn may submit it."""
    return str(assignee_id) == str(user_id)


def current_state(db: Database, user: UserDetails) -> PlotTwistCurrentState:
    with db.sessions.begin() as session:
        _require_playable(session)
    with db.sessions() as session:
        return _assemble_state(session, user)


# --- pairing (reused, not reimplemented) ---------------------------------------


def list_partners(db: Database, user: UserDetails) -> list[CouplePartnerCandidate]:
    """The questionnaire's candidate list, not a second one."""
    from . import couple_gameplay
    from .game_configs import check_playable

    with db.sessions.begin() as session:
        check_playable(session, GAME_ID)
    return couple_gameplay.list_partners(db, user)


def choose_partner(db: Database, user: UserDetails, selected_user_id: str) -> PlotTwistCurrentState:
    """Record the partner choice through the questionnaire's selection.

    When the pick is mutual and a Plot Twist event is live, the couple's
    session row (with its deterministic module selection) is created now,
    so selections persist before the first decision is ever submitted.
    """
    from . import couple_gameplay

    couple_gameplay.select_partner(db, user, selected_user_id)
    with db.sessions.begin() as session:
        from .game_configs import lock_game_for_play

        lock_game_for_play(session, GAME_ID)
        game = _latest_global_game(session)
        couple = _my_couple_session(session, user.id)
        if game is not None and couple is not None:
            _get_or_create_session(session, game, couple)
    return current_state(db, user)


# --- sessions ------------------------------------------------------------------


def enter_event(db: Database, user: UserDetails) -> PlotTwistCurrentState:
    """Enter the active event: create (or fetch) this couple's session.

    Pairs formed while no event was live otherwise sit at the partner
    picker with nothing to press: choosing again works (the selection is
    an upsert), but an explicit start is what the screen offers. Creating
    twice settles on the one row -- never two sessions for a pair.
    """
    with db.sessions.begin() as session:
        _require_playable(session)
    with db.sessions.begin() as session:
        from .game_configs import lock_game_for_play

        lock_game_for_play(session, GAME_ID)
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": PLOT_TWIST_SESSION_LOCK_KEY},
        )
        game = _latest_global_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="There is no active Plot Twist event right now. Check back soon.",
            )
        couple = _my_couple_session(session, user.id)
        if couple is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Choose a partner first: Plot Twist is played as a mutual pair.",
            )
        _get_or_create_session(session, game, couple)
    return current_state(db, user)


def _get_my_session_for_update(session, game_id: str, user_a_id: str, user_b_id: str):
    return session.scalar(
        select(PlotTwistSessionRecord)
        .where(
            PlotTwistSessionRecord.plot_twist_game_id == game_id,
            PlotTwistSessionRecord.user_a_id == user_a_id,
            PlotTwistSessionRecord.user_b_id == user_b_id,
        )
        .with_for_update()
    )


def _initial_state_with_defaults(package: dict) -> dict:
    """The starting state with declared initials filled in.

    Legacy packages (e.g. Echoes of Trust v1) shipped ``initial_state`` as
    ``{}`` while declaring ``trust: 5``: gameplay then started trust from 0
    and no ending condition could ever hold on schedule. Filling missing
    variables from their declared initials keeps old stories playable and
    new stories honest. Flag initials normalize to string lists.
    """
    state = dict(package.get("initial_state") or {})
    for var in package.get("state_vars") or []:
        name = var.get("name")
        if not name or name in state:
            continue
        initial = var.get("initial")
        if (var.get("kind") or "int") == "flag":
            if isinstance(initial, list):
                state[name] = [str(v) for v in initial if isinstance(v, str)]
            elif isinstance(initial, str):
                state[name] = [initial]
            else:
                state[name] = []
        elif isinstance(initial, int) and not isinstance(initial, bool):
            state[name] = _clamp(var, initial)
    return state


def _authored_reachable_ids(package: dict, start_id: str) -> set[str]:
    """Scene ids reachable from ``start_id`` through authored transitions."""
    scenes = {s.get("id"): s for s in (package.get("scenes") or [])}
    seen: set[str] = set()
    queue = [start_id]
    while queue:
        current = queue.pop()
        if current in seen or current not in scenes:
            continue
        seen.add(current)
        scene = scenes[current]
        if scene.get("is_terminal"):
            continue
        for choice in scene.get("choices") or []:
            nxt = choice.get("next_scene_id")
            if nxt in scenes and nxt not in seen:
                queue.append(nxt)
    return seen


def _selection_is_safe(package: dict) -> bool:
    """Whether per-couple module selection can apply without collapsing.

    Safe means: the opening and every terminal live in slot ``core``, and no
    non-core slot holds two scenes where one can reach the other. When a
    package violates this (Echoes of Trust v1 put its linear beats in
    slot-1..slot-4 twice), resolving every transition through the selection
    folds distinct beats onto one scene and replays 4 scenes forever. An
    unsafe package plays on its authored graph instead: every transition
    uses its named target.
    """
    scenes = {s.get("id"): s for s in (package.get("scenes") or [])}
    opening = scenes.get(package.get("opening_scene_id") or "")
    if not opening or (opening.get("slot") or "core") != "core":
        return False
    for scene in scenes.values():
        if scene.get("is_terminal") and (scene.get("slot") or "core") != "core":
            return False
    by_slot: dict[str, list[str]] = {}
    for scene_id, scene in scenes.items():
        slot = scene.get("slot") or "core"
        if slot == "core":
            continue
        by_slot.setdefault(slot, []).append(scene_id)
    for ids in by_slot.values():
        if len(ids) < 2:
            continue
        for source in ids:
            others = set(ids) - {source}
            if others & _authored_reachable_ids(package, source):
                return False
    return True


def _select_modules(package: dict, seed: str) -> dict:
    """Deterministic per-couple interchangeable selection, persisted once.

    For every non-core slot, one alternative is drawn from the slot's
    scenes with a seed combining the global seed, the couple and the
    package version. Scenes in slots with ``allow_repeat`` may recur;
    all other slots resolve to their single selected scene for the whole
    session and are never reshuffled. Packages that misuse slots for
    sequential beats select nothing: they play on the authored graph so a
    legacy story cannot collapse into a loop.
    """
    if not _selection_is_safe(package):
        return {"seed": seed, "slots": {}}
    by_slot: dict[str, list[dict]] = {}
    for scene in package.get("scenes") or []:
        slot = scene.get("slot") or "core"
        if slot == "core":
            continue
        by_slot.setdefault(slot, []).append(scene)
    rng = random.Random(seed)
    selection: dict[str, list[str]] = {}
    for slot in sorted(by_slot):
        alternatives = [s.get("id") for s in by_slot[slot]]
        if not alternatives:
            continue
        rng.shuffle(alternatives)
        selection[slot] = alternatives[:1]
    return {"seed": seed, "slots": selection}


def _resolve_scene(package: dict, selection: dict, scene_id: str) -> dict:
    """Map a transition target through the couple's module selection.

    A transition naming any scene of an interchangeable slot resolves to
    the couple's selected alternative for that slot, so every couple plays
    a compatible variation of the same published story. Packages whose slots
    are unsafe (sequential beats sharing one slot) ignore the selection and
    use the authored target, so legacy stories play linearly instead of
    looping.
    """
    scenes = {s.get("id"): s for s in (package.get("scenes") or [])}
    target = scenes.get(scene_id)
    if target is None:
        return {}
    if not _selection_is_safe(package):
        return target
    slot = target.get("slot") or "core"
    if slot == "core":
        return target
    picked = ((selection or {}).get("slots") or {}).get(slot) or []
    if picked and picked[0] in scenes:
        return scenes[picked[0]]
    return target


def _get_or_create_session(session, game: PlotTwistGameRecord, couple) -> PlotTwistSessionRecord:
    user_a_id, user_b_id = sorted([str(couple.user_a_id), str(couple.user_b_id)])
    existing = session.scalar(
        select(PlotTwistSessionRecord).where(
            PlotTwistSessionRecord.plot_twist_game_id == game.id,
            PlotTwistSessionRecord.user_a_id == user_a_id,
            PlotTwistSessionRecord.user_b_id == user_b_id,
        )
    )
    if existing is not None:
        return existing
    package = _package_for(session, str(game.story_package_id))
    seed = f"{game.seed}:{user_a_id}:{user_b_id}:{package.get('version', 1)}"
    selection = _select_modules(package, seed)
    opening_id = package.get("opening_scene_id")
    opening = _resolve_scene(package, selection, opening_id)
    if not opening:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The published story has no playable opening scene.",
        )
    state = _initial_state_with_defaults(package)
    tags = list(opening.get("provides_tags") or [])
    # The opening turn resolves like every other: strict actors name their
    # member, shared openings start with user_a. The stored role is always
    # a member role from here on, never "either".
    opening_role = _role_for_actor(opening.get("actor"), user_a_id, None)
    row = PlotTwistSessionRecord(
        plot_twist_game_id=str(game.id),
        story_package_id=str(game.story_package_id),
        user_a_id=user_a_id,
        user_b_id=user_b_id,
        current_scene_id=str(opening.get("id")),
        state=state,
        tags=tags,
        assigned_actor=opening_role,
        status="awaiting_user_a" if opening_role == "user_a" else "awaiting_user_b",
        selected_scenes=selection,
        visited_scenes=[str(opening.get("id"))],
        treasures=[],
        state_version=0,
    )
    session.add(row)
    try:
        session.flush()
    except IntegrityError:
        # Lost a creation race: the winner's row is the session.
        session.rollback()
        winner = session.scalar(
            select(PlotTwistSessionRecord).where(
                PlotTwistSessionRecord.plot_twist_game_id == game.id,
                PlotTwistSessionRecord.user_a_id == user_a_id,
                PlotTwistSessionRecord.user_b_id == user_b_id,
            )
        )
        assert winner is not None
        return winner
    return row


# --- choices -------------------------------------------------------------------


def submit_choice(
    db: Database, user: UserDetails, choice_id: str, state_version: int
) -> PlotTwistCurrentState:
    """Validate, record and apply one assigned turn, atomically."""
    with db.sessions.begin() as session:
        _require_playable(session)
    with db.sessions.begin() as session:
        from .game_configs import lock_game_for_play

        # ENG-001 order: lifecycle lock before the pair lock, like every
        # other gameplay transaction, so a rollover cannot end the global
        # mid-submit.
        lock_game_for_play(session, GAME_ID)
        session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"),
            {"key": PLOT_TWIST_SESSION_LOCK_KEY},
        )
        game = _latest_global_game(session)
        if game is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="There is no active Plot Twist event right now.",
            )
        couple = _my_couple_session(session, user.id)
        if couple is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Choose a partner first: Plot Twist is played as a mutual pair.",
            )
        user_a_id, user_b_id = sorted([str(couple.user_a_id), str(couple.user_b_id)])
        row = _get_my_session_for_update(session, str(game.id), user_a_id, user_b_id)
        if row is None:
            row = _get_or_create_session(session, game, couple)
            row = _get_my_session_for_update(session, str(game.id), user_a_id, user_b_id)
            assert row is not None
        if row.status == "completed":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="This story is already complete; completed sessions cannot replay choices.",
            )
        package = _package_for(session, str(row.story_package_id))
        scenes = {s.get("id"): s for s in (package.get("scenes") or [])}
        endings = {e.get("id"): e for e in (package.get("endings") or [])}
        var_map = {v.get("name"): v for v in (package.get("state_vars") or [])}
        scene = scenes.get(row.current_scene_id)
        if scene is None:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="The current scene is missing from the published story.",
            )
        if int(row.state_version) != int(state_version):
            # Idempotent replay comes before the turn-ownership check: the
            # recorded turn's actor is compared against the caller, so only
            # the participant who actually made that decision can replay it
            # -- and a retry after success answers the stored outcome
            # instead of applying consequences twice. Anything else with a
            # stale version is a 409, whoever sends it.
            prior = session.scalar(
                select(PlotTwistDecisionRecord).where(
                    PlotTwistDecisionRecord.session_id == row.id,
                    PlotTwistDecisionRecord.seq == int(state_version) + 1,
                )
            )
            if (
                prior is not None
                and str(prior.choice_id) == str(choice_id)
                and str(prior.actor_user_id) == user.id
            ):
                replay = True
            else:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail="This story has moved on since your screen loaded. Refresh to continue.",
                )
        else:
            replay = False
        if not replay:
            assignee = _assignee_member(row, scene, _last_actor(session, row))
            if not _may_submit(row, assignee, user.id):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="Wait for your partner: decisions strictly alternate.",
                )
            _apply_choice(
                session, row, user, scene, scenes, endings, var_map, package,
                choice_id, assignee,
            )
    with db.sessions() as session:
        return _assemble_state(session, user)


def _last_actor(session, row: PlotTwistSessionRecord) -> Optional[str]:
    """The member id that made the latest recorded decision, if any."""
    latest = session.scalar(
        select(PlotTwistDecisionRecord.actor_user_id)
        .where(PlotTwistDecisionRecord.session_id == row.id)
        .order_by(PlotTwistDecisionRecord.seq.desc())
        .limit(1)
    )
    return str(latest) if latest is not None else None


def _apply_choice(session, row, user, scene, scenes, endings, var_map, package, choice_id, assignee) -> None:
    """The write half of submit_choice, inside the caller's transaction."""
    _ = (scenes, assignee)  # scene lookup and turn check done by the caller
    state = dict(row.state or {})
    targets = _choice_targets(package, dict(row.selected_scenes or {}), scene)
    offered = {
        c.get("choice_id")
        for c in _offered_choices(scene, state, targets, _visited_counts(row))
    }
    if choice_id not in offered:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That choice is not available in the current scene.",
        )
    choice = next(c for c in (scene.get("choices") or []) if c.get("id") == choice_id)
    state_before = dict(state)
    state_after = apply_state_changes(var_map, state, choice.get("state_changes") or [])
    seq = len(
        session.scalars(
            select(PlotTwistDecisionRecord.id).where(
                PlotTwistDecisionRecord.session_id == row.id
            )
        ).all()
    ) + 1
    resolved = _resolve_scene(package, dict(row.selected_scenes or {}), choice.get("next_scene_id"))
    if not resolved:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="The published story points at a scene that does not exist.",
        )
    decision = PlotTwistDecisionRecord(
        session_id=str(row.id),
        seq=seq,
        scene_id=str(scene.get("id")),
        actor_user_id=user.id,
        choice_id=str(choice.get("id")),
        next_scene_id=str(resolved.get("id")),
        state_before=state_before,
        state_after=state_after,
    )
    session.add(decision)
    row.state = state_after
    for tag in resolved.get("provides_tags") or []:
        tags = list(row.tags or [])
        if tag not in tags:
            tags.append(tag)
        row.tags = tags
    treasures = list(row.treasures or [])
    tier = str(choice.get("reward_tier") or "").lower()
    if tier == "treasure" or "treasure" in str(choice.get("id") or "").lower():
        if str(choice.get("id")) not in treasures:
            treasures.append(str(choice.get("id")))
    if resolved.get("type") == "discovery" and str(resolved.get("id")) not in treasures:
        treasures.append(str(resolved.get("id")))
    row.treasures = treasures
    row.current_scene_id = str(resolved.get("id"))
    visited = list(row.visited_scenes or [])
    visited.append(str(resolved.get("id")))
    row.visited_scenes = visited
    row.state_version = int(row.state_version) + 1
    try:
        max_decisions = int(package.get("max_decisions") or PLOT_TWIST_DEFAULT_MAX_DECISIONS)
    except (TypeError, ValueError):
        max_decisions = PLOT_TWIST_DEFAULT_MAX_DECISIONS
    if resolved.get("is_terminal"):
        ending = _resolve_ending(endings, resolved.get("ending_id"), state_after)
        row.ending_id = ending.get("id") if ending else resolved.get("ending_id")
        row.status = "completed"
        row.completed_at = _now()
        row.assigned_actor = _member_role(row, user.id)
    elif seq >= max_decisions:
        # Decision budget reached on a non-terminal scene: conclude now
        # rather than letting a loop run past it. Prefer the best qualifying
        # ending, but never strand: with no qualifier the highest-priority
        # ending still settles the story instead of playing forever (the old
        # code advanced the turn here, so unqualified budgets looped past
        # the cap until some state happened to qualify).
        ending = _resolve_ending(endings, None, state_after) or _best_effort_ending(endings)
        if ending is not None:
            row.ending_id = ending.get("id")
            row.status = "completed"
            row.completed_at = _now()
            row.assigned_actor = _member_role(row, user.id)
        else:
            _advance_turn(row, resolved, user.id)
    else:
        _advance_turn(row, resolved, user.id)


def _best_effort_ending(endings: dict[str, dict]) -> Optional[dict]:
    """Highest-priority ending when the budget hits with no qualifier."""
    if not endings:
        return None
    ranked = sorted(endings.values(), key=lambda e: (-int(e.get("priority") or 0), str(e.get("id"))))
    return ranked[0]


def _member_role(row: PlotTwistSessionRecord, user_id: str) -> str:
    """The turn role (``user_a``/``user_b``) of one member."""
    return "user_a" if str(user_id) == str(row.user_a_id) else "user_b"


def _advance_turn(row: PlotTwistSessionRecord, resolved: dict, actor_id: str) -> None:
    """Pass the turn to the member who did not just act.

    Strict scene actors are honoured when they agree with alternation; a
    scene naming the member who just acted flips to the other member
    instead (see :func:`_role_for_actor`), so one side can never answer
    twice running no matter what the package authors.
    """
    role = _role_for_actor(resolved.get("actor"), str(row.user_a_id), str(actor_id))
    row.assigned_actor = role
    row.status = "awaiting_user_a" if role == "user_a" else "awaiting_user_b"


def _resolve_ending(
    endings: dict[str, dict], ending_id: Optional[str], state: dict
) -> Optional[dict]:
    """The scene's ending when its conditions hold, else the best qualifier.

    Conflicting qualifiers resolve by priority, highest first; a scene
    without any qualifying ending keeps its authored ending so completion
    can never strand.
    """
    if ending_id and ending_id in endings:
        candidate = endings[ending_id]
        if check_conditions(candidate.get("conditions") or [], state):
            return candidate
    qualified = [
        e for e in endings.values()
        if check_conditions(e.get("conditions") or [], state)
    ]
    if qualified:
        qualified.sort(key=lambda e: (-int(e.get("priority") or 0), str(e.get("id"))))
        return qualified[0]
    if ending_id and ending_id in endings:
        return endings[ending_id]
    return None
