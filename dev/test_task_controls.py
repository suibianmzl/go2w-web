"""导航点限速和续跑进度终止；只使用本地假 ROS，不连接机器人。"""
import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'server'), str(ROOT/'dev/fake_ros')]
import go2w_web as W


def record(points=None, **opts):
    return W.Server.route_record({'name': '限速路线', 'map': 'lab',
        'points': points or [dict(x=i, y=0, th=0) for i in range(4)],
        'opts': {'vmax': .5, **opts}}, 'route-one')


class PointLimits(unittest.TestCase):
    def test_point_limits_round_trip_and_inheritance(self):
        pts = [dict(x=0, y=0, th=0, vmax=.2), dict(x=1, y=0, th=0)]
        saved = record(pts)
        self.assertEqual(saved['points'][0]['vmax'], .2)
        self.assertNotIn('vmax', saved['points'][1])
        self.assertEqual(W.Server.route_record(saved, saved['id']), saved)
        for value in (True, '0.2', None, 0, -.1, 1.21, float('nan'), float('inf')):
            with self.subTest(value=value), self.assertRaises(ValueError):
                record([dict(pts[0], vmax=value), pts[1]])

    def test_angular_task_limit_accepts_two_and_rejects_larger(self):
        self.assertEqual(record(wmax=2)['opts']['wmax'], 2)
        for value in (2.01, float('nan'), True):
            with self.assertRaises(ValueError):
                record(wmax=value)

    def test_each_leg_uses_both_endpoints_and_task_ceiling(self):
        route = {'record': record([dict(x=i, y=0, th=0, **extra) for i,extra in enumerate(
            ({'vmax':.7},{'vmax':.2},{'vmax':.4},{}))]), 'done':0}
        limit = W.Server.route_leg_vmax
        self.assertEqual(limit(route, (0,1,False)), .5, '初次接近第一站时使用目标点和任务限速')
        self.assertEqual(limit(route, (1,1,False)), .2)
        self.assertEqual(limit(route, (2,1,False)), .2)
        self.assertEqual(limit(route, (3,1,False)), .4)
        route['done'] = 4
        self.assertEqual(limit(route, (0,1,True)), .5, '末站回首站也使用两个端点')
        route['vmax'] = .3
        self.assertEqual(limit(route, (3,1,False)), .3, '本次启动更低上限仍约束所有点')
        route['record']['points'][-1]['vmax'] = .1
        self.assertEqual(limit(route, (0,1,True)), .1)


class CheckpointTermination(unittest.TestCase):
    def test_termination_persists_and_keeps_route_and_other_progress(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(W, 'DATA', tmp):
            server = W.Server.__new__(W.Server)
            server.routes = {'route-one':record()}
            server.route_job = None; server.route = None
            server.resumes = {'route-one': {'status':'canceled'}, 'other': {'status':'canceled'}}
            server.resume_file = str(Path(tmp)/'route_resume.json')
            server.node = NS(event=Mock())
            before = copy.deepcopy(server.routes)
            ok, msg = server.terminate_route('route-one')
            self.assertTrue(ok, msg)
            self.assertEqual(server.routes, before)
            self.assertNotIn('route-one', json.loads(Path(server.resume_file).read_text()))
            self.assertIn('other', server.resumes)
            self.assertFalse(server.terminate_route('missing')[0])

    def test_running_route_cannot_have_its_checkpoint_removed(self):
        server = W.Server.__new__(W.Server)
        server.routes = {'route-one':record()}
        server.resumes = {'route-one': {'status':'active'}}
        server.route_job = NS(done=lambda:False)
        server.route = {'record': server.routes['route-one'], 'status':'active'}
        self.assertFalse(server.terminate_route('route-one')[0])
        self.assertIn('route-one', server.resumes)

    def test_failed_write_keeps_checkpoint_and_reports_failure(self):
        server = W.Server.__new__(W.Server)
        server.routes = {'route-one':record()}; server.route_job = None; server.route = None
        server.resumes = {'route-one': {'status':'canceled'}}
        server.write_resumes = Mock(return_value=False)
        self.assertFalse(server.terminate_route('route-one')[0])
        self.assertIn('route-one', server.resumes)


class SpeedExecution(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_start_cannot_overwrite_another_tasks_limit(self):
        server,_,_=self.server()
        route=record();server.routes={route['id']:route};server.route_job=None
        server.goal_pending=False;server.teleop=None;server.prof=lambda _:'stop'
        server.node.nav_active=lambda:False
        server.keepout_blocker=lambda *_:None
        server.start_route=Mock(return_value=(True,'started'))
        entered=asyncio.Event();release=asyncio.Event()
        async def binding(_):
            if not entered.is_set():
                entered.set();await release.wait()
            return True,''
        server.check_ordinary_taught_binding=binding
        first=asyncio.create_task(server.start_route_armed(None,{'route_id':route['id'],'vmax':.2}))
        await entered.wait()
        second=await server.start_route_armed(None,{'route_id':route['id'],'vmax':.5})
        release.set()
        self.assertTrue((await first)[0])
        self.assertFalse(second[0],'首请求等待服务时，后来的启动请求必须拒绝')
        self.assertEqual(server.start_route.call_count,1)

    async def test_manual_parameter_cannot_bypass_an_active_route_limit(self):
        server,_,speeds=self.server()
        class WS:
            closed=False
        ws=WS()
        server.clients={ws:{}};server.map_busy=False;server._taught_job=None
        server.taught_recording=lambda:False;server.taught_open=lambda:False
        server.checking_route=False;server.climb=None
        server.route_job=NS(done=lambda:False)
        server.node.params['vmax']=.2
        reply=await server.handle_cmd(ws,{'op':'set_param','key':'vmax','value':1.2})
        self.assertFalse(reply[0],reply)
        self.assertEqual(speeds,[])
        self.assertEqual(server.node.params['vmax'],.2)

    async def test_start_uses_saved_task_limits_and_accepts_two_radians(self):
        server, _, _ = self.server()
        route = record(vmax=.4, wmax=2)
        server.routes = {route['id']:route}; server.route_job=None
        server.goal_pending=False; server.teleop=None
        server.prof=lambda _: 'stop'
        server.node.nav_active=lambda:False
        server.check_ordinary_taught_binding=AsyncMock(return_value=(True,''))
        server.keepout_blocker=lambda *_:None
        server.start_route=Mock(return_value=(True,'started'))
        reply=await server.start_route_armed(None, {'route_id':route['id']})
        self.assertTrue(reply[0],reply)
        self.assertEqual(server.node.params,{'vmax':.4,'wmax':2})
        self.assertEqual(server.start_route.call_args.kwargs['vmax'],.4)
        server.start_route.reset_mock()
        self.assertFalse((await server.start_route_armed(None, {'route_id':route['id'],'wmax':2.01}))[0])
        server.start_route.assert_not_called()

    async def test_resumed_and_loop_legs_keep_endpoint_constraints(self):
        for state, done in [((2,1,False),2),((0,1,True),4)]:
            server,calls,_=self.server()
            pts=[dict(x=i,y=0,th=0,**extra) for i,extra in enumerate(
                ({'vmax':.3},{'vmax':.2},{'vmax':.4},{'vmax':.1}))]
            route={'record':record(pts,through=False),'done':done,'loops':1 if state[2] else None,
                   'resume':state,'status':'active','stop':asyncio.Event(),'index':state[0]}
            await server.run_route(None,route)
            self.assertEqual(route['status'],'succeeded',route.get('message'))
            self.assertEqual(calls[0][1],.1 if state[2] else .2)

    def server(self, through=False, fail_speed=False):
        server = W.Server.__new__(W.Server)
        current = [-1., 0., 0.]; calls = []; speeds = []
        def future(value):
            f = asyncio.get_running_loop().create_future(); f.set_result(value); return f
        def set_param(key, value):
            speeds.append(value)
            return future(NS(results=[NS(successful=not fail_speed, reason='拒绝更新')]))
        node = NS(params={'vmax':.5, 'wmax':1.2}, pset_cli=NS(service_is_ready=lambda:True),
                  set_param=set_param, lookup=lambda _:tuple(current), fixed='map', event=Mock(),
                  nav_through=NS(server_is_ready=lambda:through), lock=threading.Lock(), goal=None)
        def send(poses):
            calls.append((copy.deepcopy(poses),node.params['vmax']))
            current[:] = poses[-1]
            return future(NS(accepted=True,goal_id=NS(uuid=[len(calls)]),cancel_goal_async=lambda:future(None)))
        node.send_goal = lambda *pose:send([pose])
        node.send_through = lambda poses, _:send(poses)
        def track(handle, pose, kind):
            node.goal = {'id':bytes(handle.goal_id.uuid),'poses_left':0,'first_fb':1}
            return future(NS(status=W.GoalStatus.STATUS_SUCCEEDED))
        node.track_goal = track
        server.node=node; server.clear_before_start=AsyncMock()
        server.obstacles_critic=NS(apply=AsyncMock(return_value='OK'))
        async def aw(f, timeout): return await f
        server.aw=aw; server.route_blocker=lambda *args:None
        server.resume_checkpoint=Mock(); server.route_audio=Mock()
        def finish(route,status,msg):
            route.update(status=status,message=msg); route['stop'].set()
        server.finish_route=finish
        return server,calls,speeds

    async def run_limits(self, through):
        server,calls,speeds=self.server(through)
        pts=[dict(x=i,y=0,th=0,**extra) for i,extra in enumerate(
            ({'vmax':.7},{'vmax':.2},{'vmax':.4},{}))]
        route={'record':record(pts,through=through),'done':0,'loops':None,'resume':None,
               'status':'active','stop':asyncio.Event(),'index':0}
        await server.run_route(None,route)
        self.assertEqual(route['status'],'succeeded',route.get('message'))
        observed={int(pose[0]):v for poses,v in calls for pose in poses}
        self.assertEqual(observed,{0:.5,1:.2,2:.2,3:.4})
        self.assertEqual(server.node.params['vmax'],.5,'结束后恢复任务上限')
        if through:
            self.assertEqual([len(poses) for poses,_ in calls],[1,2,1], '只在限速变化处分段')

    async def test_single_goal_execution_uses_each_leg_limit(self):
        await self.run_limits(False)

    async def test_through_execution_splits_only_at_limit_changes(self):
        await self.run_limits(True)

    async def test_legacy_limit_boundary_keeps_saved_heading(self):
        server,calls,_ = self.server()
        pts=[dict(x=i,y=0,th=1.1,**extra) for i,extra in enumerate(
            ({'vmax':.7},{'vmax':.2},{'vmax':.4},{}))]
        route={'record':record(pts,through=False),'done':0,'loops':None,'resume':None,
               'status':'active','stop':asyncio.Event(),'index':0}
        await server.run_route(None,route)
        self.assertEqual(calls[2][0][0][2],1.1,'限速分段沿用原站点朝向；是否经过由位置单独判断')

    async def test_failed_limit_update_does_not_dispatch_navigation(self):
        server,calls,_ = self.server(fail_speed=True)
        route={'record':record([dict(x=0,y=0,th=0,vmax=.2),dict(x=1,y=0,th=0)]),
               'done':0,'loops':None,'resume':None,'status':'active','stop':asyncio.Event(),'index':0}
        await server.run_route(None,route)
        self.assertEqual(route['status'],'aborted')
        self.assertEqual(calls,[])
        self.assertIn('限速',route['message'])


if __name__ == '__main__':
    unittest.main()
