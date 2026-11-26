#!/usr/bin/env python3
"""Tests for local archive spool and TOS upload worker."""
import asyncio
import json
import struct
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'server'))

from archive import ArchiveSettings, ArchiveSpool, ArchiveWorker  # noqa: E402


class FakeObjectStore:
    bucket = 'test-bucket'

    def __init__(self):
        self.objects = {}

    def upload_file(self, key, path, content_type=None):
        self.objects[key] = {
            'data': Path(path).read_bytes(),
            'content_type': content_type,
        }


class FakeArchiveStore:
    def __init__(self):
        self.objects = []
        self.snapshots = []

    async def add_object(self, meta):
        self.objects.append(meta)

    async def set_incident_snapshot(self, robot_id, incident_id, bucket, key):
        self.snapshots.append((robot_id, incident_id, bucket, key))


async def test_spool_and_worker():
    with tempfile.TemporaryDirectory() as tmp:
        settings = ArchiveSettings(
            enabled=True,
            root=Path(tmp),
            segment_seconds=300,
            segment_bytes=32 * 1024 * 1024,
            worker_interval=1,
            retry_max=30,
            completed_retention_days=7,
        )
        spool = ArchiveSpool(settings)
        spool.append_text(
            robot_id='go2w-01',
            session_id='session-1',
            direction='up',
            message_type='pose',
            message_seq=1,
            payload={'t': 'pose', 'x': 1.2, 'y': 3.4, 'th': 0.5},
        )
        spool.append_text(
            robot_id='go2w-01',
            session_id='session-1',
            direction='down',
            message_type='cmd',
            message_seq=2,
            command_id='cmd-1',
            payload={'t': 'cmd', 'op': 'cancel'},
        )
        frame = bytes([2, 1, 2, 3, 4])
        spool.append_binary(
            robot_id='go2w-01',
            session_id='session-1',
            kind='scan',
            data=frame,
            message_seq=3,
        )
        jpeg = b'\xff\xd8\xff\xe0' + b'snapshot' + b'\xff\xd9'
        spool.append_snapshot(
            robot_id='go2w-01',
            session_id='session-1',
            incident_id='incident-1',
            data=jpeg,
        )
        spool.close_all()

        pending = list((Path(tmp) / 'spool' / 'pending').rglob('*'))
        assert sum(path.suffix == '.jsonl' for path in pending) == 2
        assert sum(path.suffix == '.bin' for path in pending) == 1

        tos = FakeObjectStore()
        store = FakeArchiveStore()
        worker = ArchiveWorker(settings, tos, store)
        assert await worker.process_once() == 4
        assert await worker.process_once() == 0
        assert len(store.objects) == 4
        assert all(item['tos_bucket'] == 'test-bucket' for item in store.objects)

        text_objects = [
            item for item in tos.objects.items() if item[0].endswith('.jsonl')
        ]
        assert len(text_objects) == 2
        records = []
        for _key, item in text_objects:
            assert item['content_type'] == 'application/jsonl'
            records.extend(
                json.loads(line)
                for line in item['data'].decode('utf-8').splitlines()
            )
        assert {record['message_type'] for record in records} == {'pose', 'cmd'}
        assert all(item['format'] == 'jsonl' for item in store.objects
                   if item['stream_type'] == 'text')
        assert next(record for record in records if record['message_type'] == 'cmd')[
            'command_id'] == 'cmd-1'

        binary_key, binary = next(
            item for item in tos.objects.items() if item[0].endswith('.bin')
        )
        length = struct.unpack_from('<I', binary['data'])[0]
        assert length == len(frame)
        assert binary['data'][4:] == frame
        assert '/stream_type=binary-scan/' in binary_key
        snapshot_key, snapshot = next(
            item for item in tos.objects.items() if item[0].endswith('.jpg')
        )
        assert snapshot['data'] == jpeg
        assert snapshot['content_type'] == 'image/jpeg'
        assert '/stream_type=incident-snapshot/' in snapshot_key
        assert store.snapshots and store.snapshots[0][:2] == (
            'go2w-01', 'incident-1')
        assert worker.touch_heartbeat(3) is None
        assert (Path(tmp) / 'worker.heartbeat').is_file()


async def main():
    await test_spool_and_worker()
    print('PASS archive: uncompressed JSONL, length-prefixed binary, TOS upload and MySQL manifest')


if __name__ == '__main__':
    asyncio.run(main())
