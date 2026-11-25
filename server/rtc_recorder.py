"""Explicit Volcengine RTC cloud-recording controller.

The controller owns StartRecord/StopRecord, RTC callbacks and the durable
recording state. It deliberately does not handle RTC media itself.
"""
import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import aiohttp


LOG = logging.getLogger('go2w_rtc_recording')
API_HOST = 'rtc.volcengineapi.com'
API_REGION = 'cn-north-1'
API_SERVICE = 'rtc'
API_VERSION = '2023-11-01'
ACTIVE_STATUSES = {'starting', 'recording', 'stopping', 'stop_failed'}
TERMINAL_STATUSES = {'completed', 'backuped', 'failed', 'abandoned'}


def utc_now():
    return datetime.now(timezone.utc)


def parse_bool(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def norm_query(params):
    parts = []
    for key in sorted(params):
        value = params[key]
        values = value if isinstance(value, (list, tuple)) else [value]
        for item in values:
            parts.append(
                f'{quote(str(key), safe="-_.~")}='
                f'{quote(str(item), safe="-_.~")}'
            )
    return '&'.join(parts)


def sha256_hex(data):
    if isinstance(data, str):
        data = data.encode('utf-8')
    return hashlib.sha256(data).hexdigest()


def sign_openapi_request(
    *,
    method,
    host,
    path,
    query,
    body,
    access_key,
    secret_key,
    region=API_REGION,
    service=API_SERVICE,
    now=None,
):
    """Return signed headers and canonical query for a Volcengine request."""
    now = now or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    now = now.astimezone(timezone.utc)
    x_date = now.strftime('%Y%m%dT%H%M%SZ')
    short_date = x_date[:8]
    body_bytes = body.encode('utf-8') if isinstance(body, str) else bytes(body or b'')
    body_hash = sha256_hex(body_bytes)
    content_type = 'application/json'
    canonical_query = norm_query(query)
    canonical_headers = '\n'.join([
        f'content-type:{content_type}',
        f'host:{host}',
        f'x-content-sha256:{body_hash}',
        f'x-date:{x_date}',
    ])
    signed_headers = 'content-type;host;x-content-sha256;x-date'
    canonical_request = '\n'.join([
        str(method).upper(),
        path or '/',
        canonical_query,
        canonical_headers,
        '',
        signed_headers,
        body_hash,
    ])
    scope = f'{short_date}/{region}/{service}/request'
    string_to_sign = '\n'.join([
        'HMAC-SHA256',
        x_date,
        scope,
        sha256_hex(canonical_request),
    ])

    def sign(key, message):
        return hmac.new(key, message.encode('utf-8'), hashlib.sha256).digest()

    signing_key = sign(secret_key.encode('utf-8'), short_date)
    signing_key = sign(signing_key, region)
    signing_key = sign(signing_key, service)
    signing_key = sign(signing_key, 'request')
    signature = hmac.new(
        signing_key, string_to_sign.encode('utf-8'), hashlib.sha256
    ).hexdigest()
    headers = {
        'Content-Type': content_type,
        'Host': host,
        'X-Date': x_date,
        'X-Content-Sha256': body_hash,
        'Authorization': (
            f'HMAC-SHA256 Credential={access_key}/{scope}, '
            f'SignedHeaders={signed_headers}, Signature={signature}'
        ),
    }
    return headers, canonical_query


def callback_signature(event, secret):
    values = [
        str(event.get('EventType') or ''),
        str(event.get('EventData') or ''),
        str(event.get('EventTime') or ''),
        str(event.get('EventId') or ''),
        str(event.get('AppId') or ''),
        str(event.get('Version') or ''),
        str(event.get('Nonce') or ''),
        str(secret or ''),
    ]
    return sha256_hex(''.join(sorted(values)))


def verify_callback(event, secret):
    if not isinstance(event, dict) or not secret:
        return False
    signature = str(event.get('Signature') or '')
    expected = callback_signature(event, secret)
    return hmac.compare_digest(signature, expected)


class RtcOpenAPIError(RuntimeError):
    def __init__(self, message, *, code=None, request_id=None):
        super().__init__(message)
        self.code = code
        self.request_id = request_id


class RtcOpenAPIClient:
    def __init__(self, access_key, secret_key, *, host=API_HOST,
                 region=API_REGION, timeout=15):
        self.access_key = access_key
        self.secret_key = secret_key
        self.host = host
        self.region = region
        self.timeout = timeout

    async def _request(self, action, payload):
        body = compact_json(payload)
        query = {'Action': action, 'Version': API_VERSION}
        headers, canonical_query = sign_openapi_request(
            method='POST',
            host=self.host,
            path='/',
            query=query,
            body=body,
            access_key=self.access_key,
            secret_key=self.secret_key,
            region=self.region,
            service=API_SERVICE,
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                f'https://{self.host}/?{canonical_query}',
                data=body.encode('utf-8'),
                headers=headers,
            ) as response:
                raw = await response.text()
                try:
                    result = json.loads(raw)
                except ValueError:
                    result = {}
                metadata = result.get('ResponseMetadata') or {}
                request_id = metadata.get('RequestId')
                error = metadata.get('Error')
                if response.status != 200 or error or result.get('Result') != 'ok':
                    if isinstance(error, dict):
                        code = error.get('Code') or error.get('CodeN')
                        message = error.get('Message') or raw
                    else:
                        code = None
                        message = raw
                    raise RtcOpenAPIError(
                        f'{action} failed: {message}',
                        code=code,
                        request_id=request_id,
                    )
                return result

    async def start_record(self, payload):
        return await self._request('StartRecord', payload)

    async def stop_record(self, payload):
        return await self._request('StopRecord', payload)


@dataclass(frozen=True)
class RtcRecordingConfig:
    requested: bool
    enabled: bool
    app_id: str
    access_key: str
    secret_key: str
    account_id: str
    bucket: str
    callback_secret: str
    tos_region: int = 0
    business_id: str = 'go2w-archive'
    prefix: tuple = ('go2w', 'video')
    file_formats: tuple = ('MP4',)
    max_idle_seconds: int = 300
    max_silence_seconds: int = 120
    max_record_seconds: int = 0
    stop_grace_seconds: int = 30
    start_retries: int = 3
    api_host: str = API_HOST
    api_region: str = API_REGION
    missing: tuple = field(default_factory=tuple)
    invalid: tuple = field(default_factory=tuple)

    @classmethod
    def from_env(cls, app_id=None):
        requested = parse_bool(os.environ.get('GO2W_RTC_RECORD_ENABLED'), False)
        prefix = tuple(
            part.strip()
            for part in os.environ.get(
                'GO2W_RTC_RECORD_PREFIX', 'go2w,video').split(',')
            if part.strip()
        )
        if not prefix or any(not re.fullmatch(r'[A-Za-z0-9]+', part) for part in prefix):
            prefix = ('go2w', 'video')
        formats = tuple(
            part.strip().upper()
            for part in os.environ.get(
                'GO2W_RTC_RECORD_FORMATS', 'MP4').split(',')
            if part.strip()
        ) or ('MP4',)
        bucket = os.environ.get('GO2W_RTC_RECORD_BUCKET', '').strip()
        if bucket.startswith('tos://'):
            bucket = bucket[len('tos://'):].split('/', 1)[0]
        values = {
            'app_id': str(app_id or '').strip(),
            'access_key': os.environ.get('GO2W_VOLC_ACCESS_KEY', '').strip(),
            'secret_key': os.environ.get('GO2W_VOLC_SECRET_KEY', '').strip(),
            'account_id': os.environ.get('GO2W_RTC_RECORD_ACCOUNT_ID', '').strip(),
            'bucket': bucket,
            'callback_secret': os.environ.get(
                'GO2W_RTC_CALLBACK_SECRET', '').strip(),
        }
        missing = tuple(name for name, value in values.items() if not value)
        invalid = ()
        if values['callback_secret'] and not re.fullmatch(
                r'[A-Za-z0-9]{1,32}', values['callback_secret']):
            invalid = ('callback_secret',)
        return cls(
            requested=requested,
            enabled=requested and not missing and not invalid,
            **values,
            tos_region=int(os.environ.get('GO2W_RTC_RECORD_TOS_REGION', '0')),
            business_id=os.environ.get(
                'GO2W_RTC_RECORD_BUSINESS_ID', 'go2w-archive').strip(),
            prefix=prefix,
            file_formats=formats,
            max_idle_seconds=max(
                10, int(os.environ.get('GO2W_RTC_RECORD_MAX_IDLE_SECONDS', '300'))),
            max_silence_seconds=min(3600, max(
                60, int(os.environ.get(
                    'GO2W_RTC_RECORD_MAX_SILENCE_SECONDS', '120')))),
            max_record_seconds=max(
                0, int(os.environ.get('GO2W_RTC_RECORD_MAX_SECONDS', '0'))),
            stop_grace_seconds=max(
                0, int(os.environ.get(
                    'GO2W_RTC_RECORD_STOP_GRACE_SECONDS', '30'))),
            missing=missing,
            invalid=invalid,
        )


@dataclass
class RecordingTask:
    recording_id: str
    robot_id: str
    online_session_id: str
    control_session_id: str
    video_session_id: str | None
    room_id: str
    task_id: str
    status: str = 'starting'
    stop_timer: asyncio.Task | None = None

    @property
    def key(self):
        return self.room_id, self.task_id

    def as_dict(self):
        return {
            'recording_id': self.recording_id,
            'robot_id': self.robot_id,
            'online_session_id': self.online_session_id,
            'control_session_id': self.control_session_id,
            'video_session_id': self.video_session_id,
            'room_id': self.room_id,
            'task_id': self.task_id,
            'status': self.status,
        }


class RtcRecorder:
    def __init__(
        self,
        config,
        store,
        *,
        client=None,
        session_provider=None,
        sleep=asyncio.sleep,
        now=utc_now,
    ):
        self.config = config
        self.store = store
        self.client = client or RtcOpenAPIClient(
            config.access_key,
            config.secret_key,
            host=config.api_host,
            region=config.api_region,
        )
        self.session_provider = session_provider or (lambda _robot_id: None)
        self._sleep = sleep
        self._now = now
        self._locks = {}
        self._active = {}
        self._by_task = {}
        self._seen_event_ids = OrderedDict()
        self._blocked_until = 0.0
        self._blocked_reason = None
        self._offline_log_at = {}
        if config.requested and (config.missing or config.invalid):
            fields = config.missing + config.invalid
            LOG.error(
                'RTC recording is enabled but configuration is incomplete: %s',
                ', '.join(fields),
            )
        elif config.enabled and not getattr(store, 'configured', False):
            LOG.error('RTC recording requires the archive MySQL connection')

    @property
    def enabled(self):
        return bool(
            self.config.enabled
            and getattr(self.store, 'configured', False)
        )

    def snapshot(self, robot_id):
        task = self._active.get(robot_id)
        if task is not None:
            return dict(task.as_dict())
        if self._blocked_until > time.monotonic():
            return {
                'robot_id': robot_id,
                'status': 'blocked',
                'reason': self._blocked_reason,
                'retry_in_seconds': round(
                    self._blocked_until - time.monotonic(), 1),
            }
        return {
            'robot_id': robot_id,
            'status': 'idle',
        }

    def _lock(self, robot_id):
        return self._locks.setdefault(robot_id, asyncio.Lock())

    def _task_from_row(self, row):
        return RecordingTask(
            recording_id=str(row['recording_id']),
            robot_id=str(row['robot_id']),
            online_session_id=str(row['online_session_id']),
            control_session_id=str(
                row.get('control_session_id') or row['online_session_id']),
            video_session_id=(
                str(row['video_session_id'])
                if row.get('video_session_id') is not None else None
            ),
            room_id=str(row['room_id']),
            task_id=str(row['task_id']),
            status=str(row.get('status') or 'starting'),
        )

    async def _remember_active(self, robot_id):
        task = self._active.get(robot_id)
        if task is not None:
            return task
        row = await self.store.active_rtc_recording(robot_id)
        if not row or row.get('status') not in ACTIVE_STATUSES:
            return None
        task = self._task_from_row(row)
        self._active[robot_id] = task
        self._by_task[task.key] = task
        return task

    def _new_task_id(self, robot_id):
        stamp = self._now().astimezone(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        value = f'{robot_id}_{stamp}_{uuid.uuid4().hex[:8]}'
        value = re.sub(r'[^A-Za-z0-9_@.-]', '_', value)
        return value[:128]

    def _start_payload(self, task):
        business = {}
        if self.config.business_id:
            business['BusinessId'] = self.config.business_id
        return {
            'AppId': self.config.app_id,
            **business,
            'RoomId': task.room_id,
            'TaskId': task.task_id,
            'RecordMode': 1,
            'TargetStreams': {
                'StreamList': [{
                    'Index': 0,
                    'UserId': f'{task.robot_id}-camera',
                    'StreamType': 0,
                }],
            },
            'Control': {
                'MediaType': 0,
                'FrameInterpolationMode': 1,
                'MaxIdleTime': self.config.max_idle_seconds,
                'MaxSilenceSeconds': self.config.max_silence_seconds,
                'MaxRecordTime': self.config.max_record_seconds,
                'EnableSyncUpload': False,
            },
            'FileFormatConfig': {
                'FileFormat': list(self.config.file_formats),
            },
            'FileNameConfig': {
                'Prefix': list(self.config.prefix),
                'Pattern': (
                    '{RoomId}/{StartTime}/'
                    '{TaskId}_{UserId}_{Duration}'
                ),
            },
            'StorageConfig': {
                'Type': 0,
                'TosConfig': {
                    'AccountId': self.config.account_id,
                    'Region': self.config.tos_region,
                    'Bucket': self.config.bucket,
                    'ObjectMetadata': {
                        'AutoSetContentType': True,
                        'ContentDisposition': 'inline',
                    },
                },
            },
        }

    async def _retry(self, action, payload):
        last_error = None
        for attempt in range(max(1, self.config.start_retries)):
            try:
                return await action(payload)
            except RtcOpenAPIError as error:
                last_error = error
                if error.code and (
                    str(error.code).startswith('AccessDenied.')
                    or str(error.code).startswith('InvalidParameter')
                ):
                    break
                if attempt + 1 >= max(1, self.config.start_retries):
                    break
                await self._sleep(0.5 * (2 ** attempt))
            except Exception as error:
                last_error = error
                if attempt + 1 >= max(1, self.config.start_retries):
                    break
                await self._sleep(0.5 * (2 ** attempt))
        raise last_error

    async def _set_status(self, task, status, **changes):
        task.status = status
        await self.store.rtc_recording_status(
            self.config.app_id,
            task.room_id,
            task.task_id,
            status,
            **changes,
        )

    def _log_offline_once(self, robot_id):
        now = time.monotonic()
        last = self._offline_log_at.get(robot_id, 0.0)
        if now - last < 60.0:
            return
        self._offline_log_at[robot_id] = now
        LOG.info('RTC recording skipped for %s: control session is offline', robot_id)

    async def start(self, robot_id, reason='publisher'):
        if not self.enabled:
            return None
        if self._blocked_until > time.monotonic():
            return None
        self._blocked_until = 0.0
        self._blocked_reason = None
        session = self.session_provider(robot_id)
        if not session or not session.get('online_session_id'):
            self._log_offline_once(robot_id)
            return None

        async with self._lock(robot_id):
            task = await self._remember_active(robot_id)
            if task is not None:
                if (
                    task.online_session_id == session['online_session_id']
                    and task.status in ACTIVE_STATUSES
                ):
                    if task.stop_timer is not None:
                        task.stop_timer.cancel()
                        task.stop_timer = None
                    return task
                if not await self._stop_locked(task, 'online session changed'):
                    return None

            task = RecordingTask(
                recording_id=uuid.uuid4().hex,
                robot_id=robot_id,
                online_session_id=session['online_session_id'],
                control_session_id=session.get(
                    'control_session_id', session['online_session_id']),
                video_session_id=session.get('video_session_id'),
                room_id=robot_id,
                task_id=self._new_task_id(robot_id),
            )
            payload = self._start_payload(task)
            await self.store.rtc_recording_created({
                **task.as_dict(),
                'app_id': self.config.app_id,
                'publisher_user_id': f'{robot_id}-camera',
                'business_id': self.config.business_id or None,
                'start_reason': reason,
                'requested_at': self._now(),
                'tos_bucket': self.config.bucket,
                'request_config': payload,
            })
            self._active[robot_id] = task
            self._by_task[task.key] = task
            try:
                await self._retry(self.client.start_record, payload)
            except Exception as error:
                if task.status == 'recording':
                    LOG.warning(
                        'StartRecord response failed after recording began for %s/%s',
                        robot_id, task.task_id,
                    )
                    return task
                LOG.exception('StartRecord failed for %s/%s', robot_id, task.task_id)
                if (
                    getattr(error, 'code', None) == 'AccessDenied.RecordNotEnabled'
                    or 'RecordNotEnabled' in str(error)
                ):
                    self._blocked_until = time.monotonic() + 300
                    self._blocked_reason = (
                        '火山 RTC 云端录制总开关未启用（RecordNotEnabled）')
                    LOG.error(
                        'RTC recording paused for 300 seconds: %s',
                        self._blocked_reason,
                    )
                await self._set_status(
                    task,
                    'failed',
                    error_code=getattr(error, 'code', None),
                    error_message=str(error),
                )
                self._active.pop(robot_id, None)
                raise
            return task

    async def _stop_locked(self, task, reason):
        if task.stop_timer is not None:
            task.stop_timer.cancel()
            task.stop_timer = None
        await self._set_status(
            task,
            'stopping',
            stopping_at=self._now(),
            stop_reason=reason,
        )
        payload = {
            'AppId': self.config.app_id,
            'RoomId': task.room_id,
            'TaskId': task.task_id,
        }
        if self.config.business_id:
            payload['BusinessId'] = self.config.business_id
        try:
            await self._retry(self.client.stop_record, payload)
        except Exception as error:
            if task.status in TERMINAL_STATUSES:
                return True
            LOG.exception('StopRecord failed for %s/%s', task.robot_id, task.task_id)
            await self._set_status(
                task,
                'stop_failed',
                error_code=getattr(error, 'code', None),
                error_message=str(error),
            )
            return False
        if self._active.get(task.robot_id) is task:
            self._active.pop(task.robot_id, None)
        return True

    async def stop(self, robot_id, reason='requested'):
        if not self.enabled:
            return False
        async with self._lock(robot_id):
            task = await self._remember_active(robot_id)
            if task is None:
                return False
            return await self._stop_locked(task, reason)

    def _schedule_stop(self, robot_id, reason, delay=None):
        if not self.enabled:
            return
        task = self._active.get(robot_id)
        if task is None:
            return
        if task.stop_timer is not None:
            task.stop_timer.cancel()
        delay = self.config.stop_grace_seconds if delay is None else delay

        async def delayed():
            try:
                await self._sleep(delay)
                await self.stop(robot_id, reason)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOG.exception('delayed RTC recording stop failed for %s', robot_id)

        task.stop_timer = asyncio.create_task(delayed())

    async def session_ended(self, robot_id, session_id, reason='control disconnected'):
        task = self._active.get(robot_id)
        if task is not None and task.online_session_id != session_id:
            return
        await self.stop(robot_id, reason)

    def video_source_ended(self, robot_id, reason='video source disconnected'):
        self._schedule_stop(robot_id, reason)

    async def camera_disabled(self, robot_id):
        await self.stop(robot_id, 'camera upload disabled')

    async def on_publisher_status(self, robot_id, state):
        if not isinstance(state, dict):
            return
        if state.get('publishing') and state.get('source_connected', True):
            await self.start(robot_id, 'publisher online')
        elif state.get('source_connected') is False:
            self._schedule_stop(robot_id, 'publisher source disconnected')

    def accept_callback(self, event):
        if not self.config.enabled or not verify_callback(
                event, self.config.callback_secret):
            return False
        event_id = str(event.get('EventId') or '')
        if event_id and event_id in self._seen_event_ids:
            return False
        if event_id:
            self._seen_event_ids[event_id] = time.monotonic()
            while len(self._seen_event_ids) > 2048:
                self._seen_event_ids.popitem(last=False)
        return True

    async def handle_callback(self, event):
        app_id = str(event.get('AppId') or '')
        if self.config.app_id and app_id and app_id != self.config.app_id:
            LOG.warning('ignored RTC callback for unexpected app_id=%s', app_id)
            return
        event_type = str(event.get('EventType') or '')
        try:
            data = json.loads(event.get('EventData') or '{}')
        except (TypeError, ValueError):
            LOG.warning('invalid RTC callback EventData for %s', event_type)
            return
        if not isinstance(data, dict):
            return
        room_id = str(data.get('RoomId') or '')
        user_id = str(data.get('UserId') or '')
        publisher_id = f'{room_id}-camera'

        if event_type == 'UserVideoStreamStart' and user_id == publisher_id:
            task = self._active.get(room_id)
            if task is not None and task.stop_timer is not None:
                task.stop_timer.cancel()
                task.stop_timer = None
            await self.start(room_id, 'rtc stream started')
            return
        if event_type == 'UserVideoStreamStop' and user_id == publisher_id:
            self._schedule_stop(room_id, 'rtc stream stopped')
            return

        task_id = str(data.get('TaskId') or '')
        if not task_id:
            return
        key = (room_id, task_id)
        task = self._by_task.get(key)
        if task is None:
            task = RecordingTask(
                recording_id=uuid.uuid4().hex,
                robot_id=room_id,
                online_session_id='',
                control_session_id='',
                video_session_id=None,
                room_id=room_id,
                task_id=task_id,
            )

        if event_type == 'RecordStarted':
            code = int(data.get('Code') or 0)
            if code == 0:
                await self._set_status(
                    task,
                    'recording',
                    recording_started_at=self._now(),
                )
            else:
                await self._set_status(
                    task,
                    'failed',
                    error_code=str(code),
                    error_message=str(data.get('ErrorMessage') or ''),
                )
                self._active.pop(task.robot_id, None)
            return

        if event_type == 'RecordStopped':
            code = int(data.get('Code') or 0)
            status = {0: 'completed', 1: 'failed', 2: 'backuped'}.get(
                code, 'failed')
            task.status = status
            files = []
            for index, item in enumerate(data.get('RecordFileList') or []):
                if not isinstance(item, dict):
                    continue
                key_name = str(item.get('ObjectKey') or '')
                if not key_name:
                    continue
                duration_ms = int(item.get('Duration') or 0)
                start_ms = int(item.get('StartTime') or 0)
                started_at = (
                    datetime.fromtimestamp(start_ms / 1000, timezone.utc)
                    if start_ms else None
                )
                ended_at = (
                    started_at + timedelta(milliseconds=duration_ms)
                    if started_at is not None else None
                )
                extension = key_name.rsplit('.', 1)[-1].upper() if '.' in key_name else ''
                files.append({
                    'recording_file_id': uuid.uuid4().hex,
                    'file_index': index,
                    'file_format': extension or (
                        self.config.file_formats[0]
                        if self.config.file_formats else 'UNKNOWN'
                    ),
                    'tos_bucket': self.config.bucket,
                    'tos_key': key_name,
                    'duration_ms': duration_ms or None,
                    'byte_size': int(item.get('Size') or 0),
                    'started_at': started_at,
                    'ended_at': ended_at,
                    'video_width': item.get('VideoWidth'),
                    'video_height': item.get('VideoHeight'),
                    'video_codec': str(item.get('VideoCodec') or '') or None,
                    'audio_codec': str(item.get('AudioCodec') or '') or None,
                    'status': 'backuped' if code == 2 else 'uploaded',
                })
            await self.store.rtc_recording_finished(
                self.config.app_id,
                room_id,
                task_id,
                status,
                stopped_at=self._now(),
                duration_ms=sum(int(item.get('duration_ms') or 0) for item in files),
                total_byte_size=sum(int(item.get('byte_size') or 0) for item in files),
                error_code=str(code) if code else None,
                error_message=str(data.get('ErrorMessage') or '') or None,
                files=files,
            )
            if self._active.get(task.robot_id) is task:
                self._active.pop(task.robot_id, None)
            self._by_task.pop(key, None)
            return

        if event_type in {
            'RecordUploadStarted', 'RecordUploadProcessing',
            'RecordUploadDone', 'RecordUploadBackuped',
        } and int(data.get('Code') or 0) != 0:
            LOG.warning(
                'RTC upload event %s failed task=%s code=%s message=%s',
                event_type, task_id, data.get('Code'), data.get('ErrorMessage'),
            )

    async def close(self):
        tasks = [
            task for task in dict(self._active).values()
            if task.stop_timer is not None
        ]
        for task in tasks:
            task.stop_timer.cancel()
        if tasks:
            await asyncio.gather(
                *(task.stop_timer for task in tasks),
                return_exceptions=True,
            )
        await asyncio.gather(
            *(self.stop(robot_id, 'gateway shutdown')
              for robot_id in list(self._active)),
            return_exceptions=True,
        )
