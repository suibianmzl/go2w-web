#!/usr/bin/env python3
"""Offline tests for explicit RTC cloud recording."""
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))

from rtc_recorder import (  # noqa: E402
    RtcRecorder,
    RtcOpenAPIError,
    RtcRecordingConfig,
    callback_signature,
    sign_openapi_request,
    verify_callback,
)


class FakeStore:
    configured = True

    def __init__(self):
        self.records = {}
        self.finished = {}

    async def rtc_recording_created(self, record):
        self.records[record['task_id']] = dict(record)

    async def rtc_recording_status(self, app_id, room_id, task_id, status, **changes):
        record = self.records[task_id]
        record['status'] = status
        record.update(changes)

    async def active_rtc_recording(self, robot_id):
        active = [
            record for record in self.records.values()
            if record['robot_id'] == robot_id
            and record.get('status') in {
                'starting', 'recording', 'stopping', 'stop_failed'}
        ]
        return active[-1] if active else None

    async def rtc_recording_finished(self, app_id, room_id, task_id, status,
                                     **changes):
        self.records[task_id]['status'] = status
        self.finished[task_id] = {
            'status': status,
            **changes,
        }


class FakeAPI:
    def __init__(self, fail_start=0):
        self.starts = []
        self.stops = []
        self.fail_start = fail_start

    async def start_record(self, payload):
        self.starts.append(payload)
        if self.fail_start:
            self.fail_start -= 1
            raise RuntimeError('temporary StartRecord failure')
        return {'Result': 'ok'}

    async def stop_record(self, payload):
        self.stops.append(payload)
        return {'Result': 'ok'}


class FatalAPI(FakeAPI):
    async def start_record(self, payload):
        self.starts.append(payload)
        raise RtcOpenAPIError(
            'StartRecord failed', code='AccessDenied.RecordNotEnabled')


def callback_event(event_type, data, event_id, secret='callback-secret'):
    event = {
        'EventType': event_type,
        'EventData': json.dumps(data, separators=(',', ':')),
        'EventTime': '2026-11-20T03:30:00Z',
        'EventId': event_id,
        'AppId': '123456781234567812345678',
        'Version': '2020-12-01',
        'Nonce': 'AbCd',
    }
    event['Signature'] = callback_signature(event, secret)
    return event


def config(**changes):
    values = {
        'requested': True,
        'enabled': True,
        'app_id': '123456781234567812345678',
        'access_key': 'test-access-key',
        'secret_key': 'test-secret-key',
        'account_id': '210000000',
        'bucket': 'evox-fleet',
        'callback_secret': 'callback-secret',
        'tos_region': 0,
        'prefix': ('go2w', 'video'),
        'start_retries': 2,
        'stop_grace_seconds': 0,
    }
    values.update(changes)
    return RtcRecordingConfig(**values)


async def test_recording_lifecycle():
    store = FakeStore()
    api = FakeAPI(fail_start=1)
    session = {
        'online_session_id': 'session-1',
        'control_session_id': 'session-1',
        'video_session_id': 'video-1',
    }
    recorder = RtcRecorder(
        config(),
        store,
        client=api,
        session_provider=lambda _robot_id: session,
        sleep=lambda _delay: asyncio.sleep(0),
    )

    await recorder.on_publisher_status('go2w-01', {
        'publishing': True,
        'source_connected': True,
    })
    task = recorder.snapshot('go2w-01')
    assert task['online_session_id'] == 'session-1'
    assert task['video_session_id'] == 'video-1'
    assert len(api.starts) == 2
    assert api.starts[0]['TaskId'] == api.starts[1]['TaskId']
    payload = api.starts[-1]
    assert payload['RecordMode'] == 1
    assert payload['TargetStreams']['StreamList'][0]['UserId'] == 'go2w-01-camera'
    assert payload['StorageConfig']['TosConfig'] == {
        'AccountId': '210000000',
        'Region': 0,
        'Bucket': 'evox-fleet',
        'ObjectMetadata': {
            'AutoSetContentType': True,
            'ContentDisposition': 'inline',
        },
    }
    assert payload['FileNameConfig']['Prefix'] == ['go2w', 'video']
    assert payload['FileNameConfig']['Pattern'] == (
        '{RoomId}/{StartTime}/{TaskId}_{UserId}_{Duration}')

    event = callback_event('RecordStarted', {
        'AppId': '123456781234567812345678',
        'RoomId': 'go2w-01',
        'TaskId': payload['TaskId'],
        'Code': 0,
    }, 'event-started')
    assert verify_callback(event, 'callback-secret')
    assert recorder.accept_callback(event)
    assert not recorder.accept_callback(event)
    await recorder.handle_callback(event)
    assert store.records[payload['TaskId']]['status'] == 'recording'

    stopped = callback_event('RecordStopped', {
        'AppId': '123456781234567812345678',
        'RoomId': 'go2w-01',
        'TaskId': payload['TaskId'],
        'Code': 0,
        'RecordFileList': [{
            'ObjectKey': (
                'go2w/video/go2w-01/2026-11-20/'
                'record_file_000001.mp4'
            ),
            'Duration': 30000,
            'Size': 1234567,
            'StartTime': 1763609400000,
            'VideoWidth': 1280,
            'VideoHeight': 720,
            'VideoCodec': '0',
        }],
    }, 'event-stopped')
    assert recorder.accept_callback(stopped)
    await recorder.handle_callback(stopped)
    finished = store.finished[payload['TaskId']]
    assert finished['status'] == 'completed'
    assert finished['duration_ms'] == 30000
    assert finished['files'][0]['tos_bucket'] == 'evox-fleet'
    assert finished['files'][0]['tos_key'].endswith('.mp4')
    assert recorder.snapshot('go2w-01')['status'] == 'idle'


async def test_online_session_changes_create_new_task():
    store = FakeStore()
    api = FakeAPI()
    session = {
        'online_session_id': 'session-1',
        'control_session_id': 'session-1',
        'video_session_id': 'video-1',
    }
    recorder = RtcRecorder(
        config(),
        store,
        client=api,
        session_provider=lambda _robot_id: session,
        sleep=lambda _delay: asyncio.sleep(0),
    )
    first = await recorder.start('go2w-01')
    resumed = await recorder.start('go2w-01')
    assert resumed.task_id == first.task_id
    assert len(api.starts) == 1

    session = {
        'online_session_id': 'session-2',
        'control_session_id': 'session-2',
        'video_session_id': 'video-2',
    }
    second = await recorder.start('go2w-01')
    assert second.task_id != first.task_id
    assert len(api.stops) == 1
    assert api.stops[0]['TaskId'] == first.task_id
    assert store.records[first.task_id]['status'] == 'stopping'
    assert store.records[second.task_id]['online_session_id'] == 'session-2'


async def test_record_not_enabled_backoff():
    store = FakeStore()
    api = FatalAPI()
    session = {
        'online_session_id': 'session-1',
        'control_session_id': 'session-1',
        'video_session_id': 'video-1',
    }
    recorder = RtcRecorder(
        config(),
        store,
        client=api,
        session_provider=lambda _robot_id: session,
        sleep=lambda _delay: asyncio.sleep(0),
    )
    try:
        await recorder.start('go2w-01')
    except RtcOpenAPIError:
        pass
    assert len(api.starts) == 1
    assert recorder.snapshot('go2w-01')['status'] == 'blocked'
    assert await recorder.start('go2w-01') is None
    assert len(api.starts) == 1


def test_signing_and_callback_verification():
    now = datetime(2026, 11, 20, 3, 30, tzinfo=timezone.utc)
    headers, query = sign_openapi_request(
        method='POST',
        host='rtc.volcengineapi.com',
        path='/',
        query={'Version': '2023-11-01', 'Action': 'StartRecord'},
        body='{"RoomId":"go2w-01"}',
        access_key='AKTEST',
        secret_key='SKTEST',
        now=now,
    )
    assert query == 'Action=StartRecord&Version=2023-11-01'
    assert headers['X-Date'] == '20261120T033000Z'
    assert 'Credential=AKTEST/20261120/cn-north-1/rtc/request' in headers[
        'Authorization']
    assert 'SignedHeaders=content-type;host;x-content-sha256;x-date' in headers[
        'Authorization']

    event = callback_event('RecordStarted', {
        'RoomId': 'go2w-01', 'TaskId': 'task-1', 'Code': 0,
    }, 'event-signature')
    assert verify_callback(event, 'callback-secret')
    event['EventData'] = event['EventData'].replace('task-1', 'task-2')
    assert not verify_callback(event, 'callback-secret')


def test_config_from_env():
    values = {
        'GO2W_RTC_RECORD_ENABLED': '1',
        'GO2W_VOLC_ACCESS_KEY': 'ak',
        'GO2W_VOLC_SECRET_KEY': 'sk',
        'GO2W_RTC_RECORD_ACCOUNT_ID': '210000000',
        'GO2W_RTC_RECORD_BUCKET': 'tos://evox-fleet/',
        'GO2W_RTC_RECORD_TOS_REGION': '0',
        'GO2W_RTC_RECORD_PREFIX': 'go2w,video',
        'GO2W_RTC_CALLBACK_SECRET': 'CallbackSecret123',
    }
    with patch.dict('os.environ', values, clear=True):
        loaded = RtcRecordingConfig.from_env(
            '123456781234567812345678')
    assert loaded.enabled and loaded.requested
    assert loaded.bucket == 'evox-fleet' and loaded.tos_region == 0
    assert loaded.prefix == ('go2w', 'video')
    assert loaded.missing == ()

    values['GO2W_RTC_CALLBACK_SECRET'] = 'too-long-' + ('A' * 32)
    with patch.dict('os.environ', values, clear=True):
        invalid = RtcRecordingConfig.from_env(
            '123456781234567812345678')
    assert not invalid.enabled
    assert invalid.invalid == ('callback_secret',)


async def main():
    test_signing_and_callback_verification()
    test_config_from_env()
    await test_recording_lifecycle()
    await test_online_session_changes_create_new_task()
    await test_record_not_enabled_backoff()
    print('PASS RTC recording: signing, retry, session binding, callbacks, TOS files')


if __name__ == '__main__':
    asyncio.run(main())
