"""Perspective-limited inputs for the opt-in director/actor/writer experiment.

This is a projection, not an access-control boundary: model output is still
checked against the authoritative world state before a turn is committed.
"""
import json
import logging

from app.llm_client import LLMCallError, RateLimitError, call_llm, parse_llm_json
from app.story.knowledge import facts_visible_to, project_knowledge_for_subject
from app.story.relationship_memory import build_relationship_context
from app.story.views import player_character_view
from fastapi import HTTPException

logger = logging.getLogger(__name__)

ACTOR_SYSTEM_PROMPT = """You play exactly one NPC in an interactive scene. You know only the
character sheet, beliefs, witnessed memories, and observable situation supplied here.
Do not infer a hidden plot from genre conventions. Do not decide the player's thoughts,
actions, world state, or another character's dialogue. Offer one small, believable
response that follows this NPC's current needs; silence or inaction is valid.
If your own evidence does not establish a technical detail or past event, do
not supply a confident explanation just to answer a question. Let this NPC
admit uncertainty, remember imperfectly, or suggest a concrete way to check.
Never invent a prior phone call, appointment, promise, or completed errand to
repair a contradiction in the scene.
Return raw JSON only: {"visible_behavior": "", "possible_dialogue": ""}.
These are suggestions for the writer, not committed events. Do not reveal private
motives or beliefs in either field unless this character deliberately says them aloud.
"""


def _scene_character_view(characters: dict, protagonist_id: str) -> dict:
    # Other characters' goals, psychology, private knowledge, and powers do not
    # belong in the writer/director's shared perspective.
    public = player_character_view(characters, "")
    protagonist = characters.get(protagonist_id) if isinstance(characters, dict) else None
    if isinstance(protagonist, dict) and protagonist_id in public:
        # Inventory and present disposition are useful to a close POV. Raw
        # backstory/psychology/knowledge may contain creator-only inferences.
        for key in ("inventory", "personality"):
            if key in protagonist:
                public[protagonist_id][key] = protagonist[key]
    return public


def _public_outcome(value: object, fields: tuple) -> object:
    if not isinstance(value, dict):
        return value if value is None else {}
    return {key: value[key] for key in fields if key in value}


def project_recent_turns(turns: object) -> list:
    """Pass published scenes and engine-confirmed state, never free-form notes."""
    if not isinstance(turns, list):
        return []
    projected = []
    for turn in turns:
        if isinstance(turn, str):
            projected.append(turn)
            continue
        if not isinstance(turn, dict):
            continue
        item = {
            key: turn[key]
            for key in ("chapter_index", "turn_index", "chapter_title", "user_input", "chapter_text")
            if key in turn
        }
        scene_record = turn.get("scene_record")
        if isinstance(scene_record, dict):
            item["scene_record"] = scene_record
        projected.append(item)
    return projected


def build_committed_scene_record(character_state: dict, character_ids: object,
                                 action_resolution: dict, inventory_resolution: dict,
                                 travel_resolution: dict, time_skip_resolution: dict,
                                 elapsed_time_resolution: dict) -> dict:
    """Structured outcomes confirmed by the engine after the scene is accepted."""
    characters = character_state.get("characters", {}) if isinstance(character_state, dict) else {}
    if isinstance(character_ids, dict):
        ids = set(character_ids)
    elif isinstance(character_ids, (list, tuple, set)):
        ids = set(character_ids)
    else:
        ids = set()
    locations = {}
    for character_id in ids:
        state = characters.get(character_id) if isinstance(characters, dict) else None
        if isinstance(state, dict) and isinstance(state.get("location"), str):
            locations[character_id] = state["location"]
    return {
        "character_locations_after_scene": locations,
        "action_outcome": _public_outcome(action_resolution, ("intent", "result")),
        "inventory_outcome": _public_outcome(
            inventory_resolution, ("status", "action", "item_name", "result")
        ),
        "travel_outcome": _public_outcome(
            travel_resolution, ("status", "destination", "elapsed_minutes")
        ),
        "time_skip_outcome": _public_outcome(
            time_skip_resolution, ("elapsed_minutes", "tick_advance")
        ),
        "elapsed_time": elapsed_time_resolution if isinstance(elapsed_time_resolution, dict) else None,
    }


def project_scene_payload(payload: dict, raw_characters: dict, canon_facts: list,
                          protagonist_id: str) -> dict:
    """Only published history, protagonist knowledge, and current observable state."""
    config = payload.get("world_config") or {}
    context = (payload.get("multi_tier_context") or {}).get("multi_tier_context") or {}
    recent = context.get("tier_1_working_memory") or {}
    summary = context.get("tier_2_rolling_summary") or {}
    beats = context.get("tier_2_memorable_beats") or {}
    visible = facts_visible_to(canon_facts, raw_characters, protagonist_id)
    # Omit creator plot thesis, locked/hidden card content, future thread ledger,
    # and the all-subject knowledge and relationship maps.
    result = {
        "world_canon_facts": [f.get("statement") for f in visible if f.get("statement")],
        "character_knowledge": {protagonist_id: project_knowledge_for_subject(
            raw_characters, protagonist_id, canon_facts)},
        "character_state": _scene_character_view(raw_characters, protagonist_id),
        "world_config": {key: config.get(key) for key in (
            "genre", "tone", "protagonist_id", "pacing_level", "output_length",
            "pov_angle", "prelude_enabled", "interaction_mode", "keyword_auto_retry",
            "checkpoint_boundary_mode", "timekeeping_mode")},
        "story_clock": payload.get("story_clock"),
        "words_per_turn_target": payload.get("words_per_turn_target"),
        "style_card": payload.get("style_card"),
        "opening_setup": payload.get("opening_setup"),
        "current_checkpoint": {
            "checkpoint_id": (payload.get("current_checkpoint") or {}).get("checkpoint_id")
        },
        "active_cards": [{"id": card.get("id"), "type": card.get("type"),
                          "name": card.get("name")}
                         for card in payload.get("active_cards", []) if isinstance(card, dict)],
        "multi_tier_context": {"multi_tier_context": {
            "tier_1_working_memory": {
                "recent_turns": project_recent_turns(recent.get("recent_turns", []))
            },
            "tier_2_rolling_summary": {"summary": summary.get("summary", "")},
            "tier_2_memorable_beats": {"beats": beats.get("beats", [])},
        }},
        "user_input": payload.get("user_input"),
        # Include only player-facing outcomes. Raw checks, rule reasons, travel
        # legs and interruption metadata can contain unrevealed mechanics or
        # hidden route structure. The full versions remain with the engine.
        "action_resolution": _public_outcome(payload.get("action_resolution"),
                                              ("intent", "result")),
        "inventory_resolution": payload.get("inventory_resolution"),
        "travel_resolution": _public_outcome(payload.get("travel_resolution"),
                                             ("status", "destination", "elapsed_minutes", "tick_advance")),
        "time_skip_resolution": _public_outcome(payload.get("time_skip_resolution"),
                                                ("elapsed_minutes", "tick_advance")),
    }
    return result


def call_actor_stages(raw_characters: dict, participants: dict, protagonist_id: str,
                      observable_action: str, scene_location: str, world_name: str,
                      canon_facts: list, prior_observations: dict = None) -> dict:
    """One call per present NPC; no shared private context between actors."""
    cues = {}
    for actor_id in participants:
        if actor_id == protagonist_id:
            continue
        actor = raw_characters.get(actor_id) or {}
        if not isinstance(actor, dict):
            continue
        own = {key: actor.get(key) for key in (
            "name", "location", "personality", "speech_style",
            "psychology", "knowledge_flags")}
        own["knowledge"] = project_knowledge_for_subject(raw_characters, actor_id, canon_facts)
        own["relationship_memories"] = build_relationship_context(raw_characters, [actor_id]).get(actor_id)
        public_others = player_character_view(
            {key: value for key, value in participants.items() if key != actor_id}, "")
        actor_payload = {
            "character_id": actor_id,
            "character": own,
            "other_people_present": public_others,
            "scene_location": scene_location,
            "observable_action": observable_action[:600],
            "prior_witnessed_turns": (prior_observations or {}).get(actor_id, [])[:4],
        }
        try:
            raw = call_llm(ACTOR_SYSTEM_PROMPT, json.dumps(actor_payload, ensure_ascii=False),
                           world_name=world_name, role="actor")
            parsed = parse_llm_json(raw, expected_type=dict)
        except RateLimitError as exc:
            raise HTTPException(status_code=429, detail={
                "message": f"OpenRouter is rate-limiting actor agent: {exc}",
                "retry_after": exc.retry_after,
            }) from exc
        except (LLMCallError, ValueError, json.JSONDecodeError) as exc:
            # In an opt-in experiment, don't silently turn an actor failure
            # into the old omniscient path or commit a half-directed scene.
            logger.warning("Actor stage failed for %s: %s", actor_id, exc)
            raise HTTPException(status_code=502, detail=f"Actor stage failed for {actor_id}.") from exc
        cue = {}
        for field in ("visible_behavior", "possible_dialogue"):
            value = parsed.get(field)
            if isinstance(value, str) and value.strip():
                cue[field] = value.strip()[:500]
        cues[actor_id] = cue
    return cues
