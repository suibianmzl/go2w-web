"""稀疏控制点预览、发布与实际处理报告；复用真实算法和临时地图。"""

import json
from pathlib import Path
import sys
import tempfile
import unittest
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT.parent / 'go2w/src/navigation/go2w_taught_route'))
from teaching import TeachingRepository, CaptureManager


class SplineRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.maps = self.root / 'maps'
        self.maps.mkdir()
        self.pixels = np.full((200, 200), 254, np.uint8)
        self.image()
        self.path = self.maps / 'lab.yaml'
        self.path.write_text(
            'image: lab.pgm\nmode: trinary\nresolution: 0.05\norigin: [-5,-5,0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.196\n'
        )
        self.repo = TeachingRepository(self.maps, self.root / 'routes')

    def image(self):
        (self.maps / 'lab.pgm').write_bytes(b'P5\n200 200\n255\n' + self.pixels.tobytes())

    def preview(self, points):
        doc = self.repo.document('lab')
        return self.repo.preview(
            'lab', points, expected_revision=doc['revision'], map_token=doc['map_token']
        )

    def test_sparse_controls_preview_a_real_curve_without_publishing(self):
        before = self.path.read_bytes()
        points = [{'x': -1.0, 'y': 0.0}, {'x': 0.0, 'y': 1.0}, {'x': 1.0, 'y': 0.0}]
        preview = self.preview(points)
        self.assertTrue(preview['executable'])
        self.assertEqual(len(preview['control_points']), 3)
        self.assertGreater(len(preview['preview_points']), 20)
        near = [p['y'] for p in preview['preview_points'] if abs(p['x'] + 0.5) < 0.04]
        self.assertGreater(max(near), 0.55, '样条不能仍是y=x+1的直线连接')
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(self.repo.document('lab')['revision'], 0)
        self.assertFalse((self.maps / 'lab.taught/versions').exists())

    def test_save_matches_preview_and_reload_keeps_sparse_handles(self):
        controls = [{'x': -1.0, 'y': 0.0}, {'x': 0.0, 'y': 1.0}, {'x': 1.0, 'y': 0.0}]
        before = self.repo.document('lab')
        preview = self.preview(controls)
        saved = self.repo.save('lab', controls, expected_revision=0, map_token=before['map_token'])
        np.testing.assert_allclose(
            [[p['x'], p['y'], p['yaw']] for p in saved['points']],
            [[p['x'], p['y'], p['yaw']] for p in preview['preview_points']],
        )
        reopened = TeachingRepository(self.maps, self.root / 'routes').document('lab')
        self.assertEqual(len(reopened['control_points']), 3)
        self.assertGreater(len(reopened['points']), 3)
        self.assertEqual(reopened['processing']['interpolation'], 'cubic_spline')
        self.assertEqual(reopened['processing']['input_count'], 3)
        self.assertEqual(reopened['processing']['output_count'], len(reopened['points']))
        self.assertTrue(reopened['processing']['full_sweep_checked'])

    def test_unsafe_curve_has_preview_but_cannot_replace_old_reference(self):
        initial = self.repo.document('lab')
        saved = self.repo.save(
            'lab',
            [{'x': -2.0, 'y': -2.0}, {'x': 0.0, 'y': -2.0}],
            expected_revision=0,
            map_token=initial['map_token'],
        )
        # 点的直线连线远离这一障碍，三次样条在右侧拱起后机器人轮廓会扫到它。
        self.pixels[200 - 1 - 145, 130] = 0
        self.image()
        controls = [{'x': -3.0, 'y': 0.0}, {'x': 0.0, 'y': 3.0}, {'x': 3.0, 'y': 0.0}]
        before = self.path.read_bytes()
        preview = self.preview(controls)
        self.assertFalse(preview['executable'])
        self.assertGreater(len(preview['preview_points']), 3)
        current = self.repo.document('lab')
        with self.assertRaises(Exception):
            self.repo.save(
                'lab', controls, expected_revision=saved['revision'], map_token=current['map_token']
            )
        self.assertEqual(self.path.read_bytes(), before)

    def test_discontinuous_controls_are_not_silently_joined(self):
        controls = [
            {'x': 0.0, 'y': 0.0, 'continuous_id': 0},
            {'x': 1.0, 'y': 0.0, 'continuous_id': 0},
            {'x': 2.0, 'y': 0.0, 'continuous_id': 1},
            {'x': 3.0, 'y': 0.0, 'continuous_id': 1},
        ]
        preview = self.preview(controls)
        self.assertFalse(preview['executable'])
        self.assertEqual({p['continuous_id'] for p in preview['preview_points']}, {0, 1})
        self.assertNotIn('go2w_taught_route:', self.path.read_text())

    def test_recorded_finish_reports_actual_smoothing_and_keeps_raw_samples(self):
        doc = self.repo.document('lab')
        engine = CaptureManager(self.repo)
        self.addCleanup(engine.close)

        def measurement(x, y, t):
            return {
                'stamp': t,
                'received_stamp': t,
                'pose_map': [x, y, 0.0],
                'pose_odom': [x, y, 0.0],
                'tf_stamp': t,
                'twist': [0.0, 0.0, 0.0],
                'map_id': doc['map_id'],
            }

        session = engine.start(
            'lab',
            mode='replace',
            expected_revision=0,
            map_token=doc['map_token'],
            measurement=measurement(0.0, 0.0, 1.0),
        )
        engine.sample(measurement(0.2, 0.04, 1.2))
        engine.sample(measurement(0.4, 0.0, 1.4))
        saved = engine.finish(session['session_id'])
        self.assertEqual(saved['processing']['interpolation'], 'recorded')
        self.assertEqual(saved['processing']['input_count'], 3)
        self.assertGreater(saved['processing']['max_offset'], 0.0)
        self.assertEqual(saved['processing']['max_offset'], saved['report']['max_offset'])
        self.assertEqual(saved['processing']['iterations'], saved['report']['iterations'])
        rows = [
            json.loads(row)
            for row in (self.maps / 'lab.taught/captures' / session['session_id'] / 'samples.jsonl')
            .read_text()
            .splitlines()
        ]
        self.assertEqual([p['pose_map'][:2] for p in rows], [[0.0, 0.0], [0.2, 0.04], [0.4, 0.0]])


if __name__ == '__main__':
    unittest.main()
