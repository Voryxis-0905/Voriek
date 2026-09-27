from fastapi import APIRouter, HTTPException
import logging
import os
import json
import re
import time
import shutil
from html import escape as html_escape

from app.storage import WORLDS_DIR, WorldFileUnreadable, read_world_runtime_override, write_world_runtime_override, require_world, require_world_raw, world_path_of, read_world_file, write_world_file, read_saves_index, write_world_style_card, _validate_world_name, redact_runtime_override_for_export, bump_world_revision, mark_builder_manual
from app.engine import TEMPLATES, _validate_imported_package, detect_story_language
from app.models import WorldConfigUpdate, WorldCreationRequest, ImportWorldRequest, ForkRequest
from app.security import is_loopback_url
from app.story.entities import validate_imported_optional_core_files
from app.world.schema import CORE_STATE_FILES, read_schema_version, SchemaVersionError
from app.world.templates import SCHEMA_VERSION

try:
    from skill_limiter import check_skill_limiter
except ImportError:
    from backend.skill_limiter import check_skill_limiter

router = APIRouter()

logger = logging.getLogger(__name__)


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/worlds")
def list_worlds():
    if not os.path.isdir(WORLDS_DIR):
        return {"worlds": []}
    result = []
    for name in sorted(os.listdir(WORLDS_DIR)):
        world_path = world_path_of(name)
        if not os.path.isdir(world_path):
            continue
        try:
            cfg = read_world_file(world_path, "world_config.json")
            status = cfg.get("creation_status", "complete")
        except (FileNotFoundError, json.JSONDecodeError):
            status = "complete"
        result.append({"name": name, "status": status})
    return {"worlds": result}


@router.put("/worlds/{world_name}/world_config")
def update_world_config(world_name: str, req: WorldConfigUpdate):
    world_path = require_world(world_name)
    world_config = read_world_file(world_path, "world_config.json")
    updates = req.dict(exclude_unset=True)
    if updates.get("lore_rag_max_tokens") is not None and updates["lore_rag_max_tokens"] < 0:
        raise HTTPException(
            status_code=400,
            detail="lore_rag_max_tokens must be >= 0 (0 = unlimited, empty = system default)."
        )
    if updates.get("opening_mode") is not None and updates["opening_mode"] not in ("ai_generate", "user_defined"):
        raise HTTPException(
            status_code=400,
            detail=f"opening_mode '{updates['opening_mode']}' not h\u1ee3p l\u1ec7 "
                   f"(only nh\u1eadn 'ai_generate' either 'user_defined')."
        )
    if updates.get("protagonist_id"):
        char_state = read_world_file(world_path, "character_state.json")
        if updates["protagonist_id"] not in char_state.get("characters", {}):
            raise HTTPException(
                status_code=400,
                detail=f"protagonist_id '{updates['protagonist_id']}' not t\u1ed3n t\u1ea1i in character_state. "
                       f"T\u1ea1o nh\u00e2n v\u1eadt tr\u01b0\u1edbc in tab Characters then quay l\u1ea1i \u0111\u00e2y."
            )
    world_config.update(updates)
    write_world_file(world_path, "world_config.json", world_config)
    bump_world_revision(world_path)
    mark_builder_manual(world_path, "skeleton")
    return {"message": "world_config updated", "world_config": world_config}


@router.get("/worlds/{world_name}")
def get_world(world_name: str):
    world_path = require_world(world_name)
    result = {}
    for filename in CORE_STATE_FILES.keys():
        file_path = os.path.join(world_path, filename)
        key = filename.replace(".json", "")
        if os.path.exists(file_path):
            result[key] = read_world_file(world_path, filename)
    result["saves"] = list(reversed(read_saves_index(world_path)))
    return result


@router.delete("/worlds/{world_name}")
def delete_world(world_name: str):
    world_path = require_world(world_name)
    shutil.rmtree(world_path)
    return {"message": "World deleted", "world_name": world_name}


def sanitize_imported_runtime_override(override: dict) -> dict:
    if not isinstance(override, dict):
        return {}
    sanitized = {
        "creator_mode_enabled": bool(override.get("creator_mode_enabled", False)),
        "editor_enabled": bool(override.get("editor_enabled", False)),
        "enable_extractor_cross_check": bool(override.get("enable_extractor_cross_check", False)),
        "openrouter_model": str(override.get("openrouter_model") or ""),
        "openrouter_api_key": "",
    }
    if "role_assignments" in override and isinstance(override["role_assignments"], dict):
        sanitized["role_assignments"] = dict(override["role_assignments"])

    chain = []
    for entry in override.get("fallback_chain", []) or []:
        if not isinstance(entry, dict):
            continue
        provider = str(entry.get("provider") or "").lower().strip()
        base_url = str(entry.get("base_url") or "").strip()
        is_loopback = is_loopback_url(base_url)
        if base_url and not is_loopback:
            continue
        chain.append({
            "provider": provider,
            "model": str(entry.get("model") or ""),
            "api_key": "",
            "base_url": base_url if is_loopback else "",
        })
    if chain:
        sanitized["fallback_chain"] = chain
    return sanitized


@router.post("/worlds/import")
def import_world(req: ImportWorldRequest):
    pkg = req.package_data
    if not isinstance(pkg, dict):
        raise HTTPException(status_code=400, detail="Invalid package data")

    declared_version = pkg.get("schema_version")
    if declared_version is None and isinstance(pkg.get("world_config"), dict):
        declared_version = pkg["world_config"].get("schema_version")
    if isinstance(declared_version, int) and not isinstance(declared_version, bool) and declared_version > SCHEMA_VERSION:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Import package schema_version {declared_version} is newer than this app supports "
                f"(max {SCHEMA_VERSION}). Update Voriek before importing."
            ),
        )

    full_cfg, card_reg, timeline, char_state = _validate_imported_package(pkg, TEMPLATES)
    full_cfg["schema_version"] = SCHEMA_VERSION

    # Validate everything before the first write, so a rejected package never
    # leaves a half-created world directory behind.
    optional_core_files = validate_imported_optional_core_files(pkg, TEMPLATES)

    raw_name = req.world_name or full_cfg.get("display_name") or "imported_world"
    target_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', raw_name).strip('_') or "imported_world"

    world_path = world_path_of(target_name)
    if os.path.exists(world_path):
        target_name = f"{target_name}_{int(time.time())}"
        world_path = world_path_of(target_name)

    os.makedirs(world_path)
    write_world_file(world_path, "world_config.json", full_cfg)
    write_world_file(world_path, "card_registry.json", card_reg)
    write_world_file(world_path, "canon_timeline.json", timeline)
    write_world_file(world_path, "character_state.json", char_state)

    chapters_data = pkg.get("chapters")
    if not chapters_data or not isinstance(chapters_data, dict):
        chapters_data = {"chapters": [], "running_summary": "", "memorable_beats": []}
    else:
        chapters_data = dict(chapters_data)
        if "chapters" not in chapters_data or not isinstance(chapters_data["chapters"], list):
            chapters_data["chapters"] = []
        if "memorable_beats" not in chapters_data:
            chapters_data["memorable_beats"] = []
    write_world_file(world_path, "chapters.json", chapters_data)

    for filename, content in optional_core_files.items():
        write_world_file(world_path, filename, content)

    if "runtime_override" in pkg and isinstance(pkg["runtime_override"], dict):
        write_world_runtime_override(target_name, sanitize_imported_runtime_override(pkg["runtime_override"]))

    if "style_card" in pkg and isinstance(pkg["style_card"], dict):
        write_world_style_card(target_name, pkg["style_card"])

    return {"message": "World imported successfully", "world_name": target_name}


@router.post("/worlds/{world_name}")
def create_world(world_name: str, req: WorldCreationRequest = None):
    _validate_world_name(world_name)
    world_path = world_path_of(world_name)
    if os.path.exists(world_path):
        raise HTTPException(status_code=400, detail="World already exists")
    os.makedirs(world_path)
    for filename, template in TEMPLATES.items():
        write_world_file(world_path, filename, template)

    cfg = read_world_file(world_path, "world_config.json")
    if req:
        if req.interaction_mode:
            cfg["interaction_mode"] = req.interaction_mode
        if req.prompt or req.scope_type:
            cfg["creation_status"] = "skeleton"
            cfg["scope_selector"] = req.scope_type
            cfg["narrative_scope_note"] = req.prompt
            if req.prompt:
                cfg["language"] = detect_story_language(user_input=req.prompt)

    write_world_file(world_path, "world_config.json", cfg)

    return {"message": "World created", "world_name": world_name}


def _read_optional_export_file(world_path: str, filename: str, warnings: list):
    """Read an optional world file for export, recording why it was skipped.

    An export doubles as a recovery copy, so a file that exists but cannot be
    read must not vanish silently: the recipient has to be able to tell that the
    copy is incomplete. Returns None when the file is absent or unreadable.
    """
    if not os.path.isfile(os.path.join(world_path, filename)):
        return None
    try:
        return read_world_file(world_path, filename)
    except WorldFileUnreadable as error:
        logger.warning("Export of %s skipped %s: %s", world_path, filename, error.reason)
        warnings.append({"file": filename, "reason": error.reason})
        return None


@router.get("/worlds/{world_name}/export")
def export_world(world_name: str):
    # Raw read: export must work even for a world we cannot migrate, so it can be
    # used as a recovery copy. It never writes or migrates in place.
    world_path = require_world_raw(world_name)
    try:
        version = read_schema_version(world_path)
    except SchemaVersionError as error:
        raise HTTPException(status_code=400, detail=str(error))
    pkg = {
        "export_version": "1.0",
        "schema_version": version,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "world_config": read_world_file(world_path, "world_config.json"),
        "card_registry": read_world_file(world_path, "card_registry.json"),
        "canon_timeline": read_world_file(world_path, "canon_timeline.json"),
        "character_state": read_world_file(world_path, "character_state.json"),
        "chapters": read_world_file(world_path, "chapters.json"),
    }

    export_warnings = []
    for filename in ("world_events.json", "location_map.json", "style_card.json"):
        content = _read_optional_export_file(world_path, filename, export_warnings)
        if content is not None:
            pkg[filename[: -len(".json")]] = content
    if export_warnings:
        pkg["export_warnings"] = export_warnings

    override = read_world_runtime_override(world_name)
    if override:
        pkg["runtime_override"] = redact_runtime_override_for_export(override)

    return pkg


@router.post("/worlds/{world_name}/fork")
def fork_timeline_at_checkpoint(world_name: str, req: ForkRequest):
    _validate_world_name(req.new_world_name)
    src_path = require_world(world_name)
    dest_path = world_path_of(req.new_world_name)

    if os.path.exists(dest_path):
        raise HTTPException(status_code=400, detail="New world name already exists.")

    shutil.copytree(
        src_path,
        dest_path,
        ignore=shutil.ignore_patterns("runtime_override.json", "_commit_journal.json*", "*.tmp")
    )

    chapters_data = read_world_file(dest_path, "chapters.json")
    chapters = chapters_data.get("chapters", [])

    cutoff_idx = len(chapters)
    for i, ch in enumerate(chapters):
        if ch.get("checkpoint_id") == req.checkpoint_id:
            cutoff_idx = i
            break

    chapters_data["chapters"] = chapters[:cutoff_idx]
    write_world_file(dest_path, "chapters.json", chapters_data)

    config = read_world_file(dest_path, "world_config.json")
    config["current_checkpoint_id"] = req.checkpoint_id
    if "completed_checkpoints" in config and req.checkpoint_id in config["completed_checkpoints"]:
        idx = config["completed_checkpoints"].index(req.checkpoint_id)
        config["completed_checkpoints"] = config["completed_checkpoints"][:idx]
    write_world_file(dest_path, "world_config.json", config)

    return {"status": "ok", "new_world_name": req.new_world_name}


@router.get("/worlds/{world_name}/export-story")
def export_story_html(world_name: str):
    world_path = require_world_raw(world_name)
    chapters_data = read_world_file(world_path, "chapters.json")
    chapters = chapters_data.get("chapters", [])
    config = read_world_file(world_path, "world_config.json")

    display_name = html_escape(str(config.get("display_name", world_name)))
    html_content = f"<html><head><meta charset='utf-8'><title>{display_name}</title>"
    html_content += "<style>body{font-family:serif; max-width:800px; margin:40px auto; line-height:1.6; color:#333; padding: 20px;} "
    html_content += ".chapter-title {text-align:center; margin-top:2em;} .user-input {font-style:italic; color:#666; margin-bottom:1em;}</style></head><body>"
    html_content += f"<h1 style='text-align:center;'>{display_name}</h1>"

    for ch in chapters:
        if ch.get("chapter_title"):
            title_text = html_escape(str(ch["chapter_title"]))
            html_content += f"<h2 class='chapter-title'>{title_text}</h2>"
        if ch.get("user_input"):
            user_input = html_escape(str(ch["user_input"]))
            html_content += f"<div class='user-input'>&gt; {user_input}</div>"
        text = html_escape(str(ch.get("chapter_text", ""))).replace("\n", "<br>")
        html_content += f"<p>{text}</p>"
        html_content += "<hr style='border:0; border-top:1px solid #eee; margin:2em 0;'>"

    html_content += "</body></html>"
    from fastapi.responses import HTMLResponse
    return HTMLResponse(content=html_content)
