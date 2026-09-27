"""Who is actually in the scene and can perceive what happens there.

The writer describes the protagonist's scene; only characters at that scene who
are conscious and able to perceive should receive the scene transcript for
perception/psychology. Everyone else must learn about it later through a
told-by or rumor channel (see app/story/knowledge.py).
"""
import re
from typing import Dict

DEFAULT_MAX_OBSERVERS = 6

# Status effects that mean the character is present but cannot perceive/hear.
_IMPAIRED_KEYWORDS = (
    "unconscious", "comatose", "asleep", "sleeping", "fainted", "deaf",
    "blinded", "dead", "paralyzed", "stunned",
)


def _effect_name(effect) -> str:
    if isinstance(effect, dict):
        return str(effect.get("name") or effect.get("effect") or "").lower()
    return str(effect).lower()


def is_observable(character: dict) -> bool:
    if not isinstance(character, dict):
        return False
    if not character.get("alive", True):
        return False
    for effect in character.get("status_effects", []) or []:
        name = _effect_name(effect)
        if any(keyword in name for keyword in _IMPAIRED_KEYWORDS):
            return False
    return True


def location_matches(a: str, b: str) -> bool:
    a = (a or "").strip().lower()
    b = (b or "").strip().lower()
    if not a or not b:
        return False
    return a == b or a.startswith(b + " - ") or b.startswith(a + " - ")


def scene_participants(character_state, scene_location: str, *,
                       protagonist_id: str = None,
                       allowed_ids=None,
                       max_observers: int = DEFAULT_MAX_OBSERVERS) -> Dict[str, dict]:
    """Present, perceiving characters at ``scene_location``, capped and ordered.

    The protagonist is always kept (they are the viewpoint) and ordered first so
    the cap never drops the viewpoint NPCs in favour of distant ones. When
    ``allowed_ids`` is given (the checkpoint boundary), only those ids count.
    """
    characters = character_state.get("characters", character_state) if isinstance(character_state, dict) else {}
    if not isinstance(characters, dict):
        return {}
    allowed = set(allowed_ids) if allowed_ids else None

    present = {}
    for char_id, character in characters.items():
        if not is_observable(character):
            continue
        if protagonist_id and char_id == protagonist_id:
            present[char_id] = character
            continue
        if allowed is not None and char_id not in allowed:
            continue
        if location_matches(character.get("location", ""), scene_location):
            present[char_id] = character

    ordered = sorted(present.items(), key=lambda item: (0 if item[0] == protagonist_id else 1, item[0]))
    if max_observers is not None and max_observers >= 0:
        ordered = ordered[:max_observers]
    return dict(ordered)


def psychology_subjects(participants: Dict[str, dict], protagonist_id: str = None) -> Dict[str, dict]:
    """Participants that get psychology calls (the protagonist is the player)."""
    return {
        char_id: character
        for char_id, character in participants.items()
        if char_id != protagonist_id
    }


def character_mention_offset(chapter_text: str, char_id: str,
                             candidates: Dict[str, dict]) -> int:
    """Find a full name or an unambiguous first name in the narrated scene."""
    state = candidates.get(char_id)
    name = state.get("name", "") if isinstance(state, dict) else ""
    if not isinstance(name, str) or not name.strip():
        return -1
    names = [name.strip()]
    first = name.split()[0]
    first_count = sum(
        isinstance(other, dict) and isinstance(other.get("name"), str)
        and other["name"].split() and other["name"].split()[0].casefold() == first.casefold()
        for other in candidates.values()
    )
    if len(first) >= 3 and first_count == 1 and first.casefold() != name.casefold():
        names.append(first)
    offsets = []
    for candidate in names:
        match = re.search(r"(?<!\w)" + re.escape(candidate) + r"(?!\w)",
                          chapter_text, re.IGNORECASE)
        if match:
            offsets.append(match.start())
    return min(offsets) if offsets else -1


def completed_scene_witnesses(start_participants: Dict[str, dict],
                              character_state: dict, end_location: str,
                              chapter_text: str, *, protagonist_id: str = None,
                              allowed_ids=None,
                              max_observers: int = DEFAULT_MAX_OBSERVERS) -> Dict[str, dict]:
    """Include a newly co-located NPC only when the final prose names them.

    A player can cross from one room to another within a turn. Start-of-turn
    observers still witnessed that turn; an NPC at the destination only gets
    the scene transcript if they visibly participated in it.
    """
    result = dict(start_participants)
    at_end = scene_participants(
        character_state, end_location, protagonist_id=protagonist_id,
        allowed_ids=allowed_ids, max_observers=max_observers,
    )
    for char_id in at_end:
        if char_id in result or character_mention_offset(chapter_text, char_id, at_end) >= 0:
            result[char_id] = at_end[char_id]
    if isinstance(max_observers, int) and max_observers >= 0:
        return dict(list(result.items())[:max_observers])
    return result
