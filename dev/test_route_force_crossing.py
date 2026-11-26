"""路线强制跨越：运行真实任务协程，使用隔离的导航和参数替身。"""
import asyncio
from pathlib import Path
import sys
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'dev/fake_ros'),
               str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')]
from go2w_web import Server, GoalStatus
from obstacles_critic import ObstaclesCriticAPI


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
        self.assertEqual(Server.route_fp(plain), '74d505e275f1339a')
        marked = self.record(points((1, 3)))
        self.assertNotEqual(Server.route_fp(plain), Server.route_fp(marked))
        renamed = {**plain, 'name':'改名'}
        self.assertEqual(Server.route_fp(plain), Server.route_fp(renamed))

    def test_marker_indices_cannot_collide_with_an_added_waypoint(self):
        marked = self.record(points((0, 1, 3))[:4])
        plain = self.record(points()[:4] + [dict(x=0, y=1, th=3)])
        self.assertNotEqual(Server.route_fp(marked), Server.route_fp(plain))
        host = Server.__new__(Server)
        host.routes = {'test':plain}
        checkpoint = dict(fp=Server.route_fp(marked), index=2, loop=1, done=2, time=time.time())
        self.assertFalse(host.resume_valid('test', checkpoint))


class CrossingExecutionTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(.005)

    def pending_navigation(self, host):
        pending = []
        original_aw = host.aw
        # rclpy future 在 asyncio 等待被取消后仍会收到真实 action 结果。
        host.aw = lambda result, timeout: original_aw(asyncio.shield(result), timeout)
        def track(handle, pose, kind):
            host.node.goal = dict(id=bytes(handle.goal_id.uuid), poses_left=len(handle.poses),
                                  first_fb=time.monotonic(), status='active')
            result = asyncio.get_running_loop().create_future()
            def cancel():
                if not result.done(): result.set_result(NS(status=GoalStatus.STATUS_CANCELED))
            handle.cancel_goal_async.side_effect = cancel
            pending.append((handle, result))
            return result
        host.node.track_goal = track
        return pending

    async def finish_pending(self, host, job, pending):
        host.node.position = pending[-1][0].poses[-1]
        pending[-1][1].set_result(NS(status=GoalStatus.STATUS_SUCCEEDED))
        await asyncio.wait_for(job, 2)

    async def test_intermediate_crossing_ends_advance_without_action_success(self):
        for through in (False, True):
            host, route, trace, _ = self.rig(through=through)
            pending = self.pending_navigation(host)
            job = asyncio.create_task(host.run_route(None, route))
            try:
                count = 3 if through else 5
                for step in range(1, count):
                    await self.wait_for(lambda: len(pending) >= step)
                    handle, result = pending[step - 1]
                    self.assertFalse(result.done(), '中间点不能靠模拟 action 成功推进')
                    host.node.goal['poses_left'] = 1
                    end = handle.poses[-1]
                    host.node.position = (end[0] + .15, end[1], end[2])
                await self.wait_for(lambda: len(pending) == count)
                self.assertEqual(route['done'], 4)
                self.assertFalse(job.done(), '最终点仍须等待 Nav2 成功')
                self.assertFalse(pending[-1][1].done())
                await self.finish_pending(host, job, pending)
                self.assertEqual(route['status'], 'succeeded', route.get('message'))
            finally:
                job.cancel(); await asyncio.gather(job, return_exceptions=True)

    async def test_speed_and_size_segment_ends_use_passage(self):
        for split in ('speed', 'size'):
            host, route, _, _ = self.rig(through=True, marked=())
            if split == 'speed':
                for point, speed in zip(route['record']['points'], (.4, .2, .3, .5, .35)):
                    point['vmax'] = speed
            pending = self.pending_navigation(host)
            with patch('go2w_web.ROUTE_SEG_MAX', 2):
                job = asyncio.create_task(host.run_route(None, route))
                try:
                    seen = 0
                    while True:
                        await self.wait_for(lambda: len(pending) > seen)
                        handle, result = pending[seen]; seen += 1
                        self.assertFalse(result.done())
                        host.node.goal['poses_left'] = 1
                        end = handle.poses[-1]
                        host.node.position = (end[0] + .15, end[1], end[2])
                        if end[0] == 5.: break
                    await asyncio.sleep(.12)
                    self.assertFalse(job.done())
                    await self.finish_pending(host, job, pending)
                    self.assertEqual(route['status'], 'succeeded', route.get('message'))
                finally:
                    job.cancel(); await asyncio.gather(job, return_exceptions=True)

    async def test_dwell_cancels_intermediate_goal_before_countdown(self):
        for through in (False, True):
            host, route, _, _ = self.rig(through=through, marked=())
            route['record']['points'][1]['dwell'] = .1
            pending = self.pending_navigation(host)
            job = asyncio.create_task(host.run_route(None, route))
            try:
                first_count = 1 if through else 2
                for step in range(first_count):
                    await self.wait_for(lambda: len(pending) > step)
                    host.node.goal['poses_left'] = 1
                    host.node.position = pending[step][0].poses[-1]
                await self.wait_for(lambda: route.get('dwell_until') is not None)
                self.assertTrue(pending[-1][0].cancel_goal_async.await_count)
                before = len(pending)
                await asyncio.sleep(.04)
                self.assertEqual(len(pending), before, '停留结束前不能下发下一段')
                await self.wait_for(lambda: len(pending) > before)
            finally:
                host.stop_route('test cleanup')
                await asyncio.wait_for(job, 2)

    async def test_final_aborted_action_cannot_be_accepted_by_distance(self):
        for through in (False, True):
            host, route, _, _ = self.rig(through=through, marked=())
            original = host.node.track_goal
            def track(handle, pose, kind):
                result = original(handle, pose, kind)
                async def fail_final():
                    reply = await result
                    if pose[0] == 5.: reply.status = GoalStatus.STATUS_ABORTED
                    return reply
                return asyncio.create_task(fail_final())
            host.node.track_goal = track
            await host.run_route(None, route)
            self.assertEqual(route['status'], 'aborted', '最终点即使位置准确，也不能忽略 Nav2 失败')

    async def test_sharp_intermediate_turn_does_not_wait_for_success(self):
        host, route, _, _ = self.rig(marked=())
        route['record']['points'] = [dict(x=3., y=0., th=0.), dict(x=3., y=3., th=0.), dict(x=0., y=3., th=0.)]
        pending = self.pending_navigation(host)
        job = asyncio.create_task(host.run_route(None, route))
        try:
            await self.wait_for(lambda: len(pending) == 1)
            host.node.position = (2.8, 0., 0.)
            await self.wait_for(lambda: len(pending) == 2)
            self.assertFalse(pending[0][1].done())
        finally:
            host.stop_route('test cleanup'); await asyncio.wait_for(job, 2)

    async def test_loop_waits_only_at_actual_final_return(self):
        for through in (False, True):
            host, route, _, _ = self.rig(through=through, loops=1)
            pending = self.pending_navigation(host)
            job = asyncio.create_task(host.run_route(None, route))
            try:
                count = 3 if through else 6
                for step in range(1, count):
                    await self.wait_for(lambda: len(pending) >= step)
                    host.node.goal['poses_left'] = 1
                    host.node.position = pending[step-1][0].poses[-1]
                await self.wait_for(lambda: len(pending) == count)
                if through:
                    host.node.position = (5., 0., 0.)
                    await self.wait_for(lambda: route['index'] == 0 and route['back'])
                host.node.position = (1., 0., 0.)
                await asyncio.sleep(.12)
                self.assertFalse(job.done())
                await self.finish_pending(host, job, pending)
                self.assertEqual(route['status'], 'succeeded', route.get('message'))
                self.assertEqual(route['done'], 6)
            finally:
                job.cancel(); await asyncio.gather(job, return_exceptions=True)

    async def test_crossing_waits_point_two_seconds_after_all_three_changes(self):
        for through in (False, True):
            host, route, _, _ = self.rig(through=through)
            writes, sends, climbs = [], [], []
            api = ObstaclesCriticAPI()
            async def write(node, parameter, value):
                await asyncio.sleep(.01)
                writes.append((value, parameter, time.monotonic()))
                return {'node':node, 'parameter':parameter, 'enabled':value == 'true'}
            api._set_parameter = write
            host.obstacles_critic = api
            original_climb = host.ctrl.climb
            def climb(on):
                original_climb(on); climbs.append((on, time.monotonic()))
            host.ctrl.climb = climb
            original_send = host.node.send_through if through else host.node.send_goal
            def send(*args):
                sends.append((args[0][0][0] if through else args[0], time.monotonic()))
                return original_send(*args)
            if through: host.node.send_through = send
            else: host.node.send_goal = send
            await host.run_route(None, route)
            self.assertEqual(route['status'], 'succeeded', route.get('message'))
            for value, next_x in (('false', 3.), ('true', 5.)):
                selected = [row for row in writes if row[0] == value]
                if value == 'true': selected = selected[-2:]
                self.assertEqual({row[1] for row in selected},
                                 {'FollowPath.ObstaclesCritic.enabled', 'voxel_layer.enabled'})
                complete = max(row[2] for row in selected)
                climb_complete = next(t for on, t in climbs if on == (value == 'false'))
                sent = next(t for x, t in sends if x == next_x)
                self.assertGreaterEqual(sent - max(complete, climb_complete), .199)

    async def test_cancel_during_crossing_delay_sends_no_next_goal(self):
        host, route, trace, _ = self.rig(through=True)
        pending = self.pending_navigation(host)
        changed = asyncio.Event()
        original = host.obstacles_critic.apply.side_effect
        async def apply(enabled):
            await original(enabled)
            if not enabled: changed.set()
        host.obstacles_critic.apply.side_effect = apply
        job = asyncio.create_task(host.run_route(None, route))
        try:
            await self.wait_for(lambda: len(pending) == 1)
            host.node.goal['poses_left'] = 1
            host.node.position = (2.15, 0., 0.)
            await asyncio.wait_for(changed.wait(), 2)
            await asyncio.sleep(.04)
            host.stop_route('延时中取消')
            await asyncio.wait_for(job, 2)
            self.assertEqual(route['status'], 'canceled')
            self.assertFalse(any(e[0] == 'send' and max(e[1]) > 2 for e in trace))
            self.assertTrue(pending[0][0].cancel_goal_async.await_count)
        finally:
            job.cancel(); await asyncio.gather(job, return_exceptions=True)

    async def test_failed_handoff_cancels_previous_pending_goal(self):
        host, route, trace, _ = self.rig(through=True)
        pending = self.pending_navigation(host)
        async def fail(enabled):
            if not enabled: raise RuntimeError('参数未生效')
        host.obstacles_critic.apply.side_effect = fail
        job = asyncio.create_task(host.run_route(None, route))
        try:
            await self.wait_for(lambda: len(pending) == 1)
            host.node.goal['poses_left'] = 1
            host.node.position = (2.15, 0., 0.)
            await asyncio.wait_for(job, 1)
            self.assertEqual(route['status'], 'aborted')
            self.assertTrue(pending[0][0].cancel_goal_async.await_count)
            self.assertFalse(any(e[0] == 'send' and max(e[1]) > 2 for e in trace))
        finally:
            job.cancel(); await asyncio.gather(job, return_exceptions=True)

    async def test_fallback_to_single_cancels_pending_through_goal_first(self):
        host, route, _, _ = self.rig(through=True)
        pending = self.pending_navigation(host)
        original = host.node.send_through
        def send(poses, *args):
            response = original(poses, *args)
            if pending: response.result().accepted = False
            return response
        host.node.send_through = send
        single = host.node.send_goal
        canceled_before_single = []
        def send_single(*pose):
            canceled_before_single.append(pending[0][0].cancel_goal_async.await_count)
            return single(*pose)
        host.node.send_goal = send_single
        job = asyncio.create_task(host.run_route(None, route))
        try:
            await self.wait_for(lambda: len(pending) == 1)
            host.node.goal['poses_left'] = 1
            host.node.position = (2.15, 0., 0.)
            await self.wait_for(lambda: bool(canceled_before_single))
            self.assertGreater(canceled_before_single[0], 0, 'ToPose 不能替换另一个 action 服务上的 Through 目标')
        finally:
            host.stop_route('test cleanup'); await asyncio.wait_for(job, 2)

    async def test_cancel_during_parameter_write_immediately_cancels_old_goal(self):
        for during_exit in (False, True):
            host, route, _, _ = self.rig(through=True)
            pending = self.pending_navigation(host)
            entered, release = asyncio.Event(), asyncio.Event()
            crossed = False
            async def apply(enabled):
                nonlocal crossed
                if not enabled: crossed = True
                if crossed and enabled == during_exit:
                    entered.set(); await release.wait()
            host.obstacles_critic.apply.side_effect = apply
            job = asyncio.create_task(host.run_route(None, route))
            try:
                for step in range(2 if during_exit else 1):
                    await self.wait_for(lambda: len(pending) > step)
                    host.node.goal['poses_left'] = 1
                    host.node.position = pending[step][0].poses[-1]
                await asyncio.wait_for(entered.wait(), 2)
                host.stop_route('参数修改时取消')
                await asyncio.sleep(.05)
                self.assertGreater(pending[-1][0].cancel_goal_async.await_count, 0,
                                   '不能等 ROS 参数超时后才停车')
            finally:
                release.set()
                host.stop_route('test cleanup'); await asyncio.wait_for(job, 2)

    def rig(self, *, through=False, marked=(1, 3), loops=None, resume=None, pos=(-1., 0., 0.)):
        host = Server.__new__(Server)
        trace, logs = [], []
        node = NS(fixed='map', lock=threading.Lock(), goal=None, position=pos,
                  nav_through=NS(server_is_ready=lambda: True), sent_poses=[], sent_speeds=[],
                  params={'vmax':.45, 'wmax':1.2}, pset_cli=NS(service_is_ready=lambda: True))
        node.lookup = lambda _: node.position
        node.event = lambda level, message: logs.append((level, message))
        counter = 0
        def send(poses, *_):
            nonlocal counter
            counter += 1
            trace.append(('send', tuple(p[0] for p in poses)))
            node.sent_poses.extend(poses)
            node.sent_speeds.extend((p[0], node.params['vmax']) for p in poses)
            handle = NS(accepted=True, goal_id=NS(uuid=[counter]), poses=poses,
                        cancel_goal_async=AsyncMock(return_value=None))
            future = asyncio.get_running_loop().create_future(); future.set_result(handle)
            return future
        node.send_goal = lambda *pose: send([pose])
        node.send_through = send
        def set_param(key, value):
            trace.append(('speed', value))
            result = asyncio.get_running_loop().create_future()
            result.set_result(NS(results=[NS(successful=True)]))
            return result
        node.set_param = set_param
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
        host.climb = None
        host.estop = False
        host.ctrl_op = None
        host.heartbeat = 20.
        host.clients = {None:{'last_ping':time.monotonic()}}
        host.remote = lambda _:'test browser'
        dual = ['rough',False,False]
        def climb(on):
            trace.append(('climb',on))
            dual[:] = ['climb' if on else 'rough',False,on]
        host.ctrl = NS(climb=climb,get_dual=lambda _:tuple(dual))
        host.ctrl_get = lambda:('Velocity',True,1)
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
        host.route = route
        return host, route, trace, logs

    async def run_case(self, **kwargs):
        host, route, trace, logs = self.rig(**kwargs)
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'succeeded', route.get('message'))
        return host, route, trace, logs

    async def test_sequential_switches_only_after_boundary_arrivals(self):
        _, _, trace, _ = await self.run_case()
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False), ('param', True)])
        opened = trace.index(('param', False))
        closed = trace.index(('param', True), opened)
        self.assertLess(trace.index(('arrive', 2.)), opened)
        self.assertLess(opened, trace.index(('send', (3.,))))
        self.assertLess(trace.index(('arrive', 4.)), closed)
        self.assertLess(closed, trace.index(('send', (5.,))))

    async def test_through_segments_end_at_boundaries(self):
        _, _, trace, _ = await self.run_case(through=True)
        self.assertEqual([e for e in trace if e[0] == 'send'],
                         [('send', (1., 2.)), ('send', (3., 4.)), ('send', (5.,))])
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False), ('param', True)])

    async def test_unmarked_routes_initialize_normal_avoidance_only(self):
        for through in (True, False):
            _, _, trace, _ = await self.run_case(through=through, marked=())
            self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True)])

    async def test_fresh_route_resets_parameter_before_any_navigation(self):
        for through in (False, True):
            for marked in ((), (1, 3)):
                _, _, trace, _ = await self.run_case(through=through, marked=marked)
                self.assertEqual(trace[0], ('param', True))

    async def test_initialization_failure_dispatches_no_navigation_and_blocks_next_goal(self):
        host, route, trace, _ = self.rig(marked=())
        async def fail(enabled):
            trace.append(('param', enabled))
            raise RuntimeError('初始化被拒绝')
        host.obstacles_critic.apply.side_effect = fail
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'aborted')
        self.assertFalse(any(e[0] == 'send' for e in trace))
        self.assertTrue(host.crossing_dirty)

    async def test_cancel_during_initialization_dispatches_no_navigation(self):
        host, route, trace, _ = self.rig()
        async def cancel(enabled):
            trace.append(('param', enabled))
            host.finish_route(route, 'canceled', '初始化时取消')
        host.obstacles_critic.apply.side_effect = cancel
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'canceled')
        self.assertFalse(any(e[0] == 'send' for e in trace))

    async def test_starting_at_marked_first_point_enables_before_departing(self):
        _, _, trace, _ = await self.run_case(marked=(0, 3), pos=(1., 0., 0.))
        self.assertEqual([e for e in trace if e[0]=='param'][:2], [('param', True), ('param', False)])
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False), ('param', True)])

    async def test_resume_inside_span_reenables_before_sending_goal(self):
        _, _, trace, _ = await self.run_case(resume=(2, 1, False))
        self.assertEqual(next(e for e in trace if e[0]=='param'), ('param', False))
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', False), ('param', True)])

    async def test_resume_after_span_does_not_enable(self):
        _, _, trace, _ = await self.run_case(resume=(4, 1, False))
        self.assertFalse(any(e == ('param', False) for e in trace))

    async def test_two_loops_switch_twice_without_enabling_on_final_return(self):
        for through in (False, True):
            _, _, trace, _ = await self.run_case(through=through, marked=(0, 3), loops=2)
            self.assertEqual([e for e in trace if e[0] == 'param'],
                             [('param', True)] + [('param', False), ('param', True)] * 2)

    async def test_return_leg_keeps_obstacles_critic_enabled(self):
        for through in (False, True):
            _, _, trace, _ = await self.run_case(through=through, marked=(0, 3), loops=2)
            last_value, returns, crossing_started = None, 0, False
            for event in trace:
                if event[0] == 'param':
                    last_value = event[1]
                    crossing_started = crossing_started or not last_value
                elif event[0] == 'send' and 1. in event[1] and crossing_started:
                    self.assertTrue(last_value, '终点回起点必须使用正常避障参数 true')
                    returns += 1
            self.assertEqual(returns, 2)

    async def test_point_speed_limits_and_crossing_boundaries_work_together(self):
        for through in (False, True):
            with self.subTest(through=through):
                host, route, trace, _ = self.rig(through=through)
                for point, speed in zip(route['record']['points'], [.4, .2, .3, .5, .35]):
                    point['vmax'] = speed
                await host.run_route(None, route)
                self.assertEqual(route['status'], 'succeeded', route.get('message'))
                self.assertEqual(dict(host.node.sent_speeds), {1.:.4, 2.:.2, 3.:.2, 4.:.3, 5.:.35})
                self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False), ('param', True)])
                self.assertEqual(host.node.params['vmax'], .45, '任务结束后恢复任务速度上限')

    async def test_cancel_during_enable_restores_true_and_sends_no_next_goal(self):
        host, route, trace, _ = self.rig()
        original = host.obstacles_critic.apply.side_effect
        async def cancel(enabled):
            await original(enabled)
            if not enabled:
                host.finish_route(route, 'canceled', '手动取消')
        host.obstacles_critic.apply.side_effect = cancel
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'canceled')
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False), ('param', True)])
        self.assertFalse(any(e[0] == 'send' and max(e[1]) > 2 for e in trace))

    async def test_enable_failure_stops_and_attempts_true(self):
        host, route, trace, _ = self.rig()
        async def fail(enabled):
            trace.append(('param', enabled))
            if not enabled:
                raise RuntimeError('设置失败')
        host.obstacles_critic.apply.side_effect = fail
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'aborted')
        self.assertEqual([e for e in trace if e[0] == 'param'], [('param', True), ('param', False), ('param', True)])
        self.assertFalse(any(e[0] == 'send' and max(e[1]) > 2 for e in trace))

    async def test_disable_failure_cannot_finish_successfully(self):
        host, route, trace, logs = self.rig()
        async def fail(enabled):
            trace.append(('param', enabled))
            if enabled and ('param', False) in trace:
                raise RuntimeError('关闭失败')
        host.obstacles_critic.apply.side_effect = fail
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'aborted')
        self.assertIn('关闭失败', route['message'])
        self.assertFalse(any(e[0] == 'send' and max(e[1]) > 4 for e in trace))
        self.assertTrue(any('恢复' in message and '失败' in message for _, message in logs))

    async def test_failed_boundary_without_physical_passage_does_not_enable(self):
        host, route, trace, _ = self.rig()
        original = host.node.track_goal
        def track(handle, pose, kind):
            reached = original(handle, pose, kind)
            async def result():
                value = await reached
                if pose[0] == 2.:
                    value.status = GoalStatus.STATUS_ABORTED
                    host.node.position = (0., 0., 0.)
                return value
            return asyncio.create_task(result())
        host.node.track_goal = track
        await host.run_route(None, route)
        self.assertEqual(route['status'], 'aborted')
        self.assertFalse(any(e == ('param', False) for e in trace))

    async def test_next_route_recovers_previous_failed_reset_before_navigation(self):
        host, route, trace, _ = self.rig(marked=())
        host.crossing_dirty = True
        await host.run_route(None, route)
        self.assertEqual(trace[0], ('param', True))
        self.assertFalse(host.crossing_dirty)

    async def test_crossing_boundaries_keep_point_headings_in_both_modes(self):
        for through in (False, True):
            with self.subTest(through=through):
                host, route, _, _ = self.rig(through=through)
                route['record']['points'][1]['th'] = 1.3
                route['record']['points'][3]['th'] = -1.3
                await host.run_route(None, route)
                sent = {p[0]:p[2] for p in host.node.sent_poses}
                self.assertEqual((sent[2.], sent[4.]), (1.3, -1.3))

    async def test_crossing_also_enables_climb_before_dispatch_and_restores_it_at_end(self):
        for through in (False,True):
            host,route,trace,_=await self.run_case(through=through)
            opened=trace.index(('param',False))
            self.assertLess(trace.index(('arrive',2.)),trace.index(('climb',True)))
            self.assertLess(trace.index(('climb',True)),opened)
            self.assertLess(opened,trace.index(('send',(3.,4.) if through else (3.,))))
            self.assertFalse(host.climb)
            self.assertFalse(route.get('climb_touched'))
            self.assertIn(('climb',False),trace)

    async def test_climb_is_reenabled_for_resume_and_each_loop_but_not_normal_routes(self):
        for through in (False,True):
            for options,count in (({'resume':(2,1,False)},1),({'marked':(0,3),'loops':2},2),({'marked':()},0)):
                _,_,trace,_=await self.run_case(through=through,**options)
                self.assertEqual(trace.count(('climb',True)),count)

    async def test_unsupported_climb_aborts_before_crossing_or_next_goal(self):
        host,route,trace,_=self.rig()
        host.ctrl.get_dual=lambda _:None
        await host.run_route(None,route)
        self.assertEqual(route['status'],'aborted')
        self.assertNotIn(('param',False),trace)
        self.assertFalse(any(e[0]=='send' and max(e[1])>2 for e in trace))

    async def test_climb_request_failure_stops_and_attempts_both_restores(self):
        host,route,trace,_=self.rig()
        def fail(on):
            trace.append(('climb',on))
            if on:raise RuntimeError('send failed')
        host.ctrl.climb=fail
        await host.run_route(None,route)
        self.assertEqual(route['status'],'aborted')
        self.assertNotIn(('param',False),trace)
        self.assertIn(('climb',False),trace)
        self.assertGreaterEqual(trace.count(('param',True)),2)
        self.assertFalse(host.climb)

    async def test_climb_close_failure_still_restores_critic_and_stops_next_goal(self):
        host,route,trace,_=self.rig()
        original=host.ctrl.climb
        def fail(on):
            trace.append(('climb_attempt',on))
            if not on:raise RuntimeError('close failed')
            original(on)
        host.ctrl.climb=fail
        await host.run_route(None,route)
        self.assertEqual(route['status'],'aborted')
        self.assertIn(('climb_attempt',False),trace)
        opened=trace.index(('param',False))
        self.assertIn(('param',True),trace[opened+1:])
        self.assertFalse(any(e[0]=='send' and max(e[1])>4 for e in trace))

    async def test_mode_remains_selected_without_clients_or_time_limit_and_manual_switches_are_allowed(self):
        host,route,trace,_=self.rig()
        await host.route_crossing_set(route,True)
        self.assertTrue(host.set_climb(None,False)[0])
        self.assertTrue(host.set_climb(None,True)[0])
        host.climb['t']=time.monotonic()-10000
        host.clients.clear()
        host.climb_tick()
        self.assertIsNotNone(host.climb)
        self.assertEqual(trace[-1],('climb',True))
        await host.route_crossing_set(route,False)

    async def test_switching_mode_during_navigation_or_before_standing_does_not_add_a_mode_lock(self):
        host,route,_,_=self.rig()
        host.route_running=lambda:True
        host.node.nav_active=lambda:True
        host.ctrl_get=lambda:('Passive',True,1)
        self.assertTrue(host.set_climb(None,True)[0])
        self.assertEqual(route['status'],'active')
        self.assertTrue(host.set_climb(None,False)[0])
        self.assertEqual(route['status'],'active')

    async def test_an_unmarked_navigation_task_does_not_reset_manually_selected_climb(self):
        host,route,trace,_=self.rig(marked=())
        self.assertTrue(host.set_climb(None,True)[0])
        await host.run_route(None,route)
        self.assertEqual(route['status'],'succeeded')
        self.assertIsNotNone(host.climb)
        self.assertNotIn(('climb',False),trace)

    async def test_cancel_while_entering_climb_never_disables_critic_or_dispatches_next_goal(self):
        host,route,trace,_=self.rig()
        original=host.ctrl.climb
        def canceled(on):
            original(on)
            if on:host.finish_route(route,'canceled','test cancel')
        host.ctrl.climb=canceled
        await host.run_route(None,route)
        self.assertEqual(route['status'],'canceled')
        self.assertNotIn(('param',False),trace)
        self.assertIn(('climb',False),trace)
        self.assertFalse(host.climb)

    async def test_single_goal_cannot_bypass_failed_crossing_reset(self):
        host, route, trace, _ = self.rig()
        crossed = False
        async def fail(enabled):
            nonlocal crossed
            if not enabled:
                crossed = True
            elif crossed:
                raise RuntimeError('reset unavailable')
        host.obstacles_critic.apply.side_effect = fail
        await host.run_route(None, route)
        self.assertTrue(host.crossing_dirty)
        class WS:
            closed = False
        ws = WS()
        host.clients = {ws:{}}
        host.route, host.route_job = route, None
        host.map_busy = host.checking_route = host.estop = host.goal_pending = False
        host._taught_job = host.climb = None
        host.taught_recording = host.taught_open = lambda: False
        host.ctrl_blocker = lambda: None
        host.prof = lambda _: 'stop'
        host.node.nav = NS(server_is_ready=lambda: True)
        host.node.map_current = 'lab'
        host.node.is_armed = lambda: True
        host.node.goal = None
        host.check_ordinary_taught_binding = AsyncMock(return_value=(True, ''))
        host.keepout_blocker = lambda *_: None
        tracked = host.node.track_goal
        host.node.track_goal = lambda handle, pose, kind='goal': tracked(handle, pose, kind)
        reply = await host.handle_cmd(ws, dict(op='goal', x=99., y=0., th=0.))
        self.assertFalse(reply[0], reply)
        self.assertFalse(any(e == ('send', (99.,)) for e in trace))


if __name__ == '__main__':
    unittest.main()
