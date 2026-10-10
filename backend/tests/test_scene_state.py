"""Inventory and NPC position commit evidence, including engine ownership."""
import unittest
from copy import deepcopy
from unittest.mock import patch
from app.story.scene_state import project_scene_state
from app.story.consistency import run_consistency_checker


class SceneStateTests(unittest.TestCase):
    def setUp(self):
        self.before = {"pc": {"name": "Rinn", "location": "Base Zone", "inventory": ["wrapped ration"]},
                       "stelle": {"name": "Stelle", "location": "Astral Express", "inventory": []}}

    def project(self, changes, **kwargs):
        return project_scene_state(self.before, changes, protagonist_id="pc", **kwargs)

    def test_missing_deltas_expose_actual_retained_ownership_and_offscene_position(self):
        evidence = self.project({"characters": {}})
        self.assertEqual(evidence["character_state_after_proposed_turn"], self.before)

    def test_consumed_handover_removes_ration_and_repairs_only_present_npc(self):
        self.before["march"] = {"location": "Astral Express", "inventory": []}
        original = deepcopy(self.before)
        after = self.project({"characters": {"pc": {"inventory_remove": ["wrapped ration"]},
                                              "stelle": {"location": "Base Zone"}}})
        saved = after["character_state_after_proposed_turn"]
        self.assertEqual(saved["pc"]["inventory"], [])
        self.assertEqual(saved["stelle"]["inventory"], [])
        self.assertEqual(saved["stelle"]["location"], "Base Zone")
        self.assertEqual(saved["march"]["location"], "Astral Express")
        self.assertEqual(original, self.before)

    def test_transfer_preserves_instance_metadata_and_quantity(self):
        item = {"instance_id": "r1", "name": "ration", "quantity": 2,
                "attributes": {"flavour": "berry"}, "drop_policy": "allowed"}
        self.before["pc"]["inventory"] = [deepcopy(item)]
        given = dict(item, quantity=1)
        saved = self.project({"characters": {
            "pc": {"inventory_remove": [{"instance_id": "r1", "quantity": 1}]},
            "stelle": {"inventory_add": [given]}}})["character_state_after_proposed_turn"]
        self.assertEqual(saved["pc"]["inventory"][0]["quantity"], 1)
        self.assertEqual(saved["stelle"]["inventory"], [given])
        self.assertEqual(self.before["pc"]["inventory"][0]["quantity"], 2)

    def test_invalid_removal_blocks_without_model_call(self):
        for reference in ("not owned", {"name": "wrapped ration", "quantity": 2},
                          {"name": "wrapped ration", "quantity": -1}):
            with self.subTest(reference=reference):
                evidence = self.project({"characters": {"pc": {"inventory_remove": [reference]}}})
                with patch("app.story.consistency.call_llm") as call:
                    result = run_consistency_checker("Rinn eats it.", {}, {}, {}, [], self.before,
                                                     engine_outcomes=evidence)
                self.assertEqual(result["status"], "failed")
                call.assert_not_called()

    def test_engine_owned_failed_item_command_discards_writer_consumption(self):
        saved = self.project({"characters": {"pc": {"inventory_remove": ["wrapped ration"]}}},
                             inventory_resolution={"status": "failed"})
        self.assertEqual(saved["character_state_after_proposed_turn"]["pc"]["inventory"], ["wrapped ration"])
        self.assertEqual(saved["state_projection_errors"], [])

    def test_engine_effect_and_travel_override_writer_and_do_not_mutate_plan(self):
        plan = {"effects": [{"type": "inventory_remove", "character_id": "pc", "item": "wrapped ration"}]}
        original = deepcopy(plan)
        evidence = self.project({"characters": {"pc": {"inventory_add": ["duplicate"], "location": "wrong"}}},
                                effect_plan=plan, travel_resolution={"status": "arrived", "destination": "Supply Zone"})
        self.assertEqual(evidence["character_state_after_proposed_turn"]["pc"],
                         {"name": "Rinn", "location": "Supply Zone", "inventory": []})
        self.assertEqual(plan, original)
