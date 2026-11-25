"""HTTP controls for Nav2's FollowPath.ObstaclesCritic parameter."""
import asyncio
from urllib.parse import urlsplit

from aiohttp import web


class ObstaclesCriticAPI:
    NODE = '/controller_server'
    PARAMETER = 'FollowPath.ObstaclesCritic.enabled'
    TIMEOUT = 10.0

    def routes(self, app):
        app.router.add_post('/api/obstacles-critic/true', self.set_from_true)
        app.router.add_post('/api/obstacles-critic/false', self.set_from_false)

    @staticmethod
    def same_origin(req):
        origin = req.headers.get('Origin')
        return not origin or urlsplit(origin).netloc == req.host

    async def set_from_true(self, req):
        return await self.set_enabled(req, True)

    async def set_from_false(self, req):
        return await self.set_enabled(req, False)

    async def set_enabled(self, req, enabled):
        if not self.same_origin(req):
            raise web.HTTPForbidden(text='只接受当前网页发起的请求')
        value = 'true' if enabled else 'false'
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                'ros2', 'param', 'set', self.NODE, self.PARAMETER, value,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=self.TIMEOUT)
        except asyncio.TimeoutError:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.communicate()
            raise web.HTTPGatewayTimeout(text='设置 ROS 参数超时')
        except OSError as e:
            raise web.HTTPBadGateway(text=f'无法执行 ros2：{e.strerror or e}')
        output = (stdout or stderr).decode('utf-8', errors='replace').strip()
        if proc.returncode != 0:
            raise web.HTTPBadGateway(text=output or f'ros2 命令失败（退出码 {proc.returncode}）')
        return web.json_response({'ok': True, 'requested': req.path.rsplit('/', 1)[-1],
                                  'enabled': enabled, 'output': output})
