"""Psychology responsibilities for Story Engine."""



PSYCHOLOGY_PERCEPTION_PROMPT = """You are a character perception agent for an interactive story engine.

Task: Given a chapter scene and a specific character's psychology state, generate that character's subjective perception of the scene.

MANDATORY RULES:
1. Return raw JSON only — no markdown, no code fences, no preamble or closing remarks.
2. The JSON must have exactly these top-level keys: "perception", "emotional_response", "noticed_threats", "noticed_opportunities", "impression_of_others", "decision".
3. "perception": A 1-3 sentence description of what this character notices, focusing on what is relevant to their personality, goals, and current psychology.
4. "emotional_response": A short phrase describing their immediate emotional reaction (e.g. "cautious curiosity", "cold anger", "quiet satisfaction").
5. "noticed_threats": Array of threats they perceive (0-3 items).
6. "noticed_opportunities": Array of opportunities they perceive (0-3 items).
7. "impression_of_others": Object mapping other character IDs to a short impression (e.g. {"char_2": "seems nervous and evasive"}).
8. "decision": A short phrase describing what they are inclined to do next (e.g. "approach cautiously", "stay back and observe", "intervene immediately").
9. Read the entire chapter_text through its final sentence before describing what remains to do. Do not claim that a character has not eaten, spoken, arrived, or finished an action if the scene shows they did. An opportunity or decision must still be open AFTER the scene ends; a completed meal or completed conversation is not a future goal. The character may misunderstand another person's motives, but not erase an observable action they witnessed.

Write in the same language as the chapter_text provided in the payload.

EXACT JSON STRUCTURE TO RETURN:
{
  "perception": "What they notice and how they interpret it",
  "emotional_response": "a short phrase",
  "noticed_threats": ["threat 1", "threat 2"],
  "noticed_opportunities": ["opportunity 1"],
  "impression_of_others": {"char_2": "seems trustworthy"},
  "decision": "what they decide to do"
}
"""


PSYCHOLOGY_UPDATE_PROMPT = """You are a psychology runtime agent for an interactive story engine.

Task: Update a character's internal psychological state based on what happened in the chapter and their perception of events.

MANDATORY RULES:
1. Return raw JSON only — no markdown, no code fences, no preamble or closing remarks.
2. The JSON must have exactly these top-level keys: "hedonic_delta", "stress_delta", "belief_updates", "theory_of_mind_updates", "trust_updates", "mood", "goal_updates".
3. "hedonic_delta": Float between -1.0 and 1.0 — how much their pleasure/satisfaction shifted this turn.
4. "stress_delta": Float between -1.0 and 1.0 — how much their stress changed (positive = more stressed).
5. "belief_updates": Object mapping belief names to delta values (float -1.0 to 1.0) or absolute values (0.0 to 1.0). E.g. {"the world is dangerous": 0.15} or {"justice prevails": 0.8}.
6. "theory_of_mind_updates": Object mapping other character IDs to objects of trait impressions. E.g. {"char_2": {"loyalty": 0.2, "competence": -0.1}}.
7. "trust_updates": Object mapping other character IDs to trust delta values (float -1.0 to 1.0).
8. "mood": A single word or short phrase describing their new mood (e.g. "tense", "hopeful", "gloomy", "determined").
9. "goal_updates": Array of new goals to add (or empty array if none).
10. Ground each update in the FINAL state of chapter_text. Do not add a goal to perform an action that the scene has already completed, even if the earlier perception_data called it an opportunity. If perception_data conflicts with the observed chapter_text, trust the observed scene for what visibly happened while preserving uncertainty about motives.

Base the updates strictly on what actually happened in the chapter and the character's perception — do not invent events.

Write in the same language as the chapter_text.

EXACT JSON STRUCTURE TO RETURN:
{
  "hedonic_delta": 0.0,
  "stress_delta": 0.0,
  "belief_updates": {"belief_name": 0.1},
  "theory_of_mind_updates": {"char_2": {"trait": 0.2}},
  "trust_updates": {"char_2": 0.1},
  "mood": "cautious",
  "goal_updates": ["new goal 1"]
}
"""


# Experimental narration keeps the same perception/update calls, but lets the
# actor retire obsolete intentions using already-confirmed recent scenes.
EXPERIMENTAL_PSYCHOLOGY_PERCEPTION_PROMPT = PSYCHOLOGY_PERCEPTION_PROMPT.replace(
    'Write in the same language as the chapter_text provided in the payload.',
    'The payload may include recent_confirmed_turns containing this character\'s own '
    'perceptions from earlier saved scenes, not omniscient summaries or proposed future beats. '
    'Before naming an opportunity or decision, check whether '
    'the character already saw that action completed there. The final chapter_text remains '
    'the authority for the present scene. An ordinary wish to finish a job before a radio '
    'bulletin or meal is not a threat or deadline without an established consequence; do not '
    'repeat it as pressure every turn.\n\n'
    'Write in the same language as the chapter_text provided in the payload.',
    1,
)

EXPERIMENTAL_PSYCHOLOGY_UPDATE_PROMPT = PSYCHOLOGY_UPDATE_PROMPT.replace(
    '"mood", "goal_updates".',
    '"mood", "goal_updates", "goal_removals".',
    1,
).replace(
    '10. Ground each update in the FINAL state of chapter_text.',
    '9a. "goal_removals": Array of exact strings from current_psychology.goals that are '
    'already achieved, obsolete, or contradicted by chapter_text or recent_confirmed_turns. '
    'Do not remove an ongoing goal merely because the character has not acted on it yet. '
    'Never add a replacement goal to repeat a completed task.\n'
    '10. Ground each update in the FINAL state of chapter_text.',
    1,
).replace(
    '  "goal_updates": ["new goal 1"]',
    '  "goal_updates": [],\n  "goal_removals": []',
    1,
)
