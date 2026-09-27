"""Experimental actor cues and post-scene witness/goal reconciliation."""
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.chapter_generator import _recent_confirmed_turns
from app.prompts import EXPERIMENTAL_PSYCHOLOGY_UPDATE_PROMPT
from app.psychology import apply_psychology_changes, update_psychology_for_character
from app.story.observer import completed_scene_witnesses, character_mention_offset


class CompletedSceneWitnessTests(unittest.TestCase):
    def test_unique_first_name_counts_as_a_named_destination_witness(self):
        characters = {
            'jun': {'name': 'Jun Arai', 'location': 'Shop - Workshop', 'alive': True},
            'kenji': {'name': 'Kenji Arai', 'location': 'Shop - Workshop', 'alive': True},
        }
        result = completed_scene_witnesses(
            {'jun': characters['jun']}, {'characters': characters}, 'Shop - Workshop',
            'Jun returned and showed Kenji the label.',
            protagonist_id='jun', allowed_ids=set(characters),
        )
        self.assertIn('kenji', result)
        text = 'Jun returned. Only then did Kenji answer him.'
        self.assertEqual(character_mention_offset(text, 'kenji', characters), text.index('Kenji'))

    def test_shared_first_name_does_not_guess_which_npc_witnessed(self):
        characters = {
            'jun': {'name': 'Jun Arai', 'location': 'Shop - Workshop', 'alive': True},
            'kenji': {'name': 'Kenji Arai', 'location': 'Shop - Workshop', 'alive': True},
            'other': {'name': 'Kenji Mori', 'location': 'Shop - Workshop', 'alive': True},
        }
        result = completed_scene_witnesses(
            {'jun': characters['jun']}, {'characters': characters}, 'Shop - Workshop',
            'Jun returned and showed Kenji the label.',
            protagonist_id='jun', allowed_ids=set(characters),
        )
        self.assertEqual(list(result), ['jun'])

    def test_named_npc_at_destination_joins_but_unnamed_bystander_does_not(self):
        characters = {
            'jun': {'name': 'Jun', 'location': 'Shop - Counter', 'alive': True},
            'kenji': {'name': 'Kenji', 'location': 'Shop - Counter', 'alive': True},
            'stranger': {'name': 'Stranger', 'location': 'Shop - Counter', 'alive': True},
        }
        result = completed_scene_witnesses(
            {'jun': characters['jun']}, {'characters': characters}, 'Shop - Counter',
            'Jun walked to the counter and asked Kenji about the order.',
            protagonist_id='jun', allowed_ids=set(characters),
        )
        self.assertEqual(list(result), ['jun', 'kenji'])


class ExperimentalPsychologyTests(unittest.TestCase):
    def setUp(self):
        self.character = {
            'name': 'Kenji',
            'psychology': {'goals': ['Send Jun to find the model number', 'Keep the shop open']},
        }
        self.confirmed = [{
            'player_action': 'Jun found the second model number.',
            'result': 'success',
            'scene_excerpt': 'Jun found it on the shelf and showed Kenji.',
        }]

    def test_experimental_update_removes_only_an_existing_exact_goal(self):
        model_response = json.dumps({
            'goal_updates': ['Call the supplier'],
            'goal_removals': ['Send Jun to find the model number', 'Invented goal'],
        })
        with patch('app.psychology.call_llm', return_value=model_response) as llm:
            update = update_psychology_for_character(
                'kenji', self.character, 'Jun sat at the counter.', {}, {},
                narration_mode='experimental',
                recent_confirmed_turns=self.confirmed,
            )
        self.assertEqual(llm.call_args.args[0], EXPERIMENTAL_PSYCHOLOGY_UPDATE_PROMPT)
        self.assertEqual(json.loads(llm.call_args.args[1])['recent_confirmed_turns'], self.confirmed)
        self.assertEqual(update['goal_removals'], ['Send Jun to find the model number'])
        result = apply_psychology_changes({'kenji': self.character}, {'kenji': update})
        self.assertEqual(result['kenji']['psychology']['goals'],
                         ['Keep the shop open', 'Call the supplier'])

    def test_classic_ignores_goal_removals_and_receives_no_history(self):
        with patch('app.psychology.call_llm', return_value=json.dumps({
            'goal_updates': [], 'goal_removals': ['Send Jun to find the model number'],
        })) as llm:
            update = update_psychology_for_character(
                'kenji', self.character, 'A quiet morning.', {}, {},
                narration_mode='classic', recent_confirmed_turns=self.confirmed,
            )
        self.assertNotIn('recent_confirmed_turns', json.loads(llm.call_args.args[1]))
        self.assertNotIn('goal_removals', update)

    def test_recent_context_is_bounded_and_witness_specific(self):
        turns = {'chapters': [{'psychology_status': {'subjects': ['kenji']},
                              'perception_data': {'kenji': {'perception': 'too old'}}}] + [
            {'psychology_status': {'subjects': ['kenji']},
             'perception_data': {'kenji': {'perception': f'Observed action {index}',
                                           'decision': 'keep watching'}}}
            for index in range(4)
        ]}
        # The bounded window excludes the earliest saved turn.
        context = _recent_confirmed_turns(turns, 'kenji', limit=4)
        self.assertEqual(len(context), 4)
        self.assertEqual(context[0]['prior_perception'], 'Observed action 0')
        self.assertEqual(context[0]['prior_decision'], 'keep watching')

        turns['chapters'][-1]['psychology_status']['subjects'] = ['someone_else']
        context = _recent_confirmed_turns(turns, 'kenji', limit=4)
        self.assertEqual(len(context), 3)


if __name__ == '__main__':
    unittest.main()
