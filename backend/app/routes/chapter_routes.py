from fastapi import APIRouter, HTTPException
import json
import os

from app.storage import (
    require_world, world_path_of, read_world_file, write_world_file,
    has_real_api_key, bump_world_revision
)
from app.engine import (
    TEMPLATES, DEFAULT_STORY_CLOCK, find_checkpoint, _generate_chapter,
    _generate_prelude, build_opening_instruction, get_open_chapter_state,
    call_llm, parse_llm_json,
    LINTER_SYSTEM_PROMPT, REWRITE_SYSTEM_PROMPT, get_world_style_card,
    LLMCallError
)
from app.models import (
    ChapterContinueRequest, ChapterStartRequest, LintChapterRequest,
    RewriteChapterRequest, RegenerateRequest, TimeSkipRequest
)
from app.persistence import commit_world_files, locked_world
from app.story.inventory import inventory_view

try:
    from prompts import LINTER_SYSTEM_PROMPT as _LSP
    from skill_limiter import check_skill_limiter
except ImportError:
    from backend.skill_limiter import check_skill_limiter

router = APIRouter()


def _read_epilogue(world_path: str):
    try:
        data = read_world_file(world_path, "chapters.json")
    except (FileNotFoundError, json.JSONDecodeError):
        return None
    epilogue = data.get("epilogue")
    if isinstance(epilogue, dict) and epilogue.get("text"):
        return epilogue
    return None


@router.post("/worlds/{world_name}/chapter/continue")
def chapter_continue(world_name: str, req: ChapterContinueRequest):
    return _generate_chapter(
        world_name,
        narrator_input=req.user_input,
        request_id=req.request_id,
        expected_revision=req.expected_revision,
        narration_mode=req.narration_mode,
    )


def _time_skip_preview(world_name: str, req: TimeSkipRequest):
    from app.story.discovery import load_discoveries
    from app.world_events import load_world_events
    from app.world.time_skip import preview_time_skip
    world_path = require_world(world_name)
    config = read_world_file(world_path, "world_config.json")
    return preview_time_skip(
        req.model_dump(), config,
        load_world_events(world_path).get("events", []),
        load_discoveries(world_path),
    )


@router.post("/worlds/{world_name}/time-skip/preview")
def time_skip_preview(world_name: str, req: TimeSkipRequest):
    return _time_skip_preview(world_name, req)


@router.post("/worlds/{world_name}/time-skip/execute")
def time_skip_execute(world_name: str, req: TimeSkipRequest):
    from app.world.time_skip import display_time_skip
    # `narration_mode` picks the prose profile only. It is kept out of the
    # engine-facing request dict so the time-skip action hash (which decides
    # whether a retry replays a committed turn) is unchanged by this feature.
    skip_request = req.model_dump(exclude={"narration_mode"})
    preview = _time_skip_preview(world_name, req)
    if preview.get("blocked"):
        raise HTTPException(status_code=409, detail={
            "reason": "known_deadline_imminent",
            "message": "A known deadline is imminent. Act now or use Creator override.",
            "preview": preview,
        })
    return _generate_chapter(
        world_name,
        narrator_input=display_time_skip(skip_request, preview),
        display_input=display_time_skip(skip_request, preview),
        request_id=req.request_id,
        expected_revision=req.expected_revision,
        time_skip_request=skip_request,
        narration_mode=req.narration_mode,
    )


@router.post("/worlds/{world_name}/chapter/start")
def chapter_start(world_name: str, req: ChapterStartRequest):
    world_path = require_world(world_name)

    chapters_data = read_world_file(world_path, "chapters.json")
    non_prelude_chapters = [c for c in chapters_data["chapters"] if c.get("chapter_index") != 0]
    if non_prelude_chapters:
        raise HTTPException(
            status_code=400,
            detail="This world already has chapters -- /chapter/start is only for the first chapter, "
                   "use /chapter/continue to write further."
        )

    world_config = read_world_file(world_path, "world_config.json")
    if world_config.get("prelude_enabled") and not world_config.get("prelude_confirmed"):
        raise HTTPException(
            status_code=400,
            detail="This world has prelude_enabled=True but the prelude has not been generated and confirmed yet. "
                   "Use /chapter/generate-prelude first, then /chapter/confirm-prelude before starting Chapter 1."
        )
    effective_mode = req.opening_mode or world_config.get("opening_mode", "ai_generate")
    if effective_mode not in ("ai_generate", "user_defined"):
        raise HTTPException(
            status_code=400,
            detail=f"opening_mode '{effective_mode}' is invalid (only accepts 'ai_generate' or 'user_defined')."
        )

    if effective_mode == "user_defined":
        opening_text = (req.opening_text or world_config.get("opening_text", "")).strip()
        if not opening_text:
            raise HTTPException(
                status_code=400,
                detail="opening_mode is 'user_defined' but ch\u01b0a c\u00f3 opening_text "
                       "(g\u1eedi k\u00e8m in request either set s\u1eb5n in world_config)."
            )
        chapter_record = {
            "chapter_index": 1,
            "turn_index": 1,
            "chapter_closed": True,
            "chapter_title": None,
            "checkpoint_id": world_config.get("current_checkpoint_id", ""),
            "user_input": "",
            "chapter_text": opening_text,
            "notes": "Opening chapter written by user (opening_mode: user_defined), "
                     "bypassed narrator/consistency checker.",
            "boundary_correction": None,
            "consistency_check": {
                "status": "unavailable",
                "severity": "none",
                "issues": [],
                "explanation": "Skipped check because this content is user-written, bypassed narrator.",
                "triggered_rewrite": False
            }
        }
        chapters_data["chapters"] = [chapter_record]
        write_world_file(world_path, "chapters.json", chapters_data)
        revision = bump_world_revision(world_path)
        return {
            "chapter": chapter_record,
            "state_changes_applied": {"characters": {}, "notes": ""},
            "used_mock_llm": False,
            "checkpoint_advanced": False,
            "lore_rag_filter": None,
            "revision": revision,
        }

    canon_timeline = read_world_file(world_path, "canon_timeline.json")
    current_checkpoint_id = world_config.get("current_checkpoint_id", "")
    checkpoint = find_checkpoint(canon_timeline["checkpoints"], current_checkpoint_id)
    if checkpoint is None:
        raise HTTPException(
            status_code=400,
            detail=f"current_checkpoint_id '{current_checkpoint_id}' khong khop checkpoint nao "
                   f"in canon_timeline (world co the la data cu, chua co field nay -- "
                   f"chay lai seed-demo hoac set thu cong)."
        )
    synthetic_instruction = build_opening_instruction(checkpoint)
    return _generate_chapter(world_name, narrator_input=synthetic_instruction,
                             display_input="", opening_setup=True,
                             narration_mode=req.narration_mode)


@router.post("/worlds/{world_name}/chapter/generate-prelude")
def chapter_generate_prelude(world_name: str):
    world_path = require_world(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")
    existing_prelude = [c for c in chapters_data.get("chapters", []) if c.get("chapter_index") == 0]
    if existing_prelude:
        raise HTTPException(
            status_code=400,
            detail="A prelude already exists for this world. Use /chapter/regenerate-prelude to replace it."
        )
    try:
        result = _generate_prelude(world_name)
        bump_world_revision(world_path)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/worlds/{world_name}/chapter/regenerate-prelude")
def chapter_regenerate_prelude(world_name: str):
    world_path = require_world(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")
    chapters = chapters_data.get("chapters", [])
    chapters[:] = [c for c in chapters if c.get("chapter_index") != 0]
    write_world_file(world_path, "chapters.json", chapters_data)
    try:
        result = _generate_prelude(world_name)
        bump_world_revision(world_path)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/worlds/{world_name}/chapter/confirm-prelude")
def chapter_confirm_prelude(world_name: str):
    world_path = require_world(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")
    prelude_exists = any(c.get("chapter_index") == 0 for c in chapters_data.get("chapters", []))
    if not prelude_exists:
        raise HTTPException(status_code=400, detail="No prelude exists to confirm. Generate one first.")
    world_config = read_world_file(world_path, "world_config.json")
    
    # Confirming a prologue is not the same as playing the first checkpoint.
    # cp_0 describes Chapter 1's playable opening; consuming it here hands the
    # narrator cp_1's future events before the player has made those choices.
    if world_config.get("prelude_confirmed"):
        return {"status": "prelude_confirmed"}

    world_config["prelude_confirmed"] = True
    write_world_file(world_path, "world_config.json", world_config)
    bump_world_revision(world_path)
    return {"status": "prelude_confirmed"}


@router.post("/worlds/{world_name}/chapter/regenerate")
def chapter_regenerate(world_name: str, req: RegenerateRequest = None):
    world_path = require_world(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")
    turns = chapters_data.get("chapters", [])

    if not turns:
        raise HTTPException(
            status_code=400,
            detail="Khong co turn nao de regenerate. World chua co chapter nao."
        )

    original_input = turns[-1].get("user_input", "")
    replacement_input = req.user_input if req and req.user_input is not None else original_input
    if not replacement_input.strip():
        raise HTTPException(status_code=422, detail="The revised action cannot be empty")
    replacement_input = replacement_input.strip()
    request_id = req.request_id if req else None
    expected_revision = req.expected_revision if req else None

    # The last turn is removed inside _generate_chapter so nothing is written
    # before the new turn commits (a blocked/failed regenerate leaves the old
    # turn intact).
    return _generate_chapter(
        world_name,
        narrator_input=replacement_input,
        display_input=replacement_input,
        request_id=request_id,
        expected_revision=expected_revision,
        regenerate=True,
        narration_mode=req.narration_mode if req else None,
    )


@router.get("/worlds/{world_name}/chapters")
def get_chapters(world_name: str):
    """Get all chapters for a world, including prelude."""
    world_path = require_world(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")
    return chapters_data.get("chapters", [])


@router.get("/worlds/{world_name}/play-state")
def get_play_state(world_name: str):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    character_state = read_world_file(world_path, "character_state.json")
    card_registry = read_world_file(world_path, "card_registry.json")
    canon_timeline = read_world_file(world_path, "canon_timeline.json")

    protagonist_id = world_config.get("protagonist_id", "")
    current_cp_id = world_config.get("current_checkpoint_id", "")
    completed = world_config.get("completed_checkpoints", [])
    checkpoints = canon_timeline.get("checkpoints", [])

    story_clock = world_config.get("story_clock")
    if not isinstance(story_clock, dict):
        story_clock = dict(DEFAULT_STORY_CLOCK)
    foreshadowing_tracker = world_config.get("foreshadowing_tracker")
    if foreshadowing_tracker is None:
        foreshadowing_tracker = world_config.get("foreshadowings", [])

    protagonist_data = None
    if protagonist_id and protagonist_id in character_state.get("characters", {}):
        p = character_state["characters"][protagonist_id]
        from app.story.knowledge import project_knowledge_for_subject
        from app.story.capabilities import capability_evidence
        from app.storage import read_world_canon
        canon_facts = read_world_canon(world_path).get("facts", [])
        protagonist_data = {
            "id": protagonist_id,
            "name": p.get("name", protagonist_id),
            "location": p.get("location", ""),
            "power_stat": p.get("power_stat", {}),
            "traits": p.get("traits", {}),
            "knowledge_flags": p.get("knowledge_flags", []),
            "inventory": inventory_view(p.get("inventory", []), owner_id=protagonist_id),
            "knowledge": project_knowledge_for_subject(
                character_state["characters"], protagonist_id, canon_facts
            ),
            "alive": p.get("alive", True),
            "relationships": p.get("relationships", {}),
            "age": p.get("age", ""),
            "capabilities": capability_evidence(p),
        }

    total_checkpoints = len(checkpoints)
    current_index = 0
    for i, cp in enumerate(checkpoints):
        if cp["checkpoint_id"] == current_cp_id:
            current_index = i
            break
    arc_progress = {
        "current_checkpoint_id": current_cp_id,
        "current_index": current_index,
        "total_checkpoints": total_checkpoints,
        "completed": completed
    }

    unlocked_cards = []
    for card in card_registry.get("cards", []):
        if card.get("status") == "unlocked":
            card_info = {
                "id": card.get("id", ""),
                "type": card.get("type", ""),
                "name": card.get("name", ""),
                "content": card.get("content", "")
            }
            if card.get("type") == "char":
                char_key = card.get("id", "")
                if char_key in character_state.get("characters", {}):
                    char = character_state["characters"][char_key]
                    card_info["affinity"] = char.get("affinity", {})
                    card_info["relationships"] = char.get("relationships", {})
                    card_info["age"] = char.get("age", "")
                    card_info["power_stat"] = char.get("power_stat", {})
            unlocked_cards.append(card_info)

    return {
        "protagonist": protagonist_data,
        "arc_progress": arc_progress,
        "arc_roadmap": world_config.get("arc_roadmap", {}),
        "unlocked_cards": unlocked_cards,
        "story_clock": story_clock,
        "calendar": world_config.get("calendar"),
        "prelude_confirmed": bool(world_config.get("prelude_confirmed")),
        "foreshadowing_tracker": foreshadowing_tracker,
        "foreshadowings": foreshadowing_tracker,
        "output_length": world_config.get("output_length", "Standard"),
        "revision": int(world_config.get("revision", 0) or 0),
        "lifecycle_status": world_config.get("lifecycle_status", "active"),
        "story_mode": world_config.get("story_mode", "endless"),
        "active_journey": world_config.get("active_journey"),
        "epilogue": _read_epilogue(world_path),
        "style_card": get_world_style_card(world_name)
    }


@router.post("/worlds/{world_name}/lint-chapter")
def lint_chapter(world_name: str, req: LintChapterRequest):
    world_path = require_world(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")
    world_config = read_world_file(world_path, "world_config.json")
    character_state = read_world_file(world_path, "character_state.json")

    chapter_turns = sorted(
        (c for c in chapters_data.get("chapters", []) if c["chapter_index"] == req.chapter_index),
        key=lambda c: c["turn_index"]
    )
    if not chapter_turns:
        raise HTTPException(status_code=404, detail="Chapter not found")

    full_chapter_text = "\n\n".join(c.get("chapter_text", "") for c in chapter_turns)

    relationships = {}
    for char_id, state in character_state.get("characters", {}).items():
        if state.get("relationships"):
            relationships[char_id] = state["relationships"]

    payload = {
        "chapter_text": full_chapter_text,
        "running_summary": chapters_data.get("running_summary", ""),
        "story_clock": world_config.get("story_clock", {}),
        "foreshadowing_tracker": world_config.get("foreshadowing_tracker", []),
        "relationships": relationships
    }

    try:
        raw = call_llm(
            LINTER_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            world_name=world_name
        )
        parsed = parse_llm_json(raw)
        return parsed
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/worlds/{world_name}/chapter/rewrite")
def rewrite_chapter(world_name: str, req: RewriteChapterRequest):
    world_path = require_world(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")

    chapter_turns = sorted(
        (c for c in chapters_data.get("chapters", []) if c["chapter_index"] == req.chapter_index),
        key=lambda c: c["turn_index"]
    )
    if not chapter_turns:
        raise HTTPException(status_code=404, detail="Chapter not found")

    full_chapter_text = "\n\n".join(c.get("chapter_text", "") for c in chapter_turns)

    payload = {
        "original_text": full_chapter_text,
        "linter_suggestions": req.linter_suggestions
    }

    try:
        raw = call_llm(
            REWRITE_SYSTEM_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            world_name=world_name
        )
        parsed = parse_llm_json(raw)
        rewritten_text = parsed.get("rewritten_text", full_chapter_text)

        first_turn_index = chapter_turns[0]["turn_index"]
        chapters_data["chapters"] = [
            t for t in chapters_data["chapters"]
            if not (t["chapter_index"] == req.chapter_index and t["turn_index"] != first_turn_index)
        ]

        for t in chapters_data["chapters"]:
            if t["chapter_index"] == req.chapter_index and t["turn_index"] == first_turn_index:
                t["chapter_text"] = rewritten_text

        write_world_file(world_path, "chapters.json", chapters_data)
        bump_world_revision(world_path)
        return {"message": "success", "rewritten_text": rewritten_text}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/worlds/{world_name}/chapter/endgame-status")
def get_endgame_status(world_name: str):
    from app.storage import read_world_file, require_world
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    lifecycle = world_config.get("lifecycle_status", "active")
    story_mode = world_config.get("story_mode", "endless")
    target = world_config.get("target_ending_scenario")
    return {
        "story_mode": story_mode,
        "lifecycle_status": lifecycle,
        "endgame_ready": lifecycle == "endgame_pending" and story_mode == "fixed_ending",
        "target_ending_summary": target.get("summary") if target else None,
        "is_fallback": target.get("used_fallback", False) if target else False,
        "lifecycle_status_completed": lifecycle == "completed",
    }


@router.post("/worlds/{world_name}/chapter/generate-epilogue-choices")
@locked_world
def generate_epilogue_choices(world_name: str):
    from app.engine import call_llm, parse_llm_json
    from prompts import EPILOGUE_CHOICES_PROMPT
    from app.storage import read_world_file, require_world, write_world_file
    import json

    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")

    if world_config.get("story_mode") != "fixed_ending":
        raise HTTPException(status_code=400, detail="Epilogue only available for fixed_ending worlds")

    if world_config.get("lifecycle_status") != "endgame_pending":
        raise HTTPException(status_code=400, detail="Endgame not ready yet")

    target = world_config.get("target_ending_scenario")
    if not target:
        raise HTTPException(status_code=400, detail="No target ending scenario configured")

    character_state = read_world_file(world_path, "character_state.json")
    chapters_data = read_world_file(world_path, "chapters.json")
    running_summary = chapters_data.get("running_summary", "")
    relationships = character_state.get("characters", {})

    is_fallback = target.get("used_fallback", False)
    payload = {
        "ending_summary": target.get("summary", ""),
        "fallback_summary": target.get("fallback_summary", "A fateful conclusion to the story."),
        "relationships": relationships,
        "is_fallback": is_fallback,
    }

    try:
        raw = call_llm(
            EPILOGUE_CHOICES_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            world_name=world_name
        )
        parsed = parse_llm_json(raw)
        choices = parsed.get("choices", [])
        if not choices or not isinstance(choices, list):
            choices = ["Accept the ending as it comes.", "Fight against fate.", "Seek a new path."]
        return {"choices": choices}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/worlds/{world_name}/chapter/generate-epilogue")
@locked_world
def generate_epilogue(world_name: str, req: dict):
    from app.engine import call_llm, parse_llm_json
    from prompts import EPILOGUE_GENERATOR_PROMPT
    from app.storage import read_world_file, require_world, write_world_file
    import json

    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    chapters_data = read_world_file(world_path, "chapters.json")
    if world_config.get("lifecycle_status") == "completed":
        saved = chapters_data.get("epilogue")
        if isinstance(saved, dict) and saved.get("text"):
            return {"epilogue": saved["text"], "lifecycle_status": "completed"}
        raise HTTPException(status_code=409, detail="This story is already completed")
    if world_config.get("story_mode") != "fixed_ending" or world_config.get("lifecycle_status") != "endgame_pending":
        raise HTTPException(status_code=409, detail="Endgame is not ready")
    target = world_config.get("target_ending_scenario")
    if not target:
        raise HTTPException(status_code=400, detail="No target ending scenario")

    chosen_choice = req.get("chosen_choice", "")
    if not isinstance(chosen_choice, str) or not chosen_choice.strip():
        raise HTTPException(status_code=400, detail="Missing chosen_choice")

    character_state = read_world_file(world_path, "character_state.json")
    chapters_data = read_world_file(world_path, "chapters.json")
    running_summary = chapters_data.get("running_summary", "")
    relationships = character_state.get("characters", {})
    output_length = world_config.get("output_length", "Standard")

    is_fallback = target.get("used_fallback", False)
    payload = {
        "chosen_choice": chosen_choice,
        "ending_summary": target.get("summary", ""),
        "fallback_summary": target.get("fallback_summary", "A fateful conclusion."),
        "is_fallback": is_fallback,
        "relationships": relationships,
        "running_summary": running_summary,
        "output_length": output_length,
    }

    try:
        raw = call_llm(
            EPILOGUE_GENERATOR_PROMPT,
            json.dumps(payload, ensure_ascii=False),
            world_name=world_name
        )
        parsed = parse_llm_json(raw)
        epilogue = parsed.get("epilogue") if isinstance(parsed, dict) else None
        if not isinstance(epilogue, str) or not epilogue.strip():
            raise HTTPException(status_code=502, detail="The model returned no epilogue; the story remains open")

        chapters_data["epilogue"] = {"text": epilogue, "chosen_choice": chosen_choice}
        last_index = max((c.get("chapter_index", 0) for c in chapters_data["chapters"]), default=0)
        chapters_data["chapters"].append({
            "chapter_index": last_index + 1,
            "turn_index": 1,
            "chapter_closed": True,
            "chapter_title": "Epilogue",
            "checkpoint_id": world_config.get("current_checkpoint_id", ""),
            "user_input": chosen_choice,
            "chapter_text": epilogue,
            "kind": "epilogue",
        })
        world_config["lifecycle_status"] = "completed"
        commit_world_files(world_path, {
            "chapters.json": chapters_data,
            "world_config.json": world_config,
        })
        bump_world_revision(world_path)

        return {"epilogue": epilogue, "lifecycle_status": "completed"}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
