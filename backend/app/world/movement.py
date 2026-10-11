"""Whether the player's input actually moves them, and where to.

2.1 recognised a move only when the input began with `travel to` or `go to`.
Everything else — "I take the lift to the Supply Zone", "I follow Arlan down to
the Supply Zone" — produced no travel resolution at all, and then the narrator's
own state changes performed the move instead, bypassing the route, the unlock
gates and the elapsed time.

Two things are needed, and only doing the first is not enough:

1. **Recognise more ways of committing to a move** — without turning every
   mention of a place into an arrival.
2. **Never let the narrator commit a move the engine did not authorise.** See
   `validate_protagonist_location` in this module, which is called before the
   turn is written to disk.

The distinction that matters is *attempt* versus *commitment*:

    "I look for a lift down to the Supply Zone."      attempt  — no move
    "I ask how to reach the Supply Zone."             ask      — no move
    "I plan to go to the Supply Zone later."          plan     — no move
    "I take the lift to the Supply Zone."             commit   — a move
    "I follow Arlan to the Supply Zone."              commit   — a move
    "I stay where I am."                              refuse   — no move

A commitment still has to survive the route and unlock checks in
`app.world.travel`; this module decides only whether the player meant to go.
"""
import re

from app.world.boundaries import _is_location_in_zone
from app.world.travel import build_travel_plan, find_location, find_containing_location

# Checked first. Any of these makes the input an attempt, an enquiry, a plan or
# a refusal, whatever else it contains — "I plan to go to X later" contains
# "go to X" and is still not a move.
_NON_COMMIT = (
    r"\b(?:look|looks|looked|looking|search|searches|searched|searching|hunt|hunts|hunting|feel|feels|feeling)\s+(?:for|around|about)\b",
    r"\b(?:look|looks|looked|looking)\s+(?:at|towards|toward)\b",
    r"\btry(?:ing)?\s+to\s+(?:find|reach|get|locate)\b",
    r"\bwhere(?:'s|s| is| are| was)\b",
    r"\bhow\s+(?:do|can|could|would|should)\s+(?:i|we)\s+(?:get|reach|find|leave)\b",
    r"\b(?:ask|asks|asked|asking|enquire|enquire[sd]?|inquire|inquire[sd]?)\b.*"
    r"\b(?:how|way|route|direction|directions|reach|get to|find)\b",
    r"\b(?:the\s+)?(?:way|route|directions?)\s+to\b",
    r"\b(?:plan|plans|planning|intend|intends|intending|mean|means|meaning)\s+to\b",
    r"\b(?:want|wants|wanted|would\s+like|hope|hopes|hoping|think|thinking|thought|"
    r"consider|considering|wonder|wondering)\s+(?:about\s+)?(?:to\s+)?"
    r"(?:go|going|head|heading|travel|travelling|traveling|move|moving|get|getting|leave|leaving)\b",
    r"\b(?:later|some\s+other\s+time|another\s+time|eventually|someday|tomorrow|afterwards|afterward)\b",
    r"\b(?:stay|stays|staying|remain|remains|remaining|wait|waits|waiting)\b",
    r"\b(?:don't|do\s+not|won't|will\s+not|refuse|refuses|refused|refusing)\s+"
    r"(?:go|leave|board|move|travel|head)\b",
    r"\b(?:not\s+going|isn't\s+going|no\s+intention)\b",
    r"\bmaps?\b",
    r"\b(?:notice|notices|sign|signs|board|schedule|manifest)\b.*\b(?:says|shows|lists|reads)\b",
)

# A commitment to travel. Deliberately about verbs of departure, not about the
# presence of a place name: naming a place is not going there.
_COMMIT = (
    r"^(?:travel|go|head|walk|move|ride|return|leave|set\s+off|depart|board)\b",
    r"\b(?:travel|travels|go|goes|head|heads|walk|walks|move|moves|return|returns)\s+to\b",
    r"\b(?:travel|travels|travelling|traveling|go|goes|going|head|heads|heading|"
    r"walk|walks|walking|move|moves|moving|return|returns|returning|leave|leaves|"
    r"leaving|depart|departs|departing)\s+(?:down|up|over|back|out|in|through|"
    r"along|across)\s+to\b",
    r"\b(?:take|takes|taking|took|board|boards|boarding|boarded|catch|catches|"
    r"catching|caught|ride|rides|riding|rode)\s+(?:the\s+|a\s+|an\s+|his\s+|her\s+|"
    r"their\s+)?(?:lift|elevator|tram|train|shuttle|car|transport|express|ship|"
    r"boat|ferry|stairs|escalator|skiff|starskiff|pod)\b",
    r"\b(?:follow|follows|following|followed|accompany|accompanies|accompanying|"
    r"accompanied|escort|escorts|escorting|escorted|join|joins|joining|joined)\s+"
    r"(?:[A-Z][\w'-]+|the\s+[\w'-]+|them|him|her)\b",
    r"\b(?:i|we)\s+(?:go|goes|head|heads|walk|walks|move|moves|ride|rides|take|takes|"
    r"board|boards|follow|follows|leave|leaves|travel|travels)\b",
    r"\b(?:towards|toward|down\s+to|up\s+to|over\s+to|back\s+to)\s+(?:the\s+)?"
    r"[A-Z][\w'-]+",
)

_COMPANION = re.compile(
    r"\b(?:follow|follows|following|followed|accompany|accompanies|accompanying|"
    r"accompanied|escort|escorts|escorting|escorted|join|joins|joining|joined)\s+"
    r"(?P<who>[A-Z][\w'-]+)",
    re.I,
)

# Negation can govern a coordinated list: "does not set off, board a vessel
# or follow anyone". None of those verbs is a positive travel instruction.
# Keep preceding/following affirmative clauses, e.g. "Go to Supply Zone.
# Do not board a ship" and "Do not board, but walk to Supply Zone instead".
_NEGATED_CLAUSE = re.compile(
    r"\b(?:does\s+not|do\s+not|did\s+not|don't|doesn't|didn't|won't|will\s+not|"
    r"never|without|(?:follow|follows|following)\s+no)\b"
    r"(?:(?!\b(?:but|then|instead|however)\b)[^.;!?])*", re.I,
)


def _matches(patterns, text: str) -> bool:
    return any(re.search(pattern, text, re.I) for pattern in patterns)


_AFTER_ARRIVAL_REST = re.compile(
    r"(?:[,;.]\s*|\b(?:and\s+then|and|then)\s+)"
    r"(?:(?:i|we|he|she)\s+)?(?:wait|waits|stay|stays|remain|remains)\b", re.I,
)


def _movement_text(text: str) -> str:
    """Use the same affirmative departure evidence for intent and destination.

    A rest *after* an unconditional trip does not cancel that trip. A question,
    plan, or wait for permission before departure still stays a non-move.
    """
    affirmative = _NEGATED_CLAUSE.sub(" ", str(text or "")).strip()
    rest = _AFTER_ARRIVAL_REST.search(affirmative)
    if rest:
        departure = affirmative[:rest.start()].strip()
        if _matches(_COMMIT, departure) and not _matches(_NON_COMMIT, departure):
            return departure
    return affirmative


def classify_movement(user_input: str) -> dict:
    """What the player is doing about a place, if anything.

    Returns `kind` one of `commit`, `attempt`, `ask`, `plan`, `refuse`, `none`,
    plus the companion named in the input when there is one. It does not decide
    whether the destination exists or is reachable.
    """
    text = str(user_input or "").strip()
    if not text:
        return {"kind": "none", "companion": ""}
    affirmative = _movement_text(text)
    companion = ""
    match = _COMPANION.search(affirmative)
    if match:
        companion = match.group("who")
    if _matches(_NON_COMMIT, affirmative):
        kind = "attempt"
        if re.search(r"\b(?:stay|stays|staying|remain|remains|remaining|wait|waits|waiting)\b", text, re.I) \
                or re.search(r"\b(?:don't|do\s+not|won't|will\s+not|refuse\w*|not\s+going)\b", text, re.I):
            kind = "refuse"
        elif re.search(r"\b(?:ask|asks|asked|asking|enquire|inquire)\b", text, re.I) \
                or re.search(r"\bhow\s+(?:do|can|could|would|should)\b", text, re.I):
            kind = "ask"
        elif re.search(r"\b(?:plan|intend|want|hope|think|consider|wonder)\w*\b", text, re.I) \
                or re.search(r"\b(?:later|someday|eventually|tomorrow)\b", text, re.I):
            kind = "plan"
        return {"kind": kind, "companion": ""}
    if _matches(_COMMIT, affirmative):
        return {"kind": "commit", "companion": companion}
    if _NEGATED_CLAUSE.search(text):
        return {"kind": "refuse", "companion": ""}
    return {"kind": "none", "companion": ""}


def find_named_destination(text: str, location_map: dict):
    """The place the input names, matched longest-name-first.

    Longest first so "Supply Zone" wins over "Zone", and so a sub-location wins
    over its parent.
    """
    if not isinstance(location_map, dict):
        return None
    lowered = _movement_text(text).lower()
    if not lowered:
        return None
    candidates = []
    for loc in location_map.get("locations", []) or []:
        if not isinstance(loc, dict):
            continue
        name = str(loc.get("name") or "").strip()
        if not name:
            continue
        # Prefer a named destination after a direction marker over an origin
        # introduced by "from" or a place mentioned elsewhere in the action.
        mention = re.search(r"(?<!\w)" + re.escape(name.lower()) + r"(?!\w)", lowered)
        directed = re.search(r"\b(?:to|towards?|into|for)\s+(?:the\s+)?"
                             + re.escape(name.lower()) + r"(?!\w)", lowered)
        if mention:
            candidates.append((bool(directed), len(name), name, loc))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return candidates[0][3]


def resolve_movement(user_input: str, location_map: dict, current_location: str, *,
                     tick_minutes: int = 60, seed_key: str = "",
                     active_journey=None) -> dict:
    """Turn a player input into a travel resolution, or into a clear non-move.

    A `commit` goes through the engine's own travel path, so the route, the
    unlock gates and the elapsed time are all the engine's. Anything else comes
    back as `status: "not_a_move"` with the classification attached, which is
    what lets the caller refuse a narrator-proposed arrival that nobody asked
    for.
    """
    # The engine's own journey commands keep their meaning.
    if re.match(r"^\s*(?:abandon|continue)\s+(?:the\s+)?journey\b", str(user_input or ""), re.I):
        plan = build_travel_plan(user_input, location_map, current_location,
                                 tick_minutes=tick_minutes, seed_key=seed_key,
                                 active_journey=active_journey)
        if plan:
            plan["movement_kind"] = "commit"
            return plan

    classified = classify_movement(user_input)
    destination = find_named_destination(user_input, location_map)
    classified["destination_ref"] = (destination or {}).get("name", "")
    classified["destination_id"] = (destination or {}).get("id", "")

    if classified["kind"] != "commit":
        return {
            "status": "not_a_move",
            "movement_kind": classified["kind"],
            "reason": {
                "attempt": "The player is looking for a way, not taking one.",
                "ask": "The player is asking for directions.",
                "plan": "The player is planning a move, not making it.",
                "refuse": "The player has refused to move.",
                "none": "The input is not a movement.",
            }.get(classified["kind"], "Not a movement."),
            "destination_ref": classified.get("destination_ref", ""),
            "companion": classified.get("companion", ""),
        }

    if destination is None:
        # A commitment with no destination the engine can resolve: the player is
        # leaving, but not to anywhere the map knows. It is not an arrival.
        return {
            "status": "not_a_move",
            "movement_kind": "commit",
            "reason": "The player committed to moving, but named no place the map knows.",
            "destination_ref": "",
            "companion": classified.get("companion", ""),
        }

    plan = build_travel_plan(user_input, location_map, current_location,
                             tick_minutes=tick_minutes, seed_key=seed_key,
                             active_journey=active_journey,
                             destination_id=destination.get("id", ""))
    if not plan:
        return {
            "status": "not_a_move",
            "movement_kind": "commit",
            "reason": "No route could be built for that destination.",
            "destination_ref": destination.get("name", ""),
            "companion": classified.get("companion", ""),
        }
    plan["movement_kind"] = "commit"
    plan["companion"] = classified.get("companion", "")
    return plan


def location_change_authorised(*, origin: str, proposed: str, travel_resolution: dict,
                               declared_move: str = "", forced_move: str = "",
                               location_map: dict = None) -> dict:
    """Is the narrator allowed to put the protagonist at `proposed`?

    Authorised when, and only when:

    * the engine's travel path arrived or was interrupted on the way (the
      destination is the engine's, not the narrator's);
    * a declared action consequence moves the protagonist (an escort, a rescue,
      an evacuation the world authored);
    * the proposed place is inside the zone the protagonist is already in, which
      is ordinary movement within a scene — walking to the far end of the hall.

    Everything else is the narrator performing a journey nobody made.
    """
    if not proposed or proposed == origin:
        return {"ok": True, "why": "no change"}
    if isinstance(travel_resolution, dict) and travel_resolution.get("status") in (
            "arrived", "interrupted"):
        landed = (travel_resolution.get("destination")
                  if travel_resolution.get("status") == "arrived"
                  else travel_resolution.get("stopped_at"))
        if landed and _is_location_in_zone(proposed, {str(landed)}):
            return {"ok": True, "why": "engine travel resolution"}
        return {
            "ok": False,
            "why": "travel_resolution_mismatch",
            "detail": f"travel ended at {landed!r}, the prose ends at {proposed!r}",
        }
    if forced_move and _is_location_in_zone(proposed, {str(forced_move)}):
        return {"ok": True, "why": "declared movement effect"}
    if declared_move and _is_location_in_zone(proposed, {str(declared_move)}):
        return {"ok": True, "why": "declared movement effect"}
    if origin and _is_location_in_zone(proposed, {str(origin)}):
        return {"ok": True, "why": "movement within the same place"}
    # Legacy prose can name sibling rooms as "Base Zone (plant room)" and
    # "Base Zone (service corridor)". Match the longest actual map location,
    # never a guessed string prefix, so known gated subareas stay distinct.
    origin_place = find_containing_location(location_map, origin)
    proposed_place = find_containing_location(location_map, proposed)
    if origin_place and origin_place == proposed_place:
        return {"ok": True, "why": "movement within the same mapped place"}
    return {
        "ok": False,
        "why": "unsupported_location_change",
        "detail": f"no travel, no declared movement: {origin!r} -> {proposed!r}",
    }


def movement_correction_note(origin: str, proposed: str, kind: str) -> str:
    """What to tell the writer when it moved the protagonist on its own."""
    what = {
        "attempt": "looking for a way",
        "ask": "asking for directions",
        "plan": "planning a journey",
        "refuse": "refusing to move",
    }.get(kind)
    opening = (f"The player's input was {what}, not a departure." if what
               else "No travel was resolved this turn.")
    return (
        f"{opening} The protagonist is still at {origin!r} and does not reach "
        f"{proposed!r} in this scene. Describe the search, the question, the plan or "
        f"the refusal, and the distance still to cover — do not narrate an arrival, "
        f"and do not write them as having arrived. state_changes must not set the "
        f"protagonist's location to anything outside {origin!r}."
    )
