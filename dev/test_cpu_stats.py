"""CPU明细的真实/proc格式差分；不连接ROS或机器狗。"""
import asyncio
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'server'))


def stat(pid, name, ticks, started=100):
    fields = ['S'] + ['0']*20
    fields[11], fields[12], fields[19] = str(ticks), '0', str(started)
    return f'{pid} ({name}) ' + ' '.join(fields)


class CpuStatsTests(unittest.IsolatedAsyncioTestCase):
    async def test_process_deltas_use_whole_machine_capacity_and_top_five(self):
        from cpu_stats import CpuStats
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'stat').write_text('cpu 100 0 0 900 0 0 0 0 0 0\n')
            for pid in range(10, 17):
                p=root/str(pid);p.mkdir()
                (p/'stat').write_text(stat(pid, f'worker {pid}', 20))
                (p/'cmdline').write_bytes(b'/usr/bin/python3\0/opt/work.py\0secret=hidden\0')
            sampler=CpuStats(root)
            def advance():
                (root/'stat').write_text('cpu 400 0 0 1100 0 0 0 0 0 0\n')
                for pid in range(10,17):
                    (root/str(pid)/'stat').write_text(stat(pid,f'worker {pid}',20+(pid-9)*10))
            with patch('cpu_stats.asyncio.sleep', side_effect=lambda _: advance()):
                result=await sampler.read()
            self.assertEqual(result['cpu'],60.)
            self.assertEqual([p['pid'] for p in result['items']],[16,15,14,13,12])
            self.assertEqual(result['items'][0]['cpu'],14.)
            self.assertEqual(result['items'][0]['name'],'work.py')
            self.assertNotIn('hidden',str(result))

    async def test_pid_reuse_exit_and_new_process_are_not_fake_zero(self):
        from cpu_stats import CpuStats
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'stat').write_text('cpu 100 0 0 900 0 0 0 0 0 0\n')
            for pid in (10,11,12):
                p=root/str(pid);p.mkdir();(p/'stat').write_text(stat(pid,'same (name)',10))
                (p/'cmdline').write_bytes(b'/opt/controller_server\0--ros-args\0-r\0__node:=controller_server\0')
            sampler=CpuStats(root)
            def advance():
                (root/'stat').write_text('cpu 200 0 0 1000 0 0 0 0 0 0\n')
                (root/'10/stat').write_text(stat(10,'same (name)',60,started=101))
                (root/'11/stat').unlink()
                (root/'12/stat').write_text(stat(12,'same (name)',50))
                (root/'13').mkdir();(root/'13/stat').write_text(stat(13,'new',500))
            with patch('cpu_stats.asyncio.sleep',side_effect=lambda _:advance()):
                result=await sampler.read()
            self.assertEqual([(p['pid'],p['cpu'],p['name']) for p in result['items']],[(12,20.,'controller_server')])

    async def test_missing_proc_reports_unavailable(self):
        from cpu_stats import CpuStats
        with tempfile.TemporaryDirectory() as tmp:
            sampler=CpuStats(Path(tmp))
            result=await sampler.read()
            self.assertFalse(result['ok'])
            self.assertIsNone(result['cpu'])
            self.assertEqual(result['items'],[])

    async def test_guest_time_is_not_double_counted(self):
        from cpu_stats import CpuStats
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'stat').write_text('cpu 100 0 0 900 0 0 0 0 50 0\n')
            sampler=CpuStats(root)
            before=sampler.snapshot()
            (root/'stat').write_text('cpu 200 0 0 1000 0 0 0 0 100 0\n')
            result=sampler.compare(before,sampler.snapshot())
            self.assertEqual(result['cpu'],50.)

    async def test_ranking_precedes_display_rounding(self):
        from cpu_stats import CpuStats
        sampler=CpuStats('/nonexistent')
        before=(1000,500,{(i,1):(0,'worker') for i in range(1,8)},0)
        after=(11000,5500,{(i,1):(i,'worker') for i in range(1,8)},0)
        result=sampler.compare(before,after)
        self.assertEqual([row['pid'] for row in result['items']],[7,6,5,4,3])

    async def test_name_lookup_cannot_attribute_old_usage_to_a_reused_pid(self):
        from cpu_stats import CpuStats
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);process=root/'10';process.mkdir()
            sampler=CpuStats(root)
            before=(1000,500,{(10,1):(10,'old_worker')},0)
            after=(1200,600,{(10,1):(90,'old_worker')},0)
            (process/'stat').write_text(stat(10,'new_worker',0,started=2))
            (process/'cmdline').write_bytes(b'/usr/bin/new_worker\0')
            result=sampler.compare(before,after)
            self.assertEqual(result['items'],[{'pid':10,'name':'old_worker','cpu':40.}])

    async def test_http_success_and_unavailable_are_read_only_and_not_cached(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        from cpu_stats import CpuStats
        sampler=CpuStats('/nonexistent')
        app=web.Application();sampler.routes(app)
        async with TestClient(TestServer(app)) as client:
            response=await client.get('/api/system/cpu')
            self.assertEqual(response.status,503)
            self.assertEqual(response.headers['Cache-Control'],'no-store')
            self.assertFalse((await response.json())['ok'])
            with patch.object(sampler,'read',return_value={'ok':True,'cpu':25,'items':[]}):
                response=await client.get('/api/system/cpu')
                self.assertEqual(response.status,200)
                self.assertEqual((await response.json())['cpu'],25)
            self.assertEqual((await client.post('/api/system/cpu')).status,405)

    async def test_canceled_client_does_not_cancel_shared_sampling(self):
        from cpu_stats import CpuStats
        sampler=CpuStats('/nonexistent')
        started,finish=asyncio.Event(),asyncio.Event()
        async def collect():
            started.set();await finish.wait()
            return {'ok':True,'items':[]}
        with patch.object(sampler,'collect',side_effect=collect) as collecting:
            first=asyncio.create_task(sampler.read())
            await started.wait()
            second=asyncio.create_task(sampler.read())
            first.cancel()
            with self.assertRaises(asyncio.CancelledError):await first
            finish.set()
            self.assertTrue((await second)['ok'])
            self.assertEqual(collecting.call_count,1)

    async def test_concurrent_requests_share_one_sampling_window_and_cached_result(self):
        from cpu_stats import CpuStats
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'stat').write_text('cpu 100 0 0 900 0 0 0 0 0 0\n')
            sampler=CpuStats(root)
            with patch.object(sampler,'snapshot',wraps=sampler.snapshot) as snapshots:
                first,second=await asyncio.gather(sampler.read(),sampler.read())
                third=await sampler.read()
            self.assertEqual(first,second)
            self.assertEqual(second,third)
            self.assertEqual(snapshots.call_count,2)


if __name__=='__main__':unittest.main()
