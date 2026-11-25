"""示教文件与持久采集的行为测试：用真实算法和临时地图，不发机器人命令。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
NAV = ROOT.parent / 'go2w/src/navigation/go2w_taught_route'
if NAV.is_dir():
    sys.path.insert(0, str(NAV))


class TeachingTests(unittest.TestCase):
    def setUp(self):
        from teaching import TeachingRepository, CaptureManager

        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.maps = self.root / 'maps'
        self.maps.mkdir()
        self.pixels = np.full((100, 100), 254, np.uint8)
        self.image()
        (self.maps / 'lab.yaml').write_text(
            '# operator comment\nimage: lab.pgm\nmode: trinary\nresolution: 0.1\n'
            'origin: [-5, -5, 0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n'
        )
        self.repo = TeachingRepository(self.maps, self.root / 'routes')
        self.manager_type = CaptureManager

    def image(self):
        (self.maps / 'lab.pgm').write_bytes(b'P5\n100 100\n255\n' + self.pixels.tobytes())

    def save(self, points, doc=None):
        doc = doc or self.repo.document('lab')
        return self.repo.save(
            'lab', points, expected_revision=doc['revision'], map_token=doc['map_token']
        )

    def measurement(self, x, y=0.0, t=1.0):
        return {
            'stamp': t,
            'received_stamp': t,
            'pose_map': [x, y, 0.0],
            'pose_odom': [x, y, 0.0],
            'tf_stamp': t,
            'twist': [0.0, 0.0, 0.0],
            'map_id': self.repo.document('lab')['map_id'],
        }

    def manager(self):
        engine = self.manager_type(self.repo)
        self.addCleanup(engine.close)
        return engine

    def start(self, engine, mode='replace', measurement=None, **kwargs):
        doc = self.repo.document('lab')
        return engine.start(
            'lab',
            mode=mode,
            expected_revision=doc['revision'],
            map_token=doc['map_token'],
            measurement=measurement or self.measurement(0.0),
            **kwargs
        )

    def test_drawing_creates_one_embedded_route_and_updates_existing_file(self):
        original = (self.maps / 'lab.pgm').read_bytes()
        first = self.save([{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}])
        self.assertTrue(first['executable'])
        self.assertEqual(first['revision'], 1)
        second = self.save([{'x': 0.0, 'y': 0.5}, {'x': 1.0, 'y': 0.5}], first)
        self.assertEqual(second['revision'], 2)
        text = (self.maps / 'lab.yaml').read_text()
        self.assertEqual(text.count('go2w_taught_route:'), 1)
        self.assertIn('# operator comment', text)
        self.assertEqual((self.maps / 'lab.pgm').read_bytes(), original)
        self.assertAlmostEqual(second['points'][0]['y'], 0.5)
        runtime = self.repo.store.default_for_map(second['map_id'])
        self.assertAlmostEqual(runtime['points'][0]['y'], 0.5)

    def test_collision_or_stale_revision_never_overwrites_saved_route(self):
        first = self.save([{'x': -2.0, 'y': 0.0}, {'x': -1.0, 'y': 0.0}])
        original = (self.maps / 'lab.yaml').read_bytes()
        with self.assertRaises(Exception):
            self.repo.save(
                'lab',
                [{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}],
                expected_revision=0,
                map_token=first['map_token'],
            )
        self.assertEqual((self.maps / 'lab.yaml').read_bytes(), original)
        # 中心x=1m处设一列墙；两个端点都在空地，但连接扫过墙，必须拒绝。
        self.pixels[:, 60] = 0
        self.image()
        changed = self.repo.document('lab')
        with self.assertRaises(Exception):
            self.save([{'x': 0.0, 'y': 0.0}, {'x': 2.0, 'y': 0.0}], changed)
        self.assertEqual((self.maps / 'lab.yaml').read_bytes(), original)

    def test_changed_map_token_is_rejected_even_before_first_save(self):
        before = self.repo.document('lab')
        self.pixels[0, 0] = 0
        self.image()
        with self.assertRaises(Exception):
            self.repo.save(
                'lab',
                [{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}],
                expected_revision=0,
                map_token=before['map_token'],
            )
        self.assertNotIn('go2w_taught_route:', (self.maps / 'lab.yaml').read_text())

    def test_restart_recovers_durable_samples_and_requires_explicit_resume_at_break(self):
        first = self.manager()
        session = self.start(first)
        sid = session['session_id']
        for i in range(1, 4):
            first.sample(self.measurement(i * 0.1, t=1 + i * 0.1))
        first.close()
        recovered = self.manager()
        state = recovered.status()
        self.assertEqual(state['state'], 'interrupted')
        self.assertEqual(state['sample_count'], 4)
        with self.assertRaises(Exception):
            recovered.resume(sid, self.measurement(1.0, t=2.0))
        self.assertEqual(recovered.status()['state'], 'interrupted')
        recovered.resume(sid, self.measurement(0.3, t=2.0))
        recovered.sample(self.measurement(0.4, t=2.1))
        saved = recovered.finish(sid)
        self.assertTrue(saved['executable'])
        self.assertEqual(len({p['continuous_id'] for p in saved['points']}), 1)
        self.assertGreater(saved['length'], 0.35)
        self.assertTrue(list((self.maps / 'lab.taught').rglob('samples.jsonl')))

    def test_append_keeps_original_reference_geometry(self):
        old = self.save([{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}])
        engine = self.manager()
        s = self.start(engine, 'append', self.measurement(1.0))
        for i in range(1, 4):
            engine.sample(self.measurement(1 + i * 0.1, t=1 + i * 0.1))
        new = engine.finish(s['session_id'])
        a = [(p['x'], p['y']) for p in old['points']]
        b = [(p['x'], p['y']) for p in new['points']]
        np.testing.assert_allclose(b[: len(a)], a, atol=1e-8)
        self.assertGreater(new['length'], 1.25)

    def test_replace_segment_preserves_both_exteriors(self):
        old = self.save([{'x': 0.0, 'y': 0.0}, {'x': 2.0, 'y': 0.0}])
        engine = self.manager()
        start = min(range(len(old['points'])), key=lambda i: abs(old['points'][i]['x'] - 0.5))
        end = min(range(len(old['points'])), key=lambda i: abs(old['points'][i]['x'] - 1.5))
        s = self.start(
            engine, 'replace_segment', self.measurement(0.5), start_index=start, end_index=end
        )
        for x, y, t in ((0.5, 0.3, 1.3), (1.5, 0.3, 1.9), (1.5, 0.0, 2.2)):
            engine.sample(self.measurement(x, y, t))
        new = engine.finish(s['session_id'])
        a = [(p['x'], p['y']) for p in old['points']]
        b = [(p['x'], p['y']) for p in new['points']]
        np.testing.assert_allclose(b[:5], a[:5], atol=1e-8)
        np.testing.assert_allclose(b[-5:], a[-5:], atol=1e-8)
        self.assertGreater(max(p['y'] for p in new['points']), 0.2)

    def test_stale_session_and_concurrent_editor_do_not_overwrite(self):
        engine = self.manager()
        s = self.start(engine)
        engine.sample(self.measurement(0.2, t=1.2))
        with self.assertRaises(Exception):
            engine.pause('another-session')
        self.assertEqual(engine.status()['state'], 'recording')
        saved = self.save([{'x': 0.0, 'y': 1.0}, {'x': 1.0, 'y': 1.0}])
        with self.assertRaises(Exception):
            engine.finish(s['session_id'])
        self.assertEqual(self.repo.document('lab')['revision'], saved['revision'])
        self.assertTrue(list((self.maps / 'lab.taught').rglob('samples.jsonl')))

    def test_same_measurement_is_not_reprocessed_after_downsampling(self):
        engine = self.manager()
        self.start(engine)
        # 网页20Hz会两次读到同一帧10Hz里程计；有效但未保留的帧也不能重复处理。
        engine.sample(self.measurement(0.0, t=1.1))
        engine.sample(self.measurement(0.0, t=1.1))
        self.assertEqual(engine.status()['state'], 'recording')
        self.assertEqual(engine.recorder.events, [])

    def test_too_few_samples_can_continue_and_failed_geometry_survives_restart(self):
        engine = self.manager()
        session = self.start(engine)
        with self.assertRaises(Exception):
            engine.finish(session['session_id'])
        self.assertEqual(engine.status()['state'], 'paused')
        engine.resume(session['session_id'], self.measurement(0.0, t=1.2))
        engine.sample(self.measurement(0.2, t=1.4))
        self.assertTrue(engine.finish(session['session_id'])['executable'])
        invalid = self.start(engine, measurement=self.measurement(-4.9, t=3.0))
        engine.sample(self.measurement(-4.7, t=3.2))
        with self.assertRaises(Exception):
            engine.finish(invalid['session_id'])
        engine.close()
        recovered = self.manager()
        status = recovered.status()
        self.assertEqual(status['session_id'], invalid['session_id'])
        self.assertEqual(status['state'], 'failed')
        self.assertGreaterEqual(status['sample_count'], 2)
        self.assertFalse(status['can_resume'])

    def test_map_change_between_document_and_grid_is_not_adopted_as_user_baseline(self):
        from unittest.mock import patch

        before = self.repo.document('lab')
        load = self.repo.grid

        def changed_grid(name):
            self.pixels[0, 0] = 0
            self.image()
            return load(name)

        with patch.object(self.repo, 'grid', side_effect=changed_grid):
            with self.assertRaises(Exception):
                self.save([{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}], before)
        self.assertNotIn('go2w_taught_route:', (self.maps / 'lab.yaml').read_text())

    def test_manually_changed_unsafe_geometry_remains_visible_for_repair(self):
        import yaml

        doc = self.save([{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}])
        path = self.maps / 'lab.yaml'
        data = yaml.safe_load(path.read_text())
        for point in data['go2w_taught_route']['reference']['points']:
            point['y'] += 10.0
        path.write_text(yaml.safe_dump(data))
        invalid = self.repo.document('lab')
        self.assertEqual(invalid['state'], 'invalid')
        self.assertFalse(invalid['executable'])
        self.assertEqual(invalid['points'][0]['y'], 10.0)
        fixed = self.save([{'x': 0.0, 'y': 0.0}, {'x': 1.0, 'y': 0.0}], invalid)
        self.assertTrue(fixed['executable'])
        self.assertEqual(fixed['revision'], doc['revision'] + 1)


if __name__ == '__main__':
    unittest.main()
