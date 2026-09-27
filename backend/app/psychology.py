import json
import logging
from typing import Dict, List, Optional, Any

from app.llm_client import call_llm, parse_llm_json
from app.models import CharacterModel
from app.prompts import PSYCHOLOGY_PERCEPTION_PROMPT, PSYCHOLOGY_UPDATE_PROMPT
from app.prompts import EXPERIMENTAL_PSYCHOLOGY_PERCEPTION_PROMPT, EXPERIMENTAL_PSYCHOLOGY_UPDATE_PROMPT
from app.story.narration_mode import is_experimental

logger = logging.getLogger(__name__)

class PsychologyState:
    def __init__(self):
        self.hedonic: float = 0.0
        self.stress: float = 0.0
        self.beliefs: Dict[str, float] = {}
        self.theory_of_mind: Dict[str, Dict[str, float]] = {}
        self.mood: str = "neutral"
        self.trust: Dict[str, float] = {}
        self.goals: List[str] = []

    def to_dict(self) -> dict:
        return {
            "hedonic": self.hedonic,
            "stress": self.stress,
            "beliefs": self.beliefs,
            "theory_of_mind": self.theory_of_mind,
            "mood": self.mood,
            "trust": self.trust,
            "goals": self.goals
        }

    @classmethod
    def from_dict(cls, data: dict) -> "PsychologyState":
        if not data:
            return cls()
        state = cls()
        state.hedonic = data.get("hedonic", 0.0)
        state.stress = data.get("stress", 0.0)
        state.beliefs = data.get("beliefs", {})
        state.theory_of_mind = data.get("theory_of_mind", {})
        state.mood = data.get("mood", "neutral")
        state.trust = data.get("trust", {})
        state.goals = data.get("goals", [])
        return state


def generate_perception_for_character(
    character_id: str,
    character_state: Dict[str, Any],
    chapter_text: str,
    world_config: dict,
    checkpoint: dict,
    all_characters: Dict[str, Dict[str, Any]],
    world_name: str = None,
    narration_mode=None,
    recent_confirmed_turns=None,
) -> Dict[str, Any]:
    """
    Generate perception data for a single character.
    Returns: dict with fields: perception, emotional_response, noticed_threats, etc.
    """
    char_name = character_state.get("name", character_id)
    psychology = PsychologyState.from_dict(character_state.get("psychology", {}))
    from app.story.relationship_memory import relevant_memories
    relationship_memories = relevant_memories(all_characters, character_id, limit=5)

    payload = {
        "character_name": char_name,
        "character_id": character_id,
        "character_personality": character_state.get("personality", ""),
        "character_goals": character_state.get("goals", []),
        "character_knowledge": character_state.get("knowledge", []) if isinstance(character_state.get("knowledge"), list) else [],
        "relationship_memories": relationship_memories,
        "location": character_state.get("location", ""),
        "psychology": psychology.to_dict(),
        "chapter_text": chapter_text,
        "other_characters": [
            {"id": cid, "name": st.get("name", cid), "realm": st.get("power_stat", {}).get("realm", "")}
            for cid, st in all_characters.items() if cid != character_id
        ],
        "checkpoint_description": checkpoint.get("description", ""),
        "world_tone": world_config.get("tone", "neutral")
    }
    if is_experimental(narration_mode):
        payload["recent_confirmed_turns"] = recent_confirmed_turns or []

    raw = call_llm(
        (EXPERIMENTAL_PSYCHOLOGY_PERCEPTION_PROMPT if is_experimental(narration_mode)
         else PSYCHOLOGY_PERCEPTION_PROMPT),
        json.dumps(payload, ensure_ascii=False),
        world_name=world_name,
        role="perception"
    )

    try:
        parsed = parse_llm_json(raw, expected_type=dict)
        return {
            "perception": parsed.get("perception", ""),
            "emotional_response": parsed.get("emotional_response", ""),
            "noticed_threats": parsed.get("noticed_threats", []),
            "noticed_opportunities": parsed.get("noticed_opportunities", []),
            "impression_of_others": parsed.get("impression_of_others", {}),
            "decision": parsed.get("decision", "observe")
        }
    except Exception as e:
        logger.warning(f"Perception generation failed for {character_id}: {e}")
        return {
            "perception": f"{char_name} observed the scene.",
            "emotional_response": "neutral",
            "noticed_threats": [],
            "noticed_opportunities": [],
            "impression_of_others": {},
            "decision": "observe"
        }


def update_psychology_for_character(
    character_id: str,
    character_state: Dict[str, Any],
    chapter_text: str,
    perception_data: Dict[str, Any],
    world_config: dict,
    world_name: str = None,
    narration_mode=None,
    recent_confirmed_turns=None,
) -> Dict[str, Any]:
    """
    Update psychology state based on events in the chapter.
    Returns: updated psychology fields (hedonic_delta, stress_delta, belief_updates, etc.)
    """
    psychology = PsychologyState.from_dict(character_state.get("psychology", {}))
    char_name = character_state.get("name", character_id)

    payload = {
        "character_name": char_name,
        "character_id": character_id,
        "current_psychology": psychology.to_dict(),
        "chapter_text": chapter_text,
        "perception_data": perception_data,
        "character_knowledge": character_state.get("knowledge", []) if isinstance(character_state.get("knowledge"), list) else [],
        "relationship_memories": character_state.get("relationship_memories", [])[:5] if isinstance(character_state.get("relationship_memories"), list) else [],
        "world_tone": world_config.get("tone", "neutral"),
        "personality": character_state.get("personality", ""),
        "goals": character_state.get("goals", [])
    }
    if is_experimental(narration_mode):
        payload["recent_confirmed_turns"] = recent_confirmed_turns or []

    raw = call_llm(
        (EXPERIMENTAL_PSYCHOLOGY_UPDATE_PROMPT if is_experimental(narration_mode)
         else PSYCHOLOGY_UPDATE_PROMPT),
        json.dumps(payload, ensure_ascii=False),
        world_name=world_name,
        role="psychology"
    )

    try:
        parsed = parse_llm_json(raw, expected_type=dict)
        current_goals = psychology.goals if isinstance(psychology.goals, list) else []
        requested_removals = parsed.get("goal_removals", []) if is_experimental(narration_mode) else []
        goal_removals = ([goal for goal in requested_removals
                          if isinstance(goal, str) and goal in current_goals]
                         if isinstance(requested_removals, list) else [])
        update = {
            "hedonic_delta": parsed.get("hedonic_delta", 0.0),
            "stress_delta": parsed.get("stress_delta", 0.0),
            "belief_updates": parsed.get("belief_updates", {}),
            "theory_of_mind_updates": parsed.get("theory_of_mind_updates", {}),
            "trust_updates": parsed.get("trust_updates", {}),
            "mood": parsed.get("mood", "neutral"),
            "goal_updates": parsed.get("goal_updates", []),
        }
        if is_experimental(narration_mode):
            update["goal_removals"] = goal_removals
        return update
    except Exception as e:
        logger.warning(f"Psychology update failed for {character_id}: {e}")
        update = {
            "hedonic_delta": 0.0,
            "stress_delta": 0.0,
            "belief_updates": {},
            "theory_of_mind_updates": {},
            "trust_updates": {},
            "mood": "neutral",
            "goal_updates": [],
        }
        if is_experimental(narration_mode):
            update["goal_removals"] = []
        return update


def apply_psychology_changes(
    character_state: Dict[str, Any],
    psychology_updates: Dict[str, Dict[str, Any]]
) -> Dict[str, Any]:
    """
    Apply psychology updates to character state.
    Returns: updated character state dict.
    """
    for char_id, updates in psychology_updates.items():
        if char_id not in character_state:
            continue
        state = character_state[char_id]
        psycho = PsychologyState.from_dict(state.get("psychology", {}))

        psycho.hedonic = max(-1.0, min(1.0, psycho.hedonic + updates.get("hedonic_delta", 0.0)))
        psycho.stress = max(0.0, min(1.0, psycho.stress + updates.get("stress_delta", 0.0)))

        for belief_key, value in updates.get("belief_updates", {}).items():
            if isinstance(value, (int, float)):
                old = psycho.beliefs.get(belief_key, 0.5)
                psycho.beliefs[belief_key] = max(0.0, min(1.0, old + value))
            elif isinstance(value, bool) or value is None:
                psycho.beliefs[belief_key] = float(value) if value is not None else 0.5

        for other_id, impressions in updates.get("theory_of_mind_updates", {}).items():
            if other_id not in psycho.theory_of_mind:
                psycho.theory_of_mind[other_id] = {}
            for trait, val in impressions.items():
                if isinstance(val, (int, float)):
                    old = psycho.theory_of_mind[other_id].get(trait, 0.5)
                    psycho.theory_of_mind[other_id][trait] = max(0.0, min(1.0, old + val))

        for other_id, trust_delta in updates.get("trust_updates", {}).items():
            if isinstance(trust_delta, (int, float)):
                old = psycho.trust.get(other_id, 0.5)
                psycho.trust[other_id] = max(0.0, min(1.0, old + trust_delta))

        if updates.get("mood"):
            psycho.mood = updates["mood"]

        if "goal_removals" in updates:
            removals = updates.get("goal_removals") or []
            if isinstance(removals, list):
                psycho.goals = [goal for goal in psycho.goals if goal not in removals]
            additions = updates.get("goal_updates") or []
            if isinstance(additions, list):
                psycho.goals = list(dict.fromkeys(
                    psycho.goals + [goal for goal in additions if isinstance(goal, str) and goal.strip()]
                ))
        elif updates.get("goal_updates"):
            # Keep the classic merge path byte-for-byte compatible.
            psycho.goals = list(set(psycho.goals + updates["goal_updates"]))

        state["psychology"] = psycho.to_dict()

    return character_state


def generate_perceptions_for_all_characters(
    active_characters: Dict[str, Dict[str, Any]],
    chapter_text: str,
    world_config: dict,
    checkpoint: dict,
    world_name: str = None,
    narration_mode=None,
    recent_confirmed_turns=None,
    observed_chapter_text_by_character=None,
) -> Dict[str, Dict[str, Any]]:
    """
    Generate perception data for all active characters.
    Returns: dict mapping character_id -> perception data
    """
    perceptions = {}
    for char_id, state in active_characters.items():
        observed_history = (recent_confirmed_turns.get(char_id, [])
                            if isinstance(recent_confirmed_turns, dict) else [])
        observed_text = (observed_chapter_text_by_character.get(char_id, chapter_text)
                         if isinstance(observed_chapter_text_by_character, dict) else chapter_text)
        perception = generate_perception_for_character(
            char_id, state, observed_text, world_config, checkpoint,
            active_characters, world_name,
            narration_mode=narration_mode,
            recent_confirmed_turns=observed_history,
        )
        perceptions[char_id] = perception
    return perceptions


def update_psychologies_for_all_characters(
    active_characters: Dict[str, Dict[str, Any]],
    chapter_text: str,
    perceptions: Dict[str, Dict[str, Any]],
    world_config: dict,
    world_name: str = None,
    narration_mode=None,
    recent_confirmed_turns=None,
    observed_chapter_text_by_character=None,
) -> Dict[str, Dict[str, Any]]:
    """
    Update psychology for all active characters.
    Returns: dict mapping character_id -> psychology updates
    """
    updates = {}
    for char_id, state in active_characters.items():
        perception = perceptions.get(char_id, {})
        observed_history = (recent_confirmed_turns.get(char_id, [])
                            if isinstance(recent_confirmed_turns, dict) else [])
        observed_text = (observed_chapter_text_by_character.get(char_id, chapter_text)
                         if isinstance(observed_chapter_text_by_character, dict) else chapter_text)
        psych_update = update_psychology_for_character(
            char_id, state, observed_text, perception, world_config, world_name,
            narration_mode=narration_mode,
            recent_confirmed_turns=observed_history,
        )
        updates[char_id] = psych_update
    return updates
