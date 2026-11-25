"""路线强制跨越：运行真实任务协程，使用隔离的导航和参数替身。"""
import asyncio
from pathlib import Path
import sys
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'dev/fake_ros'),
               str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')]
from go2w_web import Server, GoalStatus


def points(marked=()):
    return [dict(x=float(i + 1), y=0., th=0., **({'forceCrossing': True} if i in marked else {}))
            for i in range(5)]


class CrossingDataTests(unittest.TestCase):
    def record(self, pts):
        return Server.route_record(dict(name='测试', map='lab', points=pts), 'test')

    def test_old_routes_keep_original_shape(self):
        self.assertTrue(all('forceCrossing' not in p for p in self.record(points())['points']))

    def test_marked_points_survive_save(self):
        saved = self.record(points((1, 2, 3)))
        self.assertEqual([i for i, p in enumerate(saved['points']) if p.get('forceCrossing')], [1, 2, 3])

    def test_single_selection_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '至少.*两个'):
            self.record(points((1,)))

    def test_non_boolean_marker_is_rejected(self):
        for value in ['true', 1, None]:
            pts = points((1, 3)); pts[1]['forceCrossing'] = value
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, '强制跨越'):
                self.record(pts)

    def test_editing_crossing_invalidates_resume_but_old_fingerprint_is_compatible(self):
        plain = self.record(points())
        marked = self.record(points((1, 3)))
        self.assertNotEqual(Server.route_fp(plain), Server.route_fp(marked))
        renamed = {**plain, 'name':'改名'}
        self.assertEqual(Server.route_fp(plain), Server.route_fp(renamed))


class CrossingExecutionTests(unittest.IsolatedAsyncioTestCase):
    def rig(self, *, through=False, marked=(1, 3), loops=None, resume=None, pos=(-1., 0., 0.)):
        host = Server.__new__(Server)
        trace, logs = [], []
        node = NS(fixed='map', lock=threading.Lock(), goal=None, position=pos,
                  nav_through=NS(server_is_ready=lambda: True))
        node.lookup = lambda _: node.position
        node.event = lambda level, message: logs.append((level, message))
        counter = 0
        def send(poses, *_):
            nonlocal counter
            counter += 1
            trace.append(('send', tuple(p[0] for p in poses)))
            handle = NS(accepted=True, goal_id=NS(uuid=[counter]), poses=poses,
                        cancel_goal_async=AsyncMock(return_value=None))
            future = asyncio.get_running_loop().create_future(); future.set_result(handle)
            return future
        node.send_goal = lambda *pose: send([pose])
        node.send_through = send
        def track(handle, pose, kind):
            node.goal = dict(id=bytes(handle.goal_id.uuid), poses_left=1, first_fb=None)
            async def arrive():
                await asyncio.sleep(0)
                node.position = pose
                trace.append(('arrive', pose[0]))
                return NS(status=GoalStatus.STATUS_SUCCEEDED)
            return asyncio.create_task(arrive())
        node.track_goal = track
        host.node = node
        async def apply(enabled):
            trace.append(('param', enabled))
            return 'Successfully set parameter'
        host.obstacles_critic = NS(apply=AsyncMock(side_effect=apply))
        host.clear_before_start = AsyncMock()
        host.route_blocker = lambda *_: None
        host.resume_checkpoint = lambda *_: None
        host.incident = lambda *_: None
        host.route_audio = lambda *_: None
        host.audio = NS(stop_tag=AsyncMock())
        host.background = asyncio.create_task
        async def aw(future, timeout):
            return await asyncio.wait_for(future, timeout)
        host.aw = aw
        record = Server.route_record(dict(name='测试', map='lab', points=points(marked), opts=dict(through=through)), 'test')
        route = dict(record=record, loops=loops, resume=resume, index=0, loop=1, back=False,
                     stop=asyncio.Event(), status='active', started=0., done=0, checking=True,
                     goal=None, through=False)
        return host, route, trace, logs

    async def run_case(self, **kwargs):
        host, route, trace, logs = self.rig(**kwargs)
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'succeeded', route.get('message'))
        return host, route, trace, logs

    async def test_sequential_switches_only_after_boundary_arrivals(self):
        _, _, trace, _ = await self.run_case()
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False)])
        self.assertLess(trace.index(('arrive', 2.)), trace.index(('param', True)))
        self.assertLess(trace.index(('param', True)), trace.index(('send', (3.,))))
        self.assertLess(trace.index(('arrive', 4.)), trace.index(('param', False)))
        self.assertLess(trace.index(('param', False)), trace.index(('send', (5.,))))

    async def test_through_segments_end_at_boundaries(self):
        _, _, trace, _ = await self.run_case(through=True)
        self.assertEqual([e for e in trace if e[0] == 'send'],
                         [('send', (1., 2.)), ('send', (3., 4.)), ('send', (5.,))])
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False)])

    async def test_unmarked_routes_never_set_parameter(self):
        for through in (True, False):
            _, _, trace, _ = await self.run_case(through=through, marked=())
            self.assertFalse(any(e[0] == 'param' for e in trace))

    async def test_starting_at_marked_first_point_enables_before_departing(self):
        _, _, trace, _ = await self.run_case(marked=(0, 3), pos=(1., 0., 0.))
        self.assertEqual(trace[0], ('param', True))
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False)])

    async def test_resume_inside_span_reenables_before_sending_goal(self):
        _, _, trace, _ = await self.run_case(resume=(2, 1, False))
        self.assertEqual(trace[0], ('param', True))
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False)])

    async def test_resume_after_span_does_not_enable(self):
        _, _, trace, _ = await self.run_case(resume=(4, 1, False))
        self.assertFalse(any(e == ('param', True) for e in trace))

    async def test_two_loops_switch_twice_without_enabling_on_final_return(self):
        for through in (False, True):
            _, _, trace, _ = await self.run_case(through=through, marked=(0, 3), loops=2)
            self.assertEqual([e for e in trace if e[0] == 'param'],
                             [('param', True), ('param', False)] * 2)

    async def test_cancel_during_enable_restores_false_and_sends_no_next_goal(self):
        host, route, trace, _ = self.rig()
        original = host.obstacles_critic.apply.side_effect
        async def cancel(enabled):
            await original(enabled)
            if enabled:
                host.finish_route(route, 'canceled', '手动取消')
        host.obstacles_critic.apply.side_effect = cancel
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'canceled')
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False)])
        self.assertFalse(any(e[0] == 'send' and max(e[1]) > 2 for e in trace))

    async def test_enable_failure_stops_and_attempts_false(self):
        host, route, trace, _ = self.rig()
        async def fail(enabled):
            trace.append(('param', enabled))
            if enabled:
                raise RuntimeError('设置失败')
        host.obstacles_critic.apply.side_effect = fail
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'aborted')
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False)])
        self.assertFalse(any(e[0] == 'send' and max(e[1]) > 2 for e in trace))

    async def test_disable_failure_cannot_finish_successfully(self):
        host, route, trace, logs = self.rig()
        async def fail(enabled):
            trace.append(('param', enabled))
            if not enabled:
                raise RuntimeError('关闭失败')
        host.obstacles_critic.apply.side_effect = fail
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'aborted')
        self.assertIn('关闭失败', route['message'])
        self.assertFalse(any(e[0] == 'send' and max(e[1]) > 4 for e in trace))
        self.assertTrue(any('恢复' in message and '失败' in message for _, message in logs))

    async def test_failed_boundary_action_does_not_enable_even_if_close(self):
        host, route, trace, _ = self.rig()
        original = host.node.track_goal
        def track(handle, pose, kind):
            reached = original(handle, pose, kind)
            async def result():
                value = await reached
                if pose[0] == 2.:
                    value.status = GoalStatus.STATUS_ABORTED
                return value
            return asyncio.create_task(result())
        host.node.track_goal = track
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'aborted')
        self.assertFalse(any(e == ('param', True) for e in trace))

    async def test_next_route_recovers_previous_failed_reset_before_navigation(self):
        host, route, trace, _ = self.rig(marked=())
        host.crossing_dirty = True
        await host.run_route(None, route)
        self.assertEqual(trace[0], ('param', False))
        self.assertFalse(host.crossing_dirty)


if __name__ == '__main__':
    unittest.main()
