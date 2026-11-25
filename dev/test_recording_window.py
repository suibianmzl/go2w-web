"""固定的前20秒+后10秒窗口；不启动机器人。"""
import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'tools'), str(ROOT/'server'), str(ROOT/'dev/fake_ros'),
               str(ROOT.parent/'go2w/src/navigation/go2w_taught_route')]
from rollback_recorder_py import Ring

S = 1_000_000_000


class WindowTests(unittest.TestCase):
    def test_delayed_snapshot_preserves_click_window_and_excludes_future(self):
        ring = Ring(35, 10000)
        ring.add(0, '/map', b'map', latched=True)
        for i in range(60):
            ring.add(i*S, '/odom', bytes([i]))
        records = ring.snapshot(30, 50*S)
        self.assertEqual([r[0]//S for r in records if r[1] == '/odom'], list(range(24, 51)))
        self.assertEqual(records[0], (24*S, '/map', b'map'))
        self.assertEqual(ring.snapshot(1, 10*S), [])

    def test_full_requested_window_when_polled_within_buffer_margin(self):
        ring = Ring(35, 10000)
        for i in range(54):
            ring.add(i*S, '/odom', b'x')
        self.assertEqual([r[0]//S for r in ring.snapshot(30, 50*S)], list(range(20, 51)))

    def test_cpp_uses_same_fixed_window(self):
        source = (ROOT/'tools/rollback_recorder_cpp/rollback_recorder.cpp').read_text()
        ring_class = source[source.index('class Ring\n'):source.index('\nstatic void chown_tree')]
        code = '''#include <cassert>
#include <cstdint>
#include <deque>
#include <map>
#include <memory>
#include <vector>
using Msg = std::shared_ptr<int>;
size_t msg_len(const Msg &) {return 1;}
bool same_data(const Msg &a, const Msg &b) {return *a == *b;}
struct Item {int64_t t; uint32_t topic; Msg msg;};
''' + ring_class + '''
int main() {
  Ring ring(35, 10000);
  ring.add(0, 0, std::make_shared<int>(1), true, 1);
  for (int64_t i=0; i<54; ++i) ring.add(i*1000000000, 1, std::make_shared<int>(2), false, 1);
  auto records=ring.snapshot(30, 50LL*1000000000);
  assert(records.size()==32);
  assert(records.front().topic==0 && records.front().t==20LL*1000000000);
  assert(records[1].t==20LL*1000000000 && records.back().t==50LL*1000000000);
  assert(ring.snapshot(1, 10LL*1000000000).empty());
}
'''
        with tempfile.TemporaryDirectory() as d:
            cpp, binary = Path(d)/'ring.cpp', Path(d)/'ring'
            cpp.write_text(code)
            subprocess.run(['g++', '-std=c++17', '-Wall', '-Wextra', str(cpp), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


class SaveTests(unittest.IsolatedAsyncioTestCase):
    async def test_long_robot_id_and_collisions_keep_manual_capture_name_valid(self):
        import go2w_web as W
        from datetime import datetime
        import json
        with tempfile.TemporaryDirectory() as d:
            server=W.Server.__new__(W.Server)
            server.procs_status={'rollback':True};server.rollback_saving=False
            server.node=NS(event=lambda *args:None)
            rid='a'*30;names=[]
            async def advance(delay):
                if delay==10:
                    registrations=list((Path(d)/'bags/.captures/by-name').glob('*.json'))
                    self.assertTrue(all(W.P.SAFE_NAME.fullmatch(p.stem) for p in registrations),'长狗编号不能生成录包器拒绝的名字')
                request=Path(d)/'bags/.rollback/request'
                if delay!=10 and request.exists():
                    name=request.read_text().split()[0];request.unlink();names.append(name)
                    bag=Path(d)/'bags'/name;bag.mkdir()
                    (bag/'metadata.yaml').write_text('duration:\n  nanoseconds: 30000000000\n')
            stamp=datetime(2026,11,24,21,0,0)
            strftime=W.time.strftime
            with patch.object(W,'DATA',d),patch.object(W,'robot_id',return_value=(rid,'test')), \
                    patch.object(W.time,'strftime',side_effect=lambda fmt,*_args:strftime(fmt,stamp.timetuple())),patch.object(W.asyncio,'sleep',side_effect=advance):
                for _ in range(2):
                    ok,msg=await server.rollback_save('test',trigger=True,after=10)
                    self.assertTrue(ok,msg)
                    self.assertEqual(json.loads((Path(d)/'bags'/server.rollback_last/'capture.json').read_text())['robot_id'],rid)
            self.assertTrue(all(len(n)<=48 and '_trigger_' in n for n in names))
            self.assertNotEqual(names[0],names[1]);self.assertTrue(names[1].endswith('_r_2'))

    async def test_backend_reserves_slot_before_wait_and_anchors_request(self):
        import go2w_web as W
        with tempfile.TemporaryDirectory() as d:
            server = W.Server.__new__(W.Server)
            server.procs_status = {'rollback': True}
            server.rollback_saving = False
            server.node = NS(event=lambda *args: None)
            seen = []

            async def advance(delay):
                seen.append(delay)
                self.assertTrue(server.rollback_saving)
                request = Path(d)/'bags/.rollback/request'
                if delay == 10:
                    self.assertFalse(request.exists())
                    # 同一秒开始的普通分段不可占用手动回溯名称。
                    ordinary=Path(d)/'bags/go2w-01_0101_000000'
                    ordinary.mkdir(parents=True)
                    (ordinary/'metadata.yaml').write_text('duration:\n  nanoseconds: 1000000000\n')
                    ok, _ = await server.rollback_save('duplicate', after=10)
                    self.assertFalse(ok)
                elif request.exists():
                    parts = request.read_text().split()
                    self.assertNotEqual(parts[0], 'go2w-01_0101_000000')
                    self.assertEqual(parts[1:], ['30.0', '50000000000'])
                    bag = Path(d)/'bags'/parts[0]
                    bag.mkdir()
                    (bag/'metadata.yaml').write_text('duration:\n  nanoseconds: 30000000000\n')

            with patch.object(W, 'DATA', d), patch.object(W.time, 'time_ns', return_value=40*S), \
                    patch.object(W.time, 'strftime', return_value='go2w-01_0101_000000'), \
                    patch.object(W.asyncio, 'sleep', side_effect=advance):
                ok, _ = await server.rollback_save('test', after=10, request_id='request-one', voice_seconds=20)
            self.assertTrue(ok)
            self.assertEqual(seen[0], 10)
            self.assertFalse(server.rollback_saving)
            self.assertTrue((Path(d)/'bags'/server.rollback_last/'capture.json').exists())
            import json
            manifest=json.loads((Path(d)/'bags/.captures/request-one.json').read_text())
            self.assertEqual(manifest['name'],server.rollback_last)
            self.assertEqual((manifest['start_ns'],manifest['end_ns']),(20*S,50*S))
            self.assertEqual(manifest['voice'],{'state':'pending','requested_s':20})

    async def test_auto_incident_waits_for_manual_collection_and_write(self):
        import go2w_web as W
        import time
        server=W.Server.__new__(W.Server)
        server.rollback_saving=True
        server.rollback_last=None
        changes=[]
        server.incidents=NS(set_bag=lambda *args,**kw:changes.append((args,kw)),get=lambda _:None)
        now=time.monotonic()
        pending={'at':now,'t0':now,'ids':['incident']}
        server.inc_save=pending
        delays=[]
        async def advance(delay):
            delays.append(delay)
            if len(delays)==11:server.rollback_saving=False
        async def save(*args,**kwargs):
            return (False,'busy') if server.rollback_saving else (True,'saved')
        server.rollback_save=save
        with patch.object(W.asyncio,'sleep',side_effect=advance):
            await server._incident_save(pending)
        self.assertEqual(changes[-1][0],('incident','saved'))


if __name__ == '__main__':
    unittest.main()
