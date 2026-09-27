"""The ensemble opt-in must not turn shared context into NPC omniscience."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.story.ensemble import build_committed_scene_record, call_actor_stages, project_scene_payload
from app.story.ensemble import ACTOR_SYSTEM_PROMPT
from app.story.narration_mode import is_ensemble, normalize_narration_mode, select_writer_prompt


class EnsembleProjectionTests(unittest.TestCase):
    def setUp(self):
        self.characters = {
            'hero': {'name': 'Hero', 'location': 'Station', 'alive': True,
                     'backstory': 'FUTURE_SECRET',
                     'knowledge': [{'fact_id': 'seen', 'statement': 'A bell rang.'}]},
            'aya': {'name': 'Aya', 'location': 'Station', 'alive': True,
                    'personality': 'patient', 'goals': ['Return a parcel'],
                    'psychology': {'mood': 'curious'},
                    'knowledge': [{'fact_id': 'aya_private', 'statement': 'AYA_SECRET'}]},
            'ben': {'name': 'Ben', 'location': 'Station', 'alive': True,
                    'knowledge': [{'fact_id': 'ben_private', 'statement': 'BEN_SECRET'}]},
        }
        self.facts = [
            {'fact_id': 'seen', 'statement': 'A bell rang.', 'scope': 'public'},
            {'fact_id': 'aya_private', 'statement': 'AYA_SECRET', 'scope': 'world'},
            {'fact_id': 'ben_private', 'statement': 'BEN_SECRET', 'scope': 'world'},
            {'fact_id': 'future', 'statement': 'FUTURE_SECRET', 'scope': 'world'},
        ]

    def test_director_and_writer_projection_drop_hidden_global_and_other_private_context(self):
        payload = {
            'world_canon_facts': [f['statement'] for f in self.facts],
            'character_knowledge': {'aya': ['AYA_SECRET'], 'ben': ['BEN_SECRET']},
            'relationship_context': {'aya': ['AYA_SECRET']},
            'world_config': {'genre': 'mystery', 'story_thesis': 'FUTURE_SECRET',
                             'fixed_rules': ['BEN_SECRET'], 'protagonist_id': 'hero'},
            'current_checkpoint': {'checkpoint_id': 'cp', 'description': 'FUTURE_SECRET',
                                   'allowed_locations': ['FUTURE_SECRET']},
            'active_cards': [{'id': 'card', 'type': 'lore', 'name': 'Notice',
                              'content': 'FUTURE_SECRET'}],
            'multi_tier_context': {'multi_tier_context': {
                'tier_1_working_memory': {'recent_turns': ['Published action']},
                'tier_2_rolling_summary': {'summary': 'Published history'},
                'tier_4_thread_ledger': {'foreshadowing_tracker': ['FUTURE_SECRET']},
            }},
            'user_input': 'Watch the counter.', 'action_resolution': {'result': 'success'},
        }
        result = project_scene_payload(payload, self.characters, self.facts, 'hero')
        encoded = json.dumps(result)
        for secret in ('AYA_SECRET', 'BEN_SECRET', 'FUTURE_SECRET'):
            self.assertNotIn(secret, encoded)
        self.assertIn('A bell rang.', encoded)
        self.assertIn('Published action', encoded)
        self.assertNotIn('relationship_context', result)
        self.assertNotIn('goals', result['character_state']['aya'])

    def test_recent_turn_projection_uses_published_text_and_committed_record_not_notes(self):
        payload = {
            'multi_tier_context': {'multi_tier_context': {
                'tier_1_working_memory': {'recent_turns': [{
                    'chapter_index': 2,
                    'turn_index': 3,
                    'user_input': 'Check the phone.',
                    'chapter_text': 'The phone remained on its cradle.',
                    'notes': 'FALSE_CANON: Kenji called Nakagawa.',
                    'scene_record': {'character_locations_after_scene': {'kenji': 'Workshop'}},
                    'consistency_check': {'issues': ['internal diagnostic']},
                }]},
            }},
        }
        result = project_scene_payload(payload, self.characters, self.facts, 'hero')
        turns = result['multi_tier_context']['multi_tier_context']['tier_1_working_memory']['recent_turns']
        encoded = json.dumps(turns)
        self.assertIn('The phone remained on its cradle.', encoded)
        self.assertIn('Workshop', encoded)
        self.assertNotIn('FALSE_CANON', encoded)
        self.assertNotIn('consistency_check', encoded)

    def test_committed_scene_record_captures_engine_state_and_public_outcomes(self):
        record = build_committed_scene_record(
            {'characters': {
                'hero': {'location': 'Workshop'},
                'aya': {'location': 'Front Counter'},
            }},
            {'hero', 'aya'},
            {'intent': 'inspect the invoice', 'result': 'success', 'secret_reason': 'PRIVATE'},
            {'status': 'success', 'action': 'inspect', 'item_name': 'invoice',
             'private_roll': 99},
            {'status': 'arrived', 'destination': 'Workshop', 'elapsed_minutes': 5,
             'hidden_route': ['PRIVATE']},
            {'elapsed_minutes': 15, 'tick_advance': 3, 'hidden_event': 'PRIVATE'},
            {'minutes': 5},
        )
        self.assertEqual(record['character_locations_after_scene'], {
            'hero': 'Workshop', 'aya': 'Front Counter',
        })
        self.assertEqual(record['action_outcome'], {
            'intent': 'inspect the invoice', 'result': 'success',
        })
        self.assertNotIn('PRIVATE', json.dumps(record))

    def test_each_present_actor_gets_only_their_own_private_knowledge(self):
        calls = []

        def fake_call(_system, user_prompt, **_kwargs):
            calls.append(json.loads(user_prompt))
            return json.dumps({'visible_behavior': 'Waits.', 'possible_dialogue': 'Hello.'})

        with patch('app.story.ensemble.call_llm', side_effect=fake_call):
            cues = call_actor_stages(
                self.characters, {key: self.characters[key] for key in ('hero', 'aya', 'ben')},
                'hero', 'Hero looks at the counter.', 'Station', 'test', self.facts,
                {'aya': [{'prior_perception': 'Aya heard the bell.'}],
                 'ben': [{'prior_perception': 'Ben saw the train.'}]})
        self.assertEqual(set(cues), {'aya', 'ben'})
        self.assertEqual(len(calls), 2)
        aya = json.dumps(calls[0])
        ben = json.dumps(calls[1])
        self.assertIn('AYA_SECRET', aya)
        self.assertNotIn('BEN_SECRET', aya)
        self.assertNotIn('FUTURE_SECRET', aya)
        self.assertIn('BEN_SECRET', ben)
        self.assertNotIn('AYA_SECRET', ben)
        self.assertNotIn('FUTURE_SECRET', ben)
        self.assertNotIn('knowledge', calls[0]['other_people_present']['ben'])
        self.assertIn('Aya heard the bell.', aya)
        self.assertNotIn('Ben saw the train.', aya)

    def test_ensemble_is_separate_opt_in(self):
        self.assertEqual(normalize_narration_mode('ensemble'), 'ensemble')
        self.assertTrue(is_ensemble('ensemble'))
        self.assertFalse(is_ensemble('experimental'))
        self.assertFalse(is_ensemble(None))
        self.assertIn('Never invent a prior phone call', ACTOR_SYSTEM_PROMPT)
        self.assertIn('retroactive phone call', select_writer_prompt('ensemble'))


if __name__ == '__main__':
    unittest.main()
