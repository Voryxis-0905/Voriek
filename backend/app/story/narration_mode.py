"""Narration mode: which planner/writer narrative guidance a turn is written with.

Two profiles exist:

* ``classic`` (the default) - the original planner/writer guidance. A request
  that does not name a mode runs here, so an older client, a script, or a plain
  ``curl`` call keeps exactly the behaviour it had before this feature existed.
* ``experimental`` - the same engine, the same JSON contracts, the same hard
  rules, but different *narrative guidance*: a turn is not required to carry
  friction or end on a cliffhanger, length follows the scene instead of a word
  target, and present NPCs are planned with their own separate motives.

Two deliberate properties:

1. The mode is carried by the generation request itself and is **never written
   to a world's files**. A saved turn must stay exactly what it was, and
   flipping the switch must not touch a world's state, so the choice cannot
   live in ``world_config.json``. The UI remembers the player's choice per world
   on its own side (localStorage) and only sends it along with new turns.

2. Classic and experimental retain their existing call counts. The separate
   opt-in ``ensemble`` mode adds one perspective-limited call per present NPC.
"""
from app.prompts import EXPERIMENTAL_PLANNER_SYSTEM_PROMPT
from app.prompts import EXPERIMENTAL_WRITER_SYSTEM_PROMPT
from app.prompts import PLANNER_SYSTEM_PROMPT
from app.prompts import WRITER_SYSTEM_PROMPT

NARRATION_MODE_CLASSIC = "classic"
NARRATION_MODE_EXPERIMENTAL = "experimental"
NARRATION_MODE_ENSEMBLE = "ensemble"

NARRATION_MODES = (NARRATION_MODE_CLASSIC, NARRATION_MODE_EXPERIMENTAL, NARRATION_MODE_ENSEMBLE)


def normalize_narration_mode(value) -> str:
    """Return the mode to run.

    Anything that is not a recognized experimental request - a missing field, a
    blank string, an unexpected type - resolves to ``classic``. The HTTP layer
    rejects unknown *values* explicitly (422) so a typo is never silently
    downgraded; this function is the last line of defence for internal callers
    and for requests that predate the field.
    """
    if not isinstance(value, str):
        return NARRATION_MODE_CLASSIC
    key = value.strip().lower()
    if key == NARRATION_MODE_EXPERIMENTAL:
        return NARRATION_MODE_EXPERIMENTAL
    if key == NARRATION_MODE_ENSEMBLE:
        return NARRATION_MODE_ENSEMBLE
    return NARRATION_MODE_CLASSIC


def is_experimental(value) -> bool:
    return normalize_narration_mode(value) in (NARRATION_MODE_EXPERIMENTAL, NARRATION_MODE_ENSEMBLE)


def is_ensemble(value) -> bool:
    return normalize_narration_mode(value) == NARRATION_MODE_ENSEMBLE


def select_planner_prompt(narration_mode=None) -> str:
    if is_ensemble(narration_mode):
        prompt = EXPERIMENTAL_PLANNER_SYSTEM_PROMPT
        prompt = prompt.replace(
            '"scene_outline", "scene_direction", "facts_this_turn"',
            '"scene_outline", "scene_direction", "actor_observation", "facts_this_turn"', 1,
        )
        prompt = prompt.replace(
            '"scene_direction": {"focus": "Small scene focus", "stop_before": "", "actors": {}},',
            '"scene_direction": {"focus": "Small scene focus", "stop_before": "", "actors": {}},\n  "actor_observation": "Only the current visible or audible player action",',
            1,
        )
        return prompt + """

ENSEMBLE MODE: In addition to the normal JSON fields, return an
"actor_observation" string: only what an NPC physically present at the
scene could see or hear of the player's CURRENT action. Exclude thoughts,
private plans, offstage knowledge, and the outcome of an event not yet
witnessed. An empty string is correct when nothing is observable. Do not
script NPC dialogue or motives in scene_direction; set actors to {} because
separate actor calls propose their own behavior. When recent turns include a
"scene_record", treat its character locations and engine outcomes as the
confirmed end state of that published turn; do not plan from an older or
free-form synopsis.
"""
    if is_experimental(narration_mode):
        return EXPERIMENTAL_PLANNER_SYSTEM_PROMPT
    return PLANNER_SYSTEM_PROMPT


def select_writer_prompt(narration_mode=None) -> str:
    if is_ensemble(narration_mode):
        return EXPERIMENTAL_WRITER_SYSTEM_PROMPT + """

ENSEMBLE MODE: actor_cues are perspective-limited proposals from individual
NPCs, not canon or mandatory dialogue. Use only responses that fit the
player's action, physical location, and confirmed world state. The absence
of an actor cue means no new NPC action is required. Do not reveal an NPC's
private thoughts or unknown future events. The engine/checker owns truth.
Treat "planned_state_changes" as a provisional director estimate. Your
returned "state_changes" must describe only what the final chapter_text and
engine outcomes establish; omit a change when the prose leaves it uncertain.
Keep character locations synchronized with where the prose leaves them, and
show a transition before placing someone in a different room or area. Do not
write a free-form "notes" synopsis; the engine will retain the published
scene and its structured, committed state as continuity evidence.
Do not invent a retroactive phone call, promise, appointment, repair, or
technical specification to explain away an inconsistency. Preserve what is
unknown; a character may honestly say they do not remember, make a tentative
guess, or propose checking a record. A character's claim is not objective
world truth until independently established in play.
"""
    if is_experimental(narration_mode):
        return EXPERIMENTAL_WRITER_SYSTEM_PROMPT
    return WRITER_SYSTEM_PROMPT
