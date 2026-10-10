"""Side-effect-free location/inventory evidence for the state actually committed."""
from copy import deepcopy
from app.story.inventory import (add_inventory_item, remove_inventory_item,
                                find_inventory_item, apply_inventory_resolution)
from app.story.action_effects import apply_action_effects


def remove_engine_owned_inventory(changes, protagonist_id, inventory_resolution, effect_plan):
    if inventory_resolution or any(e.get("type") in ("inventory_add", "inventory_remove")
                                   for e in effect_plan.get("effects", [])):
        player = changes.setdefault("characters", {}).setdefault(protagonist_id, {})
        player.pop("inventory_add", None)
        player.pop("inventory_remove", None)


def verification_view(characters):
    return {cid: deepcopy({key: state[key] for key in
                          ("name", "location", "inventory", "alive", "knowledge_flags")
                          if key in state})
            for cid, state in characters.items() if isinstance(state, dict)}


def project_scene_state(characters, changes, *, protagonist_id, inventory_resolution=None,
                        travel_resolution=None, declared_move=None, time_skip_resolution=None,
                        effect_plan=None, opening_setup=False):
    after = verification_view(characters)
    delta = deepcopy(changes)
    plan = effect_plan or {"effects": []}
    remove_engine_owned_inventory(delta, protagonist_id, inventory_resolution, plan)
    errors = []
    if not opening_setup:
        for cid, fields in delta.get("characters", {}).items():
            if cid not in after or not isinstance(fields, dict):
                continue
            target = after[cid]
            if fields.get("location"):
                target["location"] = fields["location"]
            inventory = target.setdefault("inventory", [])
            # Keep the same add-then-remove ordering as apply_state_changes.
            for item in fields.get("inventory_add", []):
                add_inventory_item(inventory, deepcopy(item))
            for item in fields.get("inventory_remove", []):
                owned = find_inventory_item(inventory, item)
                try:
                    quantity = item.get("quantity", 1) if isinstance(item, dict) else 1
                    available = owned.get("quantity", 1) if isinstance(owned, dict) else 1
                    valid = (owned is not None and isinstance(quantity, int)
                             and not isinstance(quantity, bool) and 0 < quantity <= int(available))
                except (TypeError, ValueError):
                    valid = False
                if not valid:
                    errors.append(f"{cid}: inventory_remove must reference an owned item and available positive quantity: {item}")
                else:
                    remove_inventory_item(inventory, item)
        player = after.setdefault(protagonist_id, {})
        travel = travel_resolution or {}
        if travel.get("status") in ("arrived", "interrupted"):
            player["location"] = (travel.get("destination") if travel["status"] == "arrived"
                                  else travel.get("stopped_at", characters.get(protagonist_id, {}).get("location")))
        elif time_skip_resolution:
            player["location"] = characters.get(protagonist_id, {}).get("location")
        elif declared_move:
            player["location"] = declared_move
        if inventory_resolution:
            apply_inventory_resolution(player.setdefault("inventory", []), deepcopy(inventory_resolution))
    # These engine effects also run on opening setup; isolate config/event writes.
    apply_action_effects(deepcopy(plan), "", protagonist_id, after, {}, {}, at_tick=0)
    return {"character_state_after_proposed_turn": after, "state_projection_errors": errors}
