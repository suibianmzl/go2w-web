"""HTTP API tests for toggling Nav2's ObstaclesCritic parameter."""
import asyncio
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
from obstacles_critic import ObstaclesCriticAPI


class ObstaclesCriticAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app = web.Application()
        ObstaclesCriticAPI().routes(app)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_true_endpoint_enables_critic(self, create_process):
        process = AsyncMock()
        process.communicate.return_value = (b'Successfully set parameter', b'')
        process.returncode = 0
        create_process.return_value = process

        response = await self.client.post('/api/obstacles-critic/true')

        self.assertEqual(response.status, 200)
        self.assertTrue((await response.json())['enabled'])
        create_process.assert_awaited_once_with(
            'ros2', 'param', 'set', '/controller_server',
            'FollowPath.ObstaclesCritic.enabled', 'true',
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)

    @patch('obstacles_critic.asyncio.create_subprocess_exec', new_callable=AsyncMock)
    async def test_false_endpoint_disables_critic(self, create_process):
        process = AsyncMock()
        process.communicate.return_value = (b'Successfully set parameter', b'')
        process.returncode = 0
        create_process.return_value = process

        response = await self.client.post('/api/obstacles-critic/false')

        self.assertEqual(response.status, 200)
        self.assertFalse((await response.json())['enabled'])
        self.assertEqual(create_process.await_args.args[-1], 'false')

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
    async def test_cross_origin_write_is_rejected(self, create_process):
        response = await self.client.post('/api/obstacles-critic/true',
                                          headers={'Origin': 'http://foreign.test'})
        self.assertEqual(response.status, 403)
        create_process.assert_not_awaited()

    async def test_get_is_not_allowed(self):
        response = await self.client.get('/api/obstacles-critic/true')
        self.assertEqual(response.status, 405)


if __name__ == '__main__':
    unittest.main()
