import os
import json
import logging
import re
import time
import uuid
import shutil
from fastapi import HTTPException

logger = logging.getLogger(__name__)

try:
    from app.commit_sanitizer import atomic_write, cross_check, CommitValidationError
except ImportError:
    atomic_write = None
    cross_check = None
    CommitValidationError = None

from app.world.templates import STYLE_CARD_TEMPLATE
from app.world.schema import CORE_STATE_FILES


BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.abspath(os.environ.get("STORY_ENGINE_DATA_DIR") or os.path.join(BASE_DIR, "data"))
WORLDS_DIR = os.path.join(DATA_DIR, "worlds")
os.makedirs(WORLDS_DIR, exist_ok=True)

RUNTIME_CONFIG_PATH = os.path.join(DATA_DIR, "runtime_config.json")
DEFAULT_OPENROUTER_MODEL = "deepseek/deepseek-chat"

_VALID_ROLES = frozenset({"planner", "actor", "writer", "extractor", "editor", "checker", "summarizer"})


class WorldFileUnreadable(Exception):
    """A world state file is present but cannot be used as world state.

    Distinct from ``FileNotFoundError`` (the file is simply absent): this one
    means the world's own data is corrupt, so the request cannot be served until
    it is restored from a save or a backup.
    """

    def __init__(self, filename: str, reason: str):
        super().__init__(f"{filename} could not be read: {reason}")
        self.filename = filename
        self.reason = reason


def read_runtime_config() -> dict:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and getattr(main_mod, "read_runtime_config", None) not in (None, read_runtime_config):
        return main_mod.read_runtime_config()
    if not os.path.isfile(RUNTIME_CONFIG_PATH):
        return {"openrouter_api_key": "", "openrouter_model": ""}
    try:
        with open(RUNTIME_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"openrouter_api_key": "", "openrouter_model": ""}
    if not isinstance(data, dict):
        return {"openrouter_api_key": "", "openrouter_model": ""}
    api_key = data.get("api_key") or data.get("openrouter_api_key", "") or ""
    model_name = data.get("model_name") or data.get("openrouter_model", "") or ""
    return {
        "openrouter_api_key": api_key,
        "openrouter_model": model_name,
        "api_key": api_key,
        "model_name": model_name,
        "llm_provider": data.get("llm_provider", "openrouter"),
        "base_url": data.get("base_url", "") or "",
        "temperature": data.get("temperature", 0.7),
        "fallback_chain": data.get("fallback_chain", []),
        "creator_mode_enabled": data.get("creator_mode_enabled", False),
        "role_assignments": data.get("role_assignments", {"planner": None, "writer": None, "extractor": None, "editor": None, "checker": None, "summarizer": None}),
        "editor_enabled": data.get("editor_enabled", False),
        "enable_extractor_cross_check": data.get("enable_extractor_cross_check", False)
    }


def write_runtime_config(cfg: dict) -> None:
    os.makedirs(os.path.dirname(RUNTIME_CONFIG_PATH), exist_ok=True)
    with open(RUNTIME_CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _world_runtime_override_path(world_name: str) -> str:
    return os.path.join(WORLDS_DIR, world_name, "runtime_override.json")


def read_world_runtime_override(world_name: str) -> dict:
    if not world_name:
        return {"openrouter_api_key": "", "openrouter_model": ""}
    path = _world_runtime_override_path(world_name)
    if not os.path.isfile(path):
        return {"openrouter_api_key": "", "openrouter_model": ""}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"openrouter_api_key": "", "openrouter_model": ""}
    if not isinstance(data, dict):
        return {"openrouter_api_key": "", "openrouter_model": ""}
    # An override file written by an older build (or by hand) may carry the key
    # under "api_key" only. Mirror the app-config helper: resolve whichever of
    # the two holds the key so a stored key is never silently unusable, and so
    # clearing one spelling cannot leave the other behind.
    world_key = data.get("api_key") or data.get("openrouter_api_key", "") or ""
    return {
        "openrouter_api_key": world_key,
        "api_key": world_key,
        "openrouter_model": data.get("openrouter_model", "") or "",
        "fallback_chain": data.get("fallback_chain", []),
        "creator_mode_enabled": data.get("creator_mode_enabled", False),
        "role_assignments": data.get("role_assignments", {"planner": None, "writer": None, "extractor": None, "editor": None, "checker": None, "summarizer": None}),
        "editor_enabled": data.get("editor_enabled", False),
        "enable_extractor_cross_check": data.get("enable_extractor_cross_check", False)
    }


def write_world_runtime_override(world_name: str, cfg: dict) -> None:
    path = _world_runtime_override_path(world_name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def get_effective_api_key(world_name: str = None) -> str:
    if world_name:
        world_key = read_world_runtime_override(world_name).get("openrouter_api_key", "")
        if world_key:
            return world_key
    ui_key = read_runtime_config().get("openrouter_api_key", "")
    if ui_key:
        return ui_key
    return os.environ.get("OPENROUTER_API_KEY", "") or ""


def get_effective_model(world_name: str = None) -> str:
    if world_name:
        world_model = read_world_runtime_override(world_name).get("openrouter_model", "")
        if world_model:
            return world_model
    ui_model = read_runtime_config().get("openrouter_model", "")
    if ui_model:
        return ui_model
    return os.environ.get("OPENROUTER_MODEL") or DEFAULT_OPENROUTER_MODEL


def get_effective_fallback_chain(world_name: str = None) -> list:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and getattr(main_mod, "get_effective_fallback_chain", None) not in (None, get_effective_fallback_chain):
        return main_mod.get_effective_fallback_chain(world_name)
    app_cfg = read_runtime_config()
    if world_name:
        world_cfg = read_world_runtime_override(world_name)
        if world_cfg.get("fallback_chain"):
            # A world chain node with no key of its own is not a usable node: an
            # empty api_key would be sent to the provider. Skip such nodes so the
            # world falls through to the app config instead of failing the call.
            usable = [
                node for node in world_cfg["fallback_chain"]
                if isinstance(node, dict) and str(node.get("api_key", "")).strip()
            ]
            if usable:
                return usable
        # read_world_runtime_override normalises both spellings into
        # "openrouter_api_key", so this covers a world key stored either way.
        if world_cfg.get("openrouter_api_key"):
            return [{
                "provider": app_cfg.get("llm_provider", "openrouter"),
                "model": get_effective_model(world_name),
                "api_key": world_cfg["openrouter_api_key"],
                "base_url": app_cfg.get("base_url", ""),
            }]
    if app_cfg.get("fallback_chain"):
        # Same rule for the app chain: a node without a key is not a usable node.
        usable = [
            node for node in app_cfg["fallback_chain"]
            if isinstance(node, dict) and str(node.get("api_key", "")).strip()
        ]
        if usable:
            return usable
    api_key = get_effective_api_key(world_name)
    model = get_effective_model(world_name)
    provider = app_cfg.get("llm_provider", "openrouter")
    base_url = app_cfg.get("base_url", "")
    if api_key:
        return [{"provider": provider, "model": model, "api_key": api_key, "base_url": base_url}]
    return []


def has_real_api_key(world_name: str = None) -> bool:
    return len(get_effective_fallback_chain(world_name)) > 0


def _get_effective_role_assignments(world_name: str = None) -> dict:
    default = {"planner": None, "writer": None, "extractor": None, "editor": None, "checker": None, "summarizer": None}
    if world_name:
        world_cfg = read_world_runtime_override(world_name)
        world_ra = world_cfg.get("role_assignments")
        if isinstance(world_ra, dict) and world_ra:
            merged = dict(default)
            for role in _VALID_ROLES:
                if role in world_ra:
                    merged[role] = world_ra[role]
            return merged
    app_cfg = read_runtime_config()
    app_ra = app_cfg.get("role_assignments")
    if isinstance(app_ra, dict) and app_ra:
        merged = dict(default)
        for role in _VALID_ROLES:
            if role in app_ra:
                merged[role] = app_ra[role]
        return merged
    return default


def _get_effective_editor_enabled(world_name: str = None) -> bool:
    if world_name:
        world_cfg = read_world_runtime_override(world_name)
        if "editor_enabled" in world_cfg and world_cfg["editor_enabled"] is not None:
            return bool(world_cfg.get("editor_enabled", False))
    app_cfg = read_runtime_config()
    return bool(app_cfg.get("editor_enabled", False))


def _get_effective_extractor_cross_check(world_name: str = None) -> bool:
    if world_name:
        world_cfg = read_world_runtime_override(world_name)
        if "enable_extractor_cross_check" in world_cfg and world_cfg["enable_extractor_cross_check"] is not None:
            return bool(world_cfg.get("enable_extractor_cross_check", False))
    app_cfg = read_runtime_config()
    return bool(app_cfg.get("enable_extractor_cross_check", False))


def _default_role_index(role: str, pool_size: int):
    if pool_size == 0:
        return None
    if pool_size == 1:
        return 0
    if pool_size == 2:
        if role == "extractor":
            return 1
        return 0
    if pool_size == 3:
        if role == "extractor":
            return 1
        if role == "editor":
            return 2
        return 0
    order = ["planner", "writer", "extractor", "editor", "checker", "summarizer"]
    if role in order:
        idx = order.index(role)
        return idx if idx < pool_size else 0
    return 0



def get_effective_fallback_chain_for_role(world_name: str, role: str) -> list:
    import sys
    main_mod = sys.modules.get("main")
    if main_mod and getattr(main_mod, "get_effective_fallback_chain_for_role", None) not in (None, get_effective_fallback_chain_for_role):
        return main_mod.get_effective_fallback_chain_for_role(world_name, role)
    pool = get_effective_fallback_chain(world_name)
    if not pool or role not in _VALID_ROLES:
        return pool
    assignments = _get_effective_role_assignments(world_name)
    idx = assignments.get(role)
    if idx is None or not isinstance(idx, int) or idx < 0 or idx >= len(pool):
        idx = _default_role_index(role, len(pool))
    if idx is None or idx == 0:
        return pool
    return pool[idx:] + pool[:idx]


def _mask_api_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return key[:4] + "\u2026" + key[-4:]


def _sanitize_and_preserve_fallback_chain(new_chain: list, existing_chain: list) -> list:
    cleaned = []
    for i, item in enumerate(new_chain):
        if not isinstance(item, dict):
            continue
        provider = str(item.get("provider", "openrouter")).strip().lower()
        model = str(item.get("model", "")).strip()
        key = str(item.get("api_key", "")).strip()
        base_url = str(item.get("base_url", "")).strip()
        if (("..." in key or key == "" or "*" in key or not key) and
                i < len(existing_chain) and isinstance(existing_chain[i], dict)):
            existing_key = existing_chain[i].get("api_key", "")
            if existing_key and not ("..." in existing_key or existing_key == "" or "*" in existing_key):
                key = existing_key
        cleaned.append({
            "provider": provider or "openrouter",
            "model": model,
            "api_key": key,
            "base_url": base_url
        })
    return cleaned


def _sanitize_fallback_chain_for_status(chain: list) -> list:
    status_chain = []
    if not isinstance(chain, list):
        return status_chain
    for item in chain:
        if isinstance(item, dict):
            key = item.get("api_key", "")
            status_chain.append({
                "provider": item.get("provider", "openrouter"),
                "model": item.get("model", ""),
                "api_key_masked": _mask_api_key(key),
                "has_api_key": bool(key),
                "base_url": item.get("base_url", "")
            })
    return status_chain


def redact_runtime_override_for_export(override: dict) -> dict:
    """Return a copy of a world runtime override without any secret material.

    Runtime keys are machine-local secrets and must never travel inside export
    packages or import payloads. Non-secret fields are kept so an exported world
    still carries its model/provider preferences.
    """
    if not isinstance(override, dict):
        return {}
    redacted = {k: v for k, v in override.items() if k not in ("openrouter_api_key", "api_key")}
    redacted["openrouter_api_key"] = ""
    redacted["api_key"] = ""
    if isinstance(override.get("openrouter_model"), str):
        redacted["openrouter_model"] = override["openrouter_model"]
    chain = override.get("fallback_chain")
    if isinstance(chain, list):
        safe_chain = []
        for item in chain:
            if isinstance(item, dict):
                node = dict(item)
                node["api_key"] = ""
                safe_chain.append(node)
        redacted["fallback_chain"] = safe_chain
    return redacted


def _compute_role_assignments_effective(world_name: str = None) -> dict:
    pool = get_effective_fallback_chain(world_name)
    n = len(pool)
    assignments = _get_effective_role_assignments(world_name)
    result = {}
    for role in ("planner", "writer", "extractor", "editor", "checker", "summarizer"):
        idx = assignments.get(role)
        if idx is None or not isinstance(idx, int) or idx < 0 or idx >= n:
            idx = _default_role_index(role, n)
        result[role] = idx
    return result


def build_runtime_config_status(world_name: str = None) -> dict:
    cfg = read_runtime_config()
    ui_key = cfg.get("openrouter_api_key", "")
    ui_model = cfg.get("openrouter_model", "")
    env_key = os.environ.get("OPENROUTER_API_KEY", "") or ""
    env_model = os.environ.get("OPENROUTER_MODEL", "") or ""

    world_cfg = read_world_runtime_override(world_name)
    world_key = world_cfg.get("openrouter_api_key", "")
    world_model = world_cfg.get("openrouter_model", "")

    effective_chain = get_effective_fallback_chain(world_name)

    if world_key or (world_cfg.get("fallback_chain")):
        key_source = "world"
    elif ui_key or (cfg.get("fallback_chain")):
        key_source = "ui"
    elif env_key:
        key_source = "env"
    else:
        key_source = "none"

    if world_model:
        model_source = "world"
    elif ui_model:
        model_source = "ui"
    elif env_model:
        model_source = "env"
    else:
        model_source = "default"

    effective_key = world_key or ui_key or env_key

    return {
        "llm_provider": cfg.get("llm_provider", "openrouter"),
        "model_name": get_effective_model(world_name),
        "base_url": cfg.get("base_url", ""),
        "temperature": cfg.get("temperature", 0.7),
        "has_api_key": len(effective_chain) > 0 or key_source != "none",
        "api_key_source": key_source,
        "api_key_masked": _mask_api_key(effective_key) if key_source != "none" else None,
        "model": get_effective_model(world_name),
        "model_source": model_source,
        "fallback_chain": _sanitize_fallback_chain_for_status(effective_chain),
        "creator_mode_enabled": cfg.get("creator_mode_enabled", False),
        "app_default": {
            "has_api_key": bool(ui_key or env_key or cfg.get("fallback_chain")),
            "api_key_source": "ui" if (ui_key or cfg.get("fallback_chain")) else ("env" if env_key else "none"),
            "api_key_masked": _mask_api_key(ui_key or env_key) if (ui_key or env_key) else None,
            "model": ui_model or env_model or DEFAULT_OPENROUTER_MODEL,
            "model_source": "ui" if ui_model else ("env" if env_model else "default"),
            "fallback_chain": _sanitize_fallback_chain_for_status(cfg.get("fallback_chain", []))
        },
        "world_override": ({
            "world_name": world_name,
            "has_api_key": bool(world_key or read_world_runtime_override(world_name).get("fallback_chain")),
            "api_key_masked": _mask_api_key(world_key) if world_key else None,
            "model": world_model,
            "fallback_chain": _sanitize_fallback_chain_for_status(read_world_runtime_override(world_name).get("fallback_chain", []))
        } if world_name else None),
        "role_assignments_effective": _compute_role_assignments_effective(world_name),
        "editor_enabled": _get_effective_editor_enabled(world_name),
        "enable_extractor_cross_check": _get_effective_extractor_cross_check(world_name)
    }


def world_path_of(world_name: str) -> str:
    return os.path.join(WORLDS_DIR, world_name)


def bump_world_revision(world_path: str) -> int:
    """Increment and persist the world revision after a state-changing write."""
    from app.action_guard import bump_revision
    cfg = read_world_file(world_path, "world_config.json")
    new_revision = bump_revision(cfg)
    write_world_file(world_path, "world_config.json", cfg)
    return new_revision


def mark_builder_manual(world_path: str, step: str) -> None:
    """Remember that a builder step was edited by hand (do not auto-overwrite)."""
    cfg = read_world_file(world_path, "world_config.json")
    builder = cfg.setdefault("builder", {})
    if not isinstance(builder, dict):
        cfg["builder"] = builder = {}
    manual = builder.setdefault("manual_steps", [])
    if not isinstance(manual, list):
        builder["manual_steps"] = manual = []
    if step not in manual:
        manual.append(step)
        write_world_file(world_path, "world_config.json", cfg)


def write_world_file(world_path: str, filename: str, data: dict):
    filepath = os.path.join(world_path, filename)
    if atomic_write is not None:
        try:
            atomic_write(filepath, data)
            return
        except CommitValidationError as e:
            logger.error("Write validation failed for %s/%s: %s", world_path, filename, e)
            raise
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_world_file(world_path: str, filename: str) -> dict:
    """Read a world state file, distinguishing "absent" from "unreadable".

    ``FileNotFoundError`` still propagates, because callers use it to mean the
    file is simply not there yet. Anything else — broken JSON, a directory in
    place of a file, an unexpected top-level type — becomes ``WorldFileUnreadable``
    so the API can answer with an actionable error instead of a bare 500.
    """
    file_path = os.path.join(world_path, filename)
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        raise
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as error:
        logger.error("Unreadable world file %s: %s", file_path, error)
        raise WorldFileUnreadable(filename, str(error)) from error
    if not isinstance(data, dict):
        logger.error("World file %s holds %s, expected an object", file_path, type(data).__name__)
        raise WorldFileUnreadable(filename, f"expected a JSON object, found {type(data).__name__}")
    return data


def _validate_world_name(world_name: str):
    if not re.fullmatch(r"[\w\-]{1,100}", world_name):
        raise HTTPException(status_code=400, detail="World name can only contain alphanumeric characters, underscores, and hyphens.")


def require_world(world_name: str) -> str:
    """Validate and bring a world to the current schema before it is used.

    Every read/write route that interprets world state goes through here, so an
    incompatible (newer) world is rejected before any state is read or written.
    Old worlds are migrated with a backup.
    """
    world_path = require_world_raw(world_name)
    from app.persistence import recover_world
    recover_world(world_path)
    from app.world.schema import ensure_current_schema, SchemaVersionError
    try:
        ensure_current_schema(world_path)
    except SchemaVersionError as error:
        raise HTTPException(status_code=409, detail=str(error))
    return world_path


def require_world_raw(world_name: str) -> str:
    """Name/dir check only, without touching schema. For raw export/recovery."""
    _validate_world_name(world_name)
    world_path = world_path_of(world_name)
    if not os.path.isdir(world_path):
        raise HTTPException(status_code=404, detail="World not found")
    return world_path


def new_save_id() -> str:
    return f"save_{time.strftime('%Y%m%d_%H%M%S', time.gmtime())}_{uuid.uuid4().hex[:6]}"


def now_str() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime())


def read_saves_index(world_path: str) -> list:
    idx_path = os.path.join(world_path, "saves_index.json")
    if not os.path.exists(idx_path):
        return []
    with open(idx_path, "r", encoding="utf-8") as f:
        return json.load(f).get("saves", [])


def write_saves_index(world_path: str, saves: list):
    with open(os.path.join(world_path, "saves_index.json"), "w", encoding="utf-8") as f:
        json.dump({"saves": saves}, f, ensure_ascii=False, indent=2)


def snapshot_world_state(world_path: str, save_id: str, templates: dict = None):
    if templates is None:
        templates = CORE_STATE_FILES
    dest_dir = os.path.join(world_path, "saves", save_id)
    os.makedirs(dest_dir, exist_ok=True)
    for filename in templates.keys():
        src = os.path.join(world_path, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest_dir, filename))


TURN_SNAPSHOT_DIRNAME = "turn_snapshots"


def turn_snapshot_dir(world_path: str) -> str:
    return os.path.join(world_path, TURN_SNAPSHOT_DIRNAME, "latest")


def latest_turn_snapshot_dir(world_path: str):
    """Return the pre-turn snapshot for the most recent turn, if any."""
    path = turn_snapshot_dir(world_path)
    return path if os.path.isdir(path) else None


def create_turn_snapshot(world_path: str) -> str:
    """Copy the current (pre-turn) world state. Keeps only the latest snapshot."""
    dest_dir = turn_snapshot_dir(world_path)
    parent = os.path.dirname(dest_dir)
    if os.path.isdir(parent):
        shutil.rmtree(parent)
    os.makedirs(dest_dir)
    for filename in CORE_STATE_FILES:
        src = os.path.join(world_path, filename)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(dest_dir, filename))
    return dest_dir


def clear_turn_snapshots(world_path: str) -> None:
    parent = os.path.dirname(turn_snapshot_dir(world_path))
    if os.path.isdir(parent):
        shutil.rmtree(parent)



def build_save_entry(world_path: str, save_id: str, label: str, source: str) -> dict:
    snap_dir = os.path.join(world_path, "saves", save_id)
    checkpoint_id = ""
    chapter_count = 0
    turn_count = 0
    try:
        cfg = read_world_file(snap_dir, "world_config.json")
        checkpoint_id = cfg.get("current_checkpoint_id", "")
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    try:
        chapters_data = read_world_file(snap_dir, "chapters.json")
        turns = chapters_data.get("chapters", [])
        turn_count = len(turns)
        chapter_count = turns[-1]["chapter_index"] if turns else 0
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {
        "save_id": save_id,
        "label": label,
        "source": source,
        "created_at": now_str(),
        "checkpoint_id": checkpoint_id,
        "chapter_count": chapter_count,
        "turn_count": turn_count
    }


WORLD_CANON_FILENAME = "world_canon_store.json"
BRANCH_DELTA_FILENAME = "branch_local_delta.json"


def read_world_canon(world_path: str) -> dict:
    path = os.path.join(world_path, WORLD_CANON_FILENAME)
    if not os.path.isfile(path):
        return {"facts": []}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_world_canon(world_path: str, data: dict):
    filepath = os.path.join(world_path, WORLD_CANON_FILENAME)
    if atomic_write is not None:
        atomic_write(filepath, data)
        return
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def read_branch_delta(world_path: str) -> dict:
    path = os.path.join(world_path, BRANCH_DELTA_FILENAME)
    if not os.path.isfile(path):
        return {"overrides": {}}
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def write_branch_delta(world_path: str, data: dict):
    filepath = os.path.join(world_path, BRANCH_DELTA_FILENAME)
    if atomic_write is not None:
        atomic_write(filepath, data)
        return
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_world_style_card(world_name: str) -> dict:
    default_style = dict(STYLE_CARD_TEMPLATE)
    if not world_name:
        return default_style
    world_path = world_path_of(world_name)
    style_card_path = os.path.join(world_path, "style_card.json")
    if os.path.isfile(style_card_path):
        try:
            with open(style_card_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    res = dict(default_style)
                    res.update(data)
                    return res
        except (json.JSONDecodeError, OSError):
            pass
    if os.path.isdir(world_path):
        try:
            world_config = read_world_file(world_path, "world_config.json")
            if "style_card" in world_config and isinstance(world_config["style_card"], dict):
                res = dict(default_style)
                res.update(world_config["style_card"])
                return res
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
    return default_style


def write_world_style_card(world_name: str, style_card_data: dict) -> dict:
    world_path = require_world(world_name)
    style_card_path = os.path.join(world_path, "style_card.json")
    with open(style_card_path, "w", encoding="utf-8") as f:
        json.dump(style_card_data, f, ensure_ascii=False, indent=2)
    return style_card_data
