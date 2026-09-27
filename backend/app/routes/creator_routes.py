from fastapi import APIRouter, HTTPException
import json
import os
import shutil
from app.persistence import commit_world_files, locked_world
from app.action_guard import clear_receipts, check_expected_revision, bump_revision

from app.storage import (
    require_world, world_path_of, read_world_file, write_world_file,
    has_real_api_key, get_world_style_card, write_world_style_card,
    read_saves_index, write_saves_index, snapshot_world_state,
    build_save_entry, new_save_id, _validate_world_name, bump_world_revision,
    clear_turn_snapshots, read_world_canon, mark_builder_manual
)
from app.engine import (
    TEMPLATES, call_llm, parse_llm_json, make_card, make_checkpoint,
    make_character, LLMCallError, CREATOR_ASSISTANT_PROMPT,
    get_active_cards, get_effective_fallback_chain,
    build_rag_context_text, get_recent_turns_for_context,
    DEFAULT_LORE_RAG_MAX_TOKENS, select_relevant_lore_cards
)
from app.world.schema import CORE_STATE_FILES, ensure_current_schema, read_schema_version, SchemaVersionError
from app.world.templates import SCHEMA_VERSION
from app.world.terrain import TERRAINS, LAYERS
from app.models import (
    CardRegistryUpdate, CanonTimelineUpdate, CharacterStateUpdate,
    ForceAdvanceRequest, CreateSaveRequest, BranchRequest,
    CreatorAssistantRequest, StyleCardModel, TraitDefinition,
    ForeshadowingsUpdateReq
)

router = APIRouter()


def _ensure_snapshot_compatible(save_dir: str) -> dict:
    """Validate/migrate a save snapshot before it is restored or branched.

    A snapshot newer than the app is rejected before any write (so no byte of
    the world changes). An older snapshot is migrated in place with a backup
    inside the snapshot folder.
    """
    if not os.path.isdir(save_dir):
        raise HTTPException(status_code=404, detail="Save snapshot folder not found")
    config_path = os.path.join(save_dir, "world_config.json")
    if os.path.isfile(config_path):
        try:
            version = read_schema_version(save_dir)
        except SchemaVersionError as error:
            raise HTTPException(status_code=409, detail=str(error))
        if version > SCHEMA_VERSION:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"Save snapshot schema_version {version} is newer than this app supports "
                    f"(max {SCHEMA_VERSION}). Update Voriek before restoring or branching."
                ),
            )
    try:
        return ensure_current_schema(save_dir)
    except SchemaVersionError as error:
        raise HTTPException(status_code=409, detail=str(error))


@router.put("/worlds/{world_name}/card_registry")
def update_card_registry(world_name: str, req: CardRegistryUpdate):
    world_path = require_world(world_name)
    cards = [c.model_dump() for c in req.cards]
    ids = [c["id"] for c in cards]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="card id is duplicated, each card must have a unique id")
    write_world_file(world_path, "card_registry.json", {"cards": cards})
    bump_world_revision(world_path)
    mark_builder_manual(world_path, "cards")
    return {"message": "card_registry updated", "cards": len(cards)}


@router.put("/worlds/{world_name}/canon_timeline")
def update_canon_timeline(world_name: str, req: CanonTimelineUpdate):
    world_path = require_world(world_name)
    checkpoints = [cp.model_dump() for cp in req.checkpoints]
    ids = [cp["checkpoint_id"] for cp in checkpoints]
    if len(ids) != len(set(ids)):
        raise HTTPException(status_code=400, detail="checkpoint_id is duplicated, each checkpoint must have a unique id")
    write_world_file(world_path, "canon_timeline.json", {"checkpoints": checkpoints})
    if any(cp.get("playable_situation", "").strip() for cp in checkpoints):
        world_config = read_world_file(world_path, "world_config.json")
        world_config["checkpoint_context_version"] = 2
        write_world_file(world_path, "world_config.json", world_config)
    bump_world_revision(world_path)
    mark_builder_manual(world_path, "skeleton")
    return {"message": "canon_timeline updated", "checkpoints": len(checkpoints)}


@router.put("/worlds/{world_name}/character_state")
def update_character_state(world_name: str, req: CharacterStateUpdate):
    world_path = require_world(world_name)
    characters = {cid: c.model_dump() for cid, c in req.characters.items()}
    write_world_file(world_path, "character_state.json", {"characters": characters})
    bump_world_revision(world_path)
    mark_builder_manual(world_path, "characters")
    return {"message": "character_state updated", "characters": len(characters)}


@router.post("/worlds/{world_name}/checkpoint/force-advance")
def force_advance_checkpoint(world_name: str, req: ForceAdvanceRequest):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    canon_timeline = read_world_file(world_path, "canon_timeline.json")
    card_registry = read_world_file(world_path, "card_registry.json")
    character_state = read_world_file(world_path, "character_state.json")

    checkpoints = canon_timeline["checkpoints"]
    target_index = next(
        (i for i, cp in enumerate(checkpoints) if cp["checkpoint_id"] == req.target_checkpoint_id),
        None
    )
    if target_index is None:
        raise HTTPException(
            status_code=404,
            detail=f"Could not find checkpoint '{req.target_checkpoint_id}' in canon_timeline"
        )

    current_id = world_config.get("current_checkpoint_id", "")
    current_index = next(
        (i for i, cp in enumerate(checkpoints) if cp["checkpoint_id"] == current_id),
        None
    )

    completed = world_config.setdefault("completed_checkpoints", [])
    unlocked_card_names = []
    realm_changes = {}

    if current_index is not None and target_index > current_index:
        for i in range(current_index, target_index):
            cp_id = checkpoints[i]["checkpoint_id"]
            if cp_id not in completed:
                completed.append(cp_id)
        for i in range(current_index + 1, target_index + 1):
            cp = checkpoints[i]
            for card in card_registry["cards"]:
                if (card["id"] in cp.get("cards_unlocked", []) or card.get("unlock_checkpoint_id") == cp["checkpoint_id"]) and card["status"] != "unlocked":
                    card["status"] = "unlocked"
                    unlocked_card_names.append(card["name"])
            for char_id, new_realm in cp.get("realm_updates", {}).items():
                if char_id in character_state["characters"]:
                    character_state["characters"][char_id]["power_stat"]["realm"] = new_realm
                    realm_changes[char_id] = new_realm

    world_config["current_checkpoint_id"] = req.target_checkpoint_id
    write_world_file(world_path, "world_config.json", world_config)
    write_world_file(world_path, "card_registry.json", card_registry)
    write_world_file(world_path, "character_state.json", character_state)
    bump_world_revision(world_path)

    return {
        "message": "Forced checkpoint transition",
        "from_checkpoint_id": current_id,
        "to_checkpoint_id": req.target_checkpoint_id,
        "cards_unlocked": unlocked_card_names,
        "realm_changes": realm_changes
    }


@router.post("/worlds/{world_name}/saves")
@locked_world
def create_save(world_name: str, req: CreateSaveRequest):
    world_path = require_world(world_name)
    save_id = new_save_id()
    snapshot_world_state(world_path, save_id, CORE_STATE_FILES)
    entry = build_save_entry(world_path, save_id, req.label.strip(), "manual")
    saves = read_saves_index(world_path)
    saves.append(entry)
    write_saves_index(world_path, saves)
    return {"message": "Created save point", "save": entry}


@router.get("/worlds/{world_name}/saves")
def list_saves(world_name: str):
    world_path = require_world(world_name)
    return {"saves": list(reversed(read_saves_index(world_path)))}


@router.post("/worlds/{world_name}/saves/{save_id}/restore")
@locked_world
def restore_save(world_name: str, save_id: str):
    world_path = require_world(world_name)
    saves = read_saves_index(world_path)
    if not any(s["save_id"] == save_id for s in saves):
        raise HTTPException(status_code=404, detail=f"Could not find save '{save_id}'")

    src_dir = os.path.join(world_path, "saves", save_id)
    _ensure_snapshot_compatible(src_dir)

    safety_id = new_save_id()
    snapshot_world_state(world_path, safety_id, CORE_STATE_FILES)
    safety_entry = build_save_entry(
        world_path, safety_id,
        f"Automatic (before restoring to '{save_id}')",
        "safety_before_restore"
    )
    saves.append(safety_entry)

    updates = {}
    for filename, template in CORE_STATE_FILES.items():
        src = os.path.join(src_dir, filename)
        if os.path.exists(src):
            updates[filename] = read_world_file(src_dir, filename)
        else:
            # A legacy save must not inherit events/map/canon from its future.
            updates[filename] = template

    # A restored world is a new state: advance the revision so stale actions are
    # rejected, and drop old receipts so they cannot be replayed across a restore.
    current_revision = int(read_world_file(world_path, "world_config.json").get("revision", 0) or 0)
    updates["world_config.json"]["revision"] = current_revision + 1
    updates["world_config.json"]["pre_turn_snapshot"] = None
    updates["saves_index.json"] = {"saves": saves}
    commit_world_files(world_path, updates)
    clear_receipts(world_path)
    clear_turn_snapshots(world_path)
    return {
        "message": "Restored world to the selected save point",
        "restored_save_id": save_id,
        "safety_save_id": safety_id,
        "revision": current_revision + 1,
    }


@router.delete("/worlds/{world_name}/saves/{save_id}")
def delete_save(world_name: str, save_id: str):
    world_path = require_world(world_name)
    saves = read_saves_index(world_path)
    remaining = [s for s in saves if s["save_id"] != save_id]
    if len(remaining) == len(saves):
        raise HTTPException(status_code=404, detail=f"Could not find save '{save_id}'")
    save_dir = os.path.join(world_path, "saves", save_id)
    if os.path.isdir(save_dir):
        shutil.rmtree(save_dir)
    write_saves_index(world_path, remaining)
    return {"message": "Deleted save point", "save_id": save_id}


@router.post("/worlds/{world_name}/saves/{save_id}/branch")
@locked_world
def branch_from_save(world_name: str, save_id: str, req: BranchRequest):
    world_path = require_world(world_name)
    saves = read_saves_index(world_path)
    if not any(s["save_id"] == save_id for s in saves):
        raise HTTPException(status_code=404, detail=f"Could not find save '{save_id}'")

    src_dir = os.path.join(world_path, "saves", save_id)
    _ensure_snapshot_compatible(src_dir)

    new_name = req.new_world_name.strip()
    _validate_world_name(new_name)
    if not new_name:
        raise HTTPException(status_code=400, detail="Must enter a new world name to branch")
    new_world_path = world_path_of(new_name)
    if os.path.exists(new_world_path):
        raise HTTPException(status_code=400, detail=f"World '{new_name}' already exists")

    os.makedirs(new_world_path)
    for filename, template in CORE_STATE_FILES.items():
        src = os.path.join(src_dir, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(new_world_path, filename))
        else:
            write_world_file(new_world_path, filename, template)

    cfg = read_world_file(new_world_path, "world_config.json")
    cfg["branched_from"] = {"world": world_name, "save_id": save_id, "at": __import__('time').strftime("%Y-%m-%d %H:%M:%S UTC", __import__('time').gmtime())}
    # A branch starts its own revision timeline; the source receipts are not copied.
    cfg["revision"] = 0
    cfg["pre_turn_snapshot"] = None
    write_world_file(new_world_path, "world_config.json", cfg)
    clear_receipts(new_world_path)
    clear_turn_snapshots(new_world_path)

    return {
        "message": f"Branched new world '{new_name}' from save '{save_id}'",
        "new_world_name": new_name,
        "source_save_id": save_id
    }


@router.post("/worlds/{world_name}/creator-assistant")
def creator_assistant(world_name: str, req: CreatorAssistantRequest):
    world_path = require_world(world_name)
    if not has_real_api_key(world_name):
        return {
            "explanation": "Default Assistant Suggestions:",
            "suggestions": [
                "Consider adding a new secret card unlocked at a later checkpoint.",
                "Define a rival character with opposing traits or exclusive titles.",
                "Add an alternate outcome to the current checkpoint based on character affinity."
            ]
        }

    cfg = read_world_file(world_path, "world_config.json")
    checkpoints_data = read_world_file(world_path, "canon_timeline.json")
    card_reg = read_world_file(world_path, "card_registry.json")
    char_state = read_world_file(world_path, "character_state.json")

    payload = json.dumps({
        "world_config": cfg,
        "checkpoints": checkpoints_data.get("checkpoints", []),
        "cards": card_reg.get("cards", []),
        "characters": char_state.get("characters", {}),
        "creator_query": req.query,
        "section": req.section or "general"
    }, ensure_ascii=False)

    try:
        raw = call_llm(CREATOR_ASSISTANT_PROMPT, payload, world_name=world_name)
        res = parse_llm_json(raw, expected_type=dict)
        explanation = res.get("explanation", "Here are design suggestions for your world.")
        suggestions = res.get("suggestions", [])
        if not suggestions or not isinstance(suggestions, list):
            suggestions = [
                "Consider adding a new secret card unlocked at a later checkpoint.",
                "Define a rival character with opposing traits or exclusive titles.",
                "Add an alternate outcome to the current checkpoint based on character affinity."
            ]
        return {"explanation": explanation, "suggestions": suggestions}
    except Exception:
        return {
            "explanation": "Default Assistant Suggestions:",
            "suggestions": [
                "Consider adding a new secret card unlocked at a later checkpoint.",
                "Define a rival character with opposing traits or exclusive titles.",
                "Add an alternate outcome to the current checkpoint based on character affinity."
            ]
        }


@router.get("/worlds/{world_name}/style-card")
def get_world_style_card_endpoint(world_name: str):
    require_world(world_name)
    return get_world_style_card(world_name)


@router.put("/worlds/{world_name}/style-card")
def update_world_style_card_endpoint(world_name: str, req: StyleCardModel):
    require_world(world_name)
    data = req.model_dump()
    write_world_style_card(world_name, data)
    bump_world_revision(world_path_of(world_name))
    return {"message": "style_card updated", "style_card": data}


@router.get("/worlds/{world_name}/traits")
def get_traits(world_name: str):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    traits = world_config.get("trait_definitions", {})
    return list(traits.values())


@router.post("/worlds/{world_name}/traits")
def create_trait(world_name: str, trait: TraitDefinition):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    if "trait_definitions" not in world_config:
        world_config["trait_definitions"] = {}
    if trait.name in world_config["trait_definitions"]:
        raise HTTPException(status_code=400, detail="Trait already exists")
    world_config["trait_definitions"][trait.name] = trait.model_dump()
    write_world_file(world_path, "world_config.json", world_config)
    bump_world_revision(world_path)
    return {"status": "created", "trait": trait.name}


@router.put("/worlds/{world_name}/traits/{trait_name}")
def update_trait(world_name: str, trait_name: str, trait: TraitDefinition):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    traits = world_config.get("trait_definitions", {})
    if trait_name not in traits:
        raise HTTPException(status_code=404, detail="Trait not found")
    traits[trait_name] = trait.model_dump()
    world_config["trait_definitions"] = traits
    write_world_file(world_path, "world_config.json", world_config)
    bump_world_revision(world_path)
    return {"status": "updated", "trait": trait_name}


@router.delete("/worlds/{world_name}/traits/{trait_name}")
def delete_trait(world_name: str, trait_name: str):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    traits = world_config.get("trait_definitions", {})
    if trait_name not in traits:
        raise HTTPException(status_code=404, detail="Trait not found")
    del traits[trait_name]
    world_config["trait_definitions"] = traits
    write_world_file(world_path, "world_config.json", world_config)
    bump_world_revision(world_path)
    return {"status": "deleted", "trait": trait_name}


@router.get("/worlds/{world_name}/graph")
def get_checkpoint_graph(world_name: str):
    world_path = require_world(world_name)
    canon = read_world_file(world_path, "canon_timeline.json")

    graph = {}
    checkpoints = canon.get("checkpoints", [])

    for i, cp in enumerate(checkpoints):
        cp_id = cp["checkpoint_id"]
        next_ids = []

        alt_ids = []
        for outcome in cp.get("alternate_outcomes", []):
            if outcome.get("next_checkpoint_id"):
                alt_ids.append(outcome.get("next_checkpoint_id"))

        default_next = cp.get("default_next_checkpoint_id")
        if not default_next and i + 1 < len(checkpoints):
            default_next = checkpoints[i + 1]["checkpoint_id"]

        graph[cp_id] = {
            "default": default_next,
            "alternates": alt_ids
        }

    return {"graph": graph}


@router.get("/worlds/{world_name}/foreshadowings")
def get_foreshadowings(world_name: str):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    tracker = world_config.get("foreshadowing_tracker", world_config.get("foreshadowings", []))
    return {
        "foreshadowing_tracker": tracker,
        "foreshadowings": tracker
    }


@router.put("/worlds/{world_name}/foreshadowings")
def update_foreshadowings(world_name: str, req: ForeshadowingsUpdateReq):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    items = req.foreshadowing_tracker if req.foreshadowing_tracker is not None else req.foreshadowings
    if items is None:
        raise HTTPException(status_code=400, detail="Missing foreshadowing_tracker or foreshadowings field")
    world_config["foreshadowing_tracker"] = items
    write_world_file(world_path, "world_config.json", world_config)
    bump_world_revision(world_path)
    return {
        "message": "foreshadowings updated",
        "foreshadowing_tracker": items,
        "foreshadowings": items
    }


_EDITABLE_CHARACTER_FIELDS = frozenset({
    "alive", "location", "inventory", "knowledge_flags", "relationships",
    "karma", "age", "appearance", "personality", "backstory",
    "abilities_and_limits", "speech_style", "secrets", "capabilities",
})


@router.post("/worlds/{world_name}/creator/edit")
@locked_world
def creator_edit(world_name: str, req: dict):
    """Creator edit with preview, revision check and a revision log (W07).

    Player narration never reaches this endpoint, so a player saying "the enemy
    dies instantly" cannot rewrite canon. A creator edit previews validation
    first, refuses a stale revision, and commits the affected files as one unit.
    """
    from app.world_events import (
        load_world_events, EVENT_RESOLUTIONS, resolve_event_by_creator,
    )

    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    current_revision = check_expected_revision(world_config, req.get("expected_revision"))

    changes = req.get("changes", [])
    if not isinstance(changes, list) or not changes:
        raise HTTPException(status_code=400, detail="changes must be a non-empty list")
    preview = bool(req.get("preview"))
    reason = str(req.get("reason", ""))

    character_state = read_world_file(world_path, "character_state.json")
    canon = read_world_canon(world_path)
    world_events = load_world_events(world_path)
    try:
        location_map = read_world_file(world_path, "location_map.json")
    except FileNotFoundError:
        location_map = {"locations": []}
    characters = character_state.setdefault("characters", {})

    applied = []
    errors = []
    for change in changes:
        if not isinstance(change, dict):
            errors.append("change must be an object")
            continue
        kind = change.get("kind")
        if kind == "character":
            char_id = change.get("character_id")
            field = change.get("field")
            value = change.get("value")
            character = characters.get(char_id)
            if not isinstance(character, dict):
                errors.append(f"unknown character '{char_id}'")
                continue
            if field not in _EDITABLE_CHARACTER_FIELDS:
                errors.append(f"field '{field}' is not editable")
                continue
            if field == "alive" and not isinstance(value, bool):
                errors.append("alive must be a boolean")
                continue
            if field in ("inventory", "knowledge_flags", "capabilities") and not isinstance(value, list):
                errors.append(f"{field} must be a list")
                continue
            if field == "relationships" and not isinstance(value, dict):
                errors.append("relationships must be an object")
                continue
            character[field] = value
            applied.append({"kind": "character", "character_id": char_id, "field": field})
        elif kind == "inventory_item":
            from app.story.inventory import add_inventory_item, remove_inventory_item
            char_id = change.get("character_id")
            operation = change.get("operation")
            item = change.get("item")
            character = characters.get(char_id)
            if not isinstance(character, dict):
                errors.append(f"unknown character '{char_id}'")
                continue
            if operation not in ("add", "remove"):
                errors.append("inventory operation must be add or remove")
                continue
            if operation == "add":
                add_inventory_item(character.setdefault("inventory", []), item)
            else:
                remove_inventory_item(character.setdefault("inventory", []), item)
            applied.append({"kind": "inventory_item", "character_id": char_id, "operation": operation})
        elif kind == "location":
            location_id = change.get("location_id")
            field = change.get("field")
            value = change.get("value")
            location = next((loc for loc in location_map.get("locations", [])
                             if isinstance(loc, dict) and loc.get("id") == location_id), None)
            if location is None:
                errors.append(f"unknown location '{location_id}'")
                continue
            if field not in {"name", "description", "connected_to", "tags", "discovery_status", "terrain", "elevation", "layer"}:
                errors.append(f"location field '{field}' is not editable")
                continue
            if field in ("connected_to", "tags") and not isinstance(value, list):
                errors.append(f"{field} must be a list")
                continue
            if field == "discovery_status" and value not in {"unknown", "rumored", "discovered", "visited", "creator_only"}:
                errors.append("invalid discovery_status")
                continue
            if field == "terrain" and (not isinstance(value, str) or value not in TERRAINS):
                errors.append("invalid terrain")
                continue
            if field == "layer" and (not isinstance(value, str) or value not in LAYERS):
                errors.append("invalid layer")
                continue
            if field == "elevation" and value is not None and (type(value) is not int or not -2 <= value <= 2):
                errors.append("elevation must be -2 to 2 or null")
                continue
            location[field] = value
            applied.append({"kind": "location", "location_id": location_id, "field": field})
        elif kind == "active_journey":
            value = change.get("value")
            if value is not None and not isinstance(value, dict):
                errors.append("active journey must be an object or null")
                continue
            world_config["active_journey"] = value
            applied.append({"kind": "active_journey"})
        elif kind == "action_rules":
            value = change.get("value")
            if not isinstance(value, list) or any(not isinstance(rule, dict) for rule in value):
                errors.append("action_rules must be a list of objects")
                continue
            world_config["action_rules"] = value
            applied.append({"kind": "action_rules", "count": len(value)})
        elif kind == "fact_override":
            fact_id = change.get("fact_id")
            statement = change.get("statement")
            fact = next((f for f in canon.get("facts", [])
                         if isinstance(f, dict) and f.get("fact_id") == fact_id), None)
            if fact is None:
                errors.append(f"unknown fact '{fact_id}'")
                continue
            if not isinstance(statement, str) or not statement.strip():
                errors.append("statement must be a non-empty string")
                continue
            fact["statement"] = statement
            applied.append({"kind": "fact_override", "fact_id": fact_id})
        elif kind == "event_resolution":
            event_id = change.get("event_id")
            status = change.get("status")
            event = next((e for e in world_events.get("events", [])
                          if isinstance(e, dict) and e.get("event_id") == event_id), None)
            if event is None:
                errors.append(f"unknown event '{event_id}'")
                continue
            if status != "pending" and status not in EVENT_RESOLUTIONS:
                errors.append(f"invalid event status '{status}'")
                continue
            try:
                result = resolve_event_by_creator(
                    event, status, change.get("outcome_id"),
                    world_config.get("story_clock", {}).get("tick", 0),
                    world_config, canon, characters, location_map,
                )
            except ValueError as exc:
                errors.append(str(exc))
                continue
            applied.append({
                "kind": "event_resolution", "event_id": event_id,
                "status": status, "outcome_id": result.get("outcome_id"),
            })
        else:
            errors.append(f"unknown change kind '{kind}'")

    if any(item.get("kind") == "location" for item in applied):
        from app.services.validators import validate_location_map
        errors.extend(validate_location_map(location_map))

    validation = {"ok": not errors, "errors": errors}
    if preview:
        return {
            "preview": True, "ok": not errors, "applied": applied,
            "validation": validation, "current_revision": current_revision,
        }
    if errors:
        raise HTTPException(status_code=400, detail=validation)

    revision_no = bump_revision(world_config)
    revisions = world_config.setdefault("creator_revisions", [])
    if not isinstance(revisions, list):
        world_config["creator_revisions"] = revisions = []
    revisions.append({
        "revision": revision_no,
        "reason": reason,
        "changes": changes,
        "base_revision": current_revision,
        "at": __import__('time').strftime("%Y-%m-%d %H:%M:%S UTC", __import__('time').gmtime()),
    })
    commit_world_files(world_path, {
        "world_config.json": world_config,
        "character_state.json": character_state,
        "world_canon_store.json": canon,
        "world_events.json": world_events,
        "location_map.json": location_map,
    })
    clear_receipts(world_path)
    if any(item.get("kind") == "event_resolution" for item in applied):
        mark_builder_manual(world_path, "events")
    return {"ok": True, "revision": revision_no, "applied": applied, "validation": validation}
