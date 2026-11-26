"""Real serialized LowState layout, without robot movement or a ROS graph."""
import asyncio
from collections import deque
from pathlib import Path
import struct
import sys
import tempfile
import threading
import time
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'server'),str(ROOT/'dev/fake_ros'),str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')]

STAND=[0.,.8,-1.5]*4
LIE=[0.,1.36,-2.65]*4


def lowstate(q=STAND, soc=76, tilt=(0.,0.)):
    data=bytearray(1180);data[1]=1;data[1043]=soc
    struct.pack_into('<ff',data,68,*tilt)
    for i in range(16):
        data[112+48*i]=32
        struct.pack_into('<f',data,84+48*i,q[i] if i<12 else 0.)
        struct.pack_into('<f',data,96+48*i,1.)
    return bytes(data)


class FeedbackTests(unittest.TestCase):
    def rig(self):
        from robot_feedback import RobotFeedback
        feedback=RobotFeedback();feedback.set_targets(STAND,LIE)
        return feedback

    def test_zero_battery_is_a_real_reading_and_joint_feedback_is_required(self):
        feedback=self.rig();feedback.update(lowstate(LIE,soc=0),now=10.)
        data=feedback.view(now=10.1)
        self.assertEqual(data['battery'],0)
        self.assertEqual(data['robotFeedback']['posture'],'lying')
        self.assertEqual(len(data['motorTemp']),16)

    def test_standing_confirmation_needs_new_frames_and_a_continuous_window(self):
        feedback=self.rig();feedback.update(lowstate(),now=10.)
        self.assertFalse(feedback.confirmed(after=10.,now=10.1))
        self.assertFalse(feedback.confirmed(after=9.,now=10.4))
        feedback.update(lowstate(),now=10.4)
        self.assertTrue(feedback.confirmed(after=10.,now=10.4))
        feedback.update(lowstate(LIE),now=10.5)
        self.assertFalse(feedback.confirmed(after=10.,now=10.5))

    def test_stale_missing_invalid_and_tilted_feedback_are_not_standing(self):
        feedback=self.rig()
        self.assertIsNone(feedback.view(now=10.)['battery'])
        feedback.update(lowstate(),now=10.)
        data=feedback.view(now=13.)
        self.assertIsNone(data['battery']);self.assertFalse(data['robotFeedback']['fresh'])
        for q,tilt in [([0.]*12,(0.,0.)),(STAND,(1.2,0.)),([float('nan')]*12,(0.,0.))]:
            feedback.update(lowstate(q,tilt=tilt),now=14.)
            self.assertFalse(feedback.confirmed(after=0,now=14.5))
        for bad in (b'',bytes(1180)):
            self.assertFalse(feedback.update(bad,now=15.))

    def test_actual_control_config_supplies_targets_and_bad_config_clears_them(self):
        feedback=self.rig()
        with tempfile.TemporaryDirectory() as tmp:
            config=Path(tmp)/'config.yaml'
            config.write_text('FSM:\n  FixStand:\n    qs: [[], '+str(STAND)+']\n  Passive:\n    soft_land:\n      q: '+str(LIE)+'\n')
            feedback.load_config(config)
            feedback.update(lowstate(),now=10.)
            self.assertEqual(feedback.view(now=10.1)['robotFeedback']['posture'],'standing')
            config.write_text('FSM: {}')
            feedback.load_config(config)
            self.assertEqual(feedback.view(now=10.1)['robotFeedback']['posture'],'unknown')
            config.write_text('FSM:\n  FixStand:\n    qs: [[], '+str(STAND)+']\n')
            feedback.load_config(config)
            self.assertTrue(feedback.view(now=10.1)['robotFeedback']['configured'])

    def test_config_is_read_from_live_pid_and_independent_release_not_embedded_default(self):
        feedback=self.rig()
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);process=root/'123';process.mkdir()
            config=root/'independent-release.yaml'
            config.write_text('FSM:\n  FixStand:\n    qs: [[], '+str(STAND)+']\n')
            (process/'cmdline').write_bytes(('/opt/go2w/control-runtime/releases/v1/bin/go2w_ctrl\0--config\0'+str(config)+'\0').encode())
            feedback.load_running_config({'control':{'running':True,'pid':123}},proc=root)
            self.assertEqual(feedback.stand,tuple(STAND))
            feedback.load_running_config({'control':{'running':False,'pid':123}},proc=root)
            self.assertIsNone(feedback.stand)


class StandTests(unittest.IsolatedAsyncioTestCase):
    def rig(self,q=LIE):
        import go2w_web as W
        from robot_feedback import RobotFeedback
        host=W.Server.__new__(W.Server)
        host.node=NS(event=lambda *_:None,robot_feedback=RobotFeedback())
        host.node.robot_feedback.set_targets(STAND,LIE)
        host.node.robot_feedback.update(lowstate(q))
        host.ctrl=NS();host.ctrl_op=None;host.estop=host.ctrl_abort=False
        host.ctrl_get=lambda:('Passive',True,1)
        host.motor_hot=lambda:None
        host.auto_lie=host.stand_hold=False;host.idle_since=None
        host.stood_seq=0;host.ctrl_msg='';host.autoloc_reset=lambda:None
        host.refresh_stand_targets=lambda:None
        host.sent=[]
        async def switch(api,*_,**__):
            host.sent.append(api)
            host.ctrl_get=lambda:('FixStand' if api==1004 else 'Velocity',True,2)
        host.ctrl_switch=switch
        return host

    async def test_fsm_ready_but_joints_still_folded_does_not_start_policy_or_report_success(self):
        host=self.rig()
        with patch('go2w_web.CTRL_WAIT',.1):
            ok,msg=await host.stand_up('test')
        self.assertFalse(ok,msg)
        self.assertEqual(host.sent,[1004])
        self.assertEqual(host.stood_seq,0)

    async def test_confirmed_actual_stand_allows_policy_and_success(self):
        host=self.rig()
        async def actual():
            for _ in range(8):
                host.node.robot_feedback.update(lowstate())
                await asyncio.sleep(.1)
        task=asyncio.create_task(actual())
        ok,msg=await host.stand_up('test')
        await task
        self.assertTrue(ok,msg);self.assertEqual(host.sent,[1004,1002]);self.assertEqual(host.stood_seq,1)

    async def test_no_robot_feedback_never_sends_stand_request(self):
        host=self.rig();host.node.robot_feedback.received=0.
        ok,msg=await host.stand_up('test')
        self.assertFalse(ok,msg);self.assertEqual(host.sent,[])

    async def test_control_drop_during_confirmation_cannot_report_success(self):
        host=self.rig(STAND)
        host.node.robot_feedback.standing_since=time.monotonic()-1
        host.ctrl_get=lambda:('Passive',True,3)
        self.assertIsNotNone(await host.wait_standing(0,expected='Velocity'))

    async def test_estop_during_configuration_read_never_sends_a_stand_request(self):
        host=self.rig();started=threading.Event();release=threading.Event()
        def refresh():started.set();release.wait(2)
        host.refresh_stand_targets=refresh
        task=asyncio.create_task(host.stand_up('test'))
        await asyncio.to_thread(started.wait,2)
        host.estop=True;release.set()
        ok,msg=await task
        self.assertFalse(ok,msg);self.assertEqual(host.sent,[])

    async def test_dynamic_walking_keeps_confirmed_sequence_but_folded_stale_or_new_sequence_does_not(self):
        host=self.rig(STAND);host.ctrl_get=lambda:('Velocity',True,3)
        self.assertIsNotNone(host.ctrl_blocker())
        await asyncio.sleep(.35)
        host.node.robot_feedback.update(lowstate(STAND))
        self.assertIsNone(host.ctrl_blocker())
        gait=STAND.copy();gait[2]-=.8
        host.node.robot_feedback.update(lowstate(gait))
        self.assertIsNone(host.ctrl_blocker())
        host.ctrl_get=lambda:('Velocity',True,4)
        self.assertIsNotNone(host.ctrl_blocker())
        host.ctrl_get=lambda:('Velocity',True,3)
        self.assertIsNotNone(host.ctrl_blocker())
        host.node.robot_feedback.update(lowstate(LIE))
        self.assertIsNotNone(host.ctrl_blocker())
        host.node.robot_feedback.update(lowstate(STAND));self.assertIsNotNone(host.ctrl_blocker())
        await asyncio.sleep(.35)
        host.node.robot_feedback.update(lowstate(STAND));self.assertIsNone(host.ctrl_blocker())
        host.node.robot_feedback.received=0
        self.assertIsNotNone(host.ctrl_blocker())

    async def test_single_frame_and_old_frames_after_sequence_or_config_change_cannot_grant_readiness(self):
        host=self.rig(STAND);host.ctrl_get=lambda:('Velocity',True,3)
        self.assertIsNotNone(host.ctrl_blocker())
        await asyncio.sleep(.35)
        self.assertIsNotNone(host.ctrl_blocker())  # Time passing alone is not a new measurement.
        host.node.robot_feedback.update(lowstate(STAND))
        self.assertIsNone(host.ctrl_blocker())
        host.ctrl_get=lambda:('Velocity',True,4)
        self.assertIsNotNone(host.ctrl_blocker())
        host.node.robot_feedback.set_targets(STAND,LIE)
        self.assertIsNotNone(host.ctrl_blocker())

    async def test_unobserved_feedback_gap_folded_pose_or_tilt_cannot_revive_a_previous_token(self):
        for fault in ('gap','lying','tilt'):
            with self.subTest(fault=fault):
                clock=[10.]
                with patch('robot_feedback.time.monotonic',side_effect=lambda:clock[0]):
                    host=self.rig(STAND);host.ctrl_get=lambda:('Velocity',True,3)
                    self.assertIsNotNone(host.ctrl_blocker())
                    clock[0]=10.4;host.node.robot_feedback.update(lowstate())
                    self.assertIsNone(host.ctrl_blocker())
                    if fault=='gap':clock[0]=12.4
                    else:
                        clock[0]=10.6
                        host.node.robot_feedback.update(lowstate(LIE) if fault=='lying' else lowstate(tilt=(1.2,0)))
                        clock[0]=10.8
                    gait=STAND.copy();gait[2]-=.8
                    host.node.robot_feedback.update(lowstate(gait))
                    self.assertIsNotNone(host.ctrl_blocker())

    async def test_cpu_and_battery_push_survives_broken_navigation_data(self):
        import go2w_web as W
        from robot_feedback import RobotFeedback
        feedback=RobotFeedback();feedback.update(lowstate())
        node=NS(map_ver=0,events=deque(),robot_feedback=feedback,
                pose=lambda:(_ for _ in ()).throw(RuntimeError('no navigation TF')),
                get_logger=lambda:NS(warn=lambda *_:None))
        host=W.Server.__new__(W.Server);host.node=node;host.clients={object():{}}
        host.sys=NS(read=lambda:{'cpu':12.5,'mem':1.,'memTotal':8.})
        messages=[]
        async def send(data,**_):messages.append(__import__('json').loads(data))
        host.send_all=send
        task=asyncio.create_task(host.broadcast())
        await asyncio.sleep(.65);task.cancel()
        await asyncio.gather(task,return_exceptions=True)
        system=[message for message in messages if message.get('t')=='system']
        self.assertTrue(system)
        self.assertEqual(system[0]['sys']['cpu'],12.5)
        self.assertEqual(system[0]['battery'],76)

    async def test_read_only_robot_listener_uses_direct_lowstate_without_control_publisher(self):
        from rclpy.node import Node
        from robot_ctrl import CtrlLink
        node=Node('test');received=[]
        link=CtrlLink(node,lambda *_:None,fake=True,on_lowstate=received.append,control=False)
        self.assertIsNone(link.pub)
        self.assertIn('/lowstate',node._subs)
        self.assertNotIn('/go2w/robot_lowstate',node._subs)
        self.assertNotIn('/go2w_ctrl/state',node._subs)
        node.deliver('/lowstate',lowstate())
        self.assertEqual(len(received),1)
        with self.assertRaises(RuntimeError):link.send(1004)


if __name__=='__main__':unittest.main()
