"""Deterministic travel planning; the narrator describes, the engine decides."""
from collections import deque
import hashlib
import math
import re


# Discovery states the player is never allowed to learn about. Kept here rather
# than at the route layer so "what the player may see" has exactly one definition
# shared by the map endpoint, the travel preview and anything added later.
HIDDEN_DISCOVERY_STATES = ("unknown", "creator_only")

# What a hidden place is called once it has to be mentioned at all. A route may
# legitimately pass through somewhere the player has not discovered; the player
# is told a stop exists, never which one.
HIDDEN_LOCATION_LABEL = "Unknown location"


def is_hidden(location: dict) -> bool:
    """True when the player must not learn this place's name or details."""
    if not isinstance(location, dict):
        return False
    return location.get("discovery_status", "discovered") in HIDDEN_DISCOVERY_STATES


def _locations(location_map: dict) -> list:
    return [item for item in (location_map or {}).get("locations", []) if isinstance(item, dict)]


def _matches(location: dict, ref: str) -> bool:
    target = str(ref or "").strip().lower()
    return target in {str(location.get("id", "")).lower(), str(location.get("name", "")).lower()}


def find_location(location_map: dict, ref: str):
    return next((item for item in _locations(location_map) if _matches(item, ref)), None)



def find_containing_location(location_map: dict, ref: str):
    """Resolve a confirmed scene label to its longest known map location.

    Destinations still use exact find_location matching. This only anchors a
    player's existing room/corridor label when planning or comparing origins.
    """
    from app.world.boundaries import _is_location_in_zone
    candidates = []
    for place in _locations(location_map):
        for name in (place.get("name"), place.get("id")):
            if isinstance(name, str) and name and _is_location_in_zone(ref, {name}):
                candidates.append((len(name), place))
    return max(candidates, key=lambda pair: pair[0])[1] if candidates else None


def visible_location_map(location_map: dict, current_location: str = "") -> dict:
    """The player's projection of a location map.

    Hidden places are removed outright, except the one the protagonist stands in
    (a character is always allowed to know where they are). Edges that would name
    a hidden place are dropped too, so routing over this map cannot travel
    through - or even learn about - somewhere the player has not discovered.

    This is the single definition of "player-visible map". Both the map endpoint
    and the travel preview derive from it, so a preview can never describe a
    journey the map would not show, and neither can drift from the other.
    """
    all_locations = _locations(location_map)
    currently_here = find_containing_location(location_map, current_location) if current_location else None
    keep_ids = {
        loc.get("id") for loc in all_locations
        if not is_hidden(loc) or (currently_here and loc.get("id") == currently_here.get("id"))
    }

    def _edge_target(edge) -> str:
        if isinstance(edge, str):
            return edge
        if isinstance(edge, dict):
            for key in ("location_id", "to", "id"):
                if edge.get(key):
                    return str(edge[key])
        return ""

    visible = []
    for loc in all_locations:
        if loc.get("id") not in keep_ids:
            continue
        # A kept location may still point at a hidden neighbour. Dropping the
        # edge is the point: otherwise the routing layer could step onto a
        # hidden place and narrate its name.
        kept_edges = []
        for edge in loc.get("connected_to", []) or []:
            target = find_location(location_map, _edge_target(edge))
            if target and target.get("id") in keep_ids:
                kept_edges.append(edge)
        visible.append({**loc, "connected_to": kept_edges})
    return {"locations": visible}


def extract_travel_destination(user_input: str, location_map: dict):
    text = str(user_input or "").strip()
    match = re.match(r"^(?:travel\s+to|go\s+to|đi\s+(?:đến|tới))\s+(.+?)(?:[.!]|\s+(?:via|by|bằng|theo)\s+.*)?$", text, re.I)
    if not match:
        return None
    requested = match.group(1).strip().strip("\"'")
    exact = find_location(location_map, requested)
    if exact:
        return exact
    lowered = requested.lower()
    return next((item for item in _locations(location_map)
                 if str(item.get("name", "")).lower() in lowered), None)


def _neighbors(location: dict) -> list:
    result = []
    for edge in location.get("connected_to", []) or []:
        if isinstance(edge, str):
            result.append((edge, {}))
        elif isinstance(edge, dict):
            ref = edge.get("location_id") or edge.get("to") or edge.get("id")
            if ref:
                result.append((str(ref), edge))
    return result


def find_route(location_map: dict, start_ref: str, destination_ref: str):
    start = find_containing_location(location_map, start_ref)
    destination = find_location(location_map, destination_ref)
    if not start or not destination:
        return None
    if start.get("id") == destination.get("id"):
        return [start]
    queue = deque([(start, [start])])
    visited = {start.get("id")}
    while queue:
        current, path = queue.popleft()
        for ref, _edge in _neighbors(current):
            nxt = find_location(location_map, ref)
            if not nxt or nxt.get("id") in visited:
                continue
            next_path = path + [nxt]
            if nxt.get("id") == destination.get("id"):
                return next_path
            visited.add(nxt.get("id"))
            queue.append((nxt, next_path))
    return None


def _edge_minutes(a: dict, b: dict) -> int:
    for ref, edge in _neighbors(a):
        if _matches(b, ref) and edge.get("travel_time_minutes") is not None:
            return max(1, int(edge["travel_time_minutes"]))
    dx = float(a.get("x", 0) or 0) - float(b.get("x", 0) or 0)
    dy = float(a.get("y", 0) or 0) - float(b.get("y", 0) or 0)
    return max(10, int(round(math.hypot(dx, dy) * 3)))


def _edge_metadata(a: dict, b: dict) -> dict:
    for ref, edge in _neighbors(a):
        if _matches(b, ref):
            return edge
    return {}


def preview_travel(location_map: dict, current_location: str, destination_ref: str,
                   *, tick_minutes: int = 60) -> dict:
    destination = find_location(location_map, destination_ref)
    start = find_containing_location(location_map, current_location)
    if not destination:
        return {"status": "unknown_destination", "destination": destination_ref,
                "elapsed_minutes": 0, "estimated_ticks": 0, "route": [], "legs": []}
    if not start:
        return {"status": "blocked", "reason": "current_location_not_on_map",
                "destination": destination.get("name"), "elapsed_minutes": 0,
                "estimated_ticks": 0, "route": [], "legs": []}
    route = find_route(location_map, start.get("id"), destination.get("id"))
    if not route:
        return {"status": "unreachable", "reason": "no_connected_route",
                "origin": start.get("name"), "destination": destination.get("name"),
                "elapsed_minutes": 0, "estimated_ticks": 0, "route": [], "legs": []}
    if len(route) == 1:
        return {"status": "already_there", "origin": start.get("name"),
                "destination": destination.get("name"), "destination_id": destination.get("id"),
                "elapsed_minutes": 0, "estimated_ticks": 0,
                "route": [start.get("name")], "legs": [], "risk": {"level": "none", "known_tags": []}}
    legs = []
    minutes = 0
    known_tags = []
    max_danger = 0.0
    for a, b in zip(route, route[1:]):
        metadata = _edge_metadata(a, b)
        leg_minutes = _edge_minutes(a, b)
        danger = max(0.0, min(1.0, float(metadata.get("danger", 0) or 0)))
        tags = metadata.get("tags", []) if isinstance(metadata.get("tags", []), list) else []
        minutes += leg_minutes
        max_danger = max(max_danger, danger)
        known_tags.extend(tag for tag in tags if tag not in known_tags)
        legs.append({"from": a.get("name"), "to": b.get("name"),
                     "travel_time_minutes": leg_minutes, "danger": danger, "tags": tags})
    risk_level = "high" if max_danger >= .66 else "medium" if max_danger >= .25 else "low" if max_danger > 0 else "none"
    return {
        "status": "available", "reason": "route_available",
        "origin": start.get("name"), "destination": destination.get("name"),
        "destination_id": destination.get("id"),
        "route": [item.get("name") for item in route], "legs": legs,
        "elapsed_minutes": minutes,
        "estimated_ticks": max(1, math.ceil(minutes / max(1, tick_minutes))),
        "narration_mode": "brief" if minutes <= 30 else "timeskip" if minutes <= 480 else "journey",
        "risk": {"level": risk_level, "known_tags": known_tags},
    }


# Fields that describe the *shape* of a route. Any one of them lets a player
# count how many hidden stops sit between origin and destination, which is
# itself information they have not earned. When a route touches a hidden place,
# all of them are withheld together - redacting the names alone still leaks the
# structure (measured: `route` of length 3 and a 2-entry `legs` array told the
# player there was exactly one uncharted stop).
_ROUTE_STRUCTURE_FIELDS = ("route", "legs", "stopped_at", "waypoints")

# The one thing a redacted route is allowed to say.
REDACTED_ROUTE_NOTE = "Route passes through unexplored territory"


def scrub_preview_for_player(result: dict, location_map: dict) -> dict:
    """Strip everything a player may not know from a travel preview.

    `preview_travel` runs on the engine's full map. Real travel legitimately
    passes through places the protagonist has not discovered, and the journey
    still happens - so the preview must keep saying `available`, and it may keep
    the total duration and an overall risk level, which describe the trip the
    player is about to take rather than the ground it crosses.

    What it may not do is reveal the route's *shape*. An earlier version only
    replaced hidden names with a neutral label and kept `route` and `legs`
    intact; that still leaked the stop count (`Start -> Unknown location -> City`
    announces one hidden waypoint) and the per-leg `tags`, which carried
    `secret_tunnel` / `hidden_passage` straight into the public response.

    So: if the route touches a hidden place at all, every structural field is
    dropped, no per-leg metadata survives, and `route_redacted` is set so the UI
    has an explicit signal to render "passes through unexplored territory"
    instead of inventing a direct road. A fully visible route is returned
    untouched, per-leg preview and all.
    """
    if not isinstance(result, dict):
        return result

    hidden_ids, hidden_names, hidden_tags = _hidden_identity(location_map)
    if not hidden_ids and not hidden_names and not hidden_tags:
        return result

    # Which places on this route are hidden? Compare by id where the preview
    # carries one, by name otherwise. The engine resolves both spellings.
    def _is_hidden_ref(*candidates) -> bool:
        for value in candidates:
            text = str(value or "").strip()
            if not text:
                continue
            if text in hidden_ids or text in hidden_names:
                return True
        return False

    origin = result.get("origin")
    destination = result.get("destination")
    # `destination` is public: the player chose it. A hidden *destination* was
    # already refused upstream, so only the middle of the route can be hidden.
    touches_hidden = False
    route_items = result.get("route")
    if isinstance(route_items, list):
        for index, item in enumerate(route_items):
            if index == 0 or index == len(route_items) - 1:
                continue
            if _is_hidden_ref(item):
                touches_hidden = True
                break
    if not touches_hidden:
        for leg in result.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            if _is_hidden_ref(leg.get("from")) or _is_hidden_ref(leg.get("to")):
                touches_hidden = True
                break
    # Any per-leg tag drawn from a hidden place's vocabulary is a leak on its
    # own, even if the endpoints look clean.
    if not touches_hidden and hidden_tags:
        for leg in result.get("legs") or []:
            if not isinstance(leg, dict):
                continue
            for tag in leg.get("tags") or []:
                if str(tag) in hidden_tags:
                    touches_hidden = True
                    break
            if touches_hidden:
                break

    if not touches_hidden:
        # Fully visible route: keep the per-leg preview exactly as it was.
        return result

    for field in _ROUTE_STRUCTURE_FIELDS:
        if field in result:
            result[field] = []

    result["route_redacted"] = True
    result["route_note"] = REDACTED_ROUTE_NOTE
    # Keep origin/destination only when they are genuinely visible.
    if origin is not None and _is_hidden_ref(origin):
        result.pop("origin", None)
    if destination is not None and _is_hidden_ref(destination):
        result.pop("destination", None)
    # The protagonist standing in a hidden place still knows where they are, so
    # `destination_id` survives only if the destination itself is visible.
    if result.get("destination") is None:
        result.pop("destination_id", None)

    # Risk survives as a level (the trip's difficulty is the player's business),
    # but never as tag names: a tag like `secret_tunnel` names the hidden place.
    risk = result.get("risk")
    if isinstance(risk, dict):
        result["risk"] = {"level": risk.get("level", "unknown"), "known_tags": []}
    return result


def _hidden_identity(location_map: dict):
    """Ids, names and tags belonging to places the player must not learn about."""
    hidden_ids, hidden_names, hidden_tags = set(), set(), set()
    for loc in _locations(location_map):
        if not is_hidden(loc):
            continue
        loc_id = str(loc.get("id") or "").strip()
        if loc_id:
            hidden_ids.add(loc_id)
        name = str(loc.get("name") or "").strip()
        if name:
            hidden_names.add(name)
        for tag in loc.get("tags") or []:
            if isinstance(tag, str) and tag:
                hidden_tags.add(tag)
    return hidden_ids, hidden_names, hidden_tags


def _continue_journey_plan(active_journey: dict, *, tick_minutes: int = 60):
    if not isinstance(active_journey, dict) or active_journey.get("status") != "interrupted":
        return None
    remaining_legs = [dict(leg) for leg in active_journey.get("remaining_legs", []) if isinstance(leg, dict)]
    if not remaining_legs:
        return None
    minutes = sum(max(1, int(leg.get("travel_time_minutes", 1) or 1)) for leg in remaining_legs)
    return {
        "status": "arrived", "reason": "continued_journey",
        "origin": active_journey.get("stopped_at") or active_journey.get("origin"),
        "destination": active_journey.get("destination"),
        "destination_id": active_journey.get("destination_id"),
        "route": active_journey.get("remaining_route", []), "legs": remaining_legs,
        "elapsed_minutes": minutes,
        "tick_advance": max(1, math.ceil(minutes / max(1, tick_minutes))),
        "narration_mode": "brief" if minutes <= 30 else "timeskip" if minutes <= 480 else "journey",
        "journey_id": active_journey.get("journey_id"),
    }


def build_travel_plan(user_input: str, location_map: dict, current_location: str,
                      *, tick_minutes: int = 60, seed_key: str = "", active_journey=None,
                      destination_id: str = ""):
    """Resolve a travel input into a plan.

    `destination_id` lets a caller that has already worked out the destination
    (see `app.world.movement`, which recognises more phrasings than
    `extract_travel_destination` does) skip the phrase-matching step without
    changing what the phrase matcher accepts for everyone else.
    """
    if re.match(r"^abandon\s+(?:the\s+)?journey[.!]?$", str(user_input or "").strip(), re.I):
        if isinstance(active_journey, dict) and active_journey.get("status") == "interrupted":
            return {"status": "abandoned", "reason": "player_abandoned_journey",
                    "origin": active_journey.get("origin"),
                    "destination": active_journey.get("destination"),
                    "stopped_at": active_journey.get("stopped_at", current_location),
                    "elapsed_minutes": 0, "tick_advance": 1,
                    "journey_id": active_journey.get("journey_id")}
        return None
    if re.match(r"^continue\s+(?:the\s+)?journey[.!]?$", str(user_input or "").strip(), re.I):
        return _continue_journey_plan(active_journey, tick_minutes=tick_minutes)
    destination = find_location(location_map, destination_id) if destination_id else None
    if not destination:
        destination = extract_travel_destination(user_input, location_map)
    if not destination:
        return None
    result = preview_travel(location_map, current_location, destination.get("id"), tick_minutes=tick_minutes)
    if result.get("status") not in ("available", "already_there"):
        result["status"] = "blocked"
        result["tick_advance"] = 1
        return result
    result["status"] = "arrived"
    result["tick_advance"] = result.pop("estimated_ticks", 0) or 1
    legs = result["legs"]
    for index, leg in enumerate(legs):
        danger = leg["danger"]
        if danger <= 0:
            continue
        digest = hashlib.sha256(f"{seed_key}|{index}|{leg['from']}|{leg['to']}".encode("utf-8")).hexdigest()
        roll = int(digest[:12], 16) / float(0xFFFFFFFFFFFF)
        if roll < danger:
            elapsed_before = sum(previous["travel_time_minutes"] for previous in legs[:index])
            elapsed = elapsed_before + max(1, leg["travel_time_minutes"] // 2)
            result.update(
                status="interrupted", reason="travel_encounter",
                stopped_at=leg["from"],
                elapsed_minutes=elapsed,
                tick_advance=max(1, math.ceil(elapsed / max(1, tick_minutes))),
                interrupted_leg=leg, danger_roll=round(roll, 6),
            )
            remaining_first = dict(leg)
            remaining_first["travel_time_minutes"] = max(1, leg["travel_time_minutes"] - max(1, leg["travel_time_minutes"] // 2))
            result["remaining_legs"] = [remaining_first] + [dict(value) for value in legs[index + 1:]]
            result["remaining_route"] = result["route"][index:]
            break
    return result


def advance_clock_minutes(clock: dict, minutes: int, calendar_config: dict = None) -> None:
    from app.world.calendar_clock import advance_story_clock
    if not isinstance(clock, dict) or minutes <= 0:
        return
    advance_story_clock(clock, {"total_seconds": int(minutes) * 60}, calendar_config)
