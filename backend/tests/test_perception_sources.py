"""Actor observations must not receive the author's planned future scene."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.psychology import generate_perception_for_character
from app.story.views import narrative_character_view


class PerceptionSourceTests(unittest.TestCase):
    def test_future_checkpoint_plot_and_other_npc_secrets_never_enter_actor_payload(self):
        characters = {
            'guard': {'name': 'Guard', 'location': 'Reception',
                      'knowledge': [{'statement': 'The left door is sealed.',
                                     'source': {'kind': 'witnessed', 'who': None},
                                     'confidence': 1.0}],
                      'speech_style': 'Patient, concrete directions.'},
            'visitor': {'name': 'Visitor', 'location': 'Reception',
                        'knowledge': [{'statement': 'PRIVATE_PASSWORD'}]},
        }
        checkpoint = {'description': 'FUTURE_BETRAYAL: the guard later betrays the visitor.',
                      'playable_situation': 'A visitor waits at reception.'}
        original = copy.deepcopy(characters)
        for mode in ('classic', 'experimental'):
            captured = []

            def provider(system, prompt, **kwargs):
                captured.append(json.loads(prompt))
                return json.dumps({'perception': 'The visitor waited on their side of the cordon.'})

            with patch('app.psychology.call_llm', side_effect=provider):
                generate_perception_for_character(
                    'guard', characters['guard'],
                    'The visitor asked about the door and remained on their side of the cordon.',
                    {}, checkpoint, characters, narration_mode=mode)
            payload = captured[0]
            serialized = json.dumps(payload)
            self.assertNotIn('FUTURE_BETRAYAL', serialized)
            self.assertNotIn('PRIVATE_PASSWORD', serialized)
            self.assertNotIn('checkpoint_description', payload)
            self.assertEqual(payload['character_knowledge'][0]['statement'], 'The left door is sealed.')
            self.assertEqual(payload['character_knowledge'][0]['source']['kind'], 'witnessed')
            self.assertIn('remained on their side', payload['chapter_text'])
        self.assertEqual(characters, original)

    def test_shared_writer_view_retains_voice_without_raw_private_memory(self):
        visible = narrative_character_view({'guard': {
            'name': 'Guard', 'speech_style': 'Patient, concrete directions.',
            'personality': 'Careful', 'goals': ['Keep visitors safe'],
            'knowledge': [{'statement': 'PRIVATE_PASSWORD'}],
            'psychology': {'beliefs': {'PRIVATE_SUSPICION': 1.0}},
            'relationship_memories': [{'statement': 'PRIVATE_MEMORY'}],
        }})['guard']
        self.assertEqual(visible['speech_style'], 'Patient, concrete directions.')
        self.assertEqual(visible['goals'], ['Keep visitors safe'])
        self.assertNotIn('PRIVATE_', json.dumps(visible))


if __name__ == '__main__':
    unittest.main()
