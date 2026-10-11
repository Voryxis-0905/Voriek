"""Post-scene checkpoint effects must not manufacture player travel."""
import copy
import sys
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.checkpoint_engine import advance_checkpoint_if_ready
from app.world.movement import location_change_authorised
from app.world.boundaries import raise_boundary_hard_reject
from fastapi import HTTPException

class CheckpointPlayerLocationTests(unittest.TestCase):
    def fixture(self, desired="Supply Zone"):
        timeline = {"checkpoints": [
            {"checkpoint_id": "alert", "sub_beats": [],
             "alternate_outcomes": [{"next_checkpoint_id": "shelter", "apply": {
                 "characters": {"pc": {"location": desired, "knowledge_flags_add": ["alert heard"]},
                                "staff": {"location": "Supply Zone"}}}}]},
            {"checkpoint_id": "shelter", "required_conditions": [], "cards_unlocked": []}]}
        config = {"protagonist_id": "pc", "current_checkpoint_id": "alert"}
        chars = {"pc": {"location": "Base Zone (plant room)", "knowledge_flags": []},
                 "staff": {"location": "Base Zone"}}
        return timeline, config, chars

    def test_hsr_style_outcome_preserves_confirmed_player_scene(self):
        timeline, config, chars = self.fixture()
        original = copy.deepcopy(timeline)
        result = advance_checkpoint_if_ready(timeline, config, chars, {"cards": []}, chapter_closed=True)
        self.assertEqual(chars["pc"]["location"], "Base Zone (plant room)")
        self.assertEqual(chars["pc"]["knowledge_flags"], ["alert heard"])
        self.assertEqual(chars["staff"]["location"], "Supply Zone")
        self.assertEqual(config["current_checkpoint_id"], "shelter")
        self.assertEqual(result["ignored_location_changes"][0]["requested_location"], "Supply Zone")
        self.assertEqual(timeline, original)

    def test_checkpoint_does_not_coarsen_a_confirmed_arrival(self):
        timeline, config, chars = self.fixture()
        chars["pc"]["location"] = "Supply Zone - Railway Platform"
        advance_checkpoint_if_ready(timeline, config, chars, {"cards": []}, chapter_closed=True)
        self.assertEqual(chars["pc"]["location"], "Supply Zone - Railway Platform")

    def test_already_confirmed_exact_location_needs_no_ignored_effect(self):
        timeline, config, chars = self.fixture()
        chars["pc"]["location"] = "Supply Zone"
        result = advance_checkpoint_if_ready(timeline, config, chars, {"cards": []}, chapter_closed=True)
        self.assertEqual(chars["pc"]["location"], "Supply Zone")
        self.assertEqual(result["ignored_location_changes"], [])

    def test_outcome_cannot_bypass_real_entry_gate(self):
        timeline, config, chars = self.fixture()
        timeline["checkpoints"][1]["entry_location"] = "Supply Zone"
        result = advance_checkpoint_if_ready(timeline, config, chars, {"cards": []}, chapter_closed=True)
        self.assertIsNone(result["to_checkpoint_id"])
        self.assertEqual(chars["pc"]["knowledge_flags"], [])
        self.assertEqual(chars["pc"]["location"], "Base Zone (plant room)")

    def test_cast_promotion_cannot_teleport_the_protagonist(self):
        timeline, config, chars = self.fixture()
        chars["pc"].update(location="", presence="not_yet_in_campaign",
                           introduced_at_checkpoint="shelter", promote_location="Supply Zone")
        advance_checkpoint_if_ready(timeline, config, chars, {"cards": []}, chapter_closed=True)
        self.assertEqual(chars["pc"]["location"], "")

class LocalMovementTests(unittest.TestCase):
    def check(self, proposed, places=None):
        return location_change_authorised(origin="Base Zone (plant room)", proposed=proposed,
            travel_resolution={"status": "not_a_move"}, location_map={"locations": places or [
                {"id": "base", "name": "Base Zone"}, {"id": "supply", "name": "Supply Zone"}]})

    def test_hsr_corridor_step_is_local_movement(self):
        self.assertTrue(self.check("Base Zone (north service corridor)")["ok"])
        self.assertFalse(self.check("Supply Zone")["ok"])
        self.assertFalse(self.check("Base Zone Annex")["ok"])

    def test_known_separate_subarea_is_not_a_sibling_room(self):
        places = [{"id": "base", "name": "Base Zone"},
                  {"id": "vault", "name": "Base Zone (Restricted Vault)"}]
        self.assertFalse(self.check("Base Zone (Restricted Vault)", places)["ok"])

    def test_movement_error_names_both_endpoints(self):
        with self.assertRaises(HTTPException) as caught:
            raise_boundary_hard_reject([{"kind": "unsupported_protagonist_movement",
                "reason": "unsupported_location_change", "origin": "Base Zone", "proposed": "Supply Zone"}], {}, lang="en")
        self.assertEqual(caught.exception.status_code, 409)
        self.assertIn("Base Zone", caught.exception.detail)
        self.assertIn("Supply Zone", caught.exception.detail)
        self.assertNotIn("?", caught.exception.detail)

class TravelOriginTests(unittest.TestCase):
    def test_existing_room_label_anchors_a_real_map_route(self):
        from app.world.travel import find_route, find_location
        places = {"locations": [
            {"id": "base", "name": "Base Zone", "connected_to": [{"location_id": "supply"}]},
            {"id": "supply", "name": "Supply Zone", "connected_to": []}]}
        route = find_route(places, "Base Zone (north service corridor)", "Supply Zone")
        self.assertIsNotNone(route)
        self.assertIsNone(find_location(places, "Supply Zone Annex"))

class MovementIntentTests(unittest.TestCase):
    def setUp(self):
        self.places = {"locations": [
            {"id": "base", "name": "Base Zone", "connected_to": [{"location_id": "supply", "travel_time_minutes": 10}]},
            {"id": "supply", "name": "Supply Zone", "connected_to": []}]}

    def test_recorded_sign_observation_cannot_become_an_arrival(self):
        from app.world.movement import resolve_movement
        action = ("Rinn looks for an evacuation sign visible from where he stands in the Supply Zone. "
                  "He does not set off, board a vessel or follow anyone. Stop after the observation.")
        result = resolve_movement(action, self.places, "Supply Zone")
        self.assertEqual(result['status'], 'not_a_move')
        self.assertEqual(result['movement_kind'], 'attempt')
        self.assertEqual(result['companion'], '')
        self.assertNotIn('elapsed_minutes', result)

    def test_negated_lists_do_not_authorise_travel(self):
        from app.world.movement import classify_movement
        for action in ["Do not board a vessel or follow Arlan to Supply Zone.",
                       "He doesn't set off, board or follow anyone towards Supply Zone.",
                       "Rinn follows no new instruction to Supply Zone."]:
            with self.subTest(action=action):
                self.assertEqual(classify_movement(action), {'kind': 'refuse', 'companion': ''})

    def test_real_travel_survives_an_unrelated_negated_clause(self):
        from app.world.movement import resolve_movement
        for action in ["Go to Supply Zone. Do not board a vessel or follow anyone.",
                       "Do not board a ship, but walk to Supply Zone instead."]:
            with self.subTest(action=action):
                result = resolve_movement(action, self.places, 'Base Zone')
                self.assertEqual(result['status'], 'arrived')
                self.assertEqual(result['destination'], 'Supply Zone')
                self.assertEqual(result['elapsed_minutes'], 10)

    def test_inflected_observation_and_real_companion_stay_distinct(self):
        from app.world.movement import classify_movement
        for action in ['Rinn looks toward Supply Zone.', 'Rinn searches for a route to Supply Zone.']:
            self.assertNotEqual(classify_movement(action)['kind'], 'commit')
        self.assertEqual(classify_movement('I follow Arlan to Supply Zone.'),
                         {'kind': 'commit', 'companion': 'Arlan'})


class CompoundMovementTests(unittest.TestCase):
    def setUp(self):
        self.places = {"locations": [
            {"id": "base", "name": "Base Zone", "connected_to": [{"location_id": "supply", "travel_time_minutes": 10}]},
            {"id": "supply", "name": "Supply Zone", "connected_to": [{"location_id": "base", "travel_time_minutes": 10}]}]}

    def resolve(self, action, origin='Base Zone'):
        from app.world.movement import resolve_movement
        return resolve_movement(action, self.places, origin)

    def test_arrival_followed_by_wait_does_not_cancel_the_trip(self):
        for action in ['Go to Supply Zone and wait there.', 'Walk to Supply Zone, then wait.',
                       'I go to Supply Zone and then remain there.', 'Walk to Supply Zone. Wait there.']:
            with self.subTest(action=action):
                result = self.resolve(action)
                self.assertEqual(result['status'], 'arrived')
                self.assertEqual(result['destination'], 'Supply Zone')
                self.assertEqual(result['elapsed_minutes'], 10)

    def test_negated_other_destination_cannot_replace_the_committed_endpoint(self):
        result = self.resolve('Go to Base Zone. Do not follow anyone to Supply Zone.', 'Supply Zone')
        self.assertEqual(result['destination'], 'Base Zone')
        self.assertEqual(result['elapsed_minutes'], 10)
        self.assertEqual(result['companion'], '')

    def test_from_origin_to_destination_chooses_direction_not_longest_mention(self):
        result = self.resolve('Walk from Supply Zone to Base Zone.', 'Supply Zone')
        self.assertEqual(result['destination'], 'Base Zone')
        self.assertEqual(result['elapsed_minutes'], 10)

    def test_questions_plans_and_waiting_for_permission_still_do_not_depart(self):
        for action in ['Ask how to go to Supply Zone and then wait for an answer.',
                       'I plan to go to Supply Zone later and wait there.',
                       'Wait for March to go to Supply Zone.',
                       'Wait for permission, then go to Supply Zone.',
                       'Do not go to Supply Zone and wait here.']:
            with self.subTest(action=action):
                self.assertEqual(self.resolve(action)['status'], 'not_a_move')
