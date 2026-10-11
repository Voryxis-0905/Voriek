"""Boundaries responsibilities for Story Engine."""
from app.language_detection import detect_story_language
from fastapi import HTTPException
from typing import Optional


def _is_location_in_zone(location: str, allowed_locations: set) -> bool:
    loc_lower = location.lower().strip()
    for allowed in allowed_locations:
        allowed_lower = allowed.lower().strip()
        if loc_lower == allowed_lower:
            return True
        if any(loc_lower.startswith(allowed_lower + separator)
               for separator in (" - ", " — ", " – ", " (")):
            return True
    return False


def check_boundary_violations(state_changes: dict, checkpoint: dict) -> list:
    allowed_locations = set(checkpoint["boundary"]["locations"])
    violations = []
    for char_id, changes in state_changes.get("characters", {}).items():
        loc = changes.get("location")
        if loc and not _is_location_in_zone(loc, allowed_locations):
            violations.append({"character_id": char_id, "attempted_location": loc})
    return violations


def build_boundary_correction_note(violations: list, checkpoint: dict) -> str:
    lines = [
        f"- Character '{v['character_id']}' is moved to the location '{v['attempted_location']}', "
        "but this location is NOT within the allowed_locations of the current checkpoint."
        for v in violations
    ]
    allowed = checkpoint["boundary"]["locations"]
    return (
        "IMPORTANT NOTE - BOUNDARY VIOLATION: In the previous generation for this exact "
        "user_input this, you took the story outside the allowed scope:\n"
        + "\n".join(lines)
        + f"\nCurrent allowed_locations ONLY include: {allowed}. "
        "Note: sub-locations within a zone are valid (eg. 'Valdris Estate - Kitchen' is allowed if 'Valdris Estate' is in the list). "
        "Please completely REWRITE the chapter_text and state_changes for the same user_input. "
        "Keep characters within the allowed scope using a natural in-universe reason: "
        "the character chooses to stay because they are not done here yet, they are waiting for someone or something, "
        "they need to prepare before moving on, or a meaningful obstacle arises organically from the scene itself. "
        "Avoid generic, forced barriers like 'guards block the path' or 'a wall suddenly appears' — instead, "
        "use motivations, unfinished business, or story-appropriate complications that respect the world's premise. "
        "DO NOT move characters outside allowed_locations, and DO NOT write it like a dry system error message."
    )


BOUNDARY_HARD_REJECT_TEMPLATES = {
    "en": {
        "header": "The AI storyteller has 2 consecutive times moved the character outside the allowed scope of the current checkpoint (only includes: {allowed}):\n",
        "line": "- '{char_id}' is moved to '{attempted_location}'",
        "footer": "\nThis turn has been cancelled to avoid inconsistency between the content and character states. The action you just entered is NOT lost — please try again with a different action within the current scope, or rephrase it differently."
    },
    "vi": {
        "header": "Người kể chuyện AI đã 2 lần liên tiếp di chuyển nhân vật ra ngoài phạm vi cho phép của checkpoint hiện tại (chỉ bao gồm: {allowed}):\n",
        "line": "- Nhân vật '{char_id}' bị di chuyển tới '{attempted_location}'",
        "footer": "\nLượt này đã bị hủy để tránh bất đồng bộ giữa nội dung và trạng thái nhân vật. Hành động bạn vừa nhập KHÔNG bị mất — vui lòng thử lại với hành động khác trong phạm vi cho phép, hoặc diễn đạt theo cách khác."
    }
}


def raise_boundary_hard_reject(
    violations: list,
    checkpoint: dict,
    lang: Optional[str] = None,
    user_input: str = "",
    recent_text: str = "",
    world_config: Optional[dict] = None
) -> None:
    if not lang:
        lang = detect_story_language(user_input=user_input, recent_text=recent_text, world_config=world_config)

    tmpl = BOUNDARY_HARD_REJECT_TEMPLATES.get(lang, BOUNDARY_HARD_REJECT_TEMPLATES["en"])
    movement = [v for v in violations if isinstance(v, dict)
                and v.get("kind") == "unsupported_protagonist_movement"]
    if movement:
        details = "; ".join(
            f"{v.get('origin', '')!r} -> {v.get('proposed', '')!r}: {v['reason']}"
            for v in movement
        )
        raise HTTPException(status_code=409, detail=f"Player movement blocked: {details}")
    if violations and all(isinstance(v, dict) and v.get("reason") for v in violations):
        details = "; ".join(
            f"{v.get('attempted_location', '?')}: {v['reason']} "
            f"requires {v.get('required', '?')} (current {v.get('current', '?')})"
            for v in violations
        )
        raise HTTPException(status_code=409, detail=f"Map access blocked: {details}")
    allowed = checkpoint.get("boundary", {}).get("locations", []) if isinstance(checkpoint, dict) and "boundary" in checkpoint else []
    lines = [
        tmpl["line"].format(
            char_id=v.get("character_id", ""),
            attempted_location=v.get("attempted_location", "")
        )
        for v in violations if isinstance(v, dict)
    ]
    detail_msg = tmpl["header"].format(allowed=allowed) + "\n".join(lines) + tmpl["footer"]

    raise HTTPException(status_code=409, detail=detail_msg)
