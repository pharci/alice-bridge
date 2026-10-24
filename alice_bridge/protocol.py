"""Minimal bounded Quasar TCP / protobuf codec for Station Mini 2."""
from __future__ import annotations

import json
import socket
import struct
from collections import defaultdict

MAX_FRAME = 4 * 1024 * 1024

def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = 0
    for shift in range(0, 70, 7):
        if pos >= len(data):
            raise ValueError('truncated protobuf varint')
        byte = data[pos]
        pos += 1
        if shift == 63 and byte > 1:
            raise ValueError('protobuf varint overflow')
        value |= (byte & 127) << shift
        if byte < 128:
            return value, pos
    raise ValueError('protobuf varint overflow')

def fields(data: bytes) -> dict[int, list[int | bytes]]:
    result = defaultdict(list)
    pos = 0
    while pos < len(data):
        tag, pos = read_varint(data, pos)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ValueError('zero protobuf field number')
        if wire == 0:
            value, pos = read_varint(data, pos)
        elif wire == 2:
            size, pos = read_varint(data, pos)
            if size > len(data) - pos:
                raise ValueError('truncated protobuf bytes')
            value = data[pos:pos + size]
            pos += size
        elif wire in (1, 5):
            size = 8 if wire == 1 else 4
            if size > len(data) - pos:
                raise ValueError('truncated fixed field')
            value = data[pos:pos + size]
            pos += size
        else:
            raise ValueError(f'unsupported protobuf wire type {wire}')
        result[number].append(value)
    return dict(result)

def first(message: dict, number: int, default=None):
    values = message.get(number)
    return values[0] if values else default

def child(message: dict, number: int) -> dict:
    value = first(message, number, b'')
    return fields(value) if isinstance(value, bytes) else {}

def text(message: dict, number: int, default: str = '') -> str:
    value = first(message, number, b'')
    return value.decode('utf-8', errors='strict') if isinstance(value, bytes) else default

def pack_varint(value: int) -> bytes:
    if value < 0:
        raise ValueError('negative protobuf varint')
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)

def pack_field(number: int, value: int | bytes | str) -> bytes:
    if isinstance(value, int):
        return pack_varint(number << 3) + pack_varint(value)
    if isinstance(value, str):
        value = value.encode('utf-8')
    return pack_varint((number << 3) | 2) + pack_varint(len(value)) + value

def frame(payload: bytes) -> bytes:
    if len(payload) > MAX_FRAME:
        raise ValueError('oversized Quasar frame')
    return struct.pack('<I', len(payload)) + payload

class FrameReader:
    def __init__(self, sock: socket.socket):
        self.sock = sock
        self.buffer = bytearray()

    def receive(self) -> bytes:
        while True:
            if len(self.buffer) >= 4:
                size = struct.unpack_from('<I', self.buffer)[0]
                if size > MAX_FRAME:
                    raise ValueError('oversized Quasar frame')
                if len(self.buffer) >= 4 + size:
                    result = bytes(self.buffer[4:4 + size])
                    del self.buffer[:4 + size]
                    return result
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError('Quasar connection closed')
            self.buffer.extend(chunk)

def alice_request(payload: bytes) -> dict | None:
    root = fields(payload)
    if first(root, 253) != 210:
        return None
    remoting = child(root, 252)
    if text(remoting, 7) != 'AliceCapability':
        return None
    event = child(remoting, 18)
    if first(event, 1) != 2:
        return None
    request = child(event, 3)
    body = json.loads(text(request, 2) or '{}')
    return {'request_id': text(request, 1), 'body': body}

def alice_state(payload: bytes) -> dict | None:
    root = fields(payload)
    if first(root, 253) != 3:
        return None
    return child(root, 252)

def native_response(state: dict) -> dict | None:
    """Observed AliceState.ResponseState.raw_response JSON, before streamed text."""
    raw = text(child(state, 3), 5)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(value, dict) or not isinstance(value.get('response'), dict):
        return None
    return value

# These names describe speech, not user commands. Every other directive is
# conservatively left with the native engine, including previously unseen ones.
SPEECH_DIRECTIVES = frozenset({
    'tts_play_placeholder', 'tts_speak', 'speak', 'speechkit.speak',
    'tts.playplaceholder',
})

def action_directives(response: dict) -> list[str]:
    directives = response.get('response', {}).get('directives', [])
    if not isinstance(directives, list):
        return ['unknown_directives']
    actions = []
    for directive in directives:
        if not isinstance(directive, dict):
            actions.append('unknown_directive')
            continue
        name = directive.get('name') or directive.get('sub_name') or ''
        if not isinstance(name, str) or name.lower() not in SPEECH_DIRECTIVES:
            actions.append(name or 'unnamed_directive')
    return actions
