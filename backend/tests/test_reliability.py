"""Regression tests using temporary worlds and mocked AI only."""
import json
import os
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import main
from fastapi.testclient import TestClient
from app import storage, persistence, engine, chapter_generator, security
from app.routes import world_routes
from app.world import schema
from app.world import map_rules
from app.checkpoint_engine import check_map_based_restrictions
from app.world.map_rules import reconcile_checkpoint_location_gates
from app.world.calendar_clock import advance_story_clock, normalize_start_clock, normalize_elapsed_time


class ReliabilityTests(unittest.TestCase):
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
        self.enterContext(patch('requests.sessions.Session.send', side_effect=AssertionError('Real HTTP forbidden in tests')))
        self.client = TestClient(main.app)
        self.world = 'reliability_test'
        self.path = worlds / self.world
        self.assertEqual(self.client.post(f'/worlds/{self.world}/seed-demo').status_code, 200)
        self.cfg = self.read('world_config.json')
        self.cfg.update(protagonist_id='char_xueli', psychology_enabled=False)
        self.write('world_config.json', self.cfg)
        self.enterContext(patch.object(main, 'call_llm', side_effect=self.fake_llm))

    @staticmethod
    def fake_llm(system_prompt, user_prompt, user_input_for_mock='', mock_response=None, world_name=None, role=None):
        if system_prompt == main.PLANNER_SYSTEM_PROMPT:
            return main.mock_planner_response(user_input_for_mock)
        if system_prompt == main.WRITER_SYSTEM_PROMPT:
            return main.mock_narrator_response(user_input_for_mock)
        return main.mock_consistency_checker_response()

    def read(self, filename):
        return json.loads((self.path / filename).read_text(encoding='utf-8'))

    @staticmethod
    def read_from(world_path, filename):
        return json.loads((Path(world_path) / filename).read_text(encoding='utf-8'))

    def write(self, filename, value):
        storage.write_world_file(str(self.path), filename, value)

    def post(self, endpoint, payload=None):
        return self.client.post(f'/worlds/{self.world}/{endpoint}', json=payload or {})

    def setup_event(self):
        self.write('location_map.json', {'locations': [{'id': 'village', 'name': 'Village', 'tags': ['safe']}]})
        self.write('world_canon_store.json', {'facts': []})
        self.write('world_events.json', {'events': [{
            'event_id': 'raid', 'status': 'pending',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [{'outcome_id': 'damaged', 'canon_facts_add': ['The village was damaged.'],
                          'world_flags_set': {'raid_done': True},
                          'location_effects': [{'location_id': 'village', 'tags_add': ['ruined'], 'tags_remove': ['safe']}]}],
        }]})

    def test_new_world_owns_every_core_state_file(self):
        """A freshly created world must already own all of its state files.

        Snapshots, saves, branches and restores all copy only files that exist.
        A world born without world_events.json / location_map.json /
        discovery.json therefore has no events, no map and no knowledge record,
        and every save made from it inherits the same hole.
        """
        present = {name for name in os.listdir(self.path) if name.endswith('.json')}
        missing = sorted(set(schema.CORE_STATE_FILES) - present)
        self.assertEqual(missing, [], f'world was created without core files: {missing}')

    def test_save_round_trip_preserves_the_full_world_state(self):
        """A save must capture every core file, and restore must bring it back.

        This is the guarantee a player relies on: what happened is what comes
        back. It fails if any core file is absent at save time.
        """
        storage.write_world_file(str(self.path), 'world_events.json', {'events': [
            {'event_id': 'raid', 'status': 'pending', 'trigger_conditions': [], 'outcomes': []}
        ]})
        storage.write_world_file(str(self.path), 'discovery.json', {'discovered': {'raid': {}}})
        storage.write_world_file(str(self.path), 'location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village'}
        ]})

        save = self.client.post(f'/worlds/{self.world}/saves', json={'label': 'roundtrip'}).json()
        save_id = save['save']['save_id']
        snapshot_dir = self.path / 'saves' / save_id
        captured = {name for name in os.listdir(snapshot_dir) if name.endswith('.json')}
        missing = sorted(set(schema.CORE_STATE_FILES) - captured)
        self.assertEqual(missing, [], f'save omitted core files: {missing}')

        # Mutate the live world, then restore and confirm the state came back.
        self.write('world_events.json', {'events': []})
        restore = self.client.post(f'/worlds/{self.world}/saves/{save_id}/restore')
        self.assertEqual(restore.status_code, 200)
        self.assertEqual(len(self.read('world_events.json')['events']), 1,
                         'restore must bring back the saved event')
        self.assertIn('raid', self.read('discovery.json')['discovered'],
                      'restore must bring back what the world had discovered')

    def test_writer_invalid_json_retries_once_with_format_correction(self):
        calls = {'writer': 0}
        writer_payloads = []

        def flaky_writer(system_prompt, user_prompt, *args, **kwargs):
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                return main.mock_planner_response(kwargs.get('user_input_for_mock', ''))
            if system_prompt == main.WRITER_SYSTEM_PROMPT:
                calls['writer'] += 1
                writer_payloads.append(json.loads(user_prompt))
                if calls['writer'] == 1:
                    return 'not json at all'
                return main.mock_narrator_response(kwargs.get('user_input_for_mock', ''))
            return main.mock_consistency_checker_response()

        with patch.object(main, 'call_llm', side_effect=flaky_writer):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(calls['writer'], 2)
        self.assertNotIn('correction_note', writer_payloads[0])
        self.assertIn('valid JSON', writer_payloads[1]['correction_note'])
        self.assertEqual(len(self.read('chapters.json')['chapters']), 1)

    def test_event_consequences_persist_and_do_not_repeat(self):
        self.setup_event()
        for _ in range(2):
            self.assertEqual(self.post('chapter/continue', {'user_input': 'Observe quietly.'}).status_code, 200)
        self.assertEqual(self.read('world_events.json')['events'][0]['status'], 'resolved')
        self.assertEqual([f['statement'] for f in self.read('world_canon_store.json')['facts']], ['The village was damaged.'])
        self.assertEqual(self.read('location_map.json')['locations'][0]['tags'], ['ruined'])
        self.assertTrue(self.read('world_config.json')['world_flags']['raid_done'])
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], 2)
        self.assertEqual(self.read('world_events.json')['events'][0]['resolved_at_tick'], 1)

    def test_late_failure_does_not_resolve_events_or_change_world(self):
        self.setup_event()
        before = {p.name: p.read_bytes() for p in self.path.glob('*.json')}
        with patch.object(chapter_generator, 'check_rolling_summary_trigger', side_effect=RuntimeError('late failure')):
            with self.assertRaisesRegex(RuntimeError, 'late failure'):
                self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.glob('*.json')})

    def test_batch_write_failure_restores_original_bytes_and_absent_files(self):
        original = (self.path / 'world_config.json').read_bytes()
        real_replace = os.replace
        def fail_one_replace(src, dest):
            if Path(dest).name == 'chapters.json':
                raise OSError('simulated replacement failure')
            return real_replace(src, dest)
        with patch.object(persistence.os, 'replace', side_effect=fail_one_replace):
            with self.assertRaises(OSError):
                persistence.commit_world_files(str(self.path), {
                    'world_config.json': {**self.cfg, 'display_name': 'changed'},
                    'new_file.json': {'value': 1},
                    'chapters.json': self.read('chapters.json'),
                })
        self.assertEqual((self.path / 'world_config.json').read_bytes(), original)
        self.assertFalse((self.path / 'new_file.json').exists())
        self.assertEqual(list(self.path.glob('*.tmp')), [])

    def test_batch_validates_all_files_before_writing(self):
        original = (self.path / 'world_config.json').read_bytes()
        with self.assertRaises(Exception):
            persistence.commit_world_files(str(self.path), {
                'world_config.json': {**self.cfg, 'display_name': 'changed'},
                'character_state.json': {'characters': {'bad': {}}},
            })
        self.assertEqual((self.path / 'world_config.json').read_bytes(), original)

    def test_save_restore_and_branch_include_events_and_consequences(self):
        self.setup_event()
        save = self.post('saves', {'label': 'before raid'}).json()['save']['save_id']
        self.assertTrue((self.path / 'saves' / save / 'world_events.json').exists())
        self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(self.post(f'saves/{save}/restore').status_code, 200)
        self.assertEqual(self.read('world_events.json')['events'][0]['status'], 'pending')
        self.assertEqual(self.read('world_canon_store.json')['facts'], [])
        self.assertEqual(self.read('location_map.json')['locations'][0]['tags'], ['safe'])
        response = self.post(f'saves/{save}/branch', {'new_world_name': 'branch_test'})
        self.assertEqual(response.status_code, 200, response.text)
        branched = json.loads((self.path.parent / 'branch_test' / 'world_events.json').read_text(encoding='utf-8'))
        self.assertEqual(branched, self.read('world_events.json'))

    def test_legacy_save_does_not_keep_future_events(self):
        self.setup_event()
        save = self.post('saves', {'label': 'legacy'}).json()['save']['save_id']
        (self.path / 'saves' / save / 'world_events.json').unlink()
        self.post('chapter/continue', {'user_input': 'Observe.'})
        response = self.post(f'saves/{save}/restore')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.read('world_events.json'), {'events': []})
        safety = response.json()['safety_save_id']
        backup = json.loads((self.path / 'saves' / safety / 'world_events.json').read_text(encoding='utf-8'))
        self.assertEqual(backup['events'][0]['status'], 'resolved')

    def test_map_status_agrees_with_movement_rules(self):
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['power_stat'].update(exp=100, realm='Expert')
        self.write('character_state.json', characters)
        for requirements, unlocked in [({'unlock_exp': 10}, True), ({'unlock_exp': 101}, False),
                                       ({'unlock_realm': 'expert'}, True),
                                       ({'unlock_checkpoint_id': 'cp_0'}, True),
                                       ({'unlock_checkpoint_id': 'cp_4'}, False), ({}, True)]:
            with self.subTest(requirements=requirements):
                location_map = {'locations': [{'id': 'gate', 'name': 'Gate', **requirements}]}
                self.write('location_map.json', location_map)
                status = self.client.get(f'/worlds/{self.world}/location-map/status').json()['locations'][0]
                violations = check_map_based_restrictions({'characters': {'char_xueli': {'location': 'Gate'}}},
                                                          location_map, characters['characters'], self.cfg)
                self.assertEqual(status['is_unlocked'], not violations)
                self.assertEqual(status['is_unlocked'], unlocked)

    def test_generated_event_venue_cannot_require_experience_to_enter(self):
        timeline = [{'checkpoint_id': 'cp_0', 'boundary': {'locations': ['Road']}},
                    {'checkpoint_id': 'cp_1', 'boundary': {'locations': ['Square']}}]
        location_map = {'locations': [
            {'name': 'Road', 'unlock_exp': 30, 'unlock_checkpoint_id': 'cp_2'},
            {'name': 'Square', 'unlock_exp': 50, 'unlock_checkpoint_id': 'cp_1'},
            {'name': 'Secret Vault', 'unlock_exp': 80},
        ]}
        reconcile_checkpoint_location_gates(location_map, timeline,
                                            {'hero': {'location': 'Road'}})
        self.assertEqual(location_map['locations'][0]['unlock_exp'], 0)
        self.assertIsNone(location_map['locations'][0]['unlock_checkpoint_id'])
        self.assertEqual(location_map['locations'][1]['unlock_exp'], 0)
        self.assertIsNone(location_map['locations'][1]['unlock_checkpoint_id'])
        self.assertEqual(location_map['locations'][2]['unlock_exp'], 80)

    def test_world_calendar_rolls_month_year_and_season(self):
        calendar = {'kind': 'custom', 'months': [
            {'name': 'Ebb', 'days': 2}, {'name': 'Flood', 'days': 3}],
            'seasons': [{'name': 'Quiet Sea', 'months': [1]},
                        {'name': 'Black Tide', 'months': [2]}]}
        clock = normalize_start_clock({'year': 7, 'month': 1, 'day': 2,
                                       'minute_of_day': 1439}, calendar)
        advance_story_clock(clock, normalize_elapsed_time({'seconds': 90}), calendar)
        self.assertEqual((clock['year'], clock['month'], clock['day']), (7, 2, 1))
        self.assertEqual((clock['minute_of_day'], clock['second_of_day']), (0, 30))
        self.assertEqual(clock['season'], 'Black Tide')
        advance_story_clock(clock, normalize_elapsed_time({'days': 3}), calendar)
        self.assertEqual((clock['year'], clock['month'], clock['day']), (8, 1, 1))

    def test_gregorian_calendar_respects_leap_day(self):
        calendar = {'kind': 'gregorian'}
        clock = normalize_start_clock({'year': 2024, 'month': 2, 'day': 28,
                                       'minute_of_day': 600}, calendar)
        advance_story_clock(clock, normalize_elapsed_time({'days': 1}), calendar)
        self.assertEqual((clock['year'], clock['month'], clock['day']), (2024, 2, 29))

    def test_duration_mode_ignores_legacy_day_delta_from_model(self):
        cfg = self.read('world_config.json')
        cfg['timekeeping_mode'] = 'duration'
        cfg['calendar'] = {'kind': 'gregorian'}
        cfg['story_clock'] = normalize_start_clock(
            {'year': 2026, 'month': 9, 'day': 19, 'minute_of_day': 1260}, cfg['calendar'])
        self.write('world_config.json', cfg)
        def timed_llm(system_prompt, user_prompt, user_input_for_mock='', mock_response=None,
                      world_name=None, role=None):
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                planned = json.loads(main.mock_planner_response(user_input_for_mock))
                planned.setdefault('state_changes', {})['elapsed_time'] = {
                    'days': 0, 'hours': 0, 'minutes': 8, 'seconds': 0}
                planned['state_changes']['story_clock_delta'] = {'day': 1}
                return json.dumps(planned)
            if system_prompt == main.WRITER_SYSTEM_PROMPT:
                written = json.loads(main.mock_narrator_response(user_input_for_mock))
                written.setdefault('state_changes', {})['elapsed_time'] = {
                    'days': 0, 'hours': 0, 'minutes': 8, 'seconds': 0}
                written['state_changes']['story_clock_delta'] = {'day': 1}
                return json.dumps(written)
            return self.fake_llm(system_prompt, user_prompt, user_input_for_mock,
                                 mock_response, world_name, role)
        with patch.object(main, 'call_llm', side_effect=timed_llm):
            response = self.post('chapter/continue', {'user_input': 'Speak briefly.'})
        self.assertEqual(response.status_code, 200, response.text)
        clock = self.read('world_config.json')['story_clock']
        self.assertEqual((clock['day'], clock['minute_of_day']), (19, 1268))
        self.assertEqual(clock['tick'], 1)

    def test_opening_scene_does_not_tick_or_resolve_pending_event(self):
        cfg = self.read('world_config.json')
        cfg['timekeeping_mode'] = 'duration'
        self.write('world_config.json', cfg)
        self.setup_event()
        response = self.post('chapter/start')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], 0)
        self.assertEqual(self.read('world_events.json')['events'][0]['status'], 'pending')

    def test_structured_inventory_is_exposed_and_legacy_items_still_work(self):
        characters = self.read('character_state.json')
        hero = characters['characters']['char_xueli']
        hero['inventory'] = [
            'Old key',
            {
                'instance_id': 'inv_moon_blade', 'name': 'Moon Blade',
                'category': 'weapon', 'description': 'A cold silver blade.',
                'attributes': {'damage': 7, 'weight': 'light'},
                'abilities': [{'name': 'Moon Cut', 'effect': 'Cuts spectral bindings.'}],
                'tags': ['silver'], 'quantity': 1, 'condition': 'worn',
            },
        ]
        self.write('character_state.json', characters)
        state = self.client.get(f'/worlds/{self.world}/play-state')
        self.assertEqual(state.status_code, 200, state.text)
        inventory = state.json()['protagonist']['inventory']
        self.assertEqual([item['name'] for item in inventory], ['Old key', 'Moon Blade'])
        self.assertEqual(inventory[1]['attributes']['damage'], 7)
        self.assertEqual(inventory[1]['abilities'][0]['name'], 'Moon Cut')

    def test_structured_item_acquisition_replaces_legacy_stub_and_tracks_origin(self):
        from app.state_manager import apply_state_changes
        characters = self.read('character_state.json')['characters']
        hero = characters['char_xueli']
        hero['location'] = 'Moon Vault'
        hero['inventory'] = ['Moon Key']
        apply_state_changes(characters, {'characters': {'char_xueli': {
            'inventory_add': [{
                'name': 'Moon Key', 'category': 'key_item',
                'description': 'A silver key etched with a crescent.',
                'attributes': {'material': 'moon silver'},
                'abilities': [{'name': 'Open Moon Gate', 'effect': 'Opens lunar seals.'}],
            }],
        }}}, story_clock={'tick': 12})
        item = hero['inventory'][0]
        self.assertIsInstance(item, dict)
        self.assertEqual(item['description'], 'A silver key etched with a crescent.')
        self.assertEqual(item['acquired_at_tick'], 12)
        self.assertEqual(item['acquired_from'], 'Moon Vault')

        apply_state_changes(characters, {'characters': {'char_xueli': {
            'inventory_remove': ['Moon Key'],
        }}})
        self.assertEqual(hero['inventory'], [])

    def test_inventory_identity_and_stacking_do_not_merge_unique_same_named_items(self):
        from app.story.inventory import add_inventory_item, remove_inventory_item
        inventory = []
        add_inventory_item(inventory, {'instance_id': 'coin_a', 'item_id': 'coin', 'name': 'Coin',
                                       'stackable': False, 'quantity': 1})
        add_inventory_item(inventory, {'instance_id': 'coin_b', 'item_id': 'coin', 'name': 'Coin',
                                       'stackable': False, 'quantity': 1})
        self.assertEqual([item['instance_id'] for item in inventory], ['coin_a', 'coin_b'])
        remove_inventory_item(inventory, {'instance_id': 'coin_a'})
        self.assertEqual([item['instance_id'] for item in inventory], ['coin_b'])

        add_inventory_item(inventory, {'instance_id': 'herb_a', 'item_id': 'herb', 'name': 'Herb',
                                       'stackable': True, 'quantity': '2'})
        add_inventory_item(inventory, {'instance_id': 'herb_b', 'item_id': 'herb', 'name': 'Herb',
                                       'stackable': True, 'quantity': 3})
        herb = next(item for item in inventory if item.get('item_id') == 'herb')
        self.assertEqual(herb['quantity'], 5)

    def test_bad_ai_inventory_quantity_is_normalized_without_crashing(self):
        from app.story.inventory import normalize_item
        item = normalize_item({'name': 'Impossible bundle', 'quantity': 'many', 'charges': None})
        self.assertEqual(item['quantity'], 1)

    def test_explicit_inventory_actions_are_engine_owned(self):
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['inventory'] = [{
            'instance_id': 'lamp_1', 'item_id': 'lamp', 'name': 'Signal Lamp',
            'stackable': False, 'quantity': 1, 'equipped': False, 'charges': 2,
        }]
        self.write('character_state.json', characters)
        equipped = self.post('chapter/continue', {'user_input': 'Equip Signal Lamp.'})
        self.assertEqual(equipped.status_code, 200, equipped.text)
        resolution = equipped.json()['chapter']['inventory_resolution']
        self.assertEqual(resolution['status'], 'resolved')
        item = self.read('character_state.json')['characters']['char_xueli']['inventory'][0]
        self.assertTrue(item['equipped'])

        used = self.post('chapter/continue', {'user_input': 'Use Signal Lamp.'})
        self.assertEqual(used.status_code, 200, used.text)
        item = self.read('character_state.json')['characters']['char_xueli']['inventory'][0]
        self.assertEqual(item['charges'], 1)

        missing = self.post('chapter/continue', {'user_input': 'Use Missing Key.'})
        self.assertEqual(missing.status_code, 200, missing.text)
        self.assertEqual(missing.json()['chapter']['inventory_resolution']['reason'], 'item_not_owned')
        self.assertEqual(len(self.read('character_state.json')['characters']['char_xueli']['inventory']), 1)

    def test_travel_turn_commits_route_location_and_elapsed_clock(self):
        config = self.read('world_config.json')
        config['story_clock'].update(tick=0, day=1, time_of_day='morning')
        self.write('world_config.json', config)
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['location'] = 'Village'
        self.write('character_state.json', characters)
        timeline = self.read('canon_timeline.json')
        current = next(cp for cp in timeline['checkpoints']
                       if cp['checkpoint_id'] == config['current_checkpoint_id'])
        current['boundary']['locations'] = ['Village', 'Forest']
        self.write('canon_timeline.json', timeline)
        self.write('location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village', 'x': 10, 'y': 10,
             'connected_to': [{'to': 'forest', 'travel_time_minutes': 150}]},
            {'id': 'forest', 'name': 'Forest', 'x': 40, 'y': 10,
             'connected_to': ['village']},
        ]})

        response = self.post('chapter/continue', {'user_input': 'Travel to Forest.'})
        self.assertEqual(response.status_code, 200, response.text)
        travel = response.json()['chapter']['travel_resolution']
        self.assertEqual(travel['status'], 'arrived')
        self.assertEqual(travel['route'], ['Village', 'Forest'])
        self.assertEqual(travel['elapsed_minutes'], 150)
        self.assertEqual(self.read('character_state.json')['characters']['char_xueli']['location'], 'Forest')
        clock = self.read('world_config.json')['story_clock']
        self.assertEqual(clock['elapsed_minutes'], 150)
        self.assertEqual(clock['minute_of_day'], 630)
        self.assertEqual(clock['tick'], 3)

    def test_travel_preview_is_read_only_and_does_not_roll_encounter(self):
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['location'] = 'Village'
        self.write('character_state.json', characters)
        self.write('location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village', 'x': 0, 'y': 0,
             'connected_to': [{'to': 'forest', 'travel_time_minutes': 90,
                               'danger': 1, 'tags': ['bandit_road']}]},
            {'id': 'forest', 'name': 'Forest', 'x': 20, 'y': 0, 'connected_to': []},
        ]})
        before = {path.name: path.read_bytes() for path in self.path.glob('*.json')}
        response = self.client.post(f'/worlds/{self.world}/travel/preview', json={'destination': 'Forest'})
        self.assertEqual(response.status_code, 200, response.text)
        preview = response.json()
        self.assertEqual(preview['status'], 'available')
        self.assertEqual(preview['elapsed_minutes'], 90)
        self.assertEqual(preview['risk'], {'level': 'high', 'known_tags': ['bandit_road']})
        self.assertNotIn('danger_roll', preview)
        self.assertEqual(before, {path.name: path.read_bytes() for path in self.path.glob('*.json')})

    def test_travel_without_connected_route_is_blocked_without_moving_or_time_skip(self):
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['location'] = 'Village'
        self.write('character_state.json', characters)
        timeline = self.read('canon_timeline.json')
        current_id = self.read('world_config.json')['current_checkpoint_id']
        current = next(cp for cp in timeline['checkpoints'] if cp['checkpoint_id'] == current_id)
        current['boundary']['locations'] = ['Village', 'Island']
        self.write('canon_timeline.json', timeline)
        self.write('location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village', 'x': 10, 'y': 10, 'connected_to': []},
            {'id': 'island', 'name': 'Island', 'x': 80, 'y': 80, 'connected_to': []},
        ]})
        response = self.post('chapter/continue', {'user_input': 'Travel to Island.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['chapter']['travel_resolution']['status'], 'blocked')
        self.assertEqual(self.read('character_state.json')['characters']['char_xueli']['location'], 'Village')
        self.assertEqual(self.read('world_config.json')['story_clock'].get('elapsed_minutes', 0), 0)

    def test_map_status_exposes_route_and_disables_unreachable_destination(self):
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['location'] = 'Village'
        self.write('character_state.json', characters)
        self.write('location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village', 'x': 10, 'y': 10,
             'connected_to': ['forest']},
            {'id': 'forest', 'name': 'Forest', 'x': 40, 'y': 10,
             'connected_to': ['village']},
            {'id': 'island', 'name': 'Island', 'x': 80, 'y': 80,
             'connected_to': []},
        ]})
        response = self.client.get(f'/worlds/{self.world}/location-map/status')
        self.assertEqual(response.status_code, 200, response.text)
        locations = {item['id']: item for item in response.json()['locations']}
        self.assertTrue(locations['forest']['is_unlocked'])
        self.assertTrue(locations['forest']['is_reachable'])
        self.assertEqual(locations['forest']['route_preview'], ['Village', 'Forest'])
        self.assertFalse(locations['island']['is_unlocked'])
        self.assertFalse(locations['island']['is_reachable'])
        self.assertIn('No route connects', locations['island']['unlock_reason_missing'])

    def test_player_map_and_preview_hide_undiscovered_location(self):
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['location'] = 'Village'
        self.write('character_state.json', characters)
        self.write('location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village', 'x': 10, 'y': 10,
             'connected_to': ['vault'], 'is_starting_location': True},
            {'id': 'vault', 'name': 'Secret Moon Vault', 'description': 'Spoiler',
             'x': 50, 'y': 50, 'connected_to': [], 'tags': ['secret'],
             'discovery_status': 'unknown'},
        ]})
        # The hidden place is absent, not present-and-blank. Sending a scrubbed
        # entry would still hand the client its id, its exact x/y and a nameless
        # node to explain - the name is only one of the things that leaks.
        status = self.client.get(f'/worlds/{self.world}/location-map/status').json()['locations']
        self.assertNotIn('vault', {item['id'] for item in status})
        self.assertNotIn('Secret Moon Vault', json.dumps(status))
        self.assertNotIn('Spoiler', json.dumps(status))
        # Its edge must not survive on the visible place either, or a reader
        # learns the hidden id from a neighbour's data.
        village = next(item for item in status if item['id'] == 'village')
        self.assertEqual(village['connected_to'], [])

        # The legacy endpoint uses the same projection, so it cannot leak either.
        legacy = self.client.get(f'/worlds/{self.world}/location-map').json()['locations']
        self.assertNotIn('vault', {item['id'] for item in legacy})
        self.assertNotIn('Secret Moon Vault', json.dumps(legacy))

        preview = self.client.post(f'/worlds/{self.world}/travel/preview',
                                   json={'destination': 'Secret Moon Vault'}).json()
        self.assertEqual(preview['status'], 'blocked')
        self.assertEqual(preview['reason'], 'destination_undiscovered')
        self.assertNotIn('Secret Moon Vault', json.dumps(preview))

    def test_creator_can_preview_and_commit_route_and_inventory_edits(self):
        self.write('location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village', 'x': 10, 'y': 10,
             'connected_to': [], 'is_starting_location': True},
            {'id': 'forest', 'name': 'Forest', 'x': 50, 'y': 50, 'connected_to': []},
        ]})
        changes = [
            {'kind': 'location', 'location_id': 'village', 'field': 'connected_to',
             'value': [{'to': 'forest', 'travel_time_minutes': 45, 'danger': .2}]},
            {'kind': 'inventory_item', 'character_id': 'char_xueli', 'operation': 'add',
             'item': {'instance_id': 'rope_1', 'item_id': 'rope', 'name': 'Rope'}},
        ]
        before_map = self.read('location_map.json')
        preview = self.post('creator/edit', {'expected_revision': 0, 'preview': True, 'changes': changes})
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()['ok'])
        self.assertEqual(self.read('location_map.json'), before_map)
        commit = self.post('creator/edit', {'expected_revision': 0, 'changes': changes})
        self.assertEqual(commit.status_code, 200, commit.text)
        self.assertEqual(self.read('location_map.json')['locations'][0]['connected_to'][0]['travel_time_minutes'], 45)
        inventory = self.read('character_state.json')['characters']['char_xueli']['inventory']
        self.assertTrue(any(isinstance(item, dict) and item.get('instance_id') == 'rope_1' for item in inventory))

    def test_dangerous_travel_can_interrupt_and_still_advance_time(self):
        config = self.read('world_config.json')
        config['story_clock'].update(tick=0, day=1, time_of_day='morning')
        self.write('world_config.json', config)
        characters = self.read('character_state.json')
        characters['characters']['char_xueli']['location'] = 'Village'
        self.write('character_state.json', characters)
        timeline = self.read('canon_timeline.json')
        current = next(cp for cp in timeline['checkpoints'] if cp['checkpoint_id'] == config['current_checkpoint_id'])
        current['boundary']['locations'] = ['Village', 'Forest']
        self.write('canon_timeline.json', timeline)
        self.write('location_map.json', {'locations': [
            {'id': 'village', 'name': 'Village', 'x': 10, 'y': 10,
             'connected_to': [{'to': 'forest', 'travel_time_minutes': 120,
                               'danger': 1.0, 'tags': ['bandit_road']}]},
            {'id': 'forest', 'name': 'Forest', 'x': 40, 'y': 10, 'connected_to': ['village']},
        ]})
        response = self.post('chapter/continue', {'user_input': 'Travel to Forest.'})
        self.assertEqual(response.status_code, 200, response.text)
        travel = response.json()['chapter']['travel_resolution']
        self.assertEqual(travel['status'], 'interrupted')
        self.assertEqual(travel['stopped_at'], 'Village')
        self.assertEqual(travel['interrupted_leg']['tags'], ['bandit_road'])
        self.assertEqual(travel['elapsed_minutes'], 60)
        self.assertEqual(self.read('character_state.json')['characters']['char_xueli']['location'], 'Village')
        self.assertEqual(self.read('world_config.json')['story_clock']['elapsed_minutes'], 60)

        active = self.read('world_config.json')['active_journey']
        self.assertEqual(active['status'], 'interrupted')
        self.assertEqual(active['destination'], 'Forest')
        self.assertEqual(active['destination_id'], 'forest')
        self.assertEqual(active['remaining_legs'][0]['travel_time_minutes'], 60)
        state = self.client.get(f'/worlds/{self.world}/play-state').json()
        self.assertEqual(state['active_journey']['journey_id'], active['journey_id'])

        continued = self.post('chapter/continue', {'user_input': 'Continue journey.'})
        self.assertEqual(continued.status_code, 200, continued.text)
        resolution = continued.json()['chapter']['travel_resolution']
        self.assertEqual(resolution['reason'], 'continued_journey')
        self.assertEqual(resolution['elapsed_minutes'], 60)
        self.assertEqual(self.read('character_state.json')['characters']['char_xueli']['location'], 'Forest')
        self.assertIsNone(self.read('world_config.json')['active_journey'])
        self.assertEqual(self.read('world_config.json')['story_clock']['elapsed_minutes'], 120)

    def test_codex_returns_unlocked_cards_without_npc_private_state(self):
        characters = self.read('character_state.json')
        protagonist = characters['characters']['char_xueli']
        npc = characters['characters']['char_gu_changge']
        protagonist['secrets'] = ['the player knows this about herself']
        npc.update({
            'secrets': ['hidden betrayal'],
            'knowledge': [{'statement': 'private fact'}],
            'relationship_memories': [{'statement': 'private memory'}],
            'internal_state': {'plan': 'escape'},
        })
        self.write('character_state.json', characters)

        response = self.client.get(f'/worlds/{self.world}/codex')
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertIsInstance(body['cards'], list)
        self.assertIsInstance(body['characters'], dict)
        self.assertIn('inventory', body['characters']['char_xueli'])
        self.assertNotIn('secrets', body['characters']['char_xueli'])
        for private_field in ('secrets', 'knowledge', 'relationship_memories', 'internal_state'):
            self.assertNotIn(private_field, body['characters']['char_gu_changge'])

    def test_clear_api_key_clears_both_aliases_and_preserves_model(self):
        self.client.put('/runtime-config', json={'api_key': 'not-a-real-test-key', 'model_name': 'test/model'})
        response = self.client.delete('/runtime-config/api-key')
        self.assertFalse(response.json()['has_api_key'])
        persisted = json.loads((self.data / 'runtime_config.json').read_text(encoding='utf-8'))
        self.assertEqual(persisted['api_key'], '')
        self.assertEqual(persisted['openrouter_api_key'], '')
        self.assertEqual(persisted['fallback_chain'], [])
        self.assertEqual(persisted['model_name'], 'test/model')

    def test_world_key_takes_priority_over_app_fallback_chain(self):
        self.client.put('/runtime-config', json={'api_key': 'app-placeholder', 'model_name': 'app/model'})
        self.client.put(f'/worlds/{self.world}/runtime-config', json={
            'openrouter_api_key': 'world-placeholder', 'openrouter_model': 'world/model',
        })
        selected = storage.get_effective_fallback_chain(self.world)
        self.assertEqual(selected[0]['api_key'], 'world-placeholder')
        self.assertEqual(selected[0]['model'], 'world/model')
        self.client.delete(f'/worlds/{self.world}/runtime-config/api-key')
        self.assertEqual(storage.get_effective_fallback_chain(self.world)[0]['api_key'], 'app-placeholder')

    SENTINEL = 'sk-or-v1-SENTINEL-do-not-leak-98765'

    def test_runtime_config_get_and_export_never_return_plaintext_key(self):
        self.client.put('/runtime-config', json={'api_key': self.SENTINEL, 'model_name': 'test/model'})
        self.client.put(f'/worlds/{self.world}/runtime-config', json={'openrouter_api_key': self.SENTINEL})

        app_status = self.client.get('/runtime-config')
        world_status = self.client.get(f'/worlds/{self.world}/runtime-config')
        export_pkg = self.client.get(f'/worlds/{self.world}/export')

        for label, response in (('app GET', app_status), ('world GET', world_status), ('export', export_pkg)):
            self.assertNotIn(self.SENTINEL, response.text, f'{label} leaked the raw api key')
            self.assertEqual(response.status_code, 200)
        self.assertFalse(app_status.json().get('api_key'), 'GET must not expose a plaintext api_key field')
        self.assertEqual(export_pkg.json()['runtime_override']['openrouter_api_key'], '')
        self.assertEqual(export_pkg.json()['runtime_override']['fallback_chain'], [])
        self.assertEqual(storage.get_effective_api_key(self.world), self.SENTINEL)

    def test_saving_model_with_blank_key_keeps_existing_key(self):
        self.client.put('/runtime-config', json={'api_key': self.SENTINEL, 'model_name': 'test/model'})
        response = self.client.put('/runtime-config', json={'api_key': '', 'model_name': 'test/other-model'})
        self.assertTrue(response.json()['has_api_key'], 'blank key box must not delete the stored key')
        self.assertEqual(storage.get_effective_api_key(), self.SENTINEL)
        self.assertEqual(storage.read_runtime_config()['model_name'], 'test/other-model')

    def test_masked_key_value_is_treated_as_keep(self):
        self.client.put('/runtime-config', json={'api_key': self.SENTINEL, 'model_name': 'test/model'})
        masked = self.client.get('/runtime-config').json()['api_key_masked']
        response = self.client.put('/runtime-config', json={'api_key': masked, 'model_name': 'test/model'})
        self.assertTrue(response.json()['has_api_key'])
        self.assertEqual(storage.get_effective_api_key(), self.SENTINEL)

    def test_explicit_delete_action_and_endpoint_remove_key(self):
        self.client.put('/runtime-config', json={'api_key': self.SENTINEL, 'model_name': 'test/model'})
        response = self.client.put('/runtime-config', json={'api_key_action': 'delete'})
        self.assertFalse(response.json()['has_api_key'])
        self.assertEqual(storage.get_effective_api_key(), '')

        self.client.put('/runtime-config', json={'api_key': self.SENTINEL})
        response = self.client.delete('/runtime-config/api-key')
        self.assertFalse(response.json()['has_api_key'])
        self.assertEqual(storage.get_effective_api_key(), '')

    def test_world_key_stored_under_legacy_api_key_spelling_is_usable(self):
        """A world override written with the short spelling must not be inert.

        Older builds wrote the world key as "api_key" while the resolver read
        "openrouter_api_key", so a stored key resolved to nothing and the world
        silently fell back to the app chain.
        """
        override = self.path / 'runtime_override.json'
        override.write_text(json.dumps({'api_key': self.SENTINEL}), encoding='utf-8')
        self.assertEqual(storage.get_effective_api_key(self.world), self.SENTINEL)
        chain = storage.get_effective_fallback_chain(self.world)
        self.assertEqual([node['api_key'] for node in chain], [self.SENTINEL])

    def test_deleting_world_key_clears_both_spellings(self):
        """Deleting a world key must not leave a usable key behind.

        The resolver prefers "api_key" over "openrouter_api_key", so clearing
        only the long spelling kept the "deleted" key in force.
        """
        override = self.path / 'runtime_override.json'
        override.write_text(json.dumps({'api_key': self.SENTINEL,
                                        'openrouter_api_key': self.SENTINEL}), encoding='utf-8')
        response = self.client.delete(f'/worlds/{self.world}/runtime-config/api-key')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.get_effective_api_key(self.world), '')
        self.assertEqual(storage.get_effective_fallback_chain(self.world), [])
        self.assertEqual(response.json()['has_api_key'], False)

    def test_deleting_world_key_via_put_action_clears_short_spelling(self):
        override = self.path / 'runtime_override.json'
        override.write_text(json.dumps({'api_key': self.SENTINEL,
                                        'openrouter_api_key': self.SENTINEL}), encoding='utf-8')
        response = self.client.put(f'/worlds/{self.world}/runtime-config',
                                   json={'api_key_action': 'delete'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(storage.get_effective_api_key(self.world), '')
        self.assertEqual(storage.get_effective_fallback_chain(self.world), [])

    def test_chain_node_without_key_does_not_shadow_a_usable_key(self):
        """A keyless node is not a usable node and must not win over a real key.

        A world chain whose node carries no api_key would be sent to the
        provider without credentials; the resolver must treat the node as
        absent instead of returning it.
        """
        override = self.path / 'runtime_override.json'
        override.write_text(json.dumps({'fallback_chain': [
            {'provider': 'custom', 'model': 'x', 'api_key': '', 'base_url': ''}
        ]}), encoding='utf-8')
        self.assertEqual(storage.get_effective_fallback_chain(self.world), [])
        self.assertFalse(storage.has_real_api_key(self.world))

    def test_connection_error_message_does_not_leak_key(self):
        self.client.put('/runtime-config', json={'api_key': self.SENTINEL, 'model_name': 'test/model'})
        with patch.object(main.requests, 'post',
                          side_effect=main.requests.exceptions.RequestException('simulated network failure')):
            response = self.client.post('/runtime-config/test-connection')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['ok'])
        self.assertNotIn(self.SENTINEL, response.text)

    def test_foreign_origin_is_rejected_but_local_origin_allowed(self):
        foreign = self.client.get('/worlds', headers={'Origin': 'http://evil.example'})
        self.assertEqual(foreign.status_code, 403)
        foreign_write = self.client.post(f'/worlds/{self.world}/chapter/continue',
                                         json={'user_input': 'hi'}, headers={'Origin': 'http://evil.example'})
        self.assertEqual(foreign_write.status_code, 403)

        local_get = self.client.get('/worlds', headers={'Origin': 'http://localhost:5173'})
        self.assertEqual(local_get.status_code, 200)
        no_origin = self.client.get('/worlds')
        self.assertEqual(no_origin.status_code, 200)

    def test_output_length_persists_and_is_exposed_in_play_state(self):
        response = self.client.put(f'/worlds/{self.world}/world_config', json={'output_length': 'Concise'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['world_config']['output_length'], 'Concise')
        state = self.client.get(f'/worlds/{self.world}/play-state').json()
        self.assertEqual(state['output_length'], 'Concise')

    def make_legacy_world(self, name='legacy_schema_world'):
        path = self.data / 'worlds' / name
        path.mkdir(parents=True)
        (path / 'world_config.json').write_text(json.dumps({
            'display_name': 'Legacy', 'protagonist_id': 'char_xueli', 'completed_checkpoints': [],
        }), encoding='utf-8')
        (path / 'character_state.json').write_text(json.dumps({'characters': {
            'char_xueli': {'name': 'Xueli', 'affinity': {}, 'power_stat': {}, 'knowledge_flags': [],
                           'inventory': [], 'relationships': {}, 'alive': True},
        }}), encoding='utf-8')
        (path / 'card_registry.json').write_text(json.dumps({'cards': []}), encoding='utf-8')
        (path / 'canon_timeline.json').write_text(json.dumps({'checkpoints': []}), encoding='utf-8')
        return path

    def test_schema_migration_is_idempotent_and_backs_up_original(self):
        path = self.make_legacy_world()
        original_config = (path / 'world_config.json').read_bytes()
        first = schema.ensure_current_schema(str(path))
        self.assertTrue(first['migrated'])
        self.assertEqual(schema.read_schema_version(str(path)), schema.SCHEMA_VERSION)
        self.assertTrue(os.path.isdir(first['backup']))
        self.assertTrue((Path(first['backup']) / 'world_config.json').exists())
        migrated_bytes = (path / 'world_config.json').read_bytes()
        self.assertNotEqual(migrated_bytes, original_config)

        second = schema.ensure_current_schema(str(path))
        self.assertFalse(second['migrated'])
        self.assertEqual((path / 'world_config.json').read_bytes(), migrated_bytes)

    def test_schema_migration_rejects_newer_version(self):
        path = self.make_legacy_world('newer_schema_world')
        config = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        config['schema_version'] = schema.SCHEMA_VERSION + 1
        (path / 'world_config.json').write_text(json.dumps(config), encoding='utf-8')
        before = (path / 'world_config.json').read_bytes()
        with self.assertRaises(schema.SchemaVersionError):
            schema.ensure_current_schema(str(path))
        self.assertEqual((path / 'world_config.json').read_bytes(), before)

    def test_corrupt_or_missing_json_is_not_silently_overwritten(self):
        path = self.make_legacy_world('corrupt_schema_world')
        (path / 'world_config.json').write_text('{not valid json', encoding='utf-8')
        before = (path / 'world_config.json').read_bytes()
        with self.assertRaises(schema.SchemaVersionError):
            schema.ensure_current_schema(str(path))
        self.assertEqual((path / 'world_config.json').read_bytes(), before)

        clean = self.make_legacy_world('missing_schema_world')
        (clean / 'location_map.json').unlink(missing_ok=True)
        schema.ensure_current_schema(str(clean))
        self.assertTrue((clean / 'location_map.json').exists())
        self.assertEqual(json.loads((clean / 'location_map.json').read_text(encoding='utf-8')), {'locations': []})

    def test_get_world_migrates_legacy_world_and_export_reports_version(self):
        self.make_legacy_world('legacy_api_world')
        response = self.client.get('/worlds/legacy_api_world')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['world_config']['schema_version'], schema.SCHEMA_VERSION)
        export = self.client.get('/worlds/legacy_api_world/export').json()
        self.assertEqual(export['schema_version'], schema.SCHEMA_VERSION)
        self.assertEqual(export['world_config']['schema_version'], schema.SCHEMA_VERSION)

    def test_import_rejects_newer_schema_version(self):
        response = self.client.post('/worlds/import', json={
            'world_name': 'too_new_world',
            'package_data': {
                'schema_version': schema.SCHEMA_VERSION + 5,
                'world_config': {'display_name': 'Too New'},
                'card_registry': {'cards': []},
                'canon_timeline': {'checkpoints': []},
                'character_state': {'characters': {}},
            },
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('newer', response.json()['detail'])
        self.assertFalse((self.data / 'worlds' / 'too_new_world').exists())

    @staticmethod
    def import_package(**extra):
        package = {
            'world_config': {'display_name': 'Importable'},
            'card_registry': {'cards': []},
            'canon_timeline': {'checkpoints': []},
            'character_state': {'characters': {}},
        }
        package.update(extra)
        return package

    def test_import_writes_optional_core_files(self):
        response = self.client.post('/worlds/import', json={
            'world_name': 'import_ok',
            'package_data': self.import_package(
                world_events={'events': [{'event_id': 'ev_1'}]},
                location_map={'locations': [{'id': 'loc_1', 'name': 'Hall'}]},
            ),
        })
        self.assertEqual(response.status_code, 200, response.text)
        world = self.data / 'worlds' / 'import_ok'
        self.assertEqual(self.read_from(world, 'world_events.json')['events'], [{'event_id': 'ev_1'}])
        self.assertEqual(self.read_from(world, 'location_map.json')['locations'][0]['id'], 'loc_1')

    def test_import_rejects_optional_core_file_with_wrong_collection_type(self):
        response = self.client.post('/worlds/import', json={
            'world_name': 'import_bad_events',
            'package_data': self.import_package(world_events={'events': 'not-a-list'}),
        })
        self.assertEqual(response.status_code, 400)
        self.assertIn('world_events.events', response.json()['detail'])
        # Validation runs before the first write, so no half-created world is left.
        self.assertFalse((self.data / 'worlds' / 'import_bad_events').exists())

    def test_import_rejects_non_loopback_override_base_url(self):
        for hostile in (
            'http://attacker.example/v1',
            'http://127.0.0.1@attacker.example/v1',
            'http://notlocalhost.example/v1',
            'http://attacker.example/18789',
        ):
            with self.subTest(base_url=hostile):
                sanitized = world_routes.sanitize_imported_runtime_override({
                    'fallback_chain': [{'provider': 'custom', 'model': 'm', 'base_url': hostile}],
                })
                self.assertEqual(sanitized.get('fallback_chain', []), [])

    def test_import_keeps_loopback_override_base_url(self):
        for local in (
            'http://localhost:11434/v1',
            'http://127.0.0.1:18789/v1',
            'http://[::1]:1234/v1',
        ):
            with self.subTest(base_url=local):
                sanitized = world_routes.sanitize_imported_runtime_override({
                    'fallback_chain': [{'provider': 'custom', 'model': 'm', 'base_url': local}],
                })
                chain = sanitized.get('fallback_chain', [])
                self.assertEqual(len(chain), 1)
                self.assertEqual(chain[0]['base_url'], local)
                self.assertEqual(chain[0]['api_key'], '')

    def test_is_loopback_url_rejects_malformed_and_scheme_less_urls(self):
        # A scheme-less or malformed base_url cannot be posted to anyway, so it
        # must not be accepted as "local" either.
        for rejected in ('localhost:11434/v1', 'http://localhost:abc/v1', '', 'file:///etc/passwd'):
            with self.subTest(url=rejected):
                self.assertFalse(security.is_loopback_url(rejected))

    def corrupt(self, filename):
        (self.path / filename).write_text('{ this is not json', encoding='utf-8')

    def test_export_reports_unreadable_file_instead_of_dropping_it(self):
        self.corrupt('world_events.json')
        response = self.client.get(f'/worlds/{self.world}/export')
        self.assertEqual(response.status_code, 200, response.text)
        pkg = response.json()
        # The unreadable file is absent, but the package says so rather than
        # letting the recipient believe the copy is complete.
        self.assertNotIn('world_events', pkg)
        self.assertEqual([w['file'] for w in pkg['export_warnings']], ['world_events.json'])

    def test_corrupt_world_file_answers_409_not_500(self):
        for filename, route in (('character_state.json', f'/worlds/{self.world}'),
                                ('world_events.json', f'/worlds/{self.world}/journal')):
            with self.subTest(filename=filename):
                target = self.path / filename
                original = target.read_bytes() if target.exists() else None
                self.corrupt(filename)
                response = self.client.get(route)
                self.assertEqual(response.status_code, 409, response.text)
                self.assertIn(filename, response.json()['detail'])
                if original is None:
                    target.unlink()
                else:
                    target.write_bytes(original)

    def test_affinity_graph_contains_only_character_relationships(self):
        state = self.read('character_state.json')
        characters = state['characters']
        known = next(iter(characters))
        characters[known]['relationships'] = {
            'char_never_introduced': {'label': 'owes'},
            known: {'label': 'self'},
        }
        self.write('character_state.json', state)
        self.write('location_map.json', {'locations': [
            {'id': 'loc_known', 'name': 'Known', 'connected_to': [
                {'to': 'loc_never_visited', 'travel_time_minutes': 90},
            ]},
            {'id': 'loc_never_visited', 'name': 'Unvisited'},
        ]})

        graph = self.client.get(f'/worlds/{self.world}/affinity-graph').json()
        node_ids = {node['id'] for node in graph['nodes']}
        self.assertIn(known, node_ids)
        self.assertNotIn('char_never_introduced', node_ids)
        self.assertTrue(all(node['type'] == 'character' for node in graph['nodes']))
        self.assertNotIn('loc_known', node_ids)
        self.assertNotIn('loc_never_visited', node_ids)
        for edge in graph['edges']:
            self.assertIn(edge['source'], node_ids)
            self.assertIn(edge['target'], node_ids)
            self.assertNotEqual(edge['source'], edge['target'])
        self.assertNotIn({'source': 'loc_known', 'target': 'loc_never_visited', 'label': 'connected'},
                         graph['edges'])

    def test_map_restrictions_warn_once_when_world_has_no_protagonist(self):
        location_map = {'locations': [{'id': 'gate', 'name': 'Gate', 'unlock_exp': 50}]}
        state_changes = {'characters': {'char_xueli': {'location': 'Gate'}}}
        config = {k: v for k, v in self.cfg.items() if k not in ('protagonist_id', 'main_character_id')}

        map_rules._missing_protagonist_warned = False
        with self.assertLogs('app.world.map_rules', level='WARNING') as captured:
            self.assertEqual(check_map_based_restrictions(
                state_changes, location_map, self.read('character_state.json')['characters'], config), [])
            # A second call in the same process must not log again.
            check_map_based_restrictions(
                state_changes, location_map, self.read('character_state.json')['characters'], config)
        self.assertEqual(len(captured.records), 1)
        self.assertIn('protagonist_id', captured.records[0].getMessage())

        # With a protagonist the gate is evaluated again, so the early return
        # only covers the genuinely unconfigured world.
        config['main_character_id'] = 'char_xueli'
        self.assertTrue(check_map_based_restrictions(
            state_changes, location_map, self.read('character_state.json')['characters'], config))

    def test_save_includes_style_card_and_schema_version(self):
        self.client.put(f'/worlds/{self.world}/style-card', json={
            'perspective': 'third_person_limited', 'voice': 'narrative', 'pacing': 'moderate',
            'tone': 'balanced', 'prose_guidelines': [], 'taboo_words': [], 'custom_instructions': 'keep',
        })
        save_id = self.post('saves', {'label': 'with style'}).json()['save']['save_id']
        snap = self.path / 'saves' / save_id
        self.assertTrue((snap / 'style_card.json').exists())
        self.assertTrue((snap / 'world_config.json').exists())
        snap_config = json.loads((snap / 'world_config.json').read_text(encoding='utf-8'))
        self.assertEqual(snap_config['schema_version'], schema.SCHEMA_VERSION)

    def _checker_fake(self, checker):
        def fake(system_prompt, user_prompt, user_input_for_mock='', mock_response=None, world_name=None, role=None):
            if system_prompt == main.CONSISTENCY_CHECKER_SYSTEM_PROMPT:
                return checker()
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                return main.mock_planner_response(user_input_for_mock)
            if system_prompt == main.WRITER_SYSTEM_PROMPT:
                return main.mock_narrator_response(user_input_for_mock)
            return main.mock_consistency_checker_response()
        return fake

    def test_checker_unavailable_keeps_draft_and_does_not_commit(self):
        before_tick = self.read('world_config.json')['story_clock']['tick']
        before_chapters = len(self.read('chapters.json')['chapters'])

        def checker():
            raise main.RateLimitError('checker down', retry_after=1)

        with patch.object(main, 'call_llm', side_effect=self._checker_fake(checker)):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 503, response.text)
        detail = response.json()['detail']
        self.assertEqual(detail['status'], 'unavailable')
        self.assertFalse(detail['persisted'])
        self.assertTrue(detail['retryable'])
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], before_tick)
        self.assertEqual(len(self.read('chapters.json')['chapters']), before_chapters)

    def test_corrupt_checker_json_counts_as_unavailable(self):
        def checker():
            return 'this is not json {{{'

        with patch.object(main, 'call_llm', side_effect=self._checker_fake(checker)):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['status'], 'unavailable')

    def test_major_after_rewrite_is_not_committed_after_recheck(self):
        calls = {'n': 0}

        def checker():
            calls['n'] += 1
            return json.dumps({'consistent': False, 'severity': 'major',
                               'issues': ['contradiction'], 'explanation': 'bad'})

        before_tick = self.read('world_config.json')['story_clock']['tick']
        before_chapters = len(self.read('chapters.json')['chapters'])
        with patch.object(main, 'call_llm', side_effect=self._checker_fake(checker)):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 422, response.text)
        detail = response.json()['detail']
        self.assertEqual(detail['status'], 'failed')
        self.assertFalse(detail['persisted'])
        self.assertEqual(detail['reason'], 'consistency_check_failed')
        self.assertEqual(calls['n'], 2, 'checker must run on the original and on the rewritten text')
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], before_tick)
        self.assertEqual(len(self.read('chapters.json')['chapters']), before_chapters)

    def test_checker_schema_violations_are_unavailable_not_passed(self):
        from app.story.consistency import parse_checker_response
        for bad in (
            '{}',
            '{"consistent":"false","severity":"unknown","issues":42}',
            '{"consistent":true,"severity":"major","issues":[]}',
            '{"consistent":false,"severity":"none","issues":[]}',
            '{"consistent":true,"severity":"none"}',
        ):
            with self.subTest(raw=bad):
                result = parse_checker_response(bad)
                self.assertEqual(result['status'], 'unavailable', f'{bad} must not pass')
        good = parse_checker_response('{"consistent":true,"severity":"none","issues":[],"explanation":""}')
        self.assertEqual(good['status'], 'passed')
        bad_major = parse_checker_response('{"consistent":false,"severity":"major","issues":["x"],"explanation":""}')
        self.assertEqual(bad_major['status'], 'failed')

    def test_checker_receives_clock_window_and_proposed_location(self):
        from app.story.consistency import build_consistency_checker_payload
        config = {
            'timekeeping_mode': 'duration', 'calendar': {'kind': 'gregorian'},
            'protagonist_id': 'hero',
            'story_clock': normalize_start_clock(
                {'year': 2024, 'month': 9, 'day': 15, 'minute_of_day': 1045},
                {'kind': 'gregorian'}),
        }
        payload = build_consistency_checker_payload(
            'They only step outside the bookshop.',
            {'characters': {'hero': {'location': 'Station South Exit'}},
             'elapsed_time': {'minutes': 4}},
            config, {}, [], {'hero': {'location': 'Bookshop'}},
        )
        alignment = payload['temporal_spatial_alignment']
        self.assertEqual(alignment['clock_at_turn_start']['minute_of_day'], 1045)
        self.assertEqual(alignment['clock_after_proposed_turn']['minute_of_day'], 1049)
        self.assertEqual(alignment['protagonist_location_before'], 'Bookshop')
        self.assertEqual(alignment['protagonist_location_after_proposed_turn'], 'Station South Exit')

    def test_state_sync_failure_blocks_commit_even_when_lore_passes(self):
        from app.story.consistency import parse_checker_response
        result = parse_checker_response(json.dumps({
            'consistent': True, 'severity': 'minor', 'state_sync': False,
            'issues': ['State says station; prose ends outside bookshop.'],
            'explanation': 'The proposed location is ahead of the scene.',
        }))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['severity'], 'major')

    def test_player_stop_point_is_blocking_even_when_state_matches_prose(self):
        from app.story.consistency import parse_checker_response
        result = parse_checker_response(json.dumps({
            'consistent': True, 'severity': 'minor', 'state_sync': True,
            'action_scope': False,
            'issues': ['The player stopped at the entrance; prose walked onto the veranda.'],
            'explanation': 'The scene passed the requested stopping point.',
        }))
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['severity'], 'major')

    def test_successful_rewrite_is_rechecked_and_passes(self):
        calls = {'n': 0}

        def checker():
            calls['n'] += 1
            if calls['n'] == 1:
                return json.dumps({'consistent': False, 'severity': 'major',
                                   'issues': ['contradiction'], 'explanation': 'bad'})
            return json.dumps({'consistent': True, 'severity': 'none', 'issues': [], 'explanation': 'fixed'})

        with patch.object(main, 'call_llm', side_effect=self._checker_fake(checker)):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        cc = response.json()['chapter']['consistency_check']
        self.assertEqual(cc['status'], 'passed')
        self.assertTrue(cc['triggered_rewrite'])
        self.assertEqual(calls['n'], 2)

    def test_allow_unchecked_commit_opts_back_into_legacy_fail_open(self):
        self.cfg['allow_unchecked_commit'] = True
        self.write('world_config.json', self.cfg)

        def checker():
            raise main.RateLimitError('checker down', retry_after=1)

        with patch.object(main, 'call_llm', side_effect=self._checker_fake(checker)):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['chapter']['consistency_check']['status'], 'unavailable')

    def test_world_replace_key_updates_stored_chain_key(self):
        self.client.put(f'/worlds/{self.world}/runtime-config', json={
            'fallback_chain': [{
                'provider': 'openrouter', 'model': 'world-model',
                'api_key': 'world-old-placeholder', 'base_url': '',
            }],
        })
        self.client.put(f'/worlds/{self.world}/runtime-config', json={'openrouter_api_key': 'world-new-placeholder'})
        chain = storage.get_effective_fallback_chain(self.world)
        self.assertEqual(chain[0]['api_key'], 'world-new-placeholder')
        self.assertEqual(chain[0]['model'], 'world-model')

    def test_world_editor_toggle_does_not_rewrite_provider_or_url(self):
        self.client.put('/runtime-config', json={
            'llm_provider': 'custom', 'base_url': 'https://app.example/v1',
        })
        self.client.put(f'/worlds/{self.world}/runtime-config', json={
            'fallback_chain': [{
                'provider': 'featherless', 'model': 'world-model',
                'api_key': 'world-placeholder', 'base_url': 'https://world.example/v1',
            }],
        })
        response = self.client.put(f'/worlds/{self.world}/runtime-config', json={'editor_enabled': True})
        self.assertTrue(response.json()['editor_enabled'])
        chain = storage.get_effective_fallback_chain(self.world)
        self.assertEqual(chain[0]['provider'], 'featherless')
        self.assertEqual(chain[0]['base_url'], 'https://world.example/v1')
        self.assertEqual(chain[0]['api_key'], 'world-placeholder')

    def test_keep_key_updates_effective_chain_target(self):
        self.client.put('/runtime-config', json={
            'api_key': self.SENTINEL, 'model_name': 'model/A',
            'llm_provider': 'openrouter', 'base_url': 'https://a.example/v1',
        })
        response = self.client.put('/runtime-config', json={
            'api_key_action': 'keep', 'model_name': 'model/B',
            'llm_provider': 'custom', 'base_url': 'https://b.example/v1',
        })
        self.assertTrue(response.json()['has_api_key'])
        chain = storage.get_effective_fallback_chain()
        self.assertEqual(chain[0]['model'], 'model/B')
        self.assertEqual(chain[0]['provider'], 'custom')
        self.assertEqual(chain[0]['base_url'], 'https://b.example/v1')
        self.assertEqual(chain[0]['api_key'], self.SENTINEL)

        captured = {}

        class _Resp:
            status_code = 200
            headers = {'Content-Type': 'application/json'}

            def json(self):
                return {'choices': [{'message': {'content': 'hi'}}]}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured['url'] = url
            captured['payload'] = json
            return _Resp()

        with patch.object(main.requests, 'post', side_effect=fake_post):
            result = self.client.post('/runtime-config/test-connection').json()
        self.assertTrue(result['ok'])
        self.assertEqual(captured['payload']['model'], 'model/B')
        self.assertTrue(captured['url'].startswith('https://b.example'))

    def test_newer_schema_world_is_blocked_on_write_without_state_change(self):
        config = self.read('world_config.json')
        config['schema_version'] = schema.SCHEMA_VERSION + 3
        self.write('world_config.json', config)
        before = {p.name: p.read_bytes() for p in self.path.glob('*.json')}

        response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.glob('*.json')})

        creator = self.client.put(f'/worlds/{self.world}/world_config', json={'tone': 'changed'})
        self.assertEqual(creator.status_code, 409)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.glob('*.json')})

    def test_restore_and_branch_reject_newer_snapshot(self):
        save = self.post('saves', {'label': 'snap'}).json()['save']['save_id']
        snap_config = self.path / 'saves' / save / 'world_config.json'
        data = json.loads(snap_config.read_text(encoding='utf-8'))
        data['schema_version'] = schema.SCHEMA_VERSION + 1
        snap_config.write_text(json.dumps(data), encoding='utf-8')

        before = {p.name: p.read_bytes() for p in self.path.glob('*.json')}
        restore = self.post(f'saves/{save}/restore')
        self.assertEqual(restore.status_code, 409, restore.text)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.glob('*.json')})

        branch = self.post(f'saves/{save}/branch', {'new_world_name': 'newer_snapshot_branch'})
        self.assertEqual(branch.status_code, 409, branch.text)
        self.assertFalse((self.path.parent / 'newer_snapshot_branch').exists())

    def test_missing_world_config_is_rebuilt_from_full_template(self):
        path = self.make_legacy_world('missing_cfg_world')
        (path / 'world_config.json').unlink()
        info = schema.ensure_current_schema(str(path))
        self.assertTrue(info['migrated'])
        self.assertIn('world_config.json', info['recovered'])
        config = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        self.assertEqual(config['schema_version'], schema.SCHEMA_VERSION)
        self.assertIn('display_name', config)
        self.assertIn('output_length', config)
        self.assertGreater(len(config), 3, 'recovered config must not be just a version marker')

    def test_duplicate_request_replays_without_calling_model(self):
        calls = {'n': 0}

        def counting(*args, **kwargs):
            calls['n'] += 1
            return self.fake_llm(*args, **kwargs)

        with patch.object(main, 'call_llm', side_effect=counting):
            first = self.post('chapter/continue', {
                'user_input': 'Observe.', 'request_id': 'req-dup-1', 'expected_revision': 0,
            })
            self.assertEqual(first.status_code, 200, first.text)
            self.assertEqual(first.json()['revision'], 1)
            calls_after_first = calls['n']
            self.assertGreater(calls_after_first, 0)

            replay = self.post('chapter/continue', {
                'user_input': 'Observe.', 'request_id': 'req-dup-1', 'expected_revision': 0,
            })
            self.assertEqual(replay.status_code, 200, replay.text)
            self.assertEqual(replay.json()['chapter'], first.json()['chapter'])
            self.assertEqual(calls['n'], calls_after_first, 'replay must not call the model again')

            conflict = self.post('chapter/continue', {
                'user_input': 'A different action.', 'request_id': 'req-dup-1', 'expected_revision': 0,
            })
            self.assertEqual(conflict.status_code, 409)
            self.assertEqual(conflict.json()['detail']['reason'], 'request_id_conflict')

        self.assertEqual(len(self.read('chapters.json')['chapters']), 1)

    def test_stale_revision_is_rejected_without_side_effects(self):
        first = self.post('chapter/continue', {
            'user_input': 'Observe.', 'request_id': 'req-rev-1', 'expected_revision': 0,
        })
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(first.json()['revision'], 1)
        turns_before = len(self.read('chapters.json')['chapters'])
        tick_before = self.read('world_config.json')['story_clock']['tick']

        stale = self.post('chapter/continue', {
            'user_input': 'Another action.', 'request_id': 'req-rev-2', 'expected_revision': 0,
        })
        self.assertEqual(stale.status_code, 409, stale.text)
        self.assertEqual(stale.json()['detail']['reason'], 'stale_revision')
        self.assertEqual(len(self.read('chapters.json')['chapters']), turns_before)
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], tick_before)

    def test_creator_write_bumps_revision_and_invalidates_stale_turn(self):
        state = self.client.get(f'/worlds/{self.world}/play-state').json()
        self.assertEqual(state['revision'], 0)
        updated = self.client.put(f'/worlds/{self.world}/world_config', json={'tone': 'changed'})
        self.assertEqual(updated.status_code, 200)
        state2 = self.client.get(f'/worlds/{self.world}/play-state').json()
        self.assertEqual(state2['revision'], 1)

        stale = self.post('chapter/continue', {
            'user_input': 'Observe.', 'expected_revision': 0,
        })
        self.assertEqual(stale.status_code, 409)

    def test_restore_clears_receipts_and_advances_revision(self):
        save = self.post('saves', {'label': 'pre-turn'}).json()['save']['save_id']
        first = self.post('chapter/continue', {
            'user_input': 'Observe.', 'request_id': 'req-restore-1', 'expected_revision': 0,
        })
        self.assertEqual(first.status_code, 200, first.text)

        restore = self.post(f'saves/{save}/restore')
        self.assertEqual(restore.status_code, 200, restore.text)
        self.assertEqual(restore.json()['revision'], 2)

        # The old receipt must not replay across a restore, and the old revision
        # is now stale.
        retry = self.post('chapter/continue', {
            'user_input': 'Observe.', 'request_id': 'req-restore-1', 'expected_revision': 0,
        })
        self.assertEqual(retry.status_code, 409, retry.text)

    def test_concurrent_same_request_id_runs_pipeline_once(self):
        planner_calls = {'n': 0}

        def counting(system_prompt, *args, **kwargs):
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                planner_calls['n'] += 1
            return self.fake_llm(system_prompt, *args, **kwargs)

        payload = {'user_input': 'Observe.', 'request_id': 'req-conc-1', 'expected_revision': 0}
        with patch.object(main, 'call_llm', side_effect=counting):
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(lambda _: self.post('chapter/continue', payload), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200], [r.text for r in responses])
        self.assertEqual(planner_calls['n'], 1, 'the pipeline must run once for a duplicated request id')
        self.assertEqual(len(self.read('chapters.json')['chapters']), 1)
        self.assertEqual(self.read('world_config.json')['revision'], 1)

    def test_regenerate_starts_from_pre_turn_snapshot(self):
        self.setup_event()
        first = self.post('chapter/continue', {
            'user_input': 'Observe.', 'request_id': 'u01-turn-1', 'expected_revision': 0,
        })
        self.assertEqual(first.status_code, 200, first.text)
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], 1)
        self.assertTrue(storage.latest_turn_snapshot_dir(str(self.path)))

        regen = self.post('chapter/regenerate', {
            'request_id': 'u01-regen-1', 'expected_revision': 1,
        })
        self.assertEqual(regen.status_code, 200, regen.text)
        self.assertEqual(regen.json()['revision'], 2)
        # The last turn is rebuilt from before it happened: no accumulated tick,
        # no duplicated canon fact and no second application of the event.
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], 1)
        self.assertEqual(len(self.read('chapters.json')['chapters']), 1)
        self.assertEqual([f['statement'] for f in self.read('world_canon_store.json')['facts']],
                         ['The village was damaged.'])
        self.assertEqual(self.read('location_map.json')['locations'][0]['tags'], ['ruined'])
        self.assertEqual(self.read('world_events.json')['events'][0]['status'], 'resolved')
        self.assertEqual(self.read('world_config.json')['pre_turn_snapshot'], 'latest')

    def test_regenerate_failure_keeps_old_turn_readable(self):
        first = self.post('chapter/continue', {
            'user_input': 'Observe.', 'request_id': 'u01-keep-1', 'expected_revision': 0,
        })
        self.assertEqual(first.status_code, 200, first.text)
        before_turns = self.read('chapters.json')['chapters']
        before_tick = self.read('world_config.json')['story_clock']['tick']
        before_revision = self.read('world_config.json')['revision']

        def checker():
            raise main.RateLimitError('checker down', retry_after=1)

        with patch.object(main, 'call_llm', side_effect=self._checker_fake(checker)):
            regen = self.post('chapter/regenerate', {
                'request_id': 'u01-keep-regen', 'expected_revision': before_revision,
                'user_input': 'Use a different action, but keep this failed attempt uncommitted.',
            })
        self.assertEqual(regen.status_code, 503, regen.text)
        self.assertEqual(self.read('chapters.json')['chapters'], before_turns)
        self.assertEqual(self.read('world_config.json')['story_clock']['tick'], before_tick)
        self.assertEqual(self.read('world_config.json')['revision'], before_revision)
        self.assertTrue(storage.latest_turn_snapshot_dir(str(self.path)))

    def test_restore_clears_turn_snapshots(self):
        save = self.post('saves', {'label': 'before turn'}).json()['save']['save_id']
        self.post('chapter/continue', {'user_input': 'Observe.', 'request_id': 'u01-r-1', 'expected_revision': 0})
        self.assertTrue(storage.latest_turn_snapshot_dir(str(self.path)))

        restore = self.post(f'saves/{save}/restore')
        self.assertEqual(restore.status_code, 200, restore.text)
        self.assertFalse(storage.latest_turn_snapshot_dir(str(self.path)))
        self.assertIsNone(self.read('world_config.json')['pre_turn_snapshot'])

    def test_branch_starts_without_source_snapshots(self):
        self.post('chapter/continue', {'user_input': 'Observe.', 'request_id': 'u01-b-1', 'expected_revision': 0})
        save = self.post('saves', {'label': 'branch point'}).json()['save']['save_id']
        branch = self.post(f'saves/{save}/branch', {'new_world_name': 'u01_branch_world'})
        self.assertEqual(branch.status_code, 200, branch.text)
        branch_path = self.path.parent / 'u01_branch_world'
        self.assertFalse(storage.latest_turn_snapshot_dir(str(branch_path)))
        branch_cfg = json.loads((branch_path / 'world_config.json').read_text(encoding='utf-8'))
        self.assertEqual(branch_cfg['revision'], 0)
        self.assertIsNone(branch_cfg['pre_turn_snapshot'])

    def test_legacy_save_snapshot_is_migrated_on_restore(self):
        save = self.post('saves', {'label': 'legacy snapshot'}).json()['save']['save_id']
        snap_dir = self.path / 'saves' / save
        snap_config = snap_dir / 'world_config.json'
        data = json.loads(snap_config.read_text(encoding='utf-8'))
        data.pop('schema_version', None)
        snap_config.write_text(json.dumps(data), encoding='utf-8')

        response = self.post(f'saves/{save}/restore')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.read('world_config.json')['schema_version'], schema.SCHEMA_VERSION)
        self.assertTrue((self.path / 'location_map.json').exists())

    def test_recover_world_rolls_back_interrupted_commit(self):
        path = self.path
        old_config = self.read('world_config.json')
        backup = path / 'backup.tmp'
        backup.write_bytes((path / 'world_config.json').read_bytes())
        (path / 'world_config.json').write_text(
            json.dumps({**old_config, 'display_name': 'HALF_COMMITTED'}), encoding='utf-8')
        (path / 'location_map.json').write_text(json.dumps({'locations': [{'id': 'x'}]}), encoding='utf-8')
        (path / '_commit_journal.json').write_text(json.dumps({
            'state': 'installing',
            'entries': {
                'world_config.json': {'temp': None, 'backup': str(backup), 'had_original': True},
                'location_map.json': {'temp': None, 'backup': None, 'had_original': False},
            },
        }), encoding='utf-8')

        result = persistence.recover_world(str(path))
        self.assertEqual(result, {'recovered': True, 'action': 'rollback'})
        self.assertEqual(self.read('world_config.json'), old_config)
        self.assertFalse((path / 'location_map.json').exists())
        self.assertFalse((path / '_commit_journal.json').exists())
        self.assertFalse(persistence.recover_world(str(path))['recovered'])

    def test_process_kill_during_commit_recovers_to_whole_state(self):
        import subprocess
        path = str(self.path)
        old_config = (self.path / 'world_config.json').read_bytes()
        # The world already owns location_map.json, so the rollback must restore
        # its saved contents rather than delete it. Capture the pre-turn bytes to
        # prove the interrupted write left nothing behind.
        old_location_map = (self.path / 'location_map.json').read_bytes()
        updates = {
            'world_config.json': {**self.cfg, 'display_name': 'AFTER_KILL'},
            'location_map.json': {'locations': [{'id': 'new'}]},
        }
        backend_dir = str(Path(__file__).resolve().parents[1])
        script = (
            "import sys, os, json;"
            f"sys.path.insert(0, r'{backend_dir}');"
            "from app import persistence;"
            "real = persistence.os.replace;"
            "count = {'n': 0};\n"
            "def crash(src, dst):\n"
            "    count['n'] += 1\n"
            "    real(src, dst)\n"
            "    if count['n'] >= 2:\n"
            "        os._exit(9)\n"
            "persistence.os.replace = crash;"
            "persistence.commit_world_files(sys.argv[1], json.loads(sys.argv[2]))"
        )
        completed = subprocess.run(
            [sys.executable, '-c', script, path, json.dumps(updates)],
            capture_output=True, text=True,
        )
        self.assertNotEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((self.path / '_commit_journal.json').exists(),
                        f'a kill must leave the journal; stderr={completed.stderr!r} stdout={completed.stdout!r}')

        result = persistence.recover_world(path)
        self.assertEqual(result, {'recovered': True, 'action': 'rollback'})
        self.assertEqual((self.path / 'world_config.json').read_bytes(), old_config)
        self.assertEqual((self.path / 'location_map.json').read_bytes(), old_location_map,
                         'rollback must restore the pre-turn map, not the interrupted write')
        self.assertFalse((self.path / '_commit_journal.json').exists())
        self.assertEqual(list(self.path.glob('*.tmp')), [])

    def test_successful_commit_leaves_no_journal(self):
        response = self.post('chapter/continue', {
            'user_input': 'Observe.', 'request_id': 'req-journal-1', 'expected_revision': 0,
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertFalse((self.path / '_commit_journal.json').exists())
        receipts = json.loads((self.path / 'turn_receipts.json').read_text(encoding='utf-8'))
        self.assertIn('req-journal-1', receipts)

    def _knowledge_chars(self):
        return {
            'char_a': {'name': 'A', 'location': 'Village', 'knowledge': []},
            'char_b': {'name': 'B', 'location': 'City', 'knowledge': []},
            'char_c': {'name': 'C', 'location': 'Forest', 'knowledge': []},
        }

    def _knowledge_event(self):
        return {
            'event_id': 'raid',
            'status': 'pending',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [{
                'outcome_id': 'damaged',
                'canon_facts_add': ['The village was damaged.'],
                'location_effects': [{'location_id': 'Village', 'tags_add': ['ruined']}],
            }],
        }

    def test_witness_told_and_unknown_perspectives(self):
        from app import world_events as world_events_mod
        from app.story import knowledge
        characters = self._knowledge_chars()
        world_config = {'story_clock': {'tick': 1}}
        canon_store = {'facts': []}
        world_events_mod.tick_world_events(
            {'events': [self._knowledge_event()]}, world_config, characters, canon_store,
            {'locations': [{'id': 'Village', 'name': 'Village'}]},
        )

        fact = canon_store['facts'][0]
        self.assertTrue(fact.get('fact_id'))
        self.assertEqual(fact.get('event_id'), 'raid')

        a_view = knowledge.project_knowledge_for_subject(characters, 'char_a', canon_store['facts'])
        b_view = knowledge.project_knowledge_for_subject(characters, 'char_b', canon_store['facts'])
        c_view = knowledge.project_knowledge_for_subject(characters, 'char_c', canon_store['facts'])
        self.assertEqual(len(a_view), 1)
        self.assertEqual(a_view[0]['status'], 'true')
        self.assertEqual(a_view[0]['fact_id'], fact['fact_id'])
        self.assertEqual(a_view[0]['source']['kind'], 'witnessed')
        self.assertEqual(b_view, [])
        self.assertEqual(c_view, [])

        # B hears about it later from A: now B knows (with a source), C still not.
        knowledge.tell_knowledge(characters, 'char_a', 'char_b', fact['statement'],
                                 tick=1, fact_id=fact['fact_id'])
        b_view = knowledge.project_knowledge_for_subject(characters, 'char_b', canon_store['facts'])
        self.assertEqual(b_view[0]['source']['kind'], 'told_by')
        self.assertEqual(b_view[0]['source']['who'], 'char_a')
        self.assertEqual(b_view[0]['status'], 'true')
        self.assertEqual(knowledge.project_knowledge_for_subject(characters, 'char_c', canon_store['facts']), [])

    def test_false_rumor_does_not_change_canon(self):
        from app.story import knowledge
        characters = self._knowledge_chars()
        facts = [knowledge.make_fact('X is loyal.', category='lore')]
        knowledge.grant_knowledge(characters, ['char_b'], 'X betrayed us',
                                  source_kind='rumor', tick=2)
        view = knowledge.project_knowledge_for_subject(characters, 'char_b', facts)
        self.assertEqual(len(view), 1)
        self.assertIsNone(view[0]['fact_id'])
        self.assertEqual(view[0]['status'], 'uncertain')
        # The objective fact list is untouched by the false belief.
        self.assertEqual([f['statement'] for f in facts], ['X is loyal.'])

    def test_legacy_character_without_knowledge_has_no_world_knowledge(self):
        from app.story import knowledge
        characters = {'char_old': {'name': 'Old', 'location': 'Town'}}
        facts = [knowledge.make_fact('A secret truth.')]
        self.assertEqual(knowledge.project_knowledge_for_subject(characters, 'char_old', facts), [])
        self.assertEqual(knowledge.facts_visible_to(facts, characters, 'char_old'), [])

    def test_character_knowledge_is_in_the_model_payload(self):
        characters = self.read('character_state.json')['characters']
        characters['char_xueli']['knowledge'] = [{
            'knowledge_id': 'kn_test', 'subject': 'char_xueli',
            'statement': 'Xueli saw the sealed gate.',
            'fact_id': None, 'confidence': 1.0,
            'source': {'kind': 'witnessed', 'who': None, 'event_id': None},
            'learned_at_tick': 0,
        }]
        self.write('character_state.json', {'characters': characters})

        captured = {}

        def capture(system_prompt, user_prompt, *args, **kwargs):
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                captured['planner'] = user_prompt
            return self.fake_llm(system_prompt, user_prompt, *args, **kwargs)

        with patch.object(main, 'call_llm', side_effect=capture):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        payload = json.loads(captured['planner'])
        knowledge_payload = payload['character_knowledge']
        self.assertIn('char_xueli', knowledge_payload)
        self.assertTrue(any(
            entry['statement'] == 'Xueli saw the sealed gate.'
            for entry in knowledge_payload['char_xueli']
        ))
        self.assertEqual(knowledge_payload.get('char_gu_changge'), [])

    def test_character_knowledge_survives_save_and_restore(self):
        characters = self.read('character_state.json')['characters']
        characters['char_xueli'].setdefault('knowledge', []).append({
            'knowledge_id': 'kn_save', 'subject': 'char_xueli',
            'statement': 'Xueli remembers the promise.',
            'fact_id': None, 'confidence': 0.9,
            'source': {'kind': 'witnessed', 'who': None, 'event_id': None},
            'learned_at_tick': 0,
        })
        self.write('character_state.json', {'characters': characters})
        save = self.post('saves', {'label': 'knowledge'}).json()['save']['save_id']

        characters['char_xueli']['knowledge'] = []
        self.write('character_state.json', {'characters': characters})
        restore = self.post(f'saves/{save}/restore')
        self.assertEqual(restore.status_code, 200, restore.text)
        restored = self.read('character_state.json')['characters']['char_xueli']['knowledge']
        self.assertEqual([entry['statement'] for entry in restored], ['Xueli remembers the promise.'])

    def test_scene_participants_location_and_perception(self):
        from app.story import observer
        characters = {
            'hero': {'name': 'Hero', 'location': 'Village'},
            'npc_near': {'name': 'Near', 'location': 'Village', 'alive': True},
            'npc_deaf': {'name': 'Deaf', 'location': 'Village', 'status_effects': [{'name': 'deaf'}]},
            'npc_far': {'name': 'Far', 'location': 'City'},
            'npc_dead': {'name': 'Dead', 'location': 'Village', 'alive': False},
        }
        participants = observer.scene_participants(
            {'characters': characters}, 'Village', protagonist_id='hero')
        self.assertEqual(set(participants), {'hero', 'npc_near'})
        self.assertEqual(set(observer.psychology_subjects(participants, 'hero')), {'npc_near'})

        # The observer cap keeps the count bounded.
        many = {'characters': {'hero': {'name': 'Hero', 'location': 'Village'},
                               **{f'npc_{i}': {'name': f'N{i}', 'location': 'Village'} for i in range(20)}}}
        capped = observer.scene_participants(many, 'Village', protagonist_id='hero', max_observers=3)
        self.assertLessEqual(len(capped), 3)

    def test_distant_npcs_do_not_get_psychology_calls_or_transcript(self):
        self.cfg['psychology_enabled'] = True
        self.write('world_config.json', self.cfg)
        characters = self.read('character_state.json')['characters']
        for i in range(100):
            characters[f'far_{i}'] = {
                'name': f'Far{i}', 'location': 'Distant Land', 'affinity': {},
                'power_stat': {'realm': '', 'exp': 0, 'sub_stats': {}, 'known_skills': []},
                'knowledge_flags': [], 'inventory': [], 'relationships': {}, 'alive': True,
                'status_effects': [], 'traits': {},
            }
        self.write('character_state.json', {'characters': characters})

        counts = {'perception': 0, 'psychology': 0}
        perception_payloads = []

        def counting(system_prompt, user_prompt, *args, **kwargs):
            role = kwargs.get('role')
            if role == 'perception':
                counts['perception'] += 1
                perception_payloads.append(user_prompt)
            elif role == 'psychology':
                counts['psychology'] += 1
            return self.fake_llm(system_prompt, user_prompt, *args, **kwargs)

        with patch.object(main, 'call_llm', side_effect=counting):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)

        # Only the one allowed NPC at the scene is processed, not the 100 far ones.
        self.assertEqual(counts['perception'], 1)
        self.assertEqual(counts['psychology'], 1)
        status = response.json()['chapter']['psychology_status']
        self.assertEqual(status['status'], 'ran')
        self.assertEqual(status['subjects'], ['char_gu_changge'])
        self.assertNotIn('char_xueli', status['subjects'])
        for i in range(100):
            self.assertNotIn(f'far_{i}', status['participants'])

        payload = json.loads(perception_payloads[0])
        self.assertEqual(payload['character_id'], 'char_gu_changge')
        self.assertIn('character_knowledge', payload)
        self.assertNotIn('far_', json.dumps(payload))

    def test_psychology_error_status_is_recorded_but_turn_commits(self):
        self.cfg['psychology_enabled'] = True
        self.write('world_config.json', self.cfg)
        with patch.object(chapter_generator, 'generate_perceptions_for_all_characters',
                          side_effect=RuntimeError('psychology boom')):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        status = response.json()['chapter']['psychology_status']
        self.assertEqual(status['status'], 'error')
        self.assertIn('psychology boom', status['reason'])
        self.assertEqual(len(self.read('chapters.json')['chapters']), 1)

    def _event_world(self):
        return {'story_clock': {'tick': 1}}

    def test_organized_event_prevented_when_organizer_dead(self):
        from app import world_events as we
        event = {
            'event_id': 'coup', 'status': 'pending', 'event_class': 'organized',
            'organizer_id': 'char_leader', 'location_id': 'Village',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [
                {'outcome_id': 'coup_done', 'canon_facts_add': ['The coup happened.']},
                {'outcome_id': 'coup_prevented', 'resolution': 'prevented',
                 'canon_facts_add': ['The coup never happened.']},
            ],
        }
        characters = {
            'char_leader': {'name': 'Leader', 'alive': False, 'location': 'Village'},
            'char_x': {'name': 'X', 'location': 'Village'},
        }
        store = {'facts': []}
        location_map = {'locations': [{'id': 'Village', 'name': 'Village'}]}
        we.tick_world_events({'events': [event]}, self._event_world(), characters, store, location_map)
        self.assertEqual(event['status'], 'prevented')
        self.assertEqual(event['resolved_outcome_id'], 'coup_prevented')
        self.assertEqual([f['statement'] for f in store['facts']], ['The coup never happened.'])

        # A resolved event never runs a second time.
        we.tick_world_events({'events': [event]}, self._event_world(), characters, store, location_map)
        self.assertEqual(len(store['facts']), 1)

    def test_organized_event_transformed_when_location_gone(self):
        from app import world_events as we
        event = {
            'event_id': 'festival', 'status': 'pending', 'event_class': 'organized',
            'organizer_id': 'char_host', 'location_id': 'Temple',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [
                {'outcome_id': 'festival_normal', 'canon_facts_add': ['The festival was held at the temple.']},
                {'outcome_id': 'festival_moved', 'resolution': 'transformed',
                 'canon_facts_add': ['The festival moved to a hidden grove.']},
            ],
        }
        characters = {'char_host': {'name': 'Host', 'alive': True, 'location': 'Temple'}}
        store = {'facts': []}
        we.tick_world_events({'events': [event]}, self._event_world(), characters, store,
                             {'locations': [{'id': 'Capital', 'name': 'Capital'}]})
        self.assertEqual(event['status'], 'transformed')
        self.assertEqual([f['statement'] for f in store['facts']], ['The festival moved to a hidden grove.'])
        self.assertNotIn('The festival was held at the temple.', [f['statement'] for f in store['facts']])

    def test_contingent_event_ignores_organizer_and_location_premises(self):
        from app import world_events as we
        event = {
            'event_id': 'storm', 'status': 'pending', 'event_class': 'contingent',
            'organizer_id': 'char_god', 'location_id': 'Nowhere',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [{'outcome_id': 'storm_hits', 'canon_facts_add': ['A great storm struck.']}],
        }
        characters = {'char_god': {'name': 'God', 'alive': False, 'location': 'Sky'}}
        store = {'facts': []}
        we.tick_world_events({'events': [event]}, self._event_world(), characters, store,
                             {'locations': [{'id': 'Capital', 'name': 'Capital'}]})
        self.assertEqual(event['status'], 'resolved')
        self.assertEqual([f['statement'] for f in store['facts']], ['A great storm struck.'])

    def test_organized_event_missed_past_deadline(self):
        from app import world_events as we
        event = {
            'event_id': 'duel', 'status': 'pending', 'event_class': 'organized',
            'deadline_tick': 2,
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 5}],
            'outcomes': [{'outcome_id': 'duel_happens', 'canon_facts_add': ['The duel happened.']}],
        }
        store = {'facts': []}
        we.tick_world_events({'events': [event]}, {'story_clock': {'tick': 3}}, {}, store, {'locations': []})
        self.assertEqual(event['status'], 'missed')
        self.assertEqual(store['facts'], [])

    def test_custom_outcome_shape_passes_schema(self):
        from app import world_events as we
        outcome = {
            'outcome_id': 'weird_outcome', 'resolution': 'transformed',
            'condition': {'field': 'story_clock.tick', 'op': '>=', 'value': 1},
            'canon_facts_add': ['A strange outcome occurred.'],
            'world_flags_set': {'weird': True},
            'custom_field': {'anything': 'goes'},
        }
        self.assertEqual(we.validate_event_outcome(outcome), [])
        event = {
            'event_id': 'odd', 'status': 'pending', 'event_class': 'contingent',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [outcome],
        }
        world = self._event_world()
        store = {'facts': []}
        we.tick_world_events({'events': [event]}, world, {}, store, None)
        self.assertEqual(event['status'], 'transformed')
        self.assertTrue(world['world_flags']['weird'])

    def test_invalid_custom_outcome_is_skipped(self):
        from app import world_events as we
        self.assertTrue(we.validate_event_outcome({'resolution': 'resolved'}))  # no outcome_id
        self.assertTrue(we.validate_event_outcome({'outcome_id': 'x', 'resolution': 'maybe'}))
        event = {
            'event_id': 'bad', 'status': 'pending', 'event_class': 'contingent',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [{'outcome_id': 'x', 'resolution': 'maybe', 'canon_facts_add': ['nope']}],
        }
        store = {'facts': []}
        we.tick_world_events({'events': [event]}, self._event_world(), {}, store, None)
        self.assertEqual(event['status'], 'pending')
        self.assertEqual(store['facts'], [])

    def _resolve(self, intent, *, rules=None, characters=None, checkpoint=None, location_map=None, seed=None):
        from app.story.action_resolution import resolve_action
        chars = characters if characters is not None else {
            'hero': {
                'name': 'Hero', 'location': 'Vault Hall', 'inventory': [],
                'power_stat': {'realm': '', 'exp': 0, 'known_skills': []},
            }
        }
        world = {'action_rules': rules} if rules is not None else {}
        return resolve_action(
            intent, chars, 'hero', world,
            checkpoint=checkpoint or {'boundary': {'locations': ['Vault Hall']}},
            location_map=location_map, world_name='W', turn_index=1, seed=seed,
        )

    def test_action_resolution_is_deterministic_for_same_data(self):
        rules = [{'keywords': ['vault'], 'required_tools': ['vault_key'],
                  'alternatives': ['pry it open (noisy)']}]
        first = self._resolve('open the vault', rules=rules)
        second = self._resolve('open the vault', rules=rules)
        self.assertEqual(first, second)
        self.assertEqual(first['result'], 'conditional')
        self.assertTrue(first['alternatives'])
        self.assertIn('vault_key', first['reason'])

    def test_missing_tool_without_alternative_is_impossible(self):
        rules = [{'keywords': ['vault'], 'required_tools': ['vault_key']}]
        result = self._resolve('open the vault', rules=rules)
        self.assertEqual(result['result'], 'impossible')
        self.assertEqual(result['alternatives'], [])
        self.assertTrue(result['continuation'])

    def test_everyday_world_needs_no_realm_or_exp(self):
        # No rules at all: old free-narration adapter.
        result = self._resolve('buy bread at the market')
        self.assertEqual(result['result'], 'success')
        self.assertEqual(result['checks'], [])

        # A rule without a power requirement never checks realm/EXP.
        rules = [{'keywords': ['bread'], 'required_tools': []}]
        result = self._resolve('buy bread', rules=rules)
        self.assertEqual(result['result'], 'success')
        capability = next(c for c in result['checks'] if c['name'] == 'capability')
        self.assertTrue(capability['ok'])

    def test_opposed_action_probability_is_seeded_and_reproducible(self):
        rules = [{'keywords': ['duel'], 'opposition': {'name': 'rival'},
                  'success_probability': 0.6,
                  'consequences': [{'type': 'injury'}]}]
        a = self._resolve('duel the rival', rules=rules, seed=1234)
        b = self._resolve('duel the rival', rules=rules, seed=1234)
        self.assertEqual(a, b)
        self.assertIsNotNone(a['seed'])
        self.assertEqual(a['probability'], 0.6)
        self.assertIn(a['result'], ('partial', 'success', 'failure'))

    def test_failed_action_has_a_continuation(self):
        rules = [{'keywords': ['duel'], 'opposition': {'name': 'rival'},
                  'success_probability': 0.0}]
        result = self._resolve('duel the rival', rules=rules, seed=7)
        self.assertEqual(result['result'], 'failure')
        self.assertTrue(result['continuation'])
        self.assertTrue(result['alternatives'])

    def test_action_payload_and_record_carry_committed_outcome(self):
        self.cfg['action_rules'] = [{
            'keywords': ['vault'], 'required_tools': ['vault_key'],
            'alternatives': ['pry it open (noisy)'], 'consequences': [{'type': 'noise'}],
        }]
        self.write('world_config.json', self.cfg)

        captured = {}

        def capture(system_prompt, user_prompt, *args, **kwargs):
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                captured['planner'] = user_prompt
            return self.fake_llm(system_prompt, user_prompt, *args, **kwargs)

        with patch.object(main, 'call_llm', side_effect=capture):
            response = self.post('chapter/continue', {'user_input': 'open the vault'})
        self.assertEqual(response.status_code, 200, response.text)

        record_resolution = response.json()['chapter']['action_resolution']
        self.assertEqual(record_resolution['result'], 'conditional')
        payload = json.loads(captured['planner'])
        self.assertEqual(payload['action_resolution']['result'], 'conditional')
        self.assertIn('pry it open (noisy)', payload['action_resolution']['alternatives'])

    def _enable_quests(self):
        self.cfg['quest_board_enabled'] = True
        self.write('world_config.json', self.cfg)

    def test_quest_survives_note_change_after_discovery(self):
        self._enable_quests()
        self.write('world_events.json', {'events': [{
            'event_id': 'raid', 'status': 'pending',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [{'outcome_id': 'done', 'canon_facts_add': ['The raid happened.']}],
        }]})
        config = self.read('world_config.json')
        config['open_threads'] = [{'note': 'Investigate the raid rumors.'}]
        self.write('world_config.json', config)

        first = self.client.get(f'/worlds/{self.world}/quest_board').json()
        self.assertEqual([q['quest_id'] for q in first['quests']], ['raid'])
        self.assertEqual(first['quests'][0]['source']['kind'], 'thread')

        # Editing the prose note must not lose the quest.
        config['open_threads'] = [{'note': 'Something completely unrelated now.'}]
        self.write('world_config.json', config)
        second = self.client.get(f'/worlds/{self.world}/quest_board').json()
        self.assertEqual([q['quest_id'] for q in second['quests']], ['raid'])

    def test_far_event_is_hidden_until_discovered(self):
        self._enable_quests()
        self.write('world_events.json', {'events': [{
            'event_id': 'far_raid', 'status': 'pending',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [{'outcome_id': 'done', 'canon_facts_add': ['It happened far away.']}],
        }]})
        hidden = self.client.get(f'/worlds/{self.world}/quest_board').json()
        self.assertEqual(hidden['quests'], [])

        from app.story import discovery
        store = discovery.load_discoveries(str(self.path))
        discovery.record_discovery(store, 'far_raid', {'kind': 'rumor', 'who': 'char_gu_changge'}, 2)
        discovery.save_discoveries(str(self.path), store)
        shown = self.client.get(f'/worlds/{self.world}/quest_board').json()
        self.assertEqual([q['quest_id'] for q in shown['quests']], ['far_raid'])
        self.assertEqual(shown['quests'][0]['status'], 'active')

    def test_quest_status_follows_event_lifecycle(self):
        from app.story import discovery
        cases = [('resolved', 'completed'), ('prevented', 'prevented'),
                 ('transformed', 'transformed'), ('missed', 'missed'), ('pending', 'active')]
        for event_status, expected in cases:
            with self.subTest(event_status=event_status):
                events = [{'event_id': 'e1', 'status': event_status, 'resolution': event_status,
                           'quest_hint_title': 'Quest'}]
                store = {'discoveries': [{'event_id': 'e1', 'source': {'kind': 'start'},
                                          'at_tick': 0, 'known_deadline_tick': None}]}
                quests = discovery.quest_view(events, store)
                self.assertEqual(quests[0]['status'], expected)

    def test_deadline_is_not_inferred_from_trigger_condition(self):
        from app.story import discovery
        events = [{
            'event_id': 'e2', 'status': 'pending',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 5}],
        }]
        store = {'discoveries': [{'event_id': 'e2', 'source': {'kind': 'start'},
                                  'at_tick': 0, 'known_deadline_tick': None}]}
        self.assertIsNone(discovery.quest_view(events, store)[0]['deadline_tick'])

        store['discoveries'][0]['known_deadline_tick'] = 9
        self.assertEqual(discovery.quest_view(events, store)[0]['deadline_tick'], 9)

    def test_witnessed_event_discovery_survives_save_and_restore(self):
        self._enable_quests()
        characters = self.read('character_state.json')['characters']
        characters['char_xueli']['location'] = 'village'
        self.write('character_state.json', {'characters': characters})
        self.setup_event()

        response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        quests = self.client.get(f'/worlds/{self.world}/quest_board').json()['quests']
        self.assertEqual([q['quest_id'] for q in quests], ['raid'])
        self.assertEqual(quests[0]['status'], 'completed')
        self.assertEqual(quests[0]['source']['kind'], 'witnessed')

        save = self.post('saves', {'label': 'after raid'}).json()['save']['save_id']
        restore = self.post(f'saves/{save}/restore')
        self.assertEqual(restore.status_code, 200, restore.text)
        restored_quests = self.client.get(f'/worlds/{self.world}/quest_board').json()['quests']
        self.assertEqual([q['quest_id'] for q in restored_quests], ['raid'])
        self.assertEqual(restored_quests[0]['status'], 'completed')

    def test_betrayal_memory_only_affects_knowers(self):
        from app.story import relationship_memory as rm
        characters = {
            'char_a': {'name': 'A', 'location': 'Village'},
            'char_b': {'name': 'B', 'location': 'Village'},
            'char_c': {'name': 'C', 'location': 'Forest'},
        }
        rm.record_memory_for_knowers(characters, ['char_a'], 'char_b', 'betrayal',
                                     'B betrayed the group.', event_id='e1', tick=2, weight=0.9)
        self.assertEqual(len(rm.memories_of(characters, 'char_a')), 1)
        self.assertEqual(rm.memories_of(characters, 'char_c'), [])
        # No memory means no relationship consequence for C.
        self.assertEqual(rm.relevant_memories(characters, 'char_c'), [])

    def test_promise_kept_vs_broken_have_different_effect(self):
        from app.story import relationship_memory as rm
        kept = rm.make_memory('a', 'b', 'promise', 'I will return.', status='kept', weight=0.8)
        broken = rm.make_memory('a', 'b', 'promise', 'I will return.', status='broken', weight=0.8)
        self.assertGreater(rm.affinity_delta_from_memory(kept), 0)
        self.assertLess(rm.affinity_delta_from_memory(broken), 0)

    def test_relevant_memories_are_bounded_and_weighted(self):
        from app.story import relationship_memory as rm
        characters = {'a': {'name': 'A'}}
        for i in range(10):
            rm.record_memory(characters, 'a', 'b', 'help', f'help {i}', tick=i, weight=i / 10.0)
        top = rm.relevant_memories(characters, 'a', limit=3)
        self.assertEqual(len(top), 3)
        self.assertEqual([m['weight'] for m in top], [0.9, 0.8, 0.7])

    def test_relationship_context_is_in_the_model_payload(self):
        characters = self.read('character_state.json')['characters']
        characters['char_xueli']['relationship_memories'] = [{
            'memory_id': 'rel_test', 'subject': 'char_xueli', 'with': 'char_gu_changge',
            'kind': 'promise', 'statement': 'Gu Changge promised to protect her.',
            'event_id': 'e1', 'tick': 0, 'status': 'open', 'weight': 0.9,
            'known_by': ['char_xueli'],
        }]
        self.write('character_state.json', {'characters': characters})

        captured = {}

        def capture(system_prompt, user_prompt, *args, **kwargs):
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                captured['planner'] = user_prompt
            return self.fake_llm(system_prompt, user_prompt, *args, **kwargs)

        with patch.object(main, 'call_llm', side_effect=capture):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        payload = json.loads(captured['planner'])
        memories = payload['relationship_context']['char_xueli']['memories']
        self.assertTrue(any(m['statement'] == 'Gu Changge promised to protect her.' for m in memories))

    def test_writer_payload_is_scene_scoped_and_excludes_raw_private_state(self):
        characters = self.read('character_state.json')['characters']
        protagonist_location = characters['char_xueli']['location']
        characters['char_gu_changge']['location'] = protagonist_location
        characters['char_gu_changge']['secrets'] = ['hidden betrayal']
        characters['char_gu_changge']['knowledge'] = [{'statement': 'private fact'}]
        characters['char_gu_changge']['relationship_memories'] = [{'statement': 'private memory'}]
        characters['far_npc'] = dict(characters['char_gu_changge'])
        characters['far_npc'].update({
            'name': 'Far NPC', 'location': 'Another Realm',
            'secrets': ['far secret'], 'knowledge': [{'statement': 'far fact'}],
        })
        self.write('character_state.json', {'characters': characters})

        captured = {}
        def capture(system_prompt, user_prompt, *args, **kwargs):
            if system_prompt == main.PLANNER_SYSTEM_PROMPT:
                captured['planner'] = json.loads(user_prompt)
            return self.fake_llm(system_prompt, user_prompt, *args, **kwargs)

        with patch.object(main, 'call_llm', side_effect=capture):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        model_characters = captured['planner']['character_state']
        self.assertNotIn('far_npc', model_characters)
        self.assertIn('char_gu_changge', model_characters)
        for private_field in ('secrets', 'knowledge', 'relationship_memories', 'internal_state'):
            self.assertNotIn(private_field, model_characters['char_gu_changge'])

    def test_relationship_memory_survives_save_restore(self):
        characters = self.read('character_state.json')['characters']
        characters['char_xueli']['relationship_memories'] = [{
            'memory_id': 'rel_save', 'subject': 'char_xueli', 'with': 'char_gu_changge',
            'kind': 'debt', 'statement': 'She owes him a life debt.',
            'event_id': None, 'tick': 1, 'status': 'open', 'weight': 0.7,
            'known_by': ['char_xueli'],
        }]
        self.write('character_state.json', {'characters': characters})
        save = self.post('saves', {'label': 'memory'}).json()['save']['save_id']

        characters['char_xueli']['relationship_memories'] = []
        self.write('character_state.json', {'characters': characters})
        restore = self.post(f'saves/{save}/restore')
        self.assertEqual(restore.status_code, 200, restore.text)
        restored = self.read('character_state.json')['characters']['char_xueli']['relationship_memories']
        self.assertEqual([m['statement'] for m in restored], ['She owes him a life debt.'])

    def test_context_budget_is_respected(self):
        from app.story.memory import build_multi_tier_context
        chapters = {
            'chapters': [
                {'chapter_index': i // 2 + 1, 'turn_index': i % 2 + 1,
                 'chapter_text': f'Turn {i} ' + ('word ' * 20), 'chapter_closed': False}
                for i in range(80)
            ],
            'running_summary': 'A long compressed summary of the previous arcs. ' * 5,
            'memorable_beats': [f'[Ch.{i}] a vivid moment in chapter {i}' for i in range(1, 20)],
        }
        canon = [{'fact_id': f'f{i}', 'statement': f'Canon fact {i}', 'tags': ['Sect']} for i in range(40)]
        context = build_multi_tier_context(
            chapters, {'pacing_level': 'Balanced'}, {'boundary': {'locations': ['Sect'], 'allowed_characters': []}},
            canon, max_context_tokens=500,
        )
        self.assertLessEqual(context['context_token_estimate'], 500)
        self.assertEqual(context['context_token_budget'], 500)
        self.assertGreaterEqual(len(context['multi_tier_context']['tier_1_working_memory']['recent_turns']), 1)

    def test_long_history_does_not_lose_promise_or_key_event(self):
        from app.story import relationship_memory as rm
        from app.story.memory import merge_canon_and_delta
        chapters = {
            'chapters': [
                {'chapter_index': i + 1, 'turn_index': 1, 'chapter_text': f'Turn {i}', 'chapter_closed': True}
                for i in range(80)
            ],
            'running_summary': 'Compressed.', 'memorable_beats': [],
        }
        characters = {'hero': {'name': 'Hero', 'relationship_memories': [
            rm.make_memory('hero', 'ally', 'promise', 'The hero promised to return.', tick=1, weight=1.0),
        ]}}
        self.assertEqual(rm.relevant_memories(characters, 'hero')[0]['statement'],
                         'The hero promised to return.')
        canon = {'facts': [{'fact_id': 'f_key', 'statement': 'The gate was sealed.'}]}
        self.assertEqual(merge_canon_and_delta(canon, {})['facts'][0]['statement'], 'The gate was sealed.')
        self.assertEqual(len(chapters['chapters']), 80)

    def test_npc_perception_payload_has_no_global_summary(self):
        self.cfg['psychology_enabled'] = True
        self.write('world_config.json', self.cfg)
        characters = self.read('character_state.json')['characters']
        characters['char_gu_changge']['knowledge'] = [{
            'knowledge_id': 'kn_g', 'subject': 'char_gu_changge', 'statement': 'Gu Changge knows a secret.',
            'fact_id': None, 'confidence': 1.0,
            'source': {'kind': 'witnessed', 'who': None, 'event_id': None}, 'learned_at_tick': 0,
        }]
        self.write('character_state.json', {'characters': characters})

        perception_payloads = []

        def counting(system_prompt, user_prompt, *args, **kwargs):
            if kwargs.get('role') == 'perception':
                perception_payloads.append(user_prompt)
            return self.fake_llm(system_prompt, user_prompt, *args, **kwargs)

        with patch.object(main, 'call_llm', side_effect=counting):
            response = self.post('chapter/continue', {'user_input': 'Observe.'})
        self.assertEqual(response.status_code, 200, response.text)
        payload = json.loads(perception_payloads[0])
        self.assertNotIn('running_summary', payload)
        self.assertNotIn('multi_tier_context', payload)
        self.assertIn('character_knowledge', payload)
        self.assertIn('relationship_memories', payload)

    def test_branch_retrieval_is_isolated(self):
        from app.story.memory import merge_canon_and_delta
        base = {'facts': [{'fact_id': 'f1', 'statement': 'Shared truth.'}]}
        a = merge_canon_and_delta(base, {'overrides': {'f1': 'Branch A truth.'}})
        b = merge_canon_and_delta(base, {'overrides': {'f1': 'Branch B truth.'}})
        self.assertEqual(a['facts'][0]['statement'], 'Branch A truth.')
        self.assertEqual(b['facts'][0]['statement'], 'Branch B truth.')
        self.assertEqual(base['facts'][0]['statement'], 'Shared truth.')

    def test_journal_lists_discovered_events_with_source_and_status(self):
        self.write('world_events.json', {'events': [{
            'event_id': 'raid', 'status': 'pending', 'discoverable_from_start': True,
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            'outcomes': [{'outcome_id': 'done', 'canon_facts_add': ['The raid happened.']}],
        }]})
        first = self.client.get(f'/worlds/{self.world}/journal').json()['entries']
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0]['quest_id'], 'raid')
        self.assertEqual(first[0]['status'], 'active')
        self.assertEqual(first[0]['source']['kind'], 'start')

        events = self.read('world_events.json')
        events['events'][0]['status'] = 'resolved'
        events['events'][0]['resolution'] = 'resolved'
        self.write('world_events.json', events)
        after = self.client.get(f'/worlds/{self.world}/journal').json()['entries']
        self.assertEqual(after[0]['status'], 'completed')

    def test_play_state_exposes_lifecycle_and_saved_epilogue(self):
        self.prepare_ending()
        state = self.client.get(f'/worlds/{self.world}/play-state').json()
        self.assertEqual(state['lifecycle_status'], 'endgame_pending')

        with patch.object(engine, 'call_llm', return_value=json.dumps({'epilogue': 'They chose a new life.'})):
            self.post('chapter/generate-epilogue', {'chosen_choice': 'Leave'})

        state2 = self.client.get(f'/worlds/{self.world}/play-state').json()
        self.assertEqual(state2['lifecycle_status'], 'completed')
        self.assertEqual(state2['epilogue']['text'], 'They chose a new life.')
        self.assertEqual(self.post('chapter/continue', {'user_input': 'Continue'}).status_code, 409)

    def test_creator_preview_does_not_write(self):
        before = self.read('character_state.json')
        response = self.post('creator/edit', {
            'preview': True, 'expected_revision': 0,
            'changes': [{'kind': 'character', 'character_id': 'char_xueli', 'field': 'alive', 'value': False}],
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()['preview'])
        self.assertTrue(response.json()['ok'])
        self.assertEqual(self.read('character_state.json'), before)
        self.assertEqual(self.read('world_config.json')['revision'], 0)

    def test_creator_edit_applies_and_logs_revision(self):
        response = self.post('creator/edit', {
            'expected_revision': 0, 'reason': 'save the character',
            'changes': [{'kind': 'character', 'character_id': 'char_xueli', 'field': 'alive', 'value': False}],
        })
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['revision'], 1)
        self.assertFalse(self.read('character_state.json')['characters']['char_xueli']['alive'])
        revisions = self.read('world_config.json')['creator_revisions']
        self.assertEqual(revisions[-1]['reason'], 'save the character')
        self.assertEqual(revisions[-1]['base_revision'], 0)

    def test_creator_edit_stale_revision_is_rejected(self):
        response = self.post('creator/edit', {
            'expected_revision': 99,
            'changes': [{'kind': 'character', 'character_id': 'char_xueli', 'field': 'alive', 'value': False}],
        })
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['reason'], 'stale_revision')

    def test_creator_edit_invalid_change_is_rejected_without_write(self):
        before = self.read('character_state.json')
        response = self.post('creator/edit', {
            'expected_revision': 0,
            'changes': [{'kind': 'character', 'character_id': 'char_xueli', 'field': 'not_a_field', 'value': 1}],
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.read('character_state.json'), before)

    def test_creator_edit_resolves_event_and_applies_outcome(self):
        self.setup_event()
        response = self.post('creator/edit', {
            'expected_revision': 0, 'reason': 'resolve the raid now',
            'changes': [{
                'kind': 'event_resolution', 'event_id': 'raid',
                'status': 'resolved', 'outcome_id': 'damaged',
            }],
        })
        self.assertEqual(response.status_code, 200, response.text)
        event = self.read('world_events.json')['events'][0]
        self.assertEqual(event['status'], 'resolved')
        self.assertEqual(event['resolved_outcome_id'], 'damaged')
        self.assertEqual(
            [fact['statement'] for fact in self.read('world_canon_store.json')['facts']],
            ['The village was damaged.'],
        )
        self.assertTrue(self.read('world_config.json')['world_flags']['raid_done'])
        self.assertEqual(self.read('location_map.json')['locations'][0]['tags'], ['ruined'])

    def test_creator_edit_rejects_rewriting_terminal_event(self):
        self.setup_event()
        events = self.read('world_events.json')
        events['events'][0].update({
            'status': 'resolved', 'resolution': 'resolved',
            'resolved_outcome_id': 'damaged',
        })
        self.write('world_events.json', events)
        before = {name: self.read(name) for name in (
            'world_config.json', 'world_events.json', 'world_canon_store.json',
            'character_state.json', 'location_map.json',
        )}
        response = self.post('creator/edit', {
            'expected_revision': 0,
            'changes': [{'kind': 'event_resolution', 'event_id': 'raid', 'status': 'prevented'}],
        })
        self.assertEqual(response.status_code, 400, response.text)
        for name, value in before.items():
            self.assertEqual(self.read(name), value)

    def test_builder_events_are_generated_and_idempotent(self):
        name = 'builder_events_world'
        created = self.client.post(f'/worlds/{name}', json={'prompt': 'A detective case.', 'scope_type': 'arc-only'})
        self.assertEqual(created.status_code, 200, created.text)
        path = self.data / 'worlds' / name

        (path / 'canon_timeline.json').write_text(json.dumps({'checkpoints': [{
            'checkpoint_id': 'cp_0', 'description': 'The case begins', 'required_conditions': [],
            'boundary': {'locations': ['Manor'], 'allowed_characters': ['det'], 'time_window': ''},
            'sub_beats': [],
        }]}), encoding='utf-8')
        (path / 'character_state.json').write_text(json.dumps({'characters': {'det': {
            'name': 'Det', 'location': 'Manor', 'affinity': {},
            'power_stat': {'realm': '', 'exp': 0, 'sub_stats': {}, 'known_skills': []},
            'knowledge_flags': [], 'inventory': [], 'relationships': {}, 'alive': True,
        }}}), encoding='utf-8')
        (path / 'location_map.json').write_text(json.dumps({'locations': [
            {'id': 'Manor', 'name': 'Manor', 'connected_to': [], 'is_starting_location': True, 'tags': []},
        ]}), encoding='utf-8')
        cfg = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        cfg['creation_status'] = 'characters'
        cfg['protagonist_id'] = 'det'
        (path / 'world_config.json').write_text(json.dumps(cfg), encoding='utf-8')

        first = self.client.post(f'/worlds/{name}/builder/events')
        self.assertEqual(first.status_code, 200, first.text)
        self.assertGreater(first.json()['events_created'], 0)
        events = json.loads((path / 'world_events.json').read_text(encoding='utf-8'))['events']
        self.assertTrue(events)
        from app.world_events import validate_event_outcome
        ids = set()
        for event in events:
            self.assertNotIn(event['event_id'], ids)
            ids.add(event['event_id'])
            self.assertTrue(event['outcomes'])
            for outcome in event['outcomes']:
                self.assertEqual(validate_event_outcome(outcome), [])

        second = self.client.post(f'/worlds/{name}/builder/events')
        self.assertEqual(second.json()['events_created'], 0)
        self.assertEqual(len(json.loads((path / 'world_events.json').read_text(encoding='utf-8'))['events']),
                         len(events))

        status = self.client.get(f'/worlds/{name}/builder/status').json()
        self.assertTrue(status['has_events'])
        self.assertTrue(status['has_checkpoints'])
        self.assertTrue(status['has_characters'])

    def test_builder_status_resumes_from_draft(self):
        name = 'builder_resume_world'
        self.client.post(f'/worlds/{name}', json={'prompt': 'A mystery.', 'scope_type': 'arc-only'})
        path = self.data / 'worlds' / name
        cfg = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        cfg['creation_status'] = 'cards'
        (path / 'world_config.json').write_text(json.dumps(cfg), encoding='utf-8')
        (path / 'canon_timeline.json').write_text(json.dumps({'checkpoints': [
            {'checkpoint_id': 'cp_0', 'description': 'Start', 'required_conditions': [], 'sub_beats': []},
        ]}), encoding='utf-8')

        status = self.client.get(f'/worlds/{name}/builder/status').json()
        self.assertEqual(status['creation_status'], 'cards')
        self.assertEqual(status['next_step'], 'characters')
        self.assertIn('checkpoint_review', status['steps_done'])
        self.assertTrue(status['has_checkpoints'])
        # Approved parts are still on disk (a failed later step must not lose them).
        self.assertEqual(len(json.loads((path / 'canon_timeline.json').read_text(encoding='utf-8'))['checkpoints']), 1)

    def _make_builder_world(self, name, status='cards'):
        self.client.post(f'/worlds/{name}', json={'prompt': 'A mystery.', 'scope_type': 'arc-only'})
        path = self.data / 'worlds' / name
        cfg = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        cfg['creation_status'] = status
        (path / 'world_config.json').write_text(json.dumps(cfg), encoding='utf-8')
        return path

    def test_builder_replan_previews_and_preserves_manual_steps(self):
        name = 'builder_replan_world'
        path = self._make_builder_world(name)
        manual = self.client.put(f'/worlds/{name}/card_registry', json={'cards': []})
        self.assertEqual(manual.status_code, 200)
        cards_before = (path / 'card_registry.json').read_bytes()

        plan = self.client.post(f'/worlds/{name}/builder/replan', json={'concept': 'A pirate mystery.'})
        self.assertEqual(plan.status_code, 200, plan.text)
        body = plan.json()
        self.assertIn('cards', body['preserved_steps'])
        self.assertNotIn('cards', body['regenerate_steps'])
        self.assertIn('skeleton', body['regenerate_steps'])
        self.assertTrue(body['requires_confirmation'])
        # Preview only: no artifact overwritten.
        self.assertEqual((path / 'card_registry.json').read_bytes(), cards_before)
        cfg = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        self.assertEqual(cfg['builder']['concept_pending'], 'A pirate mystery.')

        applied = self.client.post(f'/worlds/{name}/builder/apply-replan')
        self.assertEqual(applied.status_code, 200, applied.text)
        cfg = json.loads((path / 'world_config.json').read_text(encoding='utf-8'))
        self.assertEqual(cfg['creation_status'], 'skeleton')
        self.assertEqual(cfg['builder']['concept'], 'A pirate mystery.')

    def test_builder_step_skips_manual_step_without_calling_model(self):
        from app.routes import builder_routes
        name = 'builder_manual_world'
        path = self._make_builder_world(name)
        self.client.put(f'/worlds/{name}/card_registry', json={'cards': []})
        cards_before = (path / 'card_registry.json').read_bytes()

        calls = {'n': 0}

        def counting(*args, **kwargs):
            calls['n'] += 1
            return '{}'

        with patch.object(builder_routes, 'has_real_api_key', return_value=True), \
                patch.object(main, 'call_llm', side_effect=counting):
            response = self.client.post(f'/worlds/{name}/builder/step')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['status'], 'characters')
        self.assertTrue(response.json()['kept_manual'])
        self.assertEqual(calls['n'], 0)
        self.assertEqual((path / 'card_registry.json').read_bytes(), cards_before)

    def test_story_export_escapes_html(self):
        chapters = self.read('chapters.json')
        chapters['chapters'] = [{
            'chapter_index': 1, 'turn_index': 1,
            'chapter_title': '<script>alert(1)</script>',
            'user_input': '<img src=x onerror=alert(2)>',
            'chapter_text': 'Line one\nLine two <img src=x onerror=alert(3)>',
        }]
        self.write('chapters.json', chapters)
        response = self.client.get(f'/worlds/{self.world}/export-story')
        self.assertEqual(response.status_code, 200)
        body = response.text
        self.assertNotIn('<script>alert', body)
        self.assertNotIn('<img src=x', body)
        self.assertIn('&lt;script&gt;', body)
        self.assertIn('Line one<br>Line two', body)

    def prepare_ending(self):
        self.cfg.update(story_mode='fixed_ending', lifecycle_status='endgame_pending',
                        target_ending_scenario={'summary': 'A new beginning', 'status': 'ready'})
        self.write('world_config.json', self.cfg)

    def test_epilogue_choices_load_prompt(self):
        self.prepare_ending()
        with patch.object(engine, 'call_llm', return_value=json.dumps({'choices': ['Stay', 'Leave']})):
            response = self.post('chapter/generate-epilogue-choices')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['choices'], ['Stay', 'Leave'])

    def test_epilogue_persists_is_idempotent_and_stops_continuation(self):
        self.prepare_ending()
        with patch.object(engine, 'call_llm', return_value=json.dumps({'epilogue': 'They chose a new life.'})) as model:
            for _ in range(2):
                response = self.post('chapter/generate-epilogue', {'chosen_choice': 'Leave'})
                self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(model.call_count, 1)
        history = self.read('chapters.json')
        self.assertEqual(history['epilogue']['text'], 'They chose a new life.')
        self.assertEqual(sum(c.get('kind') == 'epilogue' for c in history['chapters']), 1)
        self.assertEqual(self.read('world_config.json')['lifecycle_status'], 'completed')
        self.assertEqual(self.post('chapter/continue', {'user_input': 'Continue'}).status_code, 409)

    def test_empty_epilogue_does_not_complete_story(self):
        self.prepare_ending()
        with patch.object(engine, 'call_llm', return_value='{"epilogue": ""}'):
            response = self.post('chapter/generate-epilogue', {'chosen_choice': 'Leave'})
        self.assertEqual(response.status_code, 502)
        self.assertEqual(self.read('world_config.json')['lifecycle_status'], 'endgame_pending')
        self.assertNotIn('epilogue', self.read('chapters.json'))

    def test_epilogue_requires_endgame(self):
        with patch.object(engine, 'call_llm') as model:
            self.assertEqual(self.post('chapter/generate-epilogue', {'chosen_choice': 'Leave'}).status_code, 409)
            model.assert_not_called()

    def test_concurrent_turns_do_not_overwrite_each_other(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.post('chapter/continue', {'user_input': 'Observe.'}), range(2)))
        self.assertEqual([r.status_code for r in responses], [200, 200])
        turns = self.read('chapters.json')['chapters']
        self.assertEqual(len(turns), 2)
        self.assertEqual(len({(t['chapter_index'], t['turn_index']) for t in turns}), 2)

    def test_time_skip_preview_warns_only_about_discovered_deadlines_and_is_read_only(self):
        self.write('world_events.json', {'events': [
            {'event_id': 'known', 'status': 'pending', 'title': 'Known storm', 'deadline_tick': 3},
            {'event_id': 'secret', 'status': 'pending', 'title': 'Secret coup', 'deadline_tick': 2},
        ]})
        self.write('discovery.json', {'discoveries': [{
            'event_id': 'known', 'source': {'kind': 'rumor'}, 'at_tick': 0, 'known_deadline_tick': 3,
        }]})
        before = {p.name: p.read_bytes() for p in self.path.glob('*.json')}
        response = self.post('time-skip/preview', {
            'amount': 1, 'unit': 'days', 'activity': 'Study', 'interruption_policy': 'important_events',
        })
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual([w['event_id'] for w in body['warnings']], ['known'])
        self.assertTrue(body['will_interrupt'])
        self.assertEqual(body['granted_ticks'], 2)
        self.assertEqual(body['end_tick'], 2)
        self.assertEqual(before, {p.name: p.read_bytes() for p in self.path.glob('*.json')})

        config = self.read('world_config.json')
        config['story_clock']['tick'] = 2
        self.write('world_config.json', config)
        imminent = self.post('time-skip/execute', {
            'amount': 1, 'unit': 'hours', 'activity': 'Wait',
            'interruption_policy': 'important_events',
        })
        self.assertEqual(imminent.status_code, 409)
        self.assertEqual(imminent.json()['detail']['reason'], 'known_deadline_imminent')
        self.assertEqual(len(self.read('chapters.json')['chapters']), 0)

    def test_time_skip_execute_owns_clock_and_is_idempotent(self):
        request = {
            'amount': 2, 'unit': 'hours', 'activity': 'Practice forms',
            'interruption_policy': 'complete', 'request_id': 'skip-once',
        }
        first = self.post('time-skip/execute', request)
        self.assertEqual(first.status_code, 200, first.text)
        clock = self.read('world_config.json')['story_clock']
        self.assertEqual(clock['elapsed_minutes'], 120)
        self.assertEqual(clock['tick'], 2)
        self.assertEqual(first.json()['chapter']['time_skip_resolution']['granted_minutes'], 120)
        turns = len(self.read('chapters.json')['chapters'])
        replay = self.post('time-skip/execute', request)
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(self.read('world_config.json')['story_clock'], clock)
        self.assertEqual(len(self.read('chapters.json')['chapters']), turns)

    def test_capability_evidence_satisfies_rule_without_numeric_rank(self):
        from app.story.action_resolution import resolve_action
        character = {'hero': {
            'location': 'dojo', 'inventory': [], 'power_stat': {},
            'capabilities': [{'capability_id': 'swordsmanship', 'statement': 'Former royal swordmaster',
                              'proficiency': 'mastered', 'sources': ['backstory']}],
        }}
        config = {'action_rules': [{'keywords': ['parry'], 'required_capabilities': ['swordsmanship']}]}
        result = resolve_action('Parry the blow', character, 'hero', config)
        self.assertEqual(result['result'], 'success')
        evidence = next(c for c in result['checks'] if c['name'] == 'capability_evidence')['evidence']
        self.assertEqual(evidence[0]['evidence']['sources'], ['backstory'])

    def test_custom_openai_endpoint_does_not_rewrite_model_as_openclaw(self):
        from app.llm_client import _is_openclaw_target
        self.assertFalse(_is_openclaw_target('custom', 'https://api.deepseek.com/chat/completions'))
        self.assertTrue(_is_openclaw_target('openclaw', 'https://any.example/v1'))
        self.assertTrue(_is_openclaw_target('custom', 'http://127.0.0.1:18789/v1/chat/completions'))

    def test_committed_action_effects_drive_event_outcome_before_default(self):
        config = self.read('world_config.json')
        config['action_rules'] = [{
            'keywords': ['sever the crimson seal'],
            'consequences': [
                {'type': 'world_flag_set', 'key': 'seal_broken', 'value': True},
                {'type': 'event_influence', 'event_id': 'eclipse', 'key': 'seal_broken',
                 'value': True, 'outcome_id': 'eclipse_prevented', 'visibility': 'observable'},
                {'type': 'knowledge_flag_add', 'flag': 'severed_crimson_seal'},
            ],
        }]
        self.write('world_config.json', config)
        self.write('world_events.json', {'events': [{
            'event_id': 'eclipse', 'status': 'pending', 'event_class': 'contingent',
            'trigger_conditions': [{'field': 'story_clock.tick', 'op': '>=', 'value': 1}],
            # The default is deliberately first. Engine intervention must win.
            'outcomes': [
                {'outcome_id': 'eclipse_happens', 'resolution': 'resolved',
                 'canon_facts_add': ['The eclipse consumed the valley.']},
                {'outcome_id': 'eclipse_prevented', 'resolution': 'prevented',
                 'canon_facts_add': ['The broken seal dispersed the eclipse.']},
            ],
        }]})
        response = self.post('chapter/continue', {
            'user_input': 'Sever the crimson seal.', 'request_id': 'bridge-once',
        })
        self.assertEqual(response.status_code, 200, response.text)
        chapter = response.json()['chapter']
        self.assertEqual(len(chapter['action_effects']['applied']), 3)
        self.assertEqual(chapter['action_resolution']['engine_effects'][1]['outcome_id'], 'eclipse_prevented')
        world = self.read('world_config.json')
        self.assertTrue(world['world_flags']['seal_broken'])
        self.assertTrue(world['world_flags']['event_influence']['eclipse']['seal_broken'])
        hero = self.read('character_state.json')['characters']['char_xueli']
        self.assertIn('severed_crimson_seal', hero['knowledge_flags'])
        event = self.read('world_events.json')['events'][0]
        self.assertEqual(event['status'], 'prevented')
        self.assertEqual(event['resolved_outcome_id'], 'eclipse_prevented')
        facts = [fact['statement'] for fact in self.read('world_canon_store.json')['facts']]
        self.assertEqual(facts, ['The broken seal dispersed the eclipse.'])
        self.assertEqual(len(event['interventions']), 1)

        replay = self.post('chapter/continue', {
            'user_input': 'Sever the crimson seal.', 'request_id': 'bridge-once',
        })
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(len(self.read('world_events.json')['events'][0]['interventions']), 1)

    def test_hidden_event_effect_projection_does_not_reveal_secret_branch(self):
        from app.story.action_effects import compile_action_effects, project_action_effects

        plan = compile_action_effects({
            'result': 'success',
            'consequences': [{
                'type': 'event_influence', 'event_id': 'secret_eclipse',
                'key': 'seal_broken', 'value': True,
                'outcome_id': 'secret_eclipse_prevented',
            }],
        }, 'hero', {'hero': {}}, {'events': [{
            'event_id': 'secret_eclipse', 'status': 'pending',
            'outcomes': [{'outcome_id': 'secret_eclipse_prevented'}],
        }]})

        self.assertEqual(plan['effects'][0]['event_id'], 'secret_eclipse')
        self.assertEqual(plan['effects'][0]['outcome_id'], 'secret_eclipse_prevented')
        public = project_action_effects(plan)['effects'][0]
        self.assertEqual(public['visibility'], 'hidden')
        self.assertNotIn('event_id', public)
        self.assertNotIn('outcome_id', public)
        self.assertNotIn('seal_broken', str(public))
        rejected = project_action_effects({'effects': [], 'rejected': [{
            'index': 0, 'type': 'event_influence', 'reason': 'event_not_pending',
            'event_id': 'secret_eclipse',
        }]})['rejected'][0]
        self.assertNotIn('event_id', rejected)
        self.assertNotIn('secret_eclipse', str(rejected))

    def test_failed_or_unsupported_action_effects_cannot_mutate_world(self):
        config = self.read('world_config.json')
        config['action_rules'] = [{
            'keywords': ['open forbidden gate'], 'required_tools': ['silver key'],
            'consequences': [
                {'type': 'world_flag_set', 'key': 'gate_open', 'value': True},
                {'type': 'set', 'path': 'characters.char_xueli.alive', 'value': False},
            ],
        }]
        self.write('world_config.json', config)
        response = self.post('chapter/continue', {'user_input': 'Open forbidden gate.'})
        self.assertEqual(response.status_code, 200)
        chapter = response.json()['chapter']
        self.assertEqual(chapter['action_resolution']['result'], 'impossible')
        self.assertEqual(chapter['action_effects']['applied'], [])
        self.assertNotIn('gate_open', self.read('world_config.json').get('world_flags', {}))
        self.assertTrue(self.read('character_state.json')['characters']['char_xueli']['alive'])

    def test_creator_can_preview_and_replace_action_rules(self):
        revision = self.read('world_config.json').get('revision', 0)
        rules = [{'keywords': ['ring bell'], 'consequences': [
            {'type': 'world_flag_set', 'key': 'bell_rung', 'value': True},
        ]}]
        payload = {'expected_revision': revision, 'preview': True, 'reason': 'Add bell interaction',
                   'changes': [{'kind': 'action_rules', 'value': rules}]}
        preview = self.post('creator/edit', payload)
        self.assertEqual(preview.status_code, 200, preview.text)
        self.assertTrue(preview.json()['ok'])
        self.assertNotEqual(self.read('world_config.json').get('action_rules'), rules)
        payload['preview'] = False
        committed = self.post('creator/edit', payload)
        self.assertEqual(committed.status_code, 200, committed.text)
        self.assertEqual(self.read('world_config.json')['action_rules'], rules)

    def test_item_policies_protect_causal_items_and_require_capability(self):
        from app.story.inventory import resolve_inventory_action, apply_item_state_effects, apply_inventory_resolution, normalize_item
        artifact = {'instance_id': 'world-key-1', 'name': 'World Key', 'item_kind': 'causal_artifact', 'drop_policy': 'bound',
                    'requirements': ['ritual literacy']}
        self.assertEqual(resolve_inventory_action('Drop World Key', [artifact], {})['reason'],
                         'item_is_bound')
        self.assertEqual(resolve_inventory_action('Use World Key', [artifact], {})['reason'],
                         'capability_required')
        character = {'capabilities': [{'capability_id': 'ritual_literacy', 'statement': 'Reads ritual script'}]}
        artifact['state_effects'] = [
            {'path': 'status_effects', 'operation': 'add', 'value': {'name': 'Marked by the Key', 'duration': 3}},
            {'path': 'power_stat.realm', 'operation': 'replace', 'value': 'forbidden'},
        ]
        resolution = resolve_inventory_action('Use World Key', [artifact], character)
        self.assertEqual(resolution['status'], 'resolved')
        applied = apply_item_state_effects(character, resolution, at_tick=4)
        self.assertEqual([e['name'] for e in applied], ['Marked by the Key'])
        self.assertNotIn('power_stat', character)
        tea = {'instance_id': 'tea', 'name': 'Tea', 'quantity': 2, 'stackable': True,
               'item_kind': 'consumable', 'usage': {'mode': 'quantity', 'remaining': 2}}
        tea_resolution = resolve_inventory_action('Use Tea', [tea], {})
        inventory = [tea]
        apply_inventory_resolution(inventory, tea_resolution)
        self.assertEqual(normalize_item(inventory[0])['quantity'], 1)
        self.assertEqual(normalize_item(inventory[0])['usage']['remaining'], 1)


if __name__ == '__main__':
    unittest.main()
