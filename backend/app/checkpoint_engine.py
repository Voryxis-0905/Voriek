from copy import deepcopy
"""Orchestration and compatibility exports; implementations live in focused modules."""
from app.rag import select_relevant_lore_cards
from app.state_manager import _apply_outcome_payload
from app.state_manager import eval_condition
from app.story.checkpoint_context import playable_checkpoint_description
from app.world.boundaries import BOUNDARY_HARD_REJECT_TEMPLATES
from app.world.boundaries import _is_location_in_zone
from app.world.boundaries import build_boundary_correction_note
from app.world.boundaries import check_boundary_violations
from app.world.boundaries import raise_boundary_hard_reject
from app.world.endgame import tick_endgame
from app.world.map_rules import _get_location_unlock_requirements
from app.world.map_rules import build_map_restriction_note
from app.world.map_rules import check_map_based_restrictions
from app.world.map_rules import generate_location_map
from app.world.templates import TEMPLATES
from app.world.templates import make_card
from app.world.templates import make_character
from app.world.templates import make_checkpoint
from typing import Optional


def find_checkpoint(checkpoints: list, checkpoint_id: str):
    for cp in checkpoints:
        if cp["checkpoint_id"] == checkpoint_id:
            return cp
    return None


def get_active_cards(cards: list, checkpoint: dict, context_text: str = "",
                      max_lore_tokens: Optional[int] = None) -> list:
    allowed_chars = set(checkpoint["boundary"]["allowed_characters"])
    qualifying = []
    for c in cards:
        if c["status"] != "unlocked":
            continue
        if c["type"] == "char" and c["id"] not in allowed_chars:
            continue
        qualifying.append(c)

    lore_cards = [c for c in qualifying if c["type"] == "lore"]
    selected_lore = select_relevant_lore_cards(lore_cards, context_text, max_lore_tokens)
    selected_lore_ids = {c["id"] for c in selected_lore}

    return [c for c in qualifying if c["type"] != "lore" or c["id"] in selected_lore_ids]


def checkpoint_entry_location_met(checkpoint: dict, character_state: dict,
                                  world_config: dict = None) -> bool:
    entry_location = checkpoint.get("entry_location")
    if isinstance(entry_location, str) and entry_location.strip():
        protagonist_id = (world_config or {}).get("protagonist_id", "")
        protagonist = character_state.get(protagonist_id, {})
        current_location = protagonist.get("location", "") if isinstance(protagonist, dict) else ""
        if not isinstance(current_location, str) or not _is_location_in_zone(
            current_location, {entry_location.strip()}
        ):
            return False
    return True


def checkpoint_conditions_met(checkpoint: dict, character_state: dict, world_config: dict = None) -> bool:
    if not checkpoint_entry_location_met(checkpoint, character_state, world_config):
        return False
    conditions = checkpoint.get("required_conditions", [])
    if not conditions:
        return True
    completed = []
    if world_config and isinstance(world_config, dict):
        completed = world_config.get("completed_checkpoints", []) or []
    results = []
    for c in conditions:
        if isinstance(c, str):
            results.append(c in completed)
        else:
            results.append(eval_condition(c, character_state, world_config))
    return all(results)


def sanitize_required_conditions(checkpoints: list, character_state: dict, world_config: dict = None) -> list:
    sanitized = []
    for cp in checkpoints:
        if not isinstance(cp, dict):
            continue
        conditions = cp.get("required_conditions", [])
        if not conditions:
            sanitized.append({
                "checkpoint_id": cp.get("checkpoint_id", "?"),
                "valid": True,
                "removed": []
            })
            continue

        valid_conditions = []
        removed = []
        for cond in conditions:
            if isinstance(cond, str):
                valid_conditions.append(cond)
                continue
            if not isinstance(cond, dict):
                removed.append(str(cond))
                continue
            field = cond.get("field", "")
            if not field or not isinstance(field, str):
                removed.append(str(cond))
                continue
            parts = field.split(".")
            if len(parts) < 2:
                removed.append(field)
                continue

            # story_clock.* and world_flags.* are always valid (they resolve at runtime)
            if parts[0] in ("story_clock", "world_flags"):
                valid_conditions.append(cond)
                continue

            char_id = parts[0]
            if char_id not in character_state:
                removed.append(field)
                continue
            cur = character_state[char_id]
            valid = True
            for key in parts[1:]:
                if isinstance(cur, dict):
                    if key not in cur:
                        valid = False
                        break
                    cur = cur[key]
                elif isinstance(cur, list):
                    try:
                        idx = int(key)
                        cur = cur[idx]
                    except (ValueError, IndexError):
                        valid = False
                        break
                else:
                    valid = False
                    break
            if valid:
                valid_conditions.append(cond)
            else:
                removed.append(field)

        sanitized.append({
            "checkpoint_id": cp.get("checkpoint_id", "?"),
            "valid": len(removed) == 0,
            "removed": removed
        })

    return sanitized


def _advance_sub_beats(current_checkpoint: dict, world_config: dict,
                        character_state: dict) -> list:
    sub_beats = current_checkpoint.get("sub_beats", [])
    if not sub_beats:
        return []

    current_id = current_checkpoint["checkpoint_id"]
    sub_beats_progress = world_config.setdefault("sub_beats_progress", {})
    completed_ids = set(sub_beats_progress.get(current_id, []))

    completed_this_turn = []
    for sub_beat in sub_beats:
        if not isinstance(sub_beat, dict):
            continue
        beat_id = sub_beat.get("beat_id", "")
        if not beat_id or beat_id in completed_ids:
            continue

        conditions = sub_beat.get("required_conditions", [])
        if conditions and not all(eval_condition(c, character_state, world_config) for c in conditions):
            continue

        completed_ids.add(beat_id)
        completed_this_turn.append(beat_id)
        break

    if completed_this_turn:
        sub_beats_progress[current_id] = list(completed_ids)

    return completed_this_turn


def advance_checkpoint_if_ready(canon_timeline: dict, world_config: dict,
                                 character_state: dict, card_registry: dict,
                                 chapter_closed: bool = False):
    checkpoints = canon_timeline.get("checkpoints", [])
    current_id = world_config.get("current_checkpoint_id", "")
    current_checkpoint = find_checkpoint(checkpoints, current_id)

    if not current_checkpoint:
        return None

    sub_beats_completed = _advance_sub_beats(
        current_checkpoint, world_config, character_state
    )

    sub_beats = current_checkpoint.get("sub_beats", [])
    sub_beats_progress = world_config.setdefault("sub_beats_progress", {})
    all_sub_beats_done = all(
        sb["beat_id"] in set(sub_beats_progress.get(current_id, []))
        for sb in sub_beats if isinstance(sb, dict) and sb.get("beat_id")
    ) if sub_beats else True

    next_id_to_advance = None
    selected_outcome_payload = None

    if (not sub_beats and chapter_closed) or (sub_beats and all_sub_beats_done):
        for outcome in current_checkpoint.get("alternate_outcomes", []):
            if not isinstance(outcome, dict):
                continue
            conditions = outcome.get("conditions", [])
            if not conditions or all(eval_condition(c, character_state, world_config) for c in conditions):
                target_id = outcome.get("next_checkpoint_id") or outcome.get("to_checkpoint_id")
                target = find_checkpoint(checkpoints, target_id) if target_id else None
                if not target or not checkpoint_entry_location_met(target, character_state, world_config):
                    continue
                next_id_to_advance = target_id
                selected_outcome_payload = outcome.get("apply") or outcome.get("state_changes", {})
                break

        if not next_id_to_advance:
            default_next_id = current_checkpoint.get("default_next_checkpoint_id")

            if not default_next_id:
                current_index = next((i for i, cp in enumerate(checkpoints) if cp["checkpoint_id"] == current_id), None)
                if current_index is not None and current_index + 1 < len(checkpoints):
                    default_next_id = checkpoints[current_index + 1]["checkpoint_id"]

            if default_next_id:
                next_checkpoint = find_checkpoint(checkpoints, default_next_id)
                if next_checkpoint and checkpoint_conditions_met(next_checkpoint, character_state, world_config):
                    next_id_to_advance = default_next_id

    result = {
        "old_id": current_id,
        "sub_beats_completed": sub_beats_completed,
        "all_sub_beats_done": all_sub_beats_done,
        "to_checkpoint_id": None,
        "to_checkpoint_description": "",
        "cards_unlocked": [],
        "realm_changes": {},
        "applied_effects": [],
        "ignored_location_changes": []
    }

    if not next_id_to_advance:
        return result

    next_checkpoint = find_checkpoint(checkpoints, next_id_to_advance)
    if not next_checkpoint:
        return result

    if isinstance(selected_outcome_payload, dict) and selected_outcome_payload:
        # This runs after the scene has been checked. A checkpoint may progress
        # offstage, but cannot manufacture a journey for the viewpoint player.
        # Preserve the authored timeline and all non-location consequences.
        selected_outcome_payload = deepcopy(selected_outcome_payload)
        protagonist_id = world_config.get("protagonist_id", "")
        changes = selected_outcome_payload.get("characters", {})
        player_changes = changes.get(protagonist_id) if isinstance(changes, dict) else None
        if isinstance(player_changes, dict) and "location" in player_changes:
            requested = player_changes.pop("location")
            retained = character_state.get(protagonist_id, {}).get("location", "")
            if requested and requested != retained:
                result["ignored_location_changes"].append({
                    "character_id": protagonist_id,
                    "requested_location": requested,
                    "retained_location": retained,
                    "reason": "checkpoint_transition_is_not_player_travel",
                })
        _apply_outcome_payload(character_state, selected_outcome_payload, world_config)

    completed = world_config.setdefault("completed_checkpoints", [])
    if current_id not in completed:
        completed.append(current_id)
    world_config["current_checkpoint_id"] = next_checkpoint["checkpoint_id"]

    unlocked_card_names = []
    next_cp_id = next_checkpoint["checkpoint_id"]
    for card in card_registry["cards"]:
        if card["id"] in next_checkpoint.get("cards_unlocked", []) or card.get("unlock_checkpoint_id") == next_cp_id:
            if card["status"] != "unlocked":
                card["status"] = "unlocked"
                unlocked_card_names.append(card["name"])

    realm_changes = {}
    realm_updates = next_checkpoint.get("realm_updates", {})
    if isinstance(realm_updates, dict):
        for char_id, new_realm in realm_updates.items():
            if char_id in character_state and isinstance(character_state[char_id], dict) and "power_stat" in character_state[char_id]:
                character_state[char_id]["power_stat"]["realm"] = new_realm
                realm_changes[char_id] = new_realm

    applied_effects = []
    for effect in next_checkpoint.get("status_effects", []):
        if not isinstance(effect, dict):
            continue
        char_id = effect.get("character_id")
        duration = effect.get("duration", 0)
        if not isinstance(duration, int) or duration <= 0:
            continue
        if char_id in character_state:
            char = character_state[char_id]
            if "status_effects" not in char:
                char["status_effects"] = []
            eff_name = effect.get("name", "Unknown")
            existing = next((e for e in char["status_effects"] if isinstance(e, dict) and e.get("name") == eff_name), None)
            if existing:
                existing["duration"] = duration
            else:
                char["status_effects"].append({"name": eff_name, "duration": duration})
            applied_effects.append(f"{eff_name} ({char_id})")

    result.update({
        "to_checkpoint_id": next_checkpoint["checkpoint_id"],
        "to_checkpoint_description": playable_checkpoint_description(next_checkpoint),
        "cards_unlocked": unlocked_card_names,
        "realm_changes": realm_changes,
        "applied_effects": applied_effects
    })

    return result
