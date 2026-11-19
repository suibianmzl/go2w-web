"""采集协议的边界测试；不启动真实ROS或机器狗。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace as NS
import sys
import time
import math
import threading
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))
sys.path.insert(0, str(ROOT / 'dev/fake_ros'))
NAV = ROOT.parent / 'go2w/src/navigation/go2w_taught_route'
sys.path.insert(0, str(NAV))


class TaughtWebTests(unittest.IsolatedAsyncioTestCase):
    async def test_ordinary_navigation_never_silently_ignores_saved_teaching(self):
        from teaching_bridge import TaughtWeb

        host = TaughtWeb()
        host.node = NS(get_node_names=lambda: [])
        host.taught_repo = NS(available=True, document=lambda _: {'state': 'ready'})
        self.assertFalse((await host.check_ordinary_taught_binding('lab'))[0])
        host.taught_repo.document = lambda _: {'state': 'empty'}
        self.assertTrue((await host.check_ordinary_taught_binding('lab'))[0])
        calls = []

        async def synced(name, required=False):
            calls.append(name)
            host._taught_synced = name

        host.node.get_node_names = lambda: ['taught_route']
        host._taught_synced = None
        host.sync_taught_map = synced
        self.assertTrue((await host.check_ordinary_taught_binding('lab'))[0])
        self.assertEqual(calls, ['lab'])

    async def test_disconnect_during_start_or_resume_cannot_leave_recording_owner(self):
        from teaching_bridge import TaughtWeb
        from teaching import TeachingError

        for op in ('taught_start', 'taught_resume'):
            with self.subTest(op=op):
                started = threading.Event()
                release = threading.Event()

                class Captures:
                    state = 'paused'

                    def start(self, *args, **kwargs):
                        started.set()
                        release.wait(3)
                        self.state = 'recording'
                        return {'session_id': 'abc', 'map': 'lab', 'state': self.state}

                    resume = start

                    def pause(self, sid, reason, interrupted=False):
                        self.state = 'interrupted'
                        return {'session_id': sid, 'map': 'lab', 'state': self.state}

                class WS:
                    closed = False

                ws = WS()
                host = TaughtWeb()
                host.captures = Captures()
                host.clients = {ws: {'last_ping': time.monotonic()}}
                host._taught_capture = (
                    {'session_id': 'abc', 'map': 'lab', 'state': 'paused'}
                    if op.endswith('resume')
                    else None
                )
                host._taught_mutation = asyncio.Lock()
                host._taught_job = None
                host._taught_finish_busy = False
                host.taught_owner = None
                host.owner = None
                host.map_busy = False
                host._taught_synced = 'lab'
                host.heartbeat = 1.0

                async def checked(name):
                    return {'ok': True}, {'map_id': 'map1'}

                async def document(name):
                    return {'map': name, 'capture': host._taught_capture}

                host.taught_self_check = checked
                host.taught_document = document
                job = asyncio.create_task(
                    host.handle_taught(
                        ws, {'op': op, 'map': 'lab', 'session_id': 'abc', 'mode': 'replace'}
                    )
                )
                while not started.is_set():
                    await asyncio.sleep(0.01)
                ws.closed = True
                host.clients.pop(ws)
                await host.taught_owner_lost(ws, 'disconnect during disk write')
                release.set()
                with self.assertRaises(TeachingError):
                    await job
                self.assertEqual(host.captures.state, 'interrupted')
                self.assertIsNot(host.taught_owner, ws)

    async def test_only_capture_owner_loss_interrupts_recording(self):
        from teaching_bridge import TaughtWeb

        class Captures:
            def __init__(self):
                self.paused = []

            def pause(self, sid, reason, interrupted=False):
                self.paused.append((sid, interrupted))
                return {'session_id': sid, 'state': 'interrupted', 'map': 'lab'}

        host = TaughtWeb()
        host.captures = Captures()
        host._taught_capture = {'session_id': 'abc', 'state': 'recording', 'map': 'lab'}
        owner = object()
        host.taught_owner = owner
        await host.taught_owner_lost(object(), 'observer closed')
        self.assertEqual(host.captures.paused, [])
        await host.taught_owner_lost(owner, 'owner closed')
        self.assertEqual(host.captures.paused, [('abc', True)])

    async def test_persistence_failure_does_not_abort_motion_stop_cleanup(self):
        from teaching_bridge import TaughtWeb

        class Captures:
            def pause(self, *args, **kwargs):
                raise OSError('disk full')

        host = TaughtWeb()
        host.captures = Captures()
        events = []
        host.node = NS(event=lambda level, message: events.append((level, message)))
        host._taught_capture = {'session_id': 'abc', 'map': 'lab', 'state': 'recording'}
        await host.taught_interrupt('owner disconnected')
        self.assertEqual(host._taught_capture['state'], 'interrupted')
        self.assertTrue(events)

    async def test_unknown_localization_quality_cannot_pass_self_check(self):
        from teaching_bridge import TaughtWeb

        host = TaughtWeb()

        class Node:
            map_file = None
            amcl = None
            odom = (0.0, 0.0, 0.0)
            healthy = True
            healthy_t = time.monotonic()
            _taught_map_id = 'map1'
            _taught_scan = None

            def get_node_names(self):
                return ['amcl', 'bt_navigator', 'go2w_cmd_bridge', 'taught_route']

            def taught_measurement(self):
                return {'map_id': 'map1', 'pose_map': [0, 0, 0]}

            def nav_active(self):
                return False

        host.node = Node()
        host.estop = False
        host.busy = False
        host.map_loading = False
        host._taught_epoch = 0.0
        host.pose_set_t = time.monotonic()
        host.stood_seq = 0
        host._taught_stood_seq = 0
        host._taught_synced = 'lab'
        host.taught_checks = {}
        host.simulated = False
        host.prof = lambda mode: 'run' if mode == 'navigation' else 'stop'
        host.route_running = lambda: False
        host.taught_repo = NS(document=lambda _: {'map': 'lab', 'map_id': 'map1'})
        host.ctrl_get = lambda: ('Velocity', True, 1)
        host.ctrl_op = None
        check, measurement = await host.taught_self_check('lab')
        self.assertFalse(check['ok'])
        self.assertTrue(
            any(not row['ok'] and row['key'] == 'localization_quality' for row in check['checks'])
        )

    async def test_old_scan_delivered_now_cannot_pass_self_check(self):
        from teaching_bridge import TaughtWeb

        now = time.monotonic()
        host = TaughtWeb()
        host.node = NS(
            map_file=None,
            amcl=(0.03, 1.0, now),
            odom=(0.0, 0.0, 0.0),
            _taught_map_id='map1',
            _taught_scan={'map_id': 'map1', 'match': 100.0, 'received': now, 'stamp': 1.0},
            _taught_pose_stamp=10.0,
            get_clock=lambda: NS(now=lambda: NS(nanoseconds=11_000_000_000)),
            get_node_names=lambda: ['amcl', 'bt_navigator', 'go2w_cmd_bridge', 'taught_route'],
            taught_measurement=lambda: {'map_id': 'map1'},
            nav_active=lambda: False,
        )
        host.estop = host.busy = host.map_loading = False
        host._taught_epoch = now - 1.0
        host.pose_set_t = now - 0.5
        host.stood_seq = host._taught_stood_seq = 0
        host._taught_synced = 'lab'
        host.taught_checks = {}
        host.prof = lambda mode: 'run'
        host.route_running = lambda: False
        host.taught_repo = NS(document=lambda _: {'map_id': 'map1'})
        host.ctrl_get = lambda: ('Velocity', True, 1)
        host.ctrl_op = None
        check, _ = await host.taught_self_check('lab')
        self.assertFalse(check['ok'])


class MeasurementTests(unittest.TestCase):
    def test_map_trash_moves_teaching_history_and_raw_capture_with_map(self):
        import tempfile
        from go2w_web import Server

        with tempfile.TemporaryDirectory() as folder:
            maps = Path(folder) / 'maps'
            maps.mkdir()
            (maps / 'lab.yaml').write_text('map')
            (maps / 'other.yaml').write_text('other')
            raw = maps / 'lab.taught/captures/abc'
            raw.mkdir(parents=True)
            (raw / 'samples.jsonl').write_text('raw evidence\n')
            destination = Path(folder) / 'trash'
            size = Server.trash_map(str(maps), 'lab', str(destination), [], None)
            self.assertTrue((destination / 'lab.taught/captures/abc/samples.jsonl').is_file())
            self.assertTrue((maps / 'other.yaml').is_file())
            self.assertFalse((maps / 'lab.taught').exists())
            self.assertGreater(size, 10)

    def test_runtime_map_yaw_is_not_round_tripped_a_second_time(self):
        from teaching_bridge import TaughtMeasurements
        from go2w_taught_route.map_bundle import map_identity
        import numpy as np

        yaw = 0.1
        grid = np.zeros((20, 20), dtype=np.int8)
        node = TaughtMeasurements()
        node.init_taught_measurements()
        origin = NS(
            position=NS(x=1.0, y=-2.0),
            orientation=NS(x=0.0, y=0.0, z=math.sin(yaw / 2), w=math.cos(yaw / 2)),
        )
        node.observe_taught_map(
            NS(
                data=grid.ravel().tolist(),
                header=NS(frame_id='map'),
                info=NS(width=20, height=20, resolution=float(np.float32(0.05)), origin=origin),
            )
        )
        self.assertEqual(node._taught_map_id, map_identity(grid, 0.05, [1.0, -2.0, yaw]))


if __name__ == '__main__':
    unittest.main()
