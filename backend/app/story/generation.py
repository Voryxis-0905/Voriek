"""Generation responsibilities for Story Engine."""
import logging
from app.llm_client import LLMCallError
from app.llm_client import RateLimitError
from app.llm_client import call_llm
from app.llm_client import parse_llm_json
from app.models import StateChangesModel
from app.prompts import EDITOR_SYSTEM_PROMPT
from app.story.narration_mode import select_planner_prompt
from app.story.narration_mode import select_writer_prompt
from app.story.narration_mode import is_experimental
from fastapi import HTTPException
from pydantic import ValidationError
from typing import List
import json

logger = logging.getLogger(__name__)


def check_anchor_keywords(chapter_text: str, anchor_keywords: List[str]) -> List[str]:
    text_lower = chapter_text.lower()
    missing = []
    for kw in anchor_keywords:
        if not kw or not kw.strip():
            continue
        if kw.lower() not in text_lower:
            missing.append(kw)
    return missing


def _scene_direction_for_writer(raw: object, character_state: object,
                                protagonist_id: object = None) -> dict:
    """Keep director notes advisory and limited to characters in this scene."""
    if not isinstance(raw, dict):
        return {}
    direction = {}
    for field in ("focus", "stop_before"):
        value = raw.get(field)
        if isinstance(value, str) and value.strip():
            direction[field] = value.strip()[:400]
    actors = raw.get("actors")
    allowed = character_state if isinstance(character_state, dict) else {}
    if isinstance(actors, dict):
        clean_actors = {}
        for actor_id, cue in actors.items():
            if actor_id not in allowed or actor_id == protagonist_id or not isinstance(cue, dict):
                continue
            clean_cue = {}
            for field in ("want", "approach"):
                value = cue.get(field)
                if isinstance(value, str) and value.strip():
                    clean_cue[field] = value.strip()[:240]
            if clean_cue:
                clean_actors[actor_id] = clean_cue
        if clean_actors:
            direction["actors"] = clean_actors
    return direction


def call_planner_stage(payload: dict, user_input_for_mock: str, world_name: str = None,
                       narration_mode=None) -> dict:
    try:
        planner_raw = call_llm(
            select_planner_prompt(narration_mode),
            json.dumps(payload, ensure_ascii=False),
            user_input_for_mock=user_input_for_mock,
            world_name=world_name,
            role="planner"
        )
    except RateLimitError as e:
        raise HTTPException(
            status_code=429,
            detail={
                "message": f"OpenRouter is rate-limiting planner agent: {e}",
                "retry_after": e.retry_after
            }
        )
    except LLMCallError as e:
        raise HTTPException(status_code=503, detail=f"Failed to call planner agent: {e}")

    try:
        planner_parsed = parse_llm_json(planner_raw)
    except (json.JSONDecodeError, ValueError):
        raise HTTPException(
            status_code=502,
            detail="Planner returned invalid JSON, could not parse."
        )

    scene_outline = planner_parsed.get("scene_outline", "")
    facts_this_turn = planner_parsed.get("facts_this_turn", [])
    if not isinstance(facts_this_turn, list):
        facts_this_turn = []
    state_changes = planner_parsed.get("state_changes", {"characters": {}, "notes": ""})

    try:
        StateChangesModel(**state_changes)
    except ValidationError as e:
        raise HTTPException(
            status_code=502,
            detail=f"Planner returned malformed state_changes schema, cannot merge safely: {e}"
        )

    raw_suggested = planner_parsed.get("suggested_actions", [])
    suggested_actions = raw_suggested if isinstance(raw_suggested, list) else []
    suggested_actions = [s for s in suggested_actions if isinstance(s, str) and s.strip()][:4]

    anchor_keywords = planner_parsed.get("anchor_keywords", [])
    if not isinstance(anchor_keywords, list):
        anchor_keywords = []
    # Provider JSON can contain numbers or objects even when the prompt asks
    # for strings. Those must never reach check_anchor_keywords (which uses
    # string operations); valid classic keywords retain their original shape.
    anchor_keywords = [kw for kw in anchor_keywords if isinstance(kw, str) and kw.strip()]
    if is_experimental(narration_mode):
        anchor_keywords = anchor_keywords[:2]
    open_threads_update = planner_parsed.get("open_threads_update", "")
    if not isinstance(open_threads_update, str):
        open_threads_update = ""

    is_ooc = bool(planner_parsed.get("is_ooc", False))
    action_translation = planner_parsed.get("action_translation", "")
    if not isinstance(action_translation, str):
        action_translation = ""
    effective_user_input = action_translation if (is_ooc and action_translation.strip()) else user_input_for_mock

    return {
        "scene_outline": scene_outline,
        "facts_this_turn": facts_this_turn,
        "state_changes": state_changes,
        "suggested_actions": suggested_actions,
        "anchor_keywords": anchor_keywords,
        "open_threads_update": open_threads_update,
        "is_ooc": is_ooc,
        "action_translation": action_translation,
        "effective_user_input": effective_user_input,
        "scene_direction": (_scene_direction_for_writer(
            planner_parsed.get("scene_direction"), payload.get("character_state"),
            (payload.get("world_config") or {}).get("protagonist_id")
        ) if is_experimental(narration_mode) else {}),
        "actor_observation": (planner_parsed.get("actor_observation", "")[:600]
                              if isinstance(planner_parsed.get("actor_observation"), str) else ""),
    }


def call_writer_stage(payload: dict, scene_outline: str, facts_this_turn: list,
                      state_changes: dict, suggested_actions: list,
                      anchor_keywords: list, open_threads_update: str,
                      is_ooc: bool, action_translation: str,
                      effective_user_input: str,
                      world_name: str = None, editor_enabled: bool = False,
                      narration_mode=None, scene_direction=None):
    writer_payload = dict(payload)
    writer_payload["scene_outline"] = scene_outline
    writer_payload["facts_this_turn"] = facts_this_turn
    writer_payload["planned_state_changes"] = state_changes
    if is_experimental(narration_mode):
        # The classic writer payload remains untouched. In the experimental
        # profile, rare essential anchors are actually visible to the writer
        # instead of only being checked after generation.
        writer_payload["anchor_keywords"] = anchor_keywords
        if scene_direction:
            writer_payload["scene_direction"] = scene_direction

    # Resolved once and reused by every writer call below (first draft, format
    # retry, keyword retry). A retry that quietly reverted to the classic prompt
    # would make the switch unreliable exactly when the model is struggling.
    writer_system_prompt = select_writer_prompt(narration_mode)

    def call_writer_once(payload_for_call: dict) -> str:
        try:
            return call_llm(
                writer_system_prompt,
                json.dumps(payload_for_call, ensure_ascii=False),
                user_input_for_mock=effective_user_input,
                world_name=world_name,
                role="writer"
            )
        except RateLimitError as e:
            raise HTTPException(
                status_code=429,
                detail={
                    "message": f"OpenRouter is rate-limiting writer agent: {e}",
                    "retry_after": e.retry_after
                }
            )
        except LLMCallError as e:
            raise HTTPException(status_code=503, detail=f"Failed to call writer agent: {e}")

    writer_raw = call_writer_once(writer_payload)
    try:
        writer_parsed = parse_llm_json(writer_raw)
    except (json.JSONDecodeError, ValueError) as first_error:
        logger.warning(
            "Writer returned invalid JSON for world=%s; retrying once with a format correction: %s",
            world_name, first_error
        )
        retry_payload = dict(writer_payload)
        format_note = (
            "Your previous writer response was not valid JSON. Return ONLY valid JSON matching "
            "the writer schema. Do not include markdown, code fences, commentary, or thinking text. "
            "Required keys include chapter_text, chapter_end, chapter_title, state_changes, steps, "
            "variants, perception_data, and draft_entities."
        )
        existing_note = str(writer_payload.get("correction_note") or "").strip()
        retry_payload["correction_note"] = f"{existing_note}\n\n[Format Correction]\n{format_note}".strip() if existing_note else format_note
        retry_raw = call_writer_once(retry_payload)
        try:
            writer_parsed = parse_llm_json(retry_raw)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(
                status_code=502,
                detail="Writer returned invalid JSON after one format retry, could not parse."
            )

    chapter_text = writer_parsed.get("chapter_text", "")
    chapter_end = bool(writer_parsed.get("chapter_end", False))
    chapter_title = writer_parsed.get("chapter_title") or None

    draft_entities = writer_parsed.get("draft_entities", [])
    if not isinstance(draft_entities, list):
        draft_entities = []

    writer_state_changes = writer_parsed.get("state_changes", None)
    if writer_state_changes is not None and isinstance(writer_state_changes, dict):
        state_changes = writer_state_changes

    suggestion_source = writer_parsed

    keyword_missing = check_anchor_keywords(chapter_text, anchor_keywords)
    if keyword_missing:
        logger.warning(
            "Anchor keywords missing from chapter_text (world=%s): %s",
            world_name, keyword_missing
        )
        auto_retry = payload.get("world_config", {}).get("keyword_auto_retry", False)
        if auto_retry and anchor_keywords:
            for attempt in range(1, 3):
                retry_payload = dict(writer_payload)
                retry_payload["correction_note"] = (
                    f"The previous chapter_text was missing these required anchor keywords: "
                    f"{keyword_missing}. Please rewrite the chapter_text ensuring every one of these "
                    f"keywords appears naturally in the prose."
                )
                try:
                    retry_raw = call_llm(
                        writer_system_prompt,
                        json.dumps(retry_payload, ensure_ascii=False),
                        user_input_for_mock=effective_user_input,
                        world_name=world_name,
                        role="writer"
                    )
                    retry_parsed = parse_llm_json(retry_raw)
                    retry_text = retry_parsed.get("chapter_text", "")
                    if retry_text:
                        suggestion_source = retry_parsed
                        chapter_text = retry_text
                        chapter_end = bool(retry_parsed.get("chapter_end", chapter_end))
                        chapter_title = retry_parsed.get("chapter_title") or chapter_title
                        keyword_missing = check_anchor_keywords(chapter_text, anchor_keywords)
                        if not keyword_missing:
                            logger.info("Keyword auto-retry succeeded on attempt %d/2", attempt)
                            break
                        logger.warning(
                            "Keyword auto-retry attempt %d/2 still missing: %s", attempt, keyword_missing
                        )
                except (LLMCallError, json.JSONDecodeError, ValueError):
                    logger.warning("Keyword auto-retry attempt %d/2 failed with exception", attempt)
                    break

    editor_polished = False
    if editor_enabled:
        style_card = payload.get("style_card", {})
        editor_payload = json.dumps(
            {"chapter_text": chapter_text, "style_card": style_card},
            ensure_ascii=False
        )
        try:
            editor_raw = call_llm(
                EDITOR_SYSTEM_PROMPT,
                editor_payload,
                mock_response=json.dumps({"polished_text": chapter_text}, ensure_ascii=False),
                world_name=world_name,
                role="editor"
            )
            editor_parsed = parse_llm_json(editor_raw)
            polished = editor_parsed.get("polished_text", "")
            if polished and isinstance(polished, str) and polished.strip():
                editor_polished = (polished.strip() != chapter_text.strip())
                chapter_text = polished.strip()
        except (LLMCallError, json.JSONDecodeError, ValueError, KeyError):
            pass

    steps = writer_parsed.get("steps", [])
    if not isinstance(steps, list):
        steps = []
    variants = writer_parsed.get("variants", [])
    if not isinstance(variants, list):
        variants = []
    perception_data = writer_parsed.get("perception_data", {})
    if not isinstance(perception_data, dict):
        perception_data = {}

    if is_experimental(narration_mode):
        # Planner suggestions precede the actual scene and can name actions the
        # writer already completed. An omitted writer field is safer as no
        # quick actions than as misleading, stale quick actions.
        final_suggestions = suggestion_source.get("suggested_actions", [])
        suggested_actions = ([item.strip() for item in final_suggestions
                              if isinstance(item, str) and item.strip()][:4]
                             if isinstance(final_suggestions, list) else [])

    return chapter_text, state_changes, chapter_end, chapter_title, suggested_actions, draft_entities, editor_polished, anchor_keywords, open_threads_update, is_ooc, action_translation, keyword_missing, steps, variants, perception_data


def call_narrator_and_parse(payload: dict, user_input_for_mock: str, world_name: str = None,
                            editor_enabled: bool = False, narration_mode=None):
    planner_out = call_planner_stage(payload, user_input_for_mock, world_name,
                                     narration_mode=narration_mode)
    return call_writer_stage(
        payload,
        planner_out["scene_outline"], planner_out["facts_this_turn"],
        planner_out["state_changes"], planner_out["suggested_actions"],
        planner_out["anchor_keywords"], planner_out["open_threads_update"],
        planner_out["is_ooc"], planner_out["action_translation"],
        planner_out["effective_user_input"],
        world_name=world_name, editor_enabled=editor_enabled,
        narration_mode=narration_mode,
        scene_direction=planner_out["scene_direction"],
    )
