"""Checker evidence is saved prose, bounded and opt-in for ensemble only."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.story.consistency import build_consistency_checker_payload, recent_confirmed_scenes
from app.prompts import ENSEMBLE_CONSISTENCY_CHECKER_SYSTEM_PROMPT


class ContinuityEvidenceTests(unittest.TestCase):
    def test_only_recent_committed_prose_reaches_checker(self):
        chapters = {'chapters': [
            {'chapter_index': 1, 'turn_index': i, 'chapter_text': f'Published {i}: ' + ('x' * 20),
             'notes': 'PRIVATE_PLANNER_NOTE',
             'scene_record': {'character_locations_after_scene': {'kenji': 'Workshop'}}}
            for i in range(5)
        ]}
        result = recent_confirmed_scenes(chapters, limit=3, max_chars_per_scene=15)
        self.assertEqual([scene['turn_index'] for scene in result], [2, 3, 4])
        self.assertTrue(all(len(scene['chapter_text']) <= 15 for scene in result))
        self.assertNotIn('PRIVATE_PLANNER_NOTE', str(result))
        self.assertEqual(result[-1]['scene_record']['character_locations_after_scene']['kenji'],
                         'Workshop')

    def test_ensemble_has_evidence_and_locations_but_classic_payload_does_not(self):
        kwargs = ('Kenji answered from the front.', {'characters': {}},
                  {'protagonist_id': 'jun'}, {}, [],
                  {'jun': {'location': 'Workshop'}, 'kenji': {'location': 'Workshop'}})
        classic = build_consistency_checker_payload(*kwargs)
        ensemble = build_consistency_checker_payload(
            *kwargs, recent_scenes=[{'chapter_text': 'Kenji was in the workshop.'}])
        self.assertNotIn('recent_confirmed_scenes', classic)
        self.assertNotIn('character_locations_before_chapter', classic)
        self.assertEqual(ensemble['character_locations_before_chapter']['kenji'], 'Workshop')
        self.assertIn('Kenji was in the workshop.', str(ensemble['recent_confirmed_scenes']))
        self.assertIn('BOTH the earlier and current passages',
                      ENSEMBLE_CONSISTENCY_CHECKER_SYSTEM_PROMPT)
        self.assertIn('character_locations_before_chapter', ENSEMBLE_CONSISTENCY_CHECKER_SYSTEM_PROMPT)

    def test_ensemble_checker_does_not_treat_free_form_notes_as_proposed_canon(self):
        state_changes = {
            'characters': {'kenji': {'location': 'Workshop'}},
            'notes': 'FALSE_CANON: Kenji called Nakagawa.',
        }
        payload = build_consistency_checker_payload(
            'The phone remained on its cradle.', state_changes, {'protagonist_id': 'jun'},
            {}, [], {'kenji': {'location': 'Workshop'}},
            recent_scenes=[{'chapter_text': 'Kenji said he would call tomorrow.'}],
        )
        self.assertNotIn('notes', payload['proposed_state_changes'])
        self.assertIn('FALSE_CANON', state_changes['notes'])


if __name__ == '__main__':
    unittest.main()
