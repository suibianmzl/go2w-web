#!/usr/bin/env python3
"""隔离假机器人：示教参数、同钟测量与精确时间 TF。不会启动 ROS 或连接硬件。"""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as N
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'dev/fake_ros'))
sys.path.insert(0, os.environ.get('GO2W_NAV_SOURCE', str(ROOT.parent / 'go2w/src/navigation/go2w_taught_route')))

from go2w_fake_procs import FakeProcs
from go2w_fake_sim import Sim
from rcl_interfaces.msg import Parameter, ParameterType, ParameterValue
from rcl_interfaces.srv import GetParameters, SetParameters
from rclpy.node import Node
from rclpy.time import Time
from tf2_ros import Buffer, TransformListener


class FakeTaughtTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        maps = Path(self.tmp.name) / 'maps'
        maps.mkdir()
        self.map = maps / 'lab_20261021.yaml'
        self.map.write_text('image: lab_20261021.pgm\nresolution: 0.05\norigin: [0, 0, 0]\nnegate: 0\noccupied_thresh: 0.65\nfree_thresh: 0.25\n')
        (maps / 'lab_20261021.pgm').write_bytes(b'P5\n80 80\n255\n' + bytes([254]) * 6400)
        self.env = patch.dict(os.environ, {'GO2W_FAKE_MAP': str(self.map), 'GO2W_FAKE_STAMP_RATE': '1.5', 'GO2W_TAUGHT_ROUTE': 'false'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.data = patch('go2w_fake_sim.DATA', self.tmp.name)
        self.data.start(); self.addCleanup(self.data.stop)
        self.node = Node('taught_fake_test')
        self.sim = Sim(self.node)
        self.node.fake_sim = self.sim

    def test_explicit_profile_flag_and_parameter_services(self):
        procs = FakeProcs(self.node)
        asyncio.run(procs.start_mode('navigation', 'lab_20261021', taught_route=True))
        self.sim.jobs.get_nowait()()
        self.sim.ready_at['navigation'] = 0
        self.assertEqual(self.sim.mode_environment['GO2W_TAUGHT_ROUTE'], 'true')
        self.assertIn('taught_route', self.node.get_node_names())
        self.assertTrue(self.sim.has_service('/taught_route/get_parameters'))
        get = GetParameters.Request(); get.names = ['map_yaml', 'missing']
        response = self.sim.call_service('/taught_route/get_parameters', get)
        self.assertEqual(response.values[0].type, ParameterType.PARAMETER_STRING)
        self.assertEqual(response.values[0].string_value, str(self.map))
        self.assertEqual(response.values[1].type, ParameterType.PARAMETER_NOT_SET)
        p = Parameter(); p.name = 'map_yaml'; p.value = ParameterValue(ParameterType.PARAMETER_STRING, string_value='/fake/copy/other.yaml')
        req = SetParameters.Request(); req.parameters = [p]
        self.assertTrue(self.sim.call_service('/taught_route/set_parameters', req).results[0].successful)
        self.assertEqual(self.sim.call_service('/taught_route/get_parameters', get).values[0].string_value, '/fake/copy/other.yaml')
        p.value = ParameterValue(ParameterType.PARAMETER_DOUBLE, 2.)
        self.assertFalse(self.sim.call_service('/taught_route/set_parameters', req).results[0].successful)
        asyncio.run(procs.start_mode('navigation', 'lab_20261021'))
        self.sim.jobs.get_nowait()(); self.sim.ready_at['navigation'] = 0
        self.assertNotIn('taught_route', self.node.get_node_names())
        self.assertFalse(self.sim.has_service('/taught_route/get_parameters'))

    def test_clock_measurement_and_tf_use_one_ros_epoch(self):
        with patch('go2w_fake_sim.time.monotonic', return_value=self.sim.t0 + 2.):
            sec, ns = self.sim.stamp()
            stamp = Time.from_msg(N(sec=sec, nanosec=ns))
            self.assertEqual(stamp.nanoseconds, self.node.get_clock().now().nanoseconds)
            self.assertGreater(stamp.nanoseconds, 1_000_000_000_000_000_000)
            self.assertEqual(stamp.to_msg().sec, sec)
            self.assertEqual(stamp.to_msg().nanosec, ns)
        self.assertEqual(Time(seconds=2, nanoseconds=7).nanoseconds, 2_000_000_007)

    def test_tf_uses_recorded_pose_for_requested_measurement(self):
        self.sim.x = 1.; self.sim.y = 2.; self.sim.th = .5
        with patch('go2w_fake_sim.time.monotonic', return_value=self.sim.t0 + 3.):
            sec, ns = self.sim.stamp()
        when = Time.from_msg(N(sec=sec, nanosec=ns))
        self.sim.x = 9.; self.sim.y = 8.
        buffer = Buffer(); TransformListener(buffer, self.node)
        exact = buffer.lookup_transform('map', 'base_footprint', when)
        self.assertEqual(exact.transform.translation.x, 1.)
        self.assertEqual(exact.transform.translation.y, 2.)
        self.assertEqual(Time.from_msg(exact.header.stamp).nanoseconds, when.nanoseconds)
        self.assertEqual(buffer.lookup_transform('map', 'base_footprint', Time()).transform.translation.x, 9.)
        with self.assertRaises(LookupError):
            buffer.lookup_transform('map', 'base_footprint', Time(nanoseconds=when.nanoseconds+1))

    def test_emitted_odometry_explicitly_identifies_frames(self):
        from go2w_fake_sim import run
        from nav_msgs.msg import Odometry
        captured = []
        class StopFakeSpin(Exception):
            pass
        def blank_message():
            msg = Odometry(); msg.header.frame_id = ''; msg.child_frame_id = ''
            return msg
        def deliver(topic, msg):
            if topic == '/odom':
                captured.append(msg)
                raise StopFakeSpin()
        with patch('go2w_fake_sim.Sim', return_value=self.sim), patch('go2w_fake_sim.time.sleep'), \
             patch('nav_msgs.msg.Odometry', side_effect=blank_message), patch.object(self.node, 'deliver', side_effect=deliver):
            with self.assertRaises(StopFakeSpin):
                run(self.node)
        self.assertEqual(captured[0].header.frame_id, 'odom')
        self.assertEqual(captured[0].child_frame_id, 'base_footprint')

    def test_load_map_maps_only_container_path_prefix(self):
        from nav2_msgs.srv import LoadMap
        import shutil
        # 真实临时目录也可能在项目的 go2w/data 内，不能因为路径中段同名而再次映射。
        maps = Path(self.tmp.name) / 'project/go2w/data/development/fixture/maps'
        maps.mkdir(parents=True)
        target = maps / 'middle_path.yaml'
        shutil.copyfile(self.map, target)
        shutil.copyfile(self.map.with_suffix('.pgm'), maps / 'lab_20261021.pgm')
        req = LoadMap.Request(); req.map_url = str(target)
        self.assertEqual(self.sim.call_service('/map_server/load_map', req).result, 0)
        self.assertEqual(self.sim.map_name, 'middle_path')
        req.map_url = '/go2w/data/maps/lab_20261021.yaml'
        self.assertEqual(self.sim.call_service('/map_server/load_map', req).result, 0)
        self.assertEqual(self.sim.map_name, 'lab_20261021')
        req.map_url = '/go2w/data'
        self.assertEqual(self.sim.call_service('/map_server/load_map', req).result, 1)

    def test_emitted_map_fingerprint_matches_offline_map_identity(self):
        import numpy as np
        from go2w_taught_route.map_bundle import load_map_asset, map_identity
        from go2w_taught_route.model import map_fingerprint
        emitted = []
        with patch.object(self.node, 'deliver', side_effect=lambda topic, msg: emitted.append((topic, msg))):
            self.sim.deliver_map()
        topic, msg = emitted[-1]
        self.assertEqual(topic, '/map')
        grid = np.asarray(msg.data, dtype=np.int8).reshape(msg.info.height, msg.info.width)
        origin = (msg.info.origin.position.x, msg.info.origin.position.y, 0.)
        observed = map_fingerprint(grid, msg.info.resolution, origin, msg.header.frame_id)
        offline = load_map_asset(self.map)
        self.assertEqual(observed, map_identity(offline['grid'], offline['res'], offline['origin']))
        self.assertEqual(msg.info.resolution, float(np.float32(self.sim.res)))
        for local in (False, True):
            self.assertEqual(self.sim.costmap_msg(local).info.resolution, float(np.float32(self.sim.res)))

    def test_development_runner_copies_maps_and_adds_shared_source(self):
        root = Path(self.tmp.name)
        venv = root / 'venv'; (venv / 'bin').mkdir(parents=True)
        capture = root / 'runner-env'
        stub = venv / 'bin/python'
        stub.write_text(f'#!{sys.executable}\nimport json,os,sys,time\nfrom pathlib import Path\nif len(sys.argv)>1 and sys.argv[1]=="dev/fake_camera.py":\n time.sleep(10)\nif len(sys.argv)>1 and sys.argv[1]=="server/go2w_web.py":\n Path(os.environ["FAKE_RUNNER_ENV"]).write_text(json.dumps(dict(os.environ)))\n')
        stub.chmod(0o755)
        nav = root / 'navigation-source'; (nav / 'go2w_taught_route').mkdir(parents=True)
        data = root / 'copy'
        env = dict(os.environ, GO2W_WEB_VENV=str(venv), GO2W_DATA=str(data), GO2W_MAPS_SRC=str(self.map.parent),
                   GO2W_NAV_SOURCE=str(nav), FAKE_RUNNER_ENV=str(capture), PORT='18997')
        subprocess.run(['bash', str(ROOT / 'dev/run.sh')], env=env, cwd=ROOT, check=True, capture_output=True, text=True)
        import json
        seen = json.loads(capture.read_text())
        self.assertIn(str(nav), seen['PYTHONPATH'].split(':'))
        self.assertEqual(seen['GO2W_FAKE_MAP'], str(data / 'maps/lab_20261021.yaml'))
        self.assertEqual((data / 'maps/lab_20261021.yaml').read_bytes(), self.map.read_bytes())
        self.assertFalse((data / 'maps/lab_20261021.yaml').is_symlink())


if __name__ == '__main__':
    unittest.main(verbosity=2)
