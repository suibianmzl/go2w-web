"""MySQL and Redis storage for robot SSH port bindings."""
import asyncio
import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import aiomysql
from redis import asyncio as redis_async


KEY_PREFIX = 'evox:ssh-binding:v1'
DEVICE_INDEX_KEY = f'{KEY_PREFIX}:devices'
ONLINE_TTL = timedelta(seconds=30)


@dataclass
class SSHBinding:
    device_id: str
    device_name: str
    device_model: str
    device_enabled: bool
    online: bool
    host: str
    port: int | None
    username: str
    status: str
    binding_version: str
    last_seen_at: datetime | None
    updated_at: datetime
    serial_number: str | None = None
    firmware_version: str | None = None
    public_ssh_port: int = 22

    def to_api(self, *, include_last_seen=True):
        payload = {
            'deviceId': self.device_id,
            'deviceName': self.device_name,
            'deviceModel': self.device_model,
            'deviceEnabled': self.device_enabled,
            'online': self.online,
            'host': self.host,
            'port': self.port,
            'username': self.username,
            'status': self.status,
            'bindingVersion': self.binding_version,
        }
        if include_last_seen:
            payload['lastSeenAt'] = iso_utc(self.last_seen_at)
        if self.serial_number:
            payload['serialNumber'] = self.serial_number
        if self.firmware_version:
            payload['firmwareVersion'] = self.firmware_version
        payload['publicSshPort'] = self.public_ssh_port
        payload['updatedAt'] = iso_utc(self.updated_at)
        return payload

    def to_cache(self):
        return json.dumps(self.to_api(), ensure_ascii=False, separators=(',', ':'))


def iso_utc(value):
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00', 'Z')


def parse_mysql_dsn(dsn):
    match = re.match(r'^(.*?):(.*?)@tcp\((.+)\)/([^?]+)', dsn or '')
    if not match:
        raise ValueError('invalid MYSQL_DSN')
    user, password, address, database = match.groups()
    host, port = address.rsplit(':', 1)
    quotes = "'\""
    return {
        'user': user.strip().strip(quotes),
        'password': password.strip().strip(quotes),
        'host': host.strip().strip(quotes),
        'port': int(port),
        'db': database.strip().strip(quotes),
    }


class SSHBindingStore:
    def __init__(self, mysql_dsn=None, redis_url=None):
        self.mysql_dsn = mysql_dsn
        self.redis_url = redis_url
        self._pool = None
        self._redis = None
        self._lock = asyncio.Lock()

    @property
    def configured(self):
        return bool(self.mysql_dsn and self.redis_url)

    async def ensure(self):
        if self._pool is not None and self._redis is not None:
            return
        if not self.configured:
            raise RuntimeError('SSH binding storage is not configured')
        async with self._lock:
            if self._pool is not None and self._redis is not None:
                return
            mysql = parse_mysql_dsn(self.mysql_dsn)
            pool = await aiomysql.create_pool(
                **mysql,
                autocommit=False,
                charset='utf8mb4',
                connect_timeout=5,
                minsize=1,
                maxsize=5,
            )
            client = redis_async.from_url(
                self.redis_url,
                decode_responses=True,
                socket_connect_timeout=3,
                socket_timeout=3,
            )
            try:
                await client.ping()
            except Exception:
                pool.close()
                await pool.wait_closed()
                raise
            self._pool, self._redis = pool, client

    async def close(self):
        if self._redis is not None:
            await self._redis.aclose()
            self._redis = None
        if self._pool is not None:
            self._pool.close()
            await self._pool.wait_closed()
            self._pool = None

    async def get_by_port(self, port):
        await self.ensure()
        try:
            binding = await self._get_by_port_mysql(port)
        except Exception:
            cached = await self._redis.get(self._port_key(port))
            if cached is None:
                raise
            return binding_from_api(json.loads(cached))
        if binding is None:
            try:
                await self._redis.delete(self._port_key(port))
            except Exception:
                pass
            return None
        try:
            await self._cache(binding)
        except Exception:
            pass
        return binding

    async def get_by_device(self, device_id):
        await self.ensure()
        try:
            binding = await self._get_by_device_mysql(device_id)
        except Exception:
            cached = await self._redis.get(self._device_key(device_id))
            if cached is None:
                raise
            return binding_from_api(json.loads(cached))
        if binding is not None:
            try:
                await self._cache(binding)
            except Exception:
                pass
        return binding

    async def list(self):
        await self.ensure()
        try:
            bindings = await self._list_mysql()
        except Exception:
            return await self._list_redis()
        for binding in bindings:
            try:
                await self._cache(binding)
            except Exception:
                pass
        return bindings

    async def upsert(self, binding):
        await self.ensure()
        old_port = None
        async with self._pool.acquire() as connection:
            try:
                await connection.begin()
                async with connection.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(
                        'SELECT port FROM ssh_binding WHERE device_id=%s FOR UPDATE',
                        (binding.device_id,),
                    )
                    row = await cursor.fetchone()
                    old_port = row['port'] if row else None
                    await cursor.execute(
                        '''
                        INSERT INTO robot_device (
                            device_id, device_name, device_model, serial_number,
                            firmware_version, device_enabled, online, status,
                            last_seen_at, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s,
                            %s, %s, %s, %s,
                            %s, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)
                        )
                        ON DUPLICATE KEY UPDATE
                            device_name=VALUES(device_name),
                            device_model=VALUES(device_model),
                            serial_number=COALESCE(VALUES(serial_number), serial_number),
                            firmware_version=COALESCE(VALUES(firmware_version), firmware_version),
                            device_enabled=VALUES(device_enabled),
                            online=VALUES(online),
                            status=VALUES(status),
                            last_seen_at=COALESCE(VALUES(last_seen_at), last_seen_at),
                            updated_at=UTC_TIMESTAMP(3)
                        ''',
                        (
                            binding.device_id, binding.device_name, binding.device_model,
                            binding.serial_number, binding.firmware_version,
                            binding.device_enabled, binding.online,
                            'online' if binding.online else 'offline',
                            binding.last_seen_at,
                        ),
                    )
                    await cursor.execute(
                        '''
                        INSERT INTO ssh_binding (
                            device_id, host, port, username, status, binding_version,
                            last_seen_at, created_at, updated_at
                        ) VALUES (
                            %s, %s, %s, %s, %s, %s,
                            %s, UTC_TIMESTAMP(3), UTC_TIMESTAMP(3)
                        )
                        ON DUPLICATE KEY UPDATE
                            host=VALUES(host),
                            port=VALUES(port),
                            username=VALUES(username),
                            status=VALUES(status),
                            binding_version=VALUES(binding_version),
                            last_seen_at=VALUES(last_seen_at),
                            updated_at=UTC_TIMESTAMP(3)
                        ''',
                        (
                            binding.device_id, binding.host, binding.port,
                            binding.username, binding.status, binding.binding_version,
                            binding.last_seen_at,
                        ),
                    )
                    await cursor.execute(
                        '''
                        SELECT
                            d.device_id, d.device_name, d.device_model, d.serial_number,
                            d.firmware_version, d.device_enabled, d.online,
                            b.host, b.port, b.username, b.status, b.binding_version,
                            COALESCE(d.last_seen_at, b.last_seen_at) AS last_seen_at,
                            b.updated_at
                        FROM ssh_binding b
                        JOIN robot_device d ON d.device_id=b.device_id
                        WHERE b.device_id=%s
                        ''',
                        (binding.device_id,),
                    )
                    saved = row_to_binding(await cursor.fetchone())
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        if old_port and old_port != saved.port:
            await self._redis.delete(self._port_key(old_port))
        await self._cache(saved)
        return saved

    async def set_online(self, device_id, online, seen_at=None):
        await self.ensure()
        seen_at = seen_at or datetime.now(timezone.utc)
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    UPDATE robot_device
                    SET online=%s, status=%s, last_seen_at=%s, updated_at=UTC_TIMESTAMP(3)
                    WHERE device_id=%s
                    ''',
                    (online, 'online' if online else 'offline', seen_at, device_id),
                )
                await connection.commit()
        binding = await self.get_by_device(device_id)
        if binding is None:
            return None
        await self._cache(binding)
        return binding

    async def unbind(self, device_id):
        await self.ensure()
        old_port = None
        async with self._pool.acquire() as connection:
            try:
                await connection.begin()
                async with connection.cursor(aiomysql.DictCursor) as cursor:
                    await cursor.execute(
                        'SELECT port FROM ssh_binding WHERE device_id=%s FOR UPDATE',
                        (device_id,),
                    )
                    row = await cursor.fetchone()
                    if row is None:
                        await connection.rollback()
                        return None, None
                    old_port = row['port']
                    await cursor.execute(
                        '''
                        UPDATE ssh_binding
                        SET port=NULL, status='unbound', updated_at=UTC_TIMESTAMP(3)
                        WHERE device_id=%s
                        ''',
                        (device_id,),
                    )
                    await cursor.execute(
                        '''
                        UPDATE robot_device
                        SET online=0, status='unbound', updated_at=UTC_TIMESTAMP(3)
                        WHERE device_id=%s
                        ''',
                        (device_id,),
                    )
                await connection.commit()
            except Exception:
                await connection.rollback()
                raise
        binding = await self.get_by_device(device_id)
        if old_port is not None:
            await self._redis.delete(self._port_key(old_port))
        if binding is not None:
            await self._cache(binding)
        return binding, old_port

    async def _get_by_port_mysql(self, port):
        async with self._pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    '''
                    SELECT
                        d.device_id, d.device_name, d.device_model, d.serial_number,
                        d.firmware_version, d.device_enabled, d.online,
                        b.host, b.port, b.username, b.status, b.binding_version,
                        COALESCE(d.last_seen_at, b.last_seen_at) AS last_seen_at,
                        b.updated_at
                    FROM ssh_binding b
                    JOIN robot_device d ON d.device_id=b.device_id
                    WHERE b.port=%s AND b.port IS NOT NULL AND b.status<>'unbound'
                    LIMIT 1
                    ''',
                    (port,),
                )
                return row_to_binding(await cursor.fetchone())

    async def _get_by_device_mysql(self, device_id):
        async with self._pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    '''
                    SELECT
                        d.device_id, d.device_name, d.device_model, d.serial_number,
                        d.firmware_version, d.device_enabled, d.online,
                        b.host, b.port, b.username, b.status, b.binding_version,
                        COALESCE(d.last_seen_at, b.last_seen_at) AS last_seen_at,
                        b.updated_at
                    FROM ssh_binding b
                    JOIN robot_device d ON d.device_id=b.device_id
                    WHERE b.device_id=%s
                    LIMIT 1
                    ''',
                    (device_id,),
                )
                return row_to_binding(await cursor.fetchone())

    async def _list_mysql(self):
        async with self._pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                await cursor.execute(
                    '''
                    SELECT
                        d.device_id, d.device_name, d.device_model, d.serial_number,
                        d.firmware_version, d.device_enabled, d.online,
                        b.host, b.port, b.username, b.status, b.binding_version,
                        COALESCE(d.last_seen_at, b.last_seen_at) AS last_seen_at,
                        b.updated_at
                    FROM ssh_binding b
                    JOIN robot_device d ON d.device_id=b.device_id
                    WHERE b.port IS NOT NULL AND b.status<>'unbound'
                    ORDER BY b.port ASC
                    '''
                )
                return [row_to_binding(row) for row in await cursor.fetchall()]

    async def _cache(self, binding):
        payload = binding.to_cache()
        await self._redis.set(self._device_key(binding.device_id), payload)
        await self._redis.sadd(DEVICE_INDEX_KEY, binding.device_id)
        if binding.port is not None and binding.status != 'unbound':
            await self._redis.set(self._port_key(binding.port), payload)
        elif binding.port is not None:
            await self._redis.delete(self._port_key(binding.port))

    async def _list_redis(self):
        device_ids = await self._redis.smembers(DEVICE_INDEX_KEY)
        if not device_ids:
            return []
        payloads = await self._redis.mget([self._device_key(value) for value in sorted(device_ids)])
        bindings = []
        for payload in payloads:
            if not payload:
                continue
            binding = binding_from_api(json.loads(payload))
            if binding.port is not None and binding.status != 'unbound':
                bindings.append(binding)
        return sorted(bindings, key=lambda item: item.port)

    @staticmethod
    def _port_key(port):
        return f'{KEY_PREFIX}:port:{port}'

    @staticmethod
    def _device_key(device_id):
        return f'{KEY_PREFIX}:device:{device_id}'


def row_to_binding(row):
    if row is None:
        return None
    last_seen = row['last_seen_at']
    if last_seen is not None and last_seen.tzinfo is None:
        last_seen = last_seen.replace(tzinfo=timezone.utc)
    online = (
        bool(row['online'])
        and last_seen is not None
        and datetime.now(timezone.utc) - last_seen <= ONLINE_TTL
    )
    return SSHBinding(
        device_id=row['device_id'],
        device_name=row['device_name'],
        device_model=row['device_model'],
        device_enabled=bool(row['device_enabled']),
        online=online,
        host=row['host'],
        port=row['port'],
        username=row['username'],
        status=row['status'],
        binding_version=str(row['binding_version']),
        last_seen_at=last_seen,
        updated_at=row['updated_at'],
        serial_number=row.get('serial_number'),
        firmware_version=row.get('firmware_version'),
        public_ssh_port=int(row.get('public_ssh_port') or 22),
    )


def binding_from_api(payload):
    return SSHBinding(
        device_id=payload['deviceId'],
        device_name=payload['deviceName'],
        device_model=payload['deviceModel'],
        device_enabled=bool(payload['deviceEnabled']),
        online=bool(payload['online']),
        host=payload['host'],
        port=payload.get('port'),
        username=payload['username'],
        status=payload['status'],
        binding_version=str(payload['bindingVersion']),
        last_seen_at=parse_time(payload.get('lastSeenAt')),
        updated_at=parse_time(payload['updatedAt']) or datetime.now(timezone.utc),
        serial_number=payload.get('serialNumber'),
        firmware_version=payload.get('firmwareVersion'),
        public_ssh_port=int(payload.get('publicSshPort') or 22),
    )


def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace('Z', '+00:00'))
