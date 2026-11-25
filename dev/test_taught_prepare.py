"""准备流程的顺序和中断回归；外部运控以替身代替，不向真机发送请求。"""

import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS
from types import MethodType
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from teaching_bridge import TaughtWeb


class PrepareTests(unittest.IsolatedAsyncioTestCase):
    def rig(self, state='Passive', *, stand_ok=True):
        class WS:
            closed = False

        ws = WS()
        host = TaughtWeb()
        events = []
        host.clients = {ws: {'last_ping': time.monotonic()}}
        host.heartbeat = 20.0
        host.owner = host.taught_owner = host._taught_prepare_owner = ws
        host.estop = host.busy = host.map_loading = host.autoloc_busy = False
        host.teleop = None
        host.ctrl_op = None
        host.ctrl_abort = False
        host.ctrl = NS(state=state, steady_t=time.monotonic() - 3)
        host.ctrl_get = lambda: (host.ctrl.state, True, 1)
        host.ctrl_blocker = lambda: None if host.ctrl.state == 'Velocity' else '未站起'
        host.moving = lambda: False
        host.route_running = lambda: False
        host.prof = lambda mode: 'run' if mode == 'navigation' else 'stop'
        host.taught_repo = NS(document=lambda name: {'map_id': 'map1'})
        host.taught_checks = {'lab': {'state': 'preparing', 'ok': False}}
        host._taught_job = object()
        host._taught_epoch = 1.0
        host.pose_set_t = 0.0
        host.stood_seq = host._taught_stood_seq = 0
        host.node = NS(
            get_node_names=lambda: ['amcl', 'bt_navigator', 'go2w_cmd_bridge', 'taught_route'],
            nav_active=lambda: False,
            is_armed=lambda: False,
            _taught_map_id='map1',
            odom=(0.0, 0.0, 0.0),
            _taught_scan={'received': time.monotonic() + 1.0},
            amcl=(0.03, 1.0, time.monotonic()),
            event=lambda level, message: events.append(('event', message)),
        )

        async def stand(who, valid=None):
            events.append(('stand', host.ctrl.state))
            if stand_ok:
                if host.ctrl.state == 'Passive':
                    host.stood_seq += 1
                host.ctrl.state = 'Velocity'
            return stand_ok, '已站起' if stand_ok else '站起失败'

        async def load(name):
            events.append(('map', name))
            return True, 'map loaded'

        async def sync(name, required=False):
            events.append(('sync', name))
            return True

        async def localize(who, on_start=False):
            events.append(('localize', host.ctrl.state))
            host.pose_set_t = time.monotonic()
            return True, 'ok'

        async def checked(name):
            events.append(('check', name))
            host.taught_checks[name].update(state='ready', ok=True)
            return host.taught_checks[name], None

        async def push():
            pass

        host.stand_up = stand
        host.load_map = load
        host.sync_taught_map = sync
        host.auto_localize = localize
        host.taught_self_check = checked
        host.taught_push = push
        return host, ws, events

    async def test_lying_stands_before_loading_and_localization(self):
        host, ws, events = self.rig()
        await host.taught_prepare_job('lab', ws)
        actions = [e[0] for e in events]
        self.assertLess(actions.index('stand'), actions.index('map'))
        self.assertLess(actions.index('map'), actions.index('localize'))
        self.assertIn(('localize', 'Velocity'), events)
        self.assertTrue(host.taught_checks['lab']['ok'])

    async def test_standing_can_prepare_without_manual_pose_precondition(self):
        host, ws, events = self.rig('Velocity')
        await host.taught_prepare_job('lab', ws)
        self.assertIn(('localize', 'Velocity'), events)
        self.assertTrue(host.taught_checks['lab']['ok'])

    async def test_stand_failure_never_starts_map_or_localization(self):
        host, ws, events = self.rig(stand_ok=False)
        await host.taught_prepare_job('lab', ws)
        self.assertFalse(any(e[0] in ('map', 'localize') for e in events))
        self.assertFalse(host.taught_checks['lab']['ok'])
        self.assertIn('站起失败', host.taught_checks['lab']['message'])

    async def test_disconnect_during_stand_stops_later_stages(self):
        host, ws, events = self.rig()

        async def disconnected(who, valid=None):
            events.append(('stand', 'Passive'))
            ws.closed = True
            host.clients.pop(ws)
            host.ctrl.state = 'Velocity'
            return True, 'stood'

        host.stand_up = disconnected
        await host.taught_prepare_job('lab', ws)
        self.assertFalse(any(e[0] in ('map', 'localize') for e in events))
        self.assertFalse(host.taught_checks['lab']['ok'])

    async def test_owner_loss_aborts_existing_stand_switch(self):
        host, ws, _ = self.rig()
        host.ctrl_op = 'up'
        host._taught_capture = None
        await host.taught_owner_lost(ws, 'closed')
        self.assertTrue(host.ctrl_abort)
        self.assertIsNone(host._taught_prepare_owner)
        self.assertIsNone(host.taught_owner)

    async def test_motion_during_settling_requires_a_new_two_second_quiet_window(self):
        host, ws, events = self.rig('Velocity')
        calls = 0
        last_motion = []
        loaded = []

        def moving():
            nonlocal calls
            calls += 1
            if calls == 2:
                last_motion.append(time.monotonic())
                return True
            return False

        host.moving = moving
        original = host.load_map

        async def load(name):
            loaded.append(time.monotonic())
            return await original(name)

        host.load_map = load
        await host.taught_prepare_job('lab', ws)
        self.assertTrue(host.taught_checks['lab']['ok'])
        self.assertGreaterEqual(loaded[0] - last_motion[0], 2.0)

    async def test_owner_change_during_real_stand_switch_cannot_start_policy(self):
        # 直接绑定产品Server的真实站起/切阶段方法，只替代外部运控收发。
        fake = Path(__file__).resolve().parent / 'fake_ros'
        sys.path.insert(0, str(fake))
        from go2w_web import Server
        import robot_ctrl as R

        host, ws, events = self.rig()
        sent = []
        host.motor_hot = lambda: None
        host.ctrl_done = lambda ok, message: (ok, message)
        host.autoloc_reset = lambda: None
        other = object()

        def send(api):
            sent.append(api)
            if api == R.API_STANDUP:
                host.ctrl.state = 'FixStand'
                host.owner = other
            elif api == R.API_BALANCESTAND:
                host.ctrl.state = 'Velocity'

        host.ctrl.send = send
        host.ctrl_switch = MethodType(Server.ctrl_switch, host)
        host.stand_up = MethodType(Server.stand_up, host)
        await host.taught_prepare_job('lab', ws)
        self.assertEqual(sent, [R.API_STANDUP])
        self.assertFalse(host.taught_checks['lab']['ok'])
        self.assertFalse(any(e[0] == 'localize' for e in events))

    async def test_same_page_cancel_during_real_stand_prevents_policy_stage(self):
        sys.path.insert(0, str(Path(__file__).resolve().parent / 'fake_ros'))
        from go2w_web import Server
        import robot_ctrl as R

        host, ws, events = self.rig()
        host.map_busy = False
        host.checking_route = False
        host._taught_capture = None
        host.motion_stops = 0
        host.node.cancel_cli = NS(service_is_ready=lambda: False)
        host.motor_hot = lambda: None
        host.ctrl_done = lambda ok, message: (ok, message)
        host.autoloc_reset = lambda: None
        host.stop_route = lambda reason: None
        sent = []
        cancelled = []

        def send(api):
            sent.append(api)
            if api == R.API_STANDUP:
                host.ctrl.state = 'FixStand'
                cancelled.append(asyncio.create_task(Server.handle_cmd(host, ws, {'op': 'cancel'})))
            elif api == R.API_BALANCESTAND:
                host.ctrl.state = 'Velocity'

        host.ctrl.send = send
        host.ctrl_switch = MethodType(Server.ctrl_switch, host)
        host.stand_up = MethodType(Server.stand_up, host)
        await host.taught_prepare_job('lab', ws)
        result = await cancelled[0]
        self.assertEqual(sent, [R.API_STANDUP])
        self.assertTrue(result[0], result)
        self.assertFalse(any(e[0] == 'localize' for e in events))


if __name__ == '__main__':
    unittest.main()
