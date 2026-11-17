"""Volcengine RTC access-token generation.

Adapted from the official RTC_Token Python reference. The AppKey stays on the
server; clients receive only a room/user-scoped token.
"""
import base64
import hmac
import random
import struct
import time
from collections import OrderedDict
from hashlib import sha256


VERSION = '001'
APP_ID_LENGTH = 24
PRIV_PUBLISH_STREAM = 0
PRIV_SUBSCRIBE_STREAM = 4


def _u16(value):
    return struct.pack('<H', int(value))


def _u32(value):
    return struct.pack('<I', int(value))


def _bytes(value):
    return _u16(len(value)) + value


def _string(value):
    return _bytes(str(value).encode('utf-8'))


def _privileges(values):
    result = _u16(len(values))
    for key, value in sorted(values.items()):
        result += _u16(key) + _u32(value)
    return result


class AccessToken:
    def __init__(self, app_id, app_key, room_id, user_id, *, now=None, nonce=None):
        if len(app_id) != APP_ID_LENGTH:
            raise ValueError('RTC AppID must contain 24 characters')
        self.app_id = app_id
        self.app_key = app_key
        self.room_id = str(room_id)
        self.user_id = str(user_id)
        self.issued_at = int(time.time() if now is None else now)
        self.nonce = random.randint(1, 99999999) if nonce is None else int(nonce)
        self.expire_at = 0
        self.privileges = OrderedDict()

    def add_privilege(self, privilege, expire_at):
        self.privileges[privilege] = int(expire_at)
        if privilege == PRIV_PUBLISH_STREAM:
            for child in (1, 2, 3):
                self.privileges[child] = int(expire_at)

    def expire_time(self, expire_at):
        self.expire_at = int(expire_at)

    def pack_msg(self):
        return (
            _u32(self.nonce)
            + _u32(self.issued_at)
            + _u32(self.expire_at)
            + _string(self.room_id)
            + _string(self.user_id)
            + _privileges(self.privileges)
        )

    def serialize(self):
        message = self.pack_msg()
        signature = hmac.new(self.app_key.encode(), message, sha256).digest()
        content = _bytes(message) + _bytes(signature)
        return VERSION + self.app_id + base64.b64encode(content).decode()


def parse(raw):
    if len(raw) <= len(VERSION) or not raw.startswith(VERSION):
        return None
    token = AccessToken.__new__(AccessToken)
    token.app_id = raw[len(VERSION):len(VERSION) + APP_ID_LENGTH]
    content = base64.b64decode(raw[len(VERSION) + APP_ID_LENGTH:])

    def take(buf, pos, size):
        return buf[pos:pos + size], pos + size

    packed, offset = take(content, 0, struct.unpack_from('<H', content)[0] + 2)
    signature, _ = take(content, offset, struct.unpack_from('<H', content, offset)[0] + 2)
    msg = packed[2:]
    token.nonce, token.issued_at, token.expire_at = struct.unpack_from('<III', msg)
    pos = 12

    def read_bytes(value, position):
        length = struct.unpack_from('<H', value, position)[0]
        return value[position + 2:position + 2 + length], position + 2 + length

    room, pos = read_bytes(msg, pos)
    user, pos = read_bytes(msg, pos)
    token.room_id = room.decode()
    token.user_id = user.decode()
    count = struct.unpack_from('<H', msg, pos)[0]
    pos += 2
    token.privileges = OrderedDict()
    for _ in range(count):
        key = struct.unpack_from('<H', msg, pos)[0]
        value = struct.unpack_from('<I', msg, pos + 2)[0]
        token.privileges[key] = value
        pos += 6
    token.signature = signature[2:]
    return token
