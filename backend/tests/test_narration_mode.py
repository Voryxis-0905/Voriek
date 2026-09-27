"""The experimental narration switch: opt-in, per request, never destructive.

Contract under test:

* A request that names no mode takes the classic path. The mode is not merely
  hidden in the UI - the server defaults to the old prompts on its own.
* A request that asks for ``experimental`` uses the experimental planner and
  writer guidance, and *only* that guidance changes: the payload the engine
  hands the models is the same engine-owned truth either way.
* Every entry point that generates prose honours the switch, so no path quietly
  falls back to the old guidance.
* The switch adds no model calls, does not rewrite already-saved turns, and
  leaves idempotent replay, revision checks and the consistency gate intact.
* An unknown mode value is rejected instead of silently downgraded.

Everything here runs against temporary worlds with the provider blocked.
"""
import difflib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient
from app import storage
from app.prompts import EXPERIMENTAL_PLANNER_SYSTEM_PROMPT
from app.prompts import EXPERIMENTAL_WRITER_SYSTEM_PROMPT
from app.prompts import PLANNER_SYSTEM_PROMPT
from app.prompts import WRITER_SYSTEM_PROMPT
from app.routes import world_routes
from app.story.narration_mode import normalize_narration_mode
from app.story.consistency import build_consistency_checker_payload
from app.prompts import ENSEMBLE_CONSISTENCY_CHECKER_SYSTEM_PROMPT


def changed_blocks(old: str, new: str):
    """Return the (old_lines, new_lines) blocks that differ between two texts.

    Used to prove the experimental prompts are the classic prompts with a small
    number of named sections swapped, rather than two independently written
    prompts that could drift apart on the rules that keep the story true.
    """
    old_lines, new_lines = old.splitlines(), new.splitlines()
    blocks = []
    matcher = difflib.SequenceMatcher(None, old_lines, new_lines)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag != 'equal':
            blocks.append((old_lines[i1:i2], new_lines[j1:j2]))
    return blocks


class NarrationPromptContract(unittest.TestCase):
    """Pure prompt level: only the narrative guidance may differ."""

    def test_only_the_named_guidance_sections_change(self):
        planner_blocks = changed_blocks(PLANNER_SYSTEM_PROMPT, EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertEqual(len(planner_blocks), 6, planner_blocks)
        self.assertTrue(
            planner_blocks[0][0][0].startswith('# PACING DISCIPLINE'),
            f'planner diff starts somewhere unexpected: {planner_blocks[0][0][0]!r}',
        )
        self.assertTrue(planner_blocks[1][0][0].lstrip().startswith('- tier_4_thread_ledger'), planner_blocks)
        self.assertTrue(planner_blocks[2][0][0].startswith('2. The JSON must have'), planner_blocks)
        self.assertTrue(planner_blocks[3][0][0].startswith('4. "scene_outline"'), planner_blocks)
        self.assertEqual(planner_blocks[4][0], [], planner_blocks)
        self.assertTrue(planner_blocks[4][1][0].startswith('8a. Treat state_changes'), planner_blocks)
        self.assertTrue(planner_blocks[5][0][0].lstrip().startswith('"boundary_check"'), planner_blocks)

        writer_blocks = changed_blocks(WRITER_SYSTEM_PROMPT, EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        starts = [block[0][0] if block[0] else '' for block in writer_blocks]
        self.assertEqual(len(writer_blocks), 7, starts)
        self.assertTrue(starts[0].startswith('8. Write enough prose'), starts)
        self.assertTrue(starts[1].startswith('11. If the payload contains a "style_card"'), starts)
        self.assertTrue(starts[2].startswith('14. The payload includes "scene_outline"'), starts)
        self.assertTrue(starts[3].lstrip().startswith('- tier_4_thread_ledger'), starts)
        # The thread rule and dialogue rule are adjacent, so the diff groups
        # them in one named guidance block.
        self.assertTrue(any(line.startswith('16. When a scene involves more than one character')
                            for line in writer_blocks[3][0]), starts)
        self.assertTrue(starts[4].startswith('22. If opening_setup is true'), starts)
        self.assertEqual(starts[5], '', starts)
        self.assertIn('After finishing chapter_text', writer_blocks[5][1][1])
        self.assertTrue(starts[6].lstrip().startswith('"draft_entities"'), starts)
        self.assertIn('"suggested_actions"', writer_blocks[6][1][1])

    def test_the_engine_owned_rules_are_identical_in_both_profiles(self):
        """The rules that keep prose consistent with engine truth must not move."""
        for prompt in (EXPERIMENTAL_PLANNER_SYSTEM_PROMPT, EXPERIMENTAL_WRITER_SYSTEM_PROMPT):
            self.assertIn('Return raw JSON only', prompt)
            self.assertIn('EXACT JSON STRUCTURE TO RETURN', prompt)

        for rule in (
            'Never invent an absurd obstacle just to force the original outcome.',
            'Keep character knowledge limited to what each character could observe',
            'The JSON must have exactly these top-level keys',
        ):
            self.assertIn(rule, EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)

        for rule in (
            'NEVER kill a character unless world_config.fixed_rules explicitly allows it.',
            'You MUST narrate that committed result and must NOT change it',
            'The engine owns location and clock changes.',
            'NEVER mix languages within a single',
            'You may only mention characters listed in the provided',
        ):
            self.assertIn(rule, EXPERIMENTAL_WRITER_SYSTEM_PROMPT)

    def test_the_experimental_prompts_relax_friction_and_stop_the_old_mandates(self):
        self.assertIn('Most turns MUST include narrative friction', PLANNER_SYSTEM_PROMPT)
        self.assertNotIn('Most turns MUST include narrative friction', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('not a per-turn requirement', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('their own motives', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('Aim for 2-3 sub-beats', PLANNER_SYSTEM_PROMPT)
        self.assertNotIn('Aim for 2-3 sub-beats', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('any exposition longer than 3 sentences must be split', PLANNER_SYSTEM_PROMPT)
        self.assertNotIn('any exposition longer than 3 sentences must be split', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('at most one core mystery', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('Use [] for a turn with no such anchors', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('"anchor_keywords": [],', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('any real obstacle encountered', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('"ongoing" means no deadline', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('without bringing it back as a final-line reminder', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertNotIn('advance or resolve them within deadlines', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('playable motives, not guaranteed actions', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('Never promote a guess', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)
        self.assertIn('state_changes as a provisional estimate', EXPERIMENTAL_PLANNER_SYSTEM_PROMPT)

        # The writer no longer treats the word target as a quota, and no longer
        # treats the style card as a template every scene must copy.
        self.assertIn('target roughly `words_per_turn_target` words', WRITER_SYSTEM_PROMPT)
        self.assertNotIn('target roughly `words_per_turn_target` words', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('Let the scene decide the length', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('a chapter is a connected dramatic unit', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('A small time skip', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('Do not infer that lunch or another event has happened', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('Do not repeat or paraphrase the same image', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('A meaningful transformed callback is welcome', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertNotIn('a chapter is a connected dramatic unit', WRITER_SYSTEM_PROMPT)
        self.assertNotIn('Do not repeat or paraphrase the same image', WRITER_SYSTEM_PROMPT)
        self.assertIn('not a template every scene must copy', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('an ordinary conversation, small errand or quiet observation is enough',
                      EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('An empty list imposes no wording requirement', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('"ongoing" means there is no deadline', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('Do not end a quiet turn by recapping an unchanged offer', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertNotIn('Advance or resolve threads and hints naturally', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertIn('The planner is a director, not a source of new canon', EXPERIMENTAL_WRITER_SYSTEM_PROMPT)

    def test_absent_or_unusable_values_resolve_to_the_classic_profile(self):
        for value in (None, '', '   ', 'classic', 'CLASSIC', 'bogus', 5, {}, []):
            self.assertEqual(normalize_narration_mode(value), 'classic', repr(value))
        for value in ('experimental', 'Experimental', '  EXPERIMENTAL  '):
            self.assertEqual(normalize_narration_mode(value), 'experimental', repr(value))


class NarrationModeRequestTests(unittest.TestCase):
    """Request level: the switch reaches the prompts and changes nothing else."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.data = Path(temporary.name)
        worlds = self.data / 'worlds'
        worlds.mkdir()
        for module, name, value in (
            (storage, 'WORLDS_DIR', str(worlds)),
            (main, 'WORLDS_DIR', str(worlds)),
            (world_routes, 'WORLDS_DIR', str(worlds)),
            (storage, 'RUNTIME_CONFIG_PATH', str(self.data / 'runtime_config.json')),
        ):
            self.enterContext(patch.object(module, name, value))
        self.enterContext(patch.dict(os.environ, {'OPENROUTER_API_KEY': ''}))
        self.enterContext(patch('requests.sessions.Session.send',
                                side_effect=AssertionError('Real HTTP forbidden in tests')))
        self.client = TestClient(main.app)
        self.worlds = worlds
        self.world = 'narration_mode_test'
        self.path = self.seed(self.world)
        self.calls = []
        self.planner_anchors = []
        self.writer_suggestions = None
        self.planner_direction = None
        self.enterContext(patch.object(main, 'call_llm', side_effect=self.fake_llm))

    def seed(self, name):
        """Create a demo world and pin the protagonist used by the fixtures."""
        self.assertEqual(self.client.post(f'/worlds/{name}/seed-demo').status_code, 200)
        path = self.worlds / name
        config = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        config.update(protagonist_id='char_xueli', psychology_enabled=False)
        storage.write_world_file(str(path), 'world_config.json', config)
        return path

    def fake_llm(self, system_prompt, user_prompt, user_input_for_mock='',
                 mock_response=None, world_name=None, role=None):
        self.calls.append({'role': role, 'system_prompt': system_prompt, 'user_prompt': user_prompt})
        if role == 'planner':
            response = json.loads(main.mock_planner_response(user_input_for_mock))
            response['anchor_keywords'] = self.planner_anchors
            response['actor_observation'] = 'Xue Li asks about the courtyard.'
            if self.planner_direction is not None:
                response['scene_direction'] = self.planner_direction
            return json.dumps(response, ensure_ascii=False)
        if role == 'actor':
            return json.dumps({'visible_behavior': 'Gu Changge waits.',
                               'possible_dialogue': 'What did you notice?'})
        if role == 'writer':
            response = json.loads(main.mock_narrator_response(user_input_for_mock))
            if self.writer_suggestions is not None:
                response['suggested_actions'] = self.writer_suggestions
            return json.dumps(response, ensure_ascii=False)
        return main.mock_consistency_checker_response()

    # -- helpers ---------------------------------------------------------
    def read(self, filename):
        return json.loads((self.path / filename).read_text(encoding='utf-8'))

    def write(self, filename, value):
        storage.write_world_file(str(self.path), filename, value)

    def post(self, endpoint, payload=None):
        return self.client.post(f'/worlds/{self.world}/{endpoint}', json=payload or {})

    def systems(self):
        return [call['system_prompt'] for call in self.calls]

    def payloads_for(self, system_prompt):
        return [json.loads(c['user_prompt']) for c in self.calls if c['system_prompt'] == system_prompt]

    def reset_calls(self):
        self.calls = []

    # -- default path ----------------------------------------------------
    def test_a_request_without_a_mode_uses_the_classic_prompts(self):
        response = self.post('chapter/continue', {'user_input': 'Observe the courtyard.'})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertIn(WRITER_SYSTEM_PROMPT, self.systems())
        self.assertNotIn(EXPERIMENTAL_PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertNotIn(EXPERIMENTAL_WRITER_SYSTEM_PROMPT, self.systems())
        self.assertNotIn('context_kind', self.payloads_for(PLANNER_SYSTEM_PROMPT)[0]['current_checkpoint'])

    def test_an_explicit_classic_mode_is_the_same_as_omitting_it(self):
        response = self.post('chapter/continue', {'user_input': 'Observe.', 'narration_mode': 'classic'})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertIn(WRITER_SYSTEM_PROMPT, self.systems())

    # -- experimental path -----------------------------------------------
    def test_an_experimental_request_uses_the_experimental_prompts(self):
        response = self.post('chapter/continue', {'user_input': 'Share a meal.', 'narration_mode': 'experimental'})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(EXPERIMENTAL_PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertIn(EXPERIMENTAL_WRITER_SYSTEM_PROMPT, self.systems())

    def test_ensemble_calls_present_npc_and_keeps_its_cue_in_writer_payload(self):
        response = self.post('chapter/continue', {
            'user_input': 'Ask Gu Changge what he noticed.', 'narration_mode': 'ensemble',
        })
        self.assertEqual(response.status_code, 200, response.text)
        actor_calls = [call for call in self.calls if call['role'] == 'actor']
        self.assertEqual(len(actor_calls), 1)
        actor_payload = json.loads(actor_calls[0]['user_prompt'])
        self.assertEqual(actor_payload['character_id'], 'char_gu_changge')
        self.assertNotIn('secrets', actor_payload['other_people_present']['char_xueli'])
        writer_payload = json.loads(next(
            call['user_prompt'] for call in self.calls if call['role'] == 'writer'))
        self.assertEqual(writer_payload['actor_cues']['char_gu_changge']['possible_dialogue'],
                         'What did you notice?')
        self.assertNotIn('relationship_context', writer_payload)
        self.assertEqual(len(self.read('chapters.json')['chapters']), 1)

    def test_ensemble_checker_gets_prior_prose_and_all_eligible_start_locations(self):
        first = self.post('chapter/continue', {
            'user_input': 'Wait with Gu Changge.', 'narration_mode': 'ensemble',
        })
        self.assertEqual(first.status_code, 200, first.text)
        prior_text = self.read('chapters.json')['chapters'][-1]['chapter_text']
        self.reset_calls()

        second = self.post('chapter/continue', {
            'user_input': 'Ask what happened.', 'narration_mode': 'ensemble',
        })
        self.assertEqual(second.status_code, 200, second.text)
        checker_calls = [call for call in self.calls if call['role'] == 'checker']
        self.assertEqual(len(checker_calls), 1)
        self.assertEqual(checker_calls[0]['system_prompt'],
                         ENSEMBLE_CONSISTENCY_CHECKER_SYSTEM_PROMPT)
        payload = json.loads(checker_calls[0]['user_prompt'])
        self.assertEqual(payload['recent_confirmed_scenes'][-1]['chapter_text'], prior_text)
        self.assertIn('char_gu_changge', payload['character_locations_before_chapter'])
        self.assertIn('char_xueli', payload['character_locations_before_chapter'])
        writer = next(call for call in self.calls if call['role'] == 'writer')
        self.assertNotIn('recent_confirmed_scenes', json.loads(writer['user_prompt']))

    def test_ensemble_checker_retry_keeps_same_confirmed_evidence(self):
        first = self.post('chapter/continue', {
            'user_input': 'Observe Gu Changge.', 'narration_mode': 'ensemble',
        })
        self.assertEqual(first.status_code, 200, first.text)
        self.reset_calls()
        checks = 0

        def fail_then_pass(system_prompt, user_prompt, **kwargs):
            nonlocal checks
            if kwargs.get('role') == 'checker':
                self.calls.append({'role': 'checker', 'system_prompt': system_prompt,
                                   'user_prompt': user_prompt})
                checks += 1
                if checks == 1:
                    return json.dumps({'consistent': False, 'severity': 'major',
                                       'issues': ['Earlier scene says the door is closed; draft says open.'],
                                       'explanation': 'Direct contradiction.'})
            return self.fake_llm(system_prompt, user_prompt, **kwargs)

        with patch.object(main, 'call_llm', side_effect=fail_then_pass):
            response = self.post('chapter/continue', {
                'user_input': 'Look at the door.', 'narration_mode': 'ensemble',
            })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(checks, 2)
        checker_payloads = [json.loads(call['user_prompt']) for call in self.calls
                            if call['role'] == 'checker']
        self.assertEqual(checker_payloads[0]['recent_confirmed_scenes'],
                         checker_payloads[1]['recent_confirmed_scenes'])
        self.assertTrue(response.json()['chapter']['consistency_check']['triggered_rewrite'])

    def test_failed_actor_stage_does_not_commit_a_partial_turn(self):
        def invalid_actor(system_prompt, user_prompt, **kwargs):
            if kwargs.get('role') == 'actor':
                return 'not valid JSON'
            return self.fake_llm(system_prompt, user_prompt, **kwargs)

        with patch.object(main, 'call_llm', side_effect=invalid_actor):
            response = self.post('chapter/continue', {
                'user_input': 'Ask Gu Changge about the courtyard.',
                'narration_mode': 'ensemble',
            })
        self.assertEqual(response.status_code, 502, response.text)
        self.assertEqual(self.read('chapters.json')['chapters'], [])
        self.assertNotIn(PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertNotIn(WRITER_SYSTEM_PROMPT, self.systems())

    def test_experimental_quick_actions_come_from_the_finished_scene(self):
        self.writer_suggestions = ['Check the stock shelf']
        response = self.post('chapter/continue', {
            'user_input': 'Eat the rice.', 'narration_mode': 'experimental',
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['suggested_actions'], ['Check the stock shelf'])

        self.writer_suggestions = None
        response = self.post('chapter/continue', {
            'user_input': 'Return to the bench.', 'narration_mode': 'experimental',
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['suggested_actions'], [])

    def test_director_cues_reach_only_experimental_writer_without_extra_calls(self):
        self.planner_direction = {
            'focus': 'The small meal at the table',
            'stop_before': 'Jun chooses whether to answer the phone',
            'actors': {
                'char_gu_changge': {'want': 'Finish breakfast', 'approach': 'Answer briefly'},
                'char_xueli': {'want': 'Answer for the player', 'approach': 'Take control'},
                'not_present': {'want': 'Reveal a secret', 'approach': 'Arrive suddenly'},
            },
        }
        response = self.post('chapter/continue', {
            'user_input': 'Sit and eat.', 'narration_mode': 'experimental',
        })
        self.assertEqual(response.status_code, 200, response.text)
        writer_payloads = self.payloads_for(EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertEqual(len(writer_payloads), 1)
        self.assertEqual(writer_payloads[0]['scene_direction']['focus'],
                         'The small meal at the table')
        self.assertEqual(list(writer_payloads[0]['scene_direction']['actors']), ['char_gu_changge'])
        self.assertEqual([call['role'] for call in self.calls].count('planner'), 1)
        self.assertEqual([call['role'] for call in self.calls].count('writer'), 1)

        self.reset_calls()
        response = self.post('chapter/continue', {'user_input': 'Wait.', 'narration_mode': 'classic'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn('scene_direction', self.payloads_for(WRITER_SYSTEM_PROMPT)[0])

    def test_checker_receives_a_human_readable_clock_window(self):
        payload = build_consistency_checker_payload(
            'The eleven o’clock news began.',
            {'elapsed_time': {'minutes': 12}},
            {'timekeeping_mode': 'duration', 'protagonist_id': 'pc',
             'story_clock': {'year': 2024, 'month': 6, 'day': 3, 'minute_of_day': 670}},
            {'description': 'At the shop.'}, [], {'pc': {'location': 'Shop'}},
        )
        self.assertEqual(payload['temporal_spatial_alignment']['clock_window_local'],
                         '2024-6-3 11:10 → 2024-6-3 11:22')

    def test_new_world_sends_playable_situation_not_future_outline(self):
        timeline = self.read('canon_timeline.json')
        checkpoint = timeline['checkpoints'][0]
        checkpoint['description'] = 'UNPLAYED_GATE_DISASTER in a future branch.'
        checkpoint['playable_situation'] = 'The protagonist stands at the station with an unanswered offer.'
        checkpoint['possible_developments'] = ['If accepted, UNPLAYED_GATE_DISASTER may follow.']
        self.write('canon_timeline.json', timeline)

        response = self.post('chapter/continue', {
            'user_input': 'Wait at the station.', 'narration_mode': 'experimental',
        })

        self.assertEqual(response.status_code, 200, response.text)
        model_calls = [call for call in self.calls if call['role'] in ('planner', 'writer', 'checker')]
        self.assertTrue(model_calls)
        for call in model_calls:
            payload = json.loads(call['user_prompt'])
            self.assertNotIn('UNPLAYED_GATE_DISASTER', call['user_prompt'])
            if call['role'] in ('planner', 'writer'):
                self.assertEqual(payload['current_checkpoint']['description'],
                                 checkpoint['playable_situation'])
                self.assertEqual(payload['current_checkpoint']['context_kind'], 'playable_situation')
            else:
                self.assertEqual(payload['current_checkpoint_description'],
                                 checkpoint['playable_situation'])

    def test_creator_checkpoint_update_marks_structured_world(self):
        timeline = self.read('canon_timeline.json')
        timeline['checkpoints'][0]['playable_situation'] = 'A playable opening.'

        response = self.client.put(f'/worlds/{self.world}/canon_timeline', json=timeline)

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.read('world_config.json')['checkpoint_context_version'], 2)

    def test_optional_experimental_anchors_reach_the_writer_without_an_extra_call(self):
        self.planner_anchors = ['Harvest Bell', 'Winter Ledger', 'extra']
        response = self.post('chapter/continue', {
            'user_input': 'Ask about the harvest.', 'narration_mode': 'experimental',
        })

        self.assertEqual(response.status_code, 200, response.text)
        writer_payloads = self.payloads_for(EXPERIMENTAL_WRITER_SYSTEM_PROMPT)
        self.assertEqual(len(writer_payloads), 1)
        self.assertEqual(writer_payloads[0]['anchor_keywords'], ['Harvest Bell', 'Winter Ledger'])

    def test_malformed_anchor_elements_cannot_crash_a_turn(self):
        self.planner_anchors = [1, {'word': 'bell'}, '', 'Harvest Bell']
        response = self.post('chapter/continue', {'user_input': 'Look around.'})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['chapter']['anchor_keywords'], ['Harvest Bell'])

    def test_the_engine_truth_in_the_payload_is_the_same_in_both_modes(self):
        """Only the guidance changes - never the committed facts handed to the model.

        Two freshly seeded worlds are used so both turns start from the same
        tick; otherwise the engine's own turn-scoped ids (which embed the tick)
        would differ for reasons that have nothing to do with the switch.
        """
        self.post('chapter/continue', {'user_input': 'Share a meal.'})
        classic_writer = self.payloads_for(WRITER_SYSTEM_PROMPT)[0]

        self.reset_calls()
        self.seed('narration_mode_experimental')
        response = self.client.post('/worlds/narration_mode_experimental/chapter/continue',
                                    json={'user_input': 'Share a meal.', 'narration_mode': 'experimental'})
        self.assertEqual(response.status_code, 200, response.text)
        experimental_writer = self.payloads_for(EXPERIMENTAL_WRITER_SYSTEM_PROMPT)[0]

        for key in ('action_resolution', 'inventory_resolution', 'travel_resolution',
                    'time_skip_resolution', 'current_checkpoint', 'story_clock',
                    'words_per_turn_target', 'active_cards'):
            self.assertIn(key, experimental_writer)
            self.assertEqual(experimental_writer[key], classic_writer[key], key)

    def test_an_experimental_turn_is_generated_and_saved_normally(self):
        before = self.read('world_config.json').get('revision')
        response = self.post('chapter/continue', {'user_input': 'Rest by the fire.', 'narration_mode': 'experimental'})

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertTrue(body['chapter']['chapter_text'])
        chapters = self.read('chapters.json')['chapters']
        self.assertEqual(len(chapters), 1)
        self.assertEqual(chapters[0]['chapter_text'], body['chapter']['chapter_text'])
        after = self.read('world_config.json').get('revision')
        self.assertNotEqual(before, after, 'a committed experimental turn must still bump the revision')

    def test_the_switch_adds_no_model_calls(self):
        self.post('chapter/continue', {'user_input': 'Ask about the harvest.'})
        classic_calls = len(self.calls)

        self.reset_calls()
        self.post('chapter/continue', {'user_input': 'Ask about the harvest.', 'narration_mode': 'experimental'})

        self.assertEqual(len(self.calls), classic_calls,
                         'the experimental profile must only change prompts, not call counts')
        self.assertGreater(classic_calls, 0)

    # -- every generation entry point ------------------------------------
    def test_chapter_start_honours_the_mode(self):
        fresh = 'narration_mode_start'
        self.seed(fresh)

        self.reset_calls()
        response = self.client.post(f'/worlds/{fresh}/chapter/start',
                                    json={'opening_mode': 'ai_generate', 'narration_mode': 'experimental'})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(EXPERIMENTAL_PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertIn(EXPERIMENTAL_WRITER_SYSTEM_PROMPT, self.systems())

    def test_time_skip_execute_honours_the_mode(self):
        response = self.post('time-skip/execute', {
            'amount': 2, 'unit': 'hours', 'activity': 'Nap', 'narration_mode': 'experimental',
        })

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(EXPERIMENTAL_PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertIn(EXPERIMENTAL_WRITER_SYSTEM_PROMPT, self.systems())

    def test_regenerate_honours_the_mode(self):
        self.assertEqual(self.post('chapter/continue', {'user_input': 'Look up.'}).status_code, 200)

        self.reset_calls()
        response = self.post('chapter/regenerate', {'narration_mode': 'experimental'})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertIn(EXPERIMENTAL_PLANNER_SYSTEM_PROMPT, self.systems())
        self.assertIn(EXPERIMENTAL_WRITER_SYSTEM_PROMPT, self.systems())
        self.assertEqual(len(self.read('chapters.json')['chapters']), 1,
                         'a reroll replaces the latest turn rather than adding one')

    def test_regenerate_can_replace_the_latest_action_and_rejects_blank_edits(self):
        first = self.post('chapter/continue', {'user_input': 'Wait by the door.'})
        self.assertEqual(first.status_code, 200, first.text)
        before = self.read('chapters.json')['chapters']
        revision_before = self.read('world_config.json')['revision']

        blank = self.post('chapter/regenerate', {'user_input': '   '})
        self.assertEqual(blank.status_code, 422, blank.text)
        self.assertEqual(self.read('chapters.json')['chapters'], before)
        self.assertEqual(self.read('world_config.json')['revision'], revision_before)

        revised_input = 'Ask Nao to come with Tarou to lunch after class.'
        response = self.post('chapter/regenerate', {'user_input': revised_input})

        self.assertEqual(response.status_code, 200, response.text)
        saved_turns = self.read('chapters.json')['chapters']
        self.assertEqual(len(saved_turns), 1, 'the reroll replaces, rather than appends to, the latest turn')
        self.assertEqual(saved_turns[-1]['user_input'], revised_input)
        planner_payloads = self.payloads_for(PLANNER_SYSTEM_PROMPT)
        self.assertTrue(planner_payloads)
        self.assertEqual(planner_payloads[-1]['user_input'], revised_input)

    # -- non-destructive guarantees --------------------------------------
    def test_the_switch_never_rewrites_an_already_saved_turn(self):
        self.assertEqual(self.post('chapter/continue', {'user_input': 'First action.'}).status_code, 200)
        saved_first = self.read('chapters.json')['chapters'][0]

        self.assertEqual(self.post('chapter/continue', {
            'user_input': 'Second action.', 'narration_mode': 'experimental',
        }).status_code, 200)

        chapters = self.read('chapters.json')['chapters']
        self.assertEqual(len(chapters), 2)
        self.assertEqual(chapters[0], saved_first,
                         'turning the switch on must not touch the turn that was already saved')

    def test_replay_is_still_idempotent_in_experimental_mode(self):
        payload = {'user_input': 'Knock twice.', 'request_id': 'req_narration_1',
                   'narration_mode': 'experimental'}
        first = self.post('chapter/continue', payload)
        self.assertEqual(first.status_code, 200, first.text)
        writer_calls = len(self.payloads_for(EXPERIMENTAL_WRITER_SYSTEM_PROMPT))

        second = self.post('chapter/continue', payload)

        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(len(self.read('chapters.json')['chapters']), 1,
                         'the replayed request must not commit a second turn')
        self.assertEqual(len(self.payloads_for(EXPERIMENTAL_WRITER_SYSTEM_PROMPT)), writer_calls,
                         'the replayed request must not call the model again')

    def test_a_stale_revision_is_still_rejected_in_experimental_mode(self):
        response = self.post('chapter/continue', {
            'user_input': 'Step forward.', 'narration_mode': 'experimental', 'expected_revision': 9999,
        })

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(self.read('chapters.json')['chapters'], [])

    def test_an_unknown_mode_is_rejected_rather_than_silently_downgraded(self):
        response = self.post('chapter/continue', {'user_input': 'Look.', 'narration_mode': 'experimental-ish'})

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.calls, [], 'a rejected request must not reach the model')
        self.assertEqual(self.read('chapters.json')['chapters'], [])


if __name__ == '__main__':
    unittest.main()
