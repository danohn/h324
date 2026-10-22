"""V.80 Table 9 duplex decoding and a diagnostic CRC-framed synchronous bitstream.

The test frames are lab instrumentation, not H.223. Bit order on the synchronous
line is least-significant bit first; receive octet phase need not match transmit.
"""
import struct
import zlib

EM = 0x19
TRANSPARENCY = {
    0x5C: b'\x19', 0x76: b'\x99', 0xA0: b'\x11', 0xA1: b'\x13',
    0x5D: b'\x19\x19', 0x77: b'\x99\x99', 0xA2: b'\x11\x11', 0xA3: b'\x13\x13',
    0xA4: b'\x19\x99', 0xA5: b'\x19\x11', 0xA6: b'\x19\x13', 0xA7: b'\x99\x19',
    0xA8: b'\x99\x11', 0xA9: b'\x99\x13', 0xAA: b'\x11\x19', 0xAB: b'\x11\x99',
    0xAC: b'\x11\x13', 0xAD: b'\x13\x19', 0xAE: b'\x13\x99', 0xAF: b'\x13\x11',
}
SINGLE = {v[0]: bytes((EM, k)) for k, v in TRANSPARENCY.items() if len(v) == 1}
STATUS = {0xB0: ('abort', 0), 0xB1: ('flag', 0), 0xB2: ('frame_error', 0),
          0xB4: ('under', 0), 0xB5: ('tover', 0), 0xB6: ('rover', 0),
          0xB8: ('bnum', 2), 0xB9: ('unum', 2), 0xBA: ('eot', 0),
          0xBB: ('esc', 0), 0xBC: ('rrn', 0), 0xBD: ('rtn', 0), 0xBE: ('rate', 2),
          0x62: ('cts_off', 0), 0x63: ('cts_on', 0),
          0x66: ('dcd_off', 0), 0x67: ('dcd_on', 0),
          0x6A: ('ring_off', 0), 0x6B: ('ring_on', 0)}
# Table 10 starts 1200, 2400, then 2400-bps steps.
RATES = dict(zip(range(0x20, 0x2F), [1200, 2400, 4800, 7200, 9600, 12000,
                                              14400, 16800, 19200, 21600, 24000,
                                              26400, 28800, 31200, 33600]))


def shield(data):
    return b''.join(SINGLE.get(b, bytes((b,))) for b in data)


class Decoder:
    """Incremental decoder; unknown controls remain explicit errors, never payload.

    This implements Table 9 for full-duplex transparent access, not V.34 HD nor
    the extended +IBC command language. Those are deliberately rejected.
    """
    def __init__(self):
        self.pending = bytearray()
        self.ended = False
        self.data_octets = 0

    def feed(self, chunk):
        self.pending.extend(chunk)
        data, events = bytearray(), []
        i = 0
        while i < len(self.pending):
            b = self.pending[i]
            if self.ended:
                events.append({'name': 'post_eot', 'hex': bytes(self.pending[i:]).hex()})
                i = len(self.pending)
                break
            if b not in (0x19, 0x99):
                data.append(b)
                i += 1
                continue
            if len(self.pending) - i < 2:
                break
            code = self.pending[i + 1]
            if code in TRANSPARENCY:
                data.extend(TRANSPARENCY[code])
                i += 2
                continue
            name, count = STATUS.get(code, ('unknown', 0))
            if len(self.pending) - i < 2 + count:
                break
            params = bytes(self.pending[i + 2:i + 2 + count])
            event = {'name': name, 'code': code, 'params_hex': params.hex(),
                     'data_octet_offset': self.data_octets+len(data)}
            if name == 'rate':
                event.update(tx_bps=RATES.get(params[0]), rx_bps=RATES.get(params[1]))
            if name in ('bnum', 'unum'):
                event['octets'] = (params[0] >> 1) | ((params[1] >> 1) << 7)
            events.append(event)
            self.ended = name == 'eot'
            i += 2 + count
        del self.pending[:i]
        self.data_octets += len(data)
        return bytes(data), events


MAGIC = bytes.fromhex('d39147ab6ce825b0')
HEADER = struct.Struct('>8sBIH')


def bits(data):
    return ''.join(f'{b:08b}'[::-1] for b in data)


def octets(bit_string):
    return bytes(int(bit_string[i:i + 8][::-1], 2) for i in range(0, len(bit_string) - 7, 8))


def frame(sender, sequence, payload):
    if sender not in (0, 1) or not 0 <= sequence <= 0xFFFFFFFF or len(payload) > 4096:
        raise ValueError('sender 0/1, uint32 sequence and payload <=4096 required')
    body = HEADER.pack(MAGIC, sender, sequence, len(payload)) + payload
    return body + struct.pack('>I', zlib.crc32(body))


class BitFramer:
    """Find CRC-valid test frames at any of eight receive bit phases.

    Idle bits are discarded only outside frames. Data within a frame is never
    repaired or stripped. Each rejected candidate is reported as an error.
    """
    def __init__(self):
        self.buffer = ''
        self.offset = 0
        self.errors = []

    def feed(self, data):
        self.buffer += bits(data)
        output = []
        marker = bits(MAGIC)
        while True:
            i = self.buffer.find(marker)
            if i < 0:
                n = max(0, len(self.buffer) - len(marker) + 1)
                self.buffer = self.buffer[n:]
                self.offset += n
                break
            self.buffer = self.buffer[i:]
            self.offset += i
            if len(self.buffer) < HEADER.size * 8:
                break
            _, sender, sequence, size = HEADER.unpack(octets(self.buffer[:HEADER.size * 8]))
            if sender not in (0, 1) or size > 4096:
                self.errors.append({'error': 'invalid_header', 'bit_offset': self.offset})
                self.buffer = self.buffer[1:]
                self.offset += 1
                continue
            length = HEADER.size + size + 4
            if len(self.buffer) < length * 8:
                break
            raw = octets(self.buffer[:length * 8])
            if zlib.crc32(raw[:-4]) != int.from_bytes(raw[-4:], 'big'):
                self.errors.append({'error': 'crc_mismatch', 'sequence': sequence,
                                    'bit_offset': self.offset})
                self.buffer = self.buffer[1:]
                self.offset += 1
                continue
            output.append({'sender': sender, 'sequence': sequence,
                           'payload': raw[HEADER.size:-4],
                           'bit_offset': self.offset, 'bit_phase': self.offset % 8})
            self.buffer = self.buffer[length * 8:]
            self.offset += length * 8
        return output
