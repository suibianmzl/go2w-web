"""Real web backend and fake robot: health without navigation, and failed physical stand."""
import asyncio
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest

from aiohttp import ClientSession, WSMsgType

ROOT=Path(__file__).resolve().parents[1]


class LiveHealthIntegration(unittest.IsolatedAsyncioTestCase):
    async def run_case(self,stuck):
        with tempfile.TemporaryDirectory(prefix='go2w-health-') as tmp:
            base=Path(tmp);maps=base/'data/maps';maps.mkdir(parents=True)
            (maps/'lab.yaml').write_text('image: lab.pgm\nresolution: 0.05\norigin: [-5,-5,0]\n')
            (maps/'lab.pgm').write_bytes(b'P5\n200 200\n255\n'+bytes([254])*40000)
            with socket.socket() as sock:
                sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
            off=base/'lowstate-off'
            env={**os.environ,'GO2W_FAKE_PROCS':'1','GO2W_FAKE_NAV_OFF':'1','GO2W_FAKE_CTRL':'Passive',
                 'GO2W_FAKE_STUCK_POSTURE':'1' if stuck else '0','GO2W_FAKE_LOWSTATE_OFF':str(off),
                 'GO2W_DATA':str(base/'data'),'GO2W_FAKE_MAP':str(maps/'lab.yaml'),
                 'GO2W_CAM_ADDR':'127.0.0.1:9','GO2W_FAKE_CTRL_OFF':str(base/'ctrl-off'),
                 'PYTHONPATH':os.pathsep.join([str(ROOT/'dev/fake_ros'),str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')])}
            with (base/'server.log').open('w+') as log:
                process=subprocess.Popen([sys.executable,str(ROOT/'server/go2w_web.py'),'--port',str(port),
                                          '--static',str(ROOT/'static')],cwd=ROOT,env=env,stdout=log,stderr=log)
                try:
                    async with ClientSession() as session:
                        url=f'http://127.0.0.1:{port}'
                        for _ in range(100):
                            try:
                                async with session.get(url+'/version.json') as r:
                                    if r.status==200:break
                            except OSError:pass
                            if process.poll() is not None:
                                log.seek(0);self.fail(log.read())
                            await asyncio.sleep(.1)
                        else:self.fail('web startup timed out')
                        async with session.ws_connect(url+'/ws') as ws:
                            latest={}
                            async def receive():
                                async for message in ws:
                                    if message.type==WSMsgType.TEXT:
                                        data=json.loads(message.data);latest[data['t']]=data
                            async def ping():
                                while True:
                                    await ws.send_json({'t':'ping','ts':0});await asyncio.sleep(.3)
                            tasks=[asyncio.create_task(receive()),asyncio.create_task(ping())]
                            async def until(check,timeout=12):
                                async with asyncio.timeout(timeout):
                                    while not check():await asyncio.sleep(.05)
                            try:
                                await until(lambda:latest.get('system',{}).get('robotFeedback',{}).get('fresh')
                                            and latest.get('state',{}).get('ctrl',{}).get('state')=='Passive')
                                system=latest['system'];state=latest['state']
                                self.assertIsNotNone(system['sys']['mem'])
                                self.assertEqual(system['battery'],76)
                                self.assertEqual(system['robotFeedback']['posture'],'lying')
                                self.assertEqual(state['profiles']['navigation'],'stop')
                                self.assertEqual(state['profiles']['localization'],'stop')
                                self.assertEqual(state['profiles']['mapping'],'stop')
                                await ws.send_json({'t':'cmd','id':1,'op':'stand_up'})
                                await until(lambda:latest.get('ack',{}).get('id')==1)
                                self.assertEqual(latest['ack']['ok'],not stuck,latest['ack'])
                                await until(lambda:latest['state']['ctrl'].get('op') is None)
                                ctrl=latest['state']['ctrl']
                                self.assertEqual(ctrl['stood'],0 if stuck else 1)
                                self.assertEqual(ctrl['state'],'FixStand' if stuck else 'Velocity')
                                self.assertEqual(ctrl['ready'],not stuck)
                                self.assertEqual(latest['state']['profiles']['navigation'],'stop')
                                off.touch()
                                await until(lambda:not latest['system']['robotFeedback']['fresh'],timeout=5)
                                self.assertIsNone(latest['system']['battery'])
                                self.assertIsNotNone(latest['system']['sys']['cpu'])
                                await until(lambda:not latest['state']['ctrl']['ready'],timeout=3)
                                print('PASS 无导航直接反馈、实测站起'+('失败拦截' if stuck else '成功确认')+'、反馈丢失清空电量且CPU继续',flush=True)
                            finally:
                                for task in tasks:task.cancel()
                                await asyncio.gather(*tasks,return_exceptions=True)
                finally:
                    process.terminate()
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:process.kill();process.wait(timeout=5)

    async def test_normal_physical_stand_without_navigation(self):await self.run_case(False)
    async def test_fsm_ready_with_folded_joints_is_not_success(self):await self.run_case(True)


if __name__=='__main__':unittest.main()
