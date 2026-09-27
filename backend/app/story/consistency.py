"""Consistency responsibilities for Story Engine."""
import logging
from app.llm_client import LLMCallError
from app.llm_client import call_llm
from app.llm_client import mock_consistency_checker_response
from app.llm_client import parse_llm_json
from app.prompts import CONSISTENCY_CHECKER_SYSTEM_PROMPT
from app.prompts import ENSEMBLE_CONSISTENCY_CHECKER_SYSTEM_PROMPT
from app.prompts import EXTRACTOR_SYSTEM_PROMPT
from app.story.checkpoint_context import playable_checkpoint_description
import json
from app.world.calendar_clock import advance_story_clock, normalize_elapsed_time

logger = logging.getLogger(__name__)


def recent_confirmed_scenes(chapters_data: dict, *, limit: int = 3,
                            max_chars_per_scene: int = 3500) -> list:
    """Bounded published prose, not summaries or speculative planner notes."""
    chapters = chapters_data.get("chapters", []) if isinstance(chapters_data, dict) else []
    if not isinstance(chapters, list):
        return []
    scenes = []
    for chapter in chapters[-max(0, limit):] if limit else []:
        if not isinstance(chapter, dict):
            continue
        prose = chapter.get("chapter_text")
        if not isinstance(prose, str) or not prose.strip():
            continue
        scene = {
            "chapter_index": chapter.get("chapter_index"),
            "turn_index": chapter.get("turn_index"),
            # End-of-turn dialogue and physical positions matter most for the
            # next scene. Preserve the tail if a prior turn exceeds the budget.
            "chapter_text": prose[-max_chars_per_scene:],
        }
        record = chapter.get("scene_record")
        if isinstance(record, dict):
            scene["scene_record"] = record
        scenes.append(scene)
    return scenes


def run_extractor_cross_check(
    world_name: str,
    chapter_text: str,
    existing_facts: list,
    world_config: dict,
) -> dict:
    payload = json.dumps({
        "chapter_text": chapter_text,
        "existing_canon_facts": existing_facts,
        "world_config": {
            "genre": world_config.get("genre", ""),
            "fixed_rules": world_config.get("fixed_rules", []),
            "tone": world_config.get("tone", ""),
        }
    }, ensure_ascii=False)
    try:
        raw = call_llm(
            EXTRACTOR_SYSTEM_PROMPT,
            payload,
            world_name=world_name,
            role="extractor"
        )
        result = parse_llm_json(raw, expected_type=dict)
        issues = result.get("issues", []) if isinstance(result, dict) else []
        if not isinstance(issues, list):
            issues = []
        return {
            "checked": True,
            "issue_count": len(issues),
            "issues": issues,
            "extractor_output": result,
        }
    except (LLMCallError, ValueError, json.JSONDecodeError) as e:
        logger.warning("Extractor cross-check skipped for world=%s: %s", world_name, e)
        return {
            "checked": False,
            "issue_count": 0,
            "issues": [],
            "error": str(e),
        }


def build_consistency_checker_payload(chapter_text: str, state_changes: dict,
                                        world_config: dict, checkpoint: dict,
                                        active_cards: list,
                                        character_state_before: dict,
                                        engine_outcomes: dict = None,
                                        recent_scenes: list = None) -> dict:
    outcomes = engine_outcomes or {}
    start_clock = world_config.get("story_clock", {})
    end_clock = dict(start_clock) if isinstance(start_clock, dict) else {}
    if world_config.get("timekeeping_mode") == "duration" and end_clock:
        travel = outcomes.get("travel_resolution") or {}
        skip = outcomes.get("time_skip_resolution") or {}
        if travel and travel.get("status") in ("arrived", "interrupted"):
            elapsed = {"total_seconds": int(travel.get("elapsed_minutes", 0) or 0) * 60}
        elif skip:
            elapsed = {"total_seconds": int(skip.get("granted_minutes", 0) or 0) * 60}
        elif outcomes.get("opening_setup"):
            elapsed = {"total_seconds": 0}
        else:
            elapsed = normalize_elapsed_time(
                (state_changes or {}).get("elapsed_time"), fallback_minutes=5,
                max_minutes=360,
            )
        advance_story_clock(end_clock, elapsed, world_config.get("calendar"))
    before_characters = character_state_before if isinstance(character_state_before, dict) else {}
    protagonist_id = world_config.get("protagonist_id", "")
    protagonist_before = before_characters.get(protagonist_id, {})
    proposed_characters = (state_changes or {}).get("characters", {})
    protagonist_after = proposed_characters.get(protagonist_id, {}) if isinstance(proposed_characters, dict) else {}
    travel = outcomes.get("travel_resolution") or {}
    expected_location = (travel.get("destination") if travel.get("status") == "arrived"
                         else travel.get("stopped_at") if travel.get("status") == "interrupted"
                         else protagonist_after.get("location") if isinstance(protagonist_after, dict)
                         else None)

    def clock_label(clock: dict) -> str:
        if not isinstance(clock, dict):
            return ""
        minute = clock.get("minute_of_day")
        if not isinstance(minute, int):
            return ""
        date = "-".join(str(clock.get(part, "?")) for part in ("year", "month", "day"))
        return f"{date} {minute // 60:02d}:{minute % 60:02d}"

    proposed_changes = state_changes
    if recent_scenes is not None and isinstance(state_changes, dict):
        # Ensemble mode treats published prose plus engine-applied state as
        # durable evidence. The narrator's free-form synopsis is discarded at
        # commit and must not become a second, conflicting source of canon.
        proposed_changes = {
            key: value for key, value in state_changes.items() if key != "notes"
        }

    payload = {
        "fixed_rules": world_config.get("fixed_rules", []),
        "trait_definitions": world_config.get("trait_definitions", {}),
        "titles": world_config.get("titles", []),
        "current_checkpoint_description": playable_checkpoint_description(checkpoint),
        "active_cards": [
            {"id": c["id"], "type": c["type"], "name": c["name"], "content": c["content"]}
            for c in active_cards
        ],
        "character_state_before_chapter": character_state_before,
        "chapter_text": chapter_text,
        "proposed_state_changes": proposed_changes,
        "engine_committed_outcomes": outcomes,
        "temporal_spatial_alignment": {
            "clock_at_turn_start": start_clock,
            "clock_after_proposed_turn": end_clock,
            "clock_window_local": f"{clock_label(start_clock)} → {clock_label(end_clock)}",
            "calendar": world_config.get("calendar"),
            "protagonist_location_before": protagonist_before.get("location") if isinstance(protagonist_before, dict) else None,
            "protagonist_location_after_proposed_turn": expected_location or (
                protagonist_before.get("location") if isinstance(protagonist_before, dict) else None
            ),
        },
    }
    if recent_scenes is not None:
        payload["recent_confirmed_scenes"] = recent_scenes
        payload["character_locations_before_chapter"] = {
            char_id: state.get("location")
            for char_id, state in before_characters.items()
            if isinstance(state, dict) and isinstance(state.get("location"), str)
        }
    return payload


_CHECKER_SEVERITIES = ("none", "minor", "major")


def _unavailable(explanation: str) -> dict:
    return {
        "status": "unavailable",
        "consistent": None,
        "severity": "none",
        "issues": [],
        "explanation": explanation
    }


def parse_checker_response(raw_text: str) -> dict:
    """Parse and validate the checker output.

    A checker answer is only trusted when it fully matches the expected schema;
    a missing field, a wrong type/enum or a self-contradictory answer counts as
    "not checked" (unavailable), never as "passed".
    """
    try:
        parsed = parse_llm_json(raw_text)
    except (json.JSONDecodeError, ValueError) as error:
        return _unavailable(f"[CHECKER RESPONSE COULD NOT BE READ — treated as unchecked] {error}")
    if not isinstance(parsed, dict):
        return _unavailable("[CHECKER RESPONSE HAS AN INVALID FORMAT — treated as unchecked]")

    consistent = parsed.get("consistent")
    severity = parsed.get("severity")
    issues = parsed.get("issues")
    state_sync = parsed.get("state_sync", True)
    action_scope = parsed.get("action_scope", True)

    problems = []
    if not isinstance(consistent, bool):
        problems.append("'consistent' must be a boolean")
    if severity not in _CHECKER_SEVERITIES:
        problems.append(f"'severity' must be one of {_CHECKER_SEVERITIES}")
    if not isinstance(issues, list) or any(not isinstance(item, (str, dict)) for item in issues):
        problems.append("'issues' must be a list of strings or objects")
    if not isinstance(state_sync, bool):
        problems.append("'state_sync' must be a boolean when provided")
    if not isinstance(action_scope, bool):
        problems.append("'action_scope' must be a boolean when provided")
    if not problems:
        if severity == "major" and consistent is True:
            problems.append("'severity'=major but 'consistent'=true")
        if severity in ("none", "minor") and consistent is False:
            problems.append(f"'severity'={severity} but 'consistent'=false")

    if problems:
        return _unavailable(
            "[CHECKER RESPONSE HAS AN INVALID SCHEMA — treated as unchecked] " + "; ".join(problems)
        )

    failed = severity == "major" or consistent is False or state_sync is False or action_scope is False
    return {
        "status": "failed" if failed else "passed",
        "consistent": consistent and state_sync and action_scope,
        "severity": "major" if state_sync is False or action_scope is False else severity,
        "issues": issues,
        "explanation": parsed.get("explanation", ""),
        "state_sync": state_sync,
        "action_scope": action_scope,
    }


def run_consistency_checker(chapter_text: str, state_changes: dict, world_config: dict,
                             checkpoint: dict, active_cards: list,
                             character_state_before: dict, world_name: str = None,
                             engine_outcomes: dict = None,
                             recent_scenes: list = None) -> dict:
    payload = build_consistency_checker_payload(
        chapter_text, state_changes, world_config, checkpoint,
        active_cards, character_state_before, engine_outcomes,
        recent_scenes=recent_scenes,
    )
    try:
        raw = call_llm(
            (ENSEMBLE_CONSISTENCY_CHECKER_SYSTEM_PROMPT if recent_scenes is not None
             else CONSISTENCY_CHECKER_SYSTEM_PROMPT),
            json.dumps(payload, ensure_ascii=False),
            mock_response=mock_consistency_checker_response(),
            world_name=world_name,
            role="checker"
        )
    except LLMCallError as e:
        return {
            "status": "unavailable",
            "consistent": None,
            "severity": "none",
            "issues": [],
            "explanation": f"[CHECKER COULD NOT RUN — unchecked] {e}"
        }
    return parse_checker_response(raw)


def build_consistency_correction_note(issues: list) -> str:
    lines = "\n".join(f"- {i}" for i in issues) if issues else "- (no detailed description)"
    return (
        "IMPORTANT NOTE - CANON CONTRADICTION: The Consistency checker (running separately) "
        "detected that the PREVIOUS writing for this user_input has severe contradictions with "
        "canon:\n" + lines +
        "\nPlease completely REWRITE the chapter_text and state_changes to strictly match the given canon "
        ", try to keep the original spirit of the user_input, only fixing the exact contradicted parts."
    )
