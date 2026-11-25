"""HTTP API tests for toggling Nav2's ObstaclesCritic parameter."""
import asyncio
from pathlib import Path
import sys
from types import SimpleNamespace as NS
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from obstacles_critic import ObstaclesCriticAPI


class ObstaclesCriticAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        self.busy = False
        self.api = ObstaclesCriticAPI(busy=lambda: self.busy)
        self.api.routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_true_endpoint_enables_both_obstacle_parameters(self, create_process):
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (b'Successfully set parameter', b'')
        create_process.return_value = process

        response = await self.client.post('/api/obstacles-critic/true')

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertTrue(body['enabled'])
        self.assertEqual([item['enabled'] for item in body['parameters']], [True, True])
        self.assertEqual(
            [call.args for call in create_process.await_args_list],
            [('ros2', 'param', 'set', '/controller_server',
              'FollowPath.ObstaclesCritic.enabled', 'true'),
             ('ros2', 'param', 'set', '/global_costmap/global_costmap',
              'voxel_layer.enabled', 'true')])

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_false_endpoint_disables_both_obstacle_parameters(self, create_process):
        process = AsyncMock(returncode=0)
        process.communicate.return_value = (b'Successfully set parameter', b'')
        create_process.return_value = process

        response = await self.client.post('/api/obstacles-critic/false')

        self.assertEqual(response.status, 200)
        body = await response.json()
        self.assertFalse(body['enabled'])
        self.assertEqual([item['enabled'] for item in body['parameters']], [False, False])
        self.assertEqual(
            [call.args[-1] for call in create_process.await_args_list], ['false', 'false'])

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_ros_command_failure_returns_gateway_error(self, create_process):
        process = AsyncMock()
        process.communicate.return_value = (b'', b'node not found')
        process.returncode = 1
        create_process.return_value = process

        response = await self.client.post('/api/obstacles-critic/true')

        self.assertEqual(response.status, 502)
        self.assertIn('node not found', await response.text())

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_ros_parameter_rejection_with_zero_exit_is_an_error(self, create_process):
        process = Mock(returncode=0)
        process.communicate = AsyncMock()
        create_process.return_value = process
        for stdout in (b'', b'client information'):
            process.communicate.return_value = (stdout, b'Setting parameter failed: read-only parameter')
            response = await self.client.post('/api/obstacles-critic/true')
            self.assertEqual(response.status, 502)
            self.assertIn('read-only parameter', await response.text())

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_only_successful_true_notifies_recovery(self, create_process):
        self.api.on_enabled = Mock()
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b'', b'Setting parameter failed: rejected'))
        create_process.return_value = process
        await self.client.post('/api/obstacles-critic/true')
        self.api.on_enabled.assert_not_called()
        process.communicate.return_value = (b'Set parameter successful', b'')
        response = await self.client.post('/api/obstacles-critic/false')
        self.assertEqual(response.status, 200)
        self.api.on_enabled.assert_not_called()
        response = await self.client.post('/api/obstacles-critic/true')
        self.assertEqual(response.status, 200)
        self.api.on_enabled.assert_called_once_with()

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_cross_origin_write_is_rejected(self, create_process):
        response = await self.client.post('/api/obstacles-critic/true',
                                          headers={'Origin': 'http://foreign.test'})
        self.assertEqual(response.status, 403)
        create_process.assert_not_awaited()

    async def test_get_is_not_allowed(self):
        response = await self.client.get('/api/obstacles-critic/true')
        self.assertEqual(response.status, 405)

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_manual_write_is_rejected_while_route_controls_parameter(self, create_process):
        self.busy = True
        response = await self.client.post('/api/obstacles-critic/false')
        self.assertEqual(response.status, 409)
        create_process.assert_not_awaited()

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_queued_manual_write_rechecks_route_after_lock(self, create_process):
        process = Mock(returncode=0)
        process.communicate = AsyncMock(return_value=(b'OK', b''))
        create_process.return_value = process
        await self.api.lock.acquire()
        request = asyncio.create_task(self.api.set_enabled(NS(headers={}, host='localhost', path='/api/obstacles-critic/false'), False))
        await asyncio.sleep(0)
        self.busy = True
        self.api.lock.release()
        with self.assertRaises(web.HTTPConflict):
            await request
        create_process.assert_not_awaited()

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_missing_ros_command_returns_gateway_error(self, create_process):
        create_process.side_effect = FileNotFoundError('ros2')
        response = await self.client.post('/api/obstacles-critic/true')
        self.assertEqual(response.status, 502)

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_timeout_kills_and_reaps_command(self, create_process):
        process = Mock(returncode=None)
        process.communicate = AsyncMock(side_effect=[asyncio.TimeoutError(), (b'', b'')])
        create_process.return_value = process
        response = await self.client.post('/api/obstacles-critic/true')
        self.assertEqual(response.status, 504)
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_canceled_internal_call_kills_command(self, create_process):
        process = Mock(returncode=None)
        process.communicate = AsyncMock(side_effect=[asyncio.CancelledError(), (b'', b'')])
        create_process.return_value = process
        with self.assertRaises(asyncio.CancelledError):
            await ObstaclesCriticAPI().apply(True)
        process.kill.assert_called_once()
        self.assertEqual(process.communicate.await_count, 2)

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_parameter_writes_are_serialized(self, create_process):
        entered, release = [asyncio.Event() for _ in range(2)], [asyncio.Event() for _ in range(2)]
        processes = [Mock(returncode=0) for _ in range(4)]
        async def pending(index):
            entered[index].set()
            await release[index].wait()
            return b'OK', b''
        async def first():return await pending(0)
        async def second():return await pending(1)
        processes[0].communicate = AsyncMock(side_effect=first)
        processes[1].communicate = AsyncMock(side_effect=second)
        for process in processes[2:]:process.communicate = AsyncMock(return_value=(b'OK',b''))
        create_process.side_effect = processes
        api = ObstaclesCriticAPI()
        one = asyncio.create_task(api.apply(True))
        await entered[0].wait()
        two = asyncio.create_task(api.apply(False))
        await asyncio.sleep(0)
        self.assertEqual(create_process.await_count, 1)
        release[0].set()
        await entered[1].wait()
        await asyncio.sleep(0)
        self.assertEqual(create_process.await_count,2,'第二个参数完成前不能交叉下一次请求')
        release[1].set()
        results=await asyncio.gather(one,two)
        self.assertEqual([[row['enabled'] for row in rows] for rows in results],[[True,True],[False,False]])
        self.assertEqual([c.args[-1] for c in create_process.await_args_list], ['true','true','false','false'])


if __name__ == '__main__':
    unittest.main()
