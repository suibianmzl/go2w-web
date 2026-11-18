#!/usr/bin/env python3
"""Durable robot message spool and asynchronous TOS archive worker."""
import argparse
import asyncio
import gzip
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import struct
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiomysql

from ssh_bindings import parse_mysql_dsn


LOG = logging.getLogger('go2w_archive')
SCHEMA_VERSION = 1
TEXT_EXT = '.jsonl'
BINARY_EXT = '.bin'
META_SUFFIX = '.meta.json'
SAFE_COMPONENT = re.compile(r'^[A-Za-z0-9_.-]+$')


def utc_now():
    return datetime.now(timezone.utc)


def iso_utc(value=None):
    value = value or utc_now()
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec='milliseconds').replace(
        '+00:00', 'Z')


def safe_component(value, fallback='unknown'):
    text = str(value or '').strip()
    if text and SAFE_COMPONENT.fullmatch(text):
        return text
    text = re.sub(r'[^A-Za-z0-9_.-]+', '-', text).strip('.-')
    return text[:128] or fallback


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


@dataclass
class ArchiveSettings:
    enabled: bool
    root: Path
    segment_seconds: int
    segment_bytes: int
    worker_interval: float
    retry_max: float
    completed_retention_days: int

    @classmethod
    def from_env(cls):
        return cls(
            enabled=parse_bool(os.environ.get('GO2W_ARCHIVE_ENABLED'), False),
            root=Path(os.environ.get('GO2W_ARCHIVE_ROOT', '/opt/go2w-web/archive')),
            segment_seconds=max(10, int(os.environ.get(
                'GO2W_ARCHIVE_SEGMENT_SECONDS', '300'))),
            segment_bytes=max(64 * 1024, int(os.environ.get(
                'GO2W_ARCHIVE_SEGMENT_BYTES', str(32 * 1024 * 1024)))),
            worker_interval=max(1.0, float(os.environ.get(
                'GO2W_ARCHIVE_WORKER_INTERVAL', '5'))),
            retry_max=max(10.0, float(os.environ.get(
                'GO2W_ARCHIVE_RETRY_MAX', '300'))),
            completed_retention_days=max(0, int(os.environ.get(
                'GO2W_ARCHIVE_COMPLETED_RETENTION_DAYS', '7'))),
        )


class ArchiveStore:
    """MySQL metadata for sessions, commands and TOS objects."""

    def __init__(self, mysql_dsn=None, auto_schema=True):
        self.mysql_dsn = mysql_dsn
        self.auto_schema = auto_schema
        self._pool = None
        self._lock = asyncio.Lock()
        self._schema_ready = False

    @classmethod
    def from_env(cls):
        return cls(
            os.environ.get('GO2W_ARCHIVE_MYSQL_DSN')
            or os.environ.get('GO2W_SSH_MYSQL_DSN'),
            parse_bool(os.environ.get('GO2W_ARCHIVE_AUTO_SCHEMA'), True),
        )

    @property
    def configured(self):
        return bool(self.mysql_dsn)

    async def ensure(self):
        if self._pool is not None:
            return
        if not self.configured:
            raise RuntimeError('archive MySQL is not configured')
        async with self._lock:
            if self._pool is not None:
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
            try:
                self._pool = pool
                if self.auto_schema and not self._schema_ready:
                    await self._ensure_schema()
                self._schema_ready = True
            except Exception:
                self._pool = None
                pool.close()
                await pool.wait_closed()
                raise

    async def close(self):
        if self._pool is None:
            return
        self._pool.close()
        await self._pool.wait_closed()
        self._pool = None
        self._schema_ready = False

    async def _ensure_schema(self):
        statements = [
            '''
            CREATE TABLE IF NOT EXISTS robot_session (
                session_id VARCHAR(64) NOT NULL,
                robot_id VARCHAR(128) NOT NULL,
                connected_at DATETIME(3) NOT NULL,
                disconnected_at DATETIME(3) NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                remote_info VARCHAR(255) NULL,
                edge_version VARCHAR(128) NULL,
                created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                    ON UPDATE CURRENT_TIMESTAMP(3),
                PRIMARY KEY (session_id),
                KEY idx_robot_session_robot_time (robot_id, connected_at),
                KEY idx_robot_session_status (status, updated_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_0900_ai_ci
            ''',
            '''
            CREATE TABLE IF NOT EXISTS robot_command (
                command_id VARCHAR(64) NOT NULL,
                robot_id VARCHAR(128) NOT NULL,
                session_id VARCHAR(64) NULL,
                browser_session VARCHAR(64) NULL,
                request_id VARCHAR(128) NULL,
                upstream_id VARCHAR(128) NULL,
                operation VARCHAR(64) NOT NULL,
                task_id VARCHAR(128) NULL,
                payload JSON NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'issued',
                issued_at DATETIME(3) NOT NULL,
                sent_at DATETIME(3) NULL,
                ack_at DATETIME(3) NULL,
                ok TINYINT(1) NULL,
                message TEXT NULL,
                response JSON NULL,
                created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                    ON UPDATE CURRENT_TIMESTAMP(3),
                PRIMARY KEY (command_id),
                KEY idx_robot_command_robot_time (robot_id, issued_at),
                KEY idx_robot_command_status (status, updated_at),
                KEY idx_robot_command_session (session_id, issued_at)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_0900_ai_ci
            ''',
            '''
            CREATE TABLE IF NOT EXISTS robot_data_object (
                object_id VARCHAR(64) NOT NULL,
                robot_id VARCHAR(128) NOT NULL,
                session_id VARCHAR(64) NULL,
                direction VARCHAR(16) NOT NULL,
                stream_type VARCHAR(64) NOT NULL,
                format VARCHAR(32) NOT NULL,
                period_start DATETIME(3) NOT NULL,
                period_end DATETIME(3) NOT NULL,
                message_count BIGINT UNSIGNED NOT NULL DEFAULT 0,
                byte_size BIGINT UNSIGNED NOT NULL DEFAULT 0,
                tos_bucket VARCHAR(128) NOT NULL,
                tos_key VARCHAR(512) NOT NULL,
                sha256 CHAR(64) NOT NULL,
                schema_version INT UNSIGNED NOT NULL DEFAULT 1,
                status VARCHAR(32) NOT NULL DEFAULT 'uploaded',
                uploaded_at DATETIME(3) NOT NULL,
                created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                PRIMARY KEY (object_id),
                UNIQUE KEY uk_robot_data_object_tos_key (tos_bucket, tos_key),
                KEY idx_robot_data_object_robot_time (robot_id, period_start),
                KEY idx_robot_data_object_session (session_id, period_start),
                KEY idx_robot_data_object_type (stream_type, period_start)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_0900_ai_ci
            ''',
            '''
            CREATE TABLE IF NOT EXISTS robot_incident (
                robot_id VARCHAR(128) NOT NULL,
                incident_id VARCHAR(128) NOT NULL,
                started_at_ms BIGINT NOT NULL,
                ended_at_ms BIGINT NULL,
                duration_s DOUBLE NULL,
                ongoing TINYINT(1) NOT NULL DEFAULT 0,
                incident_type VARCHAR(64) NOT NULL,
                label VARCHAR(128) NOT NULL,
                level VARCHAR(16) NOT NULL,
                message TEXT NULL,
                map_name VARCHAR(255) NULL,
                payload JSON NOT NULL,
                created_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
                updated_at DATETIME(3) NOT NULL DEFAULT CURRENT_TIMESTAMP(3)
                    ON UPDATE CURRENT_TIMESTAMP(3),
                PRIMARY KEY (robot_id, incident_id),
                KEY idx_robot_incident_time (robot_id, started_at_ms),
                KEY idx_robot_incident_type (robot_id, incident_type, started_at_ms),
                KEY idx_robot_incident_ongoing (robot_id, ongoing, started_at_ms)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
              COLLATE=utf8mb4_0900_ai_ci
            ''',
        ]
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                for statement in statements:
                    await cursor.execute(statement)
            await connection.commit()

    async def session_started(self, robot_id, session_id, connected_at,
                              remote_info=None, edge_version=None):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    INSERT INTO robot_session (
                        session_id, robot_id, connected_at, status, remote_info,
                        edge_version
                    ) VALUES (%s, %s, %s, 'active', %s, %s)
                    ON DUPLICATE KEY UPDATE
                        robot_id=VALUES(robot_id),
                        connected_at=VALUES(connected_at),
                        disconnected_at=NULL,
                        status='active',
                        remote_info=COALESCE(VALUES(remote_info), remote_info),
                        edge_version=COALESCE(VALUES(edge_version), edge_version),
                        updated_at=UTC_TIMESTAMP(3)
                    ''',
                    (session_id, robot_id, connected_at, remote_info, edge_version),
                )
            await connection.commit()

    async def session_ended(self, session_id, disconnected_at=None, status='closed'):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    UPDATE robot_session
                    SET disconnected_at=%s, status=%s, updated_at=UTC_TIMESTAMP(3)
                    WHERE session_id=%s
                    ''',
                    (disconnected_at or utc_now(), status, session_id),
                )
            await connection.commit()

    async def command_issued(self, record):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    INSERT INTO robot_command (
                        command_id, robot_id, session_id, browser_session,
                        request_id, upstream_id, operation, task_id, payload,
                        status, issued_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, 'issued', %s
                    )
                    ON DUPLICATE KEY UPDATE
                        status=VALUES(status),
                        payload=VALUES(payload),
                        updated_at=UTC_TIMESTAMP(3)
                    ''',
                    (
                        record['command_id'], record['robot_id'],
                        record.get('session_id'), record.get('browser_session'),
                        record.get('request_id'), record.get('upstream_id'),
                        record['operation'], record.get('task_id'),
                        compact_json(record.get('payload')),
                        record.get('issued_at') or utc_now(),
                    ),
                )
            await connection.commit()

    async def command_sent(self, command_id, sent_at=None):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    UPDATE robot_command
                    SET status='sent', sent_at=%s, updated_at=UTC_TIMESTAMP(3)
                    WHERE command_id=%s
                    ''',
                    (sent_at or utc_now(), command_id),
                )
            await connection.commit()

    async def command_ack(self, command_id, ok, message='', response=None, ack_at=None):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    UPDATE robot_command
                    SET status=%s, ack_at=%s, ok=%s, message=%s, response=%s,
                        updated_at=UTC_TIMESTAMP(3)
                    WHERE command_id=%s
                    ''',
                    (
                        'acked' if ok else 'failed', ack_at or utc_now(),
                        1 if ok else 0, str(message or '')[:65000],
                        compact_json(response) if response is not None else None,
                        command_id,
                    ),
                )
            await connection.commit()

    async def command_failed(self, command_id, status='failed', message=''):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    UPDATE robot_command
                    SET status=%s, message=%s, updated_at=UTC_TIMESTAMP(3)
                    WHERE command_id=%s
                    ''',
                    (status, str(message or '')[:65000], command_id),
                )
            await connection.commit()

    async def add_object(self, meta):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    INSERT INTO robot_data_object (
                        object_id, robot_id, session_id, direction, stream_type,
                        format, period_start, period_end, message_count, byte_size,
                        tos_bucket, tos_key, sha256, schema_version, status,
                        uploaded_at
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, 'uploaded', %s
                    )
                    ON DUPLICATE KEY UPDATE
                        format=VALUES(format),
                        period_start=VALUES(period_start),
                        period_end=VALUES(period_end),
                        message_count=VALUES(message_count),
                        byte_size=VALUES(byte_size),
                        tos_key=VALUES(tos_key),
                        sha256=VALUES(sha256),
                        status='uploaded',
                        uploaded_at=VALUES(uploaded_at)
                    ''',
                    (
                        meta['object_id'], meta['robot_id'], meta.get('session_id'),
                        meta['direction'], meta['stream_type'], meta['format'],
                        datetime.fromisoformat(meta['period_start'].replace('Z', '+00:00')),
                        datetime.fromisoformat(meta['period_end'].replace('Z', '+00:00')),
                        int(meta.get('message_count') or 0),
                        int(meta.get('byte_size') or 0),
                        meta['tos_bucket'], meta['tos_key'], meta['sha256'],
                        int(meta.get('schema_version') or SCHEMA_VERSION),
                        datetime.fromisoformat(meta['uploaded_at'].replace('Z', '+00:00')),
                    ),
                )
            await connection.commit()

    async def upsert_incident(self, robot_id, item):
        await self.ensure()
        async with self._pool.acquire() as connection:
            async with connection.cursor() as cursor:
                await cursor.execute(
                    '''
                    INSERT INTO robot_incident (
                        robot_id, incident_id, started_at_ms, ended_at_ms,
                        duration_s, ongoing, incident_type, label, level,
                        message, map_name, payload
                    ) VALUES (
                        %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                    )
                    ON DUPLICATE KEY UPDATE
                        started_at_ms=VALUES(started_at_ms),
                        ended_at_ms=VALUES(ended_at_ms),
                        duration_s=VALUES(duration_s),
                        ongoing=VALUES(ongoing),
                        incident_type=VALUES(incident_type),
                        label=VALUES(label),
                        level=VALUES(level),
                        message=VALUES(message),
                        map_name=VALUES(map_name),
                        payload=VALUES(payload),
                        updated_at=UTC_TIMESTAMP(3)
                    ''',
                    (
                        robot_id,
                        str(item['id']),
                        int(item['ts']),
                        int(item['end']) if item.get('end') is not None else None,
                        float(item['dur']) if item.get('dur') is not None else None,
                        1 if item.get('ongoing') else 0,
                        str(item.get('type') or 'unknown'),
                        str(item.get('label') or item.get('type') or '异常'),
                        str(item.get('level') or 'warn'),
                        str(item.get('msg') or '')[:65000],
                        str(item.get('map') or '') or None,
                        compact_json(item),
                    ),
                )
            await connection.commit()

    async def list_incidents(self, robot_id, limit=50, before_ms=None):
        await self.ensure()
        limit = max(1, min(500, int(limit)))
        async with self._pool.acquire() as connection:
            async with connection.cursor(aiomysql.DictCursor) as cursor:
                if before_ms is None:
                    await cursor.execute(
                        '''
                        SELECT payload FROM robot_incident
                        WHERE robot_id=%s
                        ORDER BY started_at_ms DESC, incident_id DESC
                        LIMIT %s
                        ''',
                        (robot_id, limit),
                    )
                else:
                    await cursor.execute(
                        '''
                        SELECT payload FROM robot_incident
                        WHERE robot_id=%s AND started_at_ms < %s
                        ORDER BY started_at_ms DESC, incident_id DESC
                        LIMIT %s
                        ''',
                        (robot_id, int(before_ms), limit),
                    )
                rows = await cursor.fetchall()
        items = []
        for row in rows:
            payload = row['payload']
            if isinstance(payload, (bytes, bytearray)):
                payload = payload.decode('utf-8')
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except ValueError:
                    continue
            if isinstance(payload, dict):
                items.append(payload)
        return items


class _SegmentWriter:
    def __init__(self, root, robot_id, session_id, direction, stream_type,
                 source_format, target_format, extension):
        self.root = root
        self.robot_id = robot_id
        self.session_id = session_id
        self.direction = direction
        self.stream_type = stream_type
        self.source_format = source_format
        self.target_format = target_format
        self.extension = extension
        self.object_id = uuid.uuid4().hex
        self.started_at = utc_now()
        self.opened_monotonic = time.monotonic()
        self.last_write = time.monotonic()
        self.message_count = 0
        self.byte_size = 0

        date = self.started_at.strftime('%Y-%m-%d')
        hour = self.started_at.strftime('%H')
        folder = (
            root / 'spool' / 'pending' / date / hour
            / safe_component(robot_id) / safe_component(session_id)
            / safe_component(direction) / safe_component(stream_type)
        )
        folder.mkdir(parents=True, exist_ok=True)
        self.ready_path = folder / f'{self.object_id}{extension}'
        self.open_path = folder / f'{self.object_id}{extension}.open'
        self.meta_path = folder / f'{self.object_id}{META_SUFFIX}'
        self._fh = self.open_path.open('wb')

    @property
    def age(self):
        return time.monotonic() - self.opened_monotonic

    def append(self, data):
        self._fh.write(data)
        self.byte_size += len(data)
        self.message_count += 1
        self.last_write = time.monotonic()

    def metadata(self, closed=False):
        return {
            'schema_version': SCHEMA_VERSION,
            'object_id': self.object_id,
            'robot_id': self.robot_id,
            'session_id': self.session_id,
            'direction': self.direction,
            'stream_type': self.stream_type,
            'source_format': self.source_format,
            'format': self.target_format,
            'period_start': iso_utc(self.started_at),
            'period_end': iso_utc() if closed else iso_utc(self.started_at),
            'message_count': self.message_count,
            'byte_size': self.byte_size,
        }

    def _write_meta(self, meta):
        tmp = self.meta_path.with_suffix('.tmp')
        tmp.write_text(compact_json(meta), encoding='utf-8')
        os.replace(tmp, self.meta_path)

    def finalize(self):
        if self._fh.closed:
            return
        self._fh.flush()
        os.fsync(self._fh.fileno())
        self._fh.close()
        self._write_meta(self.metadata(closed=True))
        os.replace(self.open_path, self.ready_path)

    def abort(self):
        if not self._fh.closed:
            self._fh.close()


class ArchiveSpool:
    """Low-latency durable spool used directly by CloudGateway."""

    def __init__(self, settings=None):
        self.settings = settings or ArchiveSettings.from_env()
        self.enabled = self.settings.enabled
        self.root = self.settings.root
        self._writers = {}
        if self.enabled:
            for name in ('pending', 'uploading', 'completed', 'failed'):
                (self.root / 'spool' / name).mkdir(parents=True, exist_ok=True)
            (self.root / 'logs').mkdir(parents=True, exist_ok=True)

    @classmethod
    def from_env(cls):
        return cls(ArchiveSettings.from_env())

    def append_text(self, *, robot_id, session_id, direction, message_type,
                    payload, message_seq=None, browser_session_id=None,
                    command_id=None, task_id=None, gateway_ts=None):
        if not self.enabled:
            return
        event_id = uuid.uuid4().hex
        record = {
            'schema_version': SCHEMA_VERSION,
            'event_id': event_id,
            'robot_id': robot_id,
            'session_id': session_id,
            'direction': direction,
            'message_type': message_type,
            'message_seq': message_seq,
            'browser_session_id': browser_session_id,
            'command_id': command_id,
            'task_id': task_id,
            'gateway_ts': gateway_ts or iso_utc(),
            'payload': payload,
        }
        data = (compact_json(record) + '\n').encode('utf-8')
        key = (session_id, direction, 'text', None)
        writer = self._writer(
            key, robot_id, session_id, direction, 'text',
            'jsonl', 'jsonl.gz', TEXT_EXT,
        )
        writer.append(data)
        self._rotate_if_needed(key, writer)

    def append_binary(self, *, robot_id, session_id, kind, data, message_seq=None):
        if not self.enabled or not data:
            return
        stream_type = f'binary-{safe_component(kind, "unknown")}'
        key = (session_id, 'up', stream_type, stream_type)
        writer = self._writer(
            key, robot_id, session_id, 'up', stream_type,
            'length-prefixed-websocket-frames', 'bin', BINARY_EXT,
        )
        writer.append(struct.pack('<I', len(data)) + data)
        self._rotate_if_needed(key, writer)

    def _writer(self, key, robot_id, session_id, direction, stream_type,
                source_format, target_format, extension):
        writer = self._writers.get(key)
        if writer is not None:
            return writer
        writer = _SegmentWriter(
            self.root, robot_id, session_id, direction, stream_type,
            source_format, target_format, extension,
        )
        self._writers[key] = writer
        return writer

    def _rotate_if_needed(self, key, writer):
        if (
            writer.byte_size >= self.settings.segment_bytes
            or writer.age >= self.settings.segment_seconds
        ):
            self.close_writer(key)

    def close_writer(self, key):
        writer = self._writers.pop(key, None)
        if writer is not None:
            writer.finalize()

    def close_session(self, session_id):
        for key in list(self._writers):
            if key[0] == session_id:
                self.close_writer(key)

    def close_all(self):
        for key in list(self._writers):
            self.close_writer(key)

    def status(self):
        base = self.root / 'spool'
        result = {'enabled': self.enabled, 'root': str(self.root)}
        for name in ('pending', 'uploading', 'completed', 'failed'):
            folder = base / name
            result[name] = sum(
                1 for path in folder.rglob('*')
                if path.is_file() and not path.name.endswith(META_SUFFIX)
                and not path.name.endswith('.open')
            ) if folder.exists() else 0
        heartbeat = self.root / 'worker.heartbeat'
        if heartbeat.is_file():
            try:
                payload = json.loads(heartbeat.read_text(encoding='utf-8'))
                result['worker'] = payload
                result['worker_age_seconds'] = round(
                    max(0.0, time.time() - heartbeat.stat().st_mtime), 1)
            except (OSError, ValueError):
                pass
        return result


class TosObjectStore:
    def __init__(self, ak, sk, endpoint, region, bucket):
        if not all((ak, sk, endpoint, region, bucket)):
            raise RuntimeError('TOS configuration is incomplete')
        import tos

        self.bucket = bucket
        self.client = tos.TosClientV2(ak, sk, endpoint, region)

    def upload_file(self, key, path, content_type=None):
        self.client.put_object_from_file(
            self.bucket,
            key,
            str(path),
            content_type=content_type,
        )
        return f'tos://{self.bucket}/{key}'


class ArchiveWorker:
    def __init__(self, settings, object_store, store):
        self.settings = settings
        self.object_store = object_store
        self.store = store
        self.root = settings.root
        self.retry = {}
        self.attempts = {}

    async def process_once(self):
        self._recover_uploading()
        count = 0
        for path in self._ready_files():
            key = path.name
            if self.retry.get(key, 0) > time.monotonic():
                continue
            try:
                await self._process_file(path)
                self.retry.pop(key, None)
                self.attempts.pop(key, None)
                count += 1
            except Exception as error:
                LOG.exception('archive failed for %s: %s', path, error)
                self._restore_pending(path)
                attempt = self.attempts.get(key, 0) + 1
                self.attempts[key] = attempt
                delay = min(self.settings.retry_max, 5.0 * (2 ** min(attempt - 1, 6)))
                self.retry[key] = time.monotonic() + delay
        self._cleanup_completed()
        return count

    def _ready_files(self):
        pending = self.root / 'spool' / 'pending'
        if not pending.exists():
            return []
        return sorted(
            (path for path in pending.rglob('*')
             if path.is_file() and path.suffix in {TEXT_EXT, BINARY_EXT}),
            key=lambda path: path.stat().st_mtime,
        )

    def _meta_path(self, path):
        return path.with_name(f'{path.stem}{META_SUFFIX}')

    async def _process_file(self, source):
        meta_source = self._meta_path(source)
        if not meta_source.is_file():
            LOG.warning('archive segment has no metadata: %s', source)
            return
        uploading = self.root / 'spool' / 'uploading' / source.name
        uploading_meta = self.root / 'spool' / 'uploading' / meta_source.name
        os.replace(source, uploading)
        os.replace(meta_source, uploading_meta)

        meta, upload_path, key = await asyncio.to_thread(
            self._prepare_and_upload, uploading, uploading_meta)
        meta.update({
            'tos_bucket': self.object_store.bucket,
            'tos_key': key,
            'uploaded_at': iso_utc(),
            'status': 'uploaded',
        })
        await self.store.add_object(meta)

        completed = self.root / 'spool' / 'completed'
        completed.mkdir(parents=True, exist_ok=True)
        uploading_meta.write_text(compact_json(meta), encoding='utf-8')
        for path in (uploading, upload_path, uploading_meta):
            if path.exists():
                os.replace(path, completed / path.name)

    def _prepare_and_upload(self, uploading, uploading_meta):
        meta = json.loads(uploading_meta.read_text(encoding='utf-8'))
        upload_path = uploading
        if uploading.suffix == TEXT_EXT:
            upload_path = uploading.with_name(f'{uploading.stem}.jsonl.gz')
            with uploading.open('rb') as src, upload_path.open('wb') as raw:
                with gzip.GzipFile(fileobj=raw, mode='wb', compresslevel=6, mtime=0) as dst:
                    shutil.copyfileobj(src, dst)
            meta['format'] = 'jsonl.gz'
            content_type = 'application/gzip'
        else:
            meta['format'] = 'bin'
            content_type = 'application/octet-stream'

        digest = hashlib.sha256()
        with upload_path.open('rb') as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)

        key_suffix = '.jsonl.gz' if meta['format'] == 'jsonl.gz' else '.bin'
        key = self._object_key(meta, key_suffix)
        self.object_store.upload_file(key, upload_path, content_type=content_type)
        meta['sha256'] = digest.hexdigest()
        return meta, upload_path, key

    def _object_key(self, meta, suffix):
        start = datetime.fromisoformat(meta['period_start'].replace('Z', '+00:00'))
        return '/'.join([
            'raw',
            f"robot_id={safe_component(meta['robot_id'])}",
            f"date={start.strftime('%Y-%m-%d')}",
            f"hour={start.strftime('%H')}",
            f"session_id={safe_component(meta['session_id'])}",
            f"direction={safe_component(meta['direction'])}",
            f"stream_type={safe_component(meta['stream_type'])}",
            f"{meta['object_id']}{suffix}",
        ])

    def _restore_pending(self, pending_path):
        pending = self.root / 'spool' / 'pending'
        pending.mkdir(parents=True, exist_ok=True)
        for folder_name in ('uploading', 'pending'):
            folder = self.root / 'spool' / folder_name
            for path in folder.rglob(f'{pending_path.stem}*'):
                if not path.is_file():
                    continue
                if path.name.endswith('.jsonl.gz'):
                    path.unlink()
                elif folder == pending and path.name == pending_path.name:
                    continue
                else:
                    os.replace(path, pending / path.name)

    def _recover_uploading(self):
        uploading = self.root / 'spool' / 'uploading'
        if not uploading.exists():
            return
        cutoff = time.time() - 600
        pending = self.root / 'spool' / 'pending'
        for path in uploading.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                os.replace(path, pending / path.name)

    def _cleanup_completed(self):
        days = self.settings.completed_retention_days
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        completed = self.root / 'spool' / 'completed'
        if not completed.exists():
            return
        for path in completed.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()

    def touch_heartbeat(self, processed):
        payload = {
            'ts': iso_utc(),
            'processed': processed,
            'pending': len(self._ready_files()),
        }
        path = self.root / 'worker.heartbeat'
        tmp = path.with_suffix('.tmp')
        tmp.write_text(compact_json(payload), encoding='utf-8')
        os.replace(tmp, path)


def object_store_from_env():
    return TosObjectStore(
        os.environ.get('GO2W_TOS_ACCESS_KEY'),
        os.environ.get('GO2W_TOS_SECRET_KEY'),
        os.environ.get('GO2W_TOS_ENDPOINT'),
        os.environ.get('GO2W_TOS_REGION'),
        os.environ.get('GO2W_TOS_BUCKET'),
    )


async def run_worker():
    settings = ArchiveSettings.from_env()
    if not settings.enabled:
        raise RuntimeError('GO2W_ARCHIVE_ENABLED is off')
    store = ArchiveStore.from_env()
    worker = ArchiveWorker(settings, object_store_from_env(), store)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        while not stop.is_set():
            processed = await worker.process_once()
            worker.touch_heartbeat(processed)
            try:
                await asyncio.wait_for(stop.wait(), settings.worker_interval)
            except asyncio.TimeoutError:
                pass
    finally:
        await store.close()


def main():
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(message)s',
    )
    parser = argparse.ArgumentParser()
    parser.add_argument('--once', action='store_true', help='process current spool once')
    args = parser.parse_args()
    if args.once:
        settings = ArchiveSettings.from_env()
        store = ArchiveStore.from_env()
        worker = ArchiveWorker(settings, object_store_from_env(), store)

        async def run_once():
            try:
                return await worker.process_once()
            finally:
                await store.close()

        print(json.dumps({'processed': asyncio.run(run_once())}))
        return 0
    asyncio.run(run_worker())
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
