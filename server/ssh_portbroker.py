"""Client for the external SSH reverse-port broker."""
import asyncio

from aiohttp import ClientError, ClientSession, ClientTimeout


class PortBrokerError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class PortBrokerClient:
    def __init__(self, base_url='https://ssh-portbroker-api.evoxrobot.com'):
        self.base_url = base_url.rstrip('/')
        self._session = None
        self._lock = asyncio.Lock()

    async def _client(self):
        if self._session is None or self._session.closed:
            async with self._lock:
                if self._session is None or self._session.closed:
                    self._session = ClientSession(timeout=ClientTimeout(total=12))
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def _request(self, method, path, payload=None, allow_404=False):
        session = await self._client()
        try:
            async with session.request(
                method,
                self.base_url + path,
                json=payload,
                headers={'Accept': 'application/json'},
            ) as response:
                try:
                    data = await response.json(content_type=None)
                except Exception:
                    data = {'detail': await response.text()}
                if response.status == 404 and allow_404:
                    return None
                if response.status >= 400:
                    detail = data.get('detail') if isinstance(data, dict) else data
                    raise PortBrokerError(response.status, str(detail or f'HTTP {response.status}'))
                return data
        except ClientError as error:
            raise PortBrokerError(503, f'broker unavailable: {error}') from error

    async def health(self):
        return await self._request('GET', '/health')

    async def public_target(self):
        return await self._request('GET', '/v1/public')

    async def get_dog_port(self, dog_id):
        return await self._request(
            'GET', f'/v1/dogs/{dog_id}/port', allow_404=True)

    async def allocate(self, dog_id, note='register'):
        return await self._request(
            'POST', '/v1/ports/allocate', {'dog_id': dog_id, 'note': note})

    async def release(self, dog_id, port, reason='destroy'):
        return await self._request(
            'POST',
            '/v1/ports/release',
            {'dog_id': dog_id, 'port': int(port), 'reason': reason},
            allow_404=True,
        )
