import random
from types import SimpleNamespace

import pytest

from h324.v80 import Decoder, TRANSPARENCY, BitFramer, bits, octets, frame, shield


def test_standard_transparency_vectors_and_fragmentation():
    # Independent Table 9 vector, including the other legal EM prefix (99h).
    encoded = bytes.fromhex('195c997619a019a119a419af')
    decoder = Decoder()
    output = bytearray()
    for b in encoded:
        data, events = decoder.feed(bytes((b,)))
        assert not events
        output.extend(data)
    assert output == bytes.fromhex('1999111319991311')
    assert not decoder.pending
    assert shield(bytes.fromhex('19991113')) == bytes.fromhex('195c197619a019a1')


def test_all_pair_transparency_codes_and_every_split():
    wire = b''.join(bytes((0x19, k)) for k in TRANSPARENCY)
    expected = b''.join(TRANSPARENCY.values())
    for split in range(len(wire) + 1):
        d = Decoder()
        a, ae = d.feed(wire[:split])
        b, be = d.feed(wire[split:])
        assert a + b == expected
        assert not ae + be


def test_all_byte_values_and_statuses_split_at_every_boundary():
    payload = bytes(range(256))
    wire = shield(payload) + bytes.fromhex('19b419be262419b8040219b519ba0d0a4e4f20434152524945520d0a')
    for split in range(len(wire) + 1):
        d = Decoder()
        a, ae = d.feed(wire[:split])
        b, be = d.feed(wire[split:])
        events = ae + be
        assert a + b == payload
        assert [e['name'] for e in events[:5]] == ['under', 'rate', 'bnum', 'tover', 'eot']
        assert events[1]['tx_bps'] == 14400 and events[1]['rx_bps'] == 9600
        assert events[2]['octets'] == 130
        assert events[-1]['name'] == 'post_eot'


def test_unknown_control_is_not_silently_payload():
    d = Decoder()
    data, events = d.feed(b'A\x19\xc1B')
    assert data == b'AB'
    assert events[0]['name'] == 'unknown'


@pytest.mark.parametrize('phase', range(8))
def test_bit_alignment_idle_gaps_and_fragmented_frames(phase):
    payload = bytes(range(256)) + bytes.fromhex('19119913') * 16
    raw = frame(1, 0x123456, payload)
    # Fixed LSB-first byte fixture for 01h followed by 80h.
    assert bits(bytes.fromhex('0180')) == '1000000000000001'
    stream_bits = '1' * (32 + phase) + bits(raw) + '1' * 37 + bits(frame(0, 7, b'after idle')) + '1' * 8
    wire = octets(stream_bits)
    framer = BitFramer()
    output = []
    rng = random.Random(80)
    while wire:
        size = rng.randrange(1, 20)
        output += framer.feed(wire[:size])
        wire = wire[size:]
    assert not framer.errors
    assert len(output) == 2
    assert output[0]['payload'] == payload
    assert output[0]['sender'] == 1 and output[0]['sequence'] == 0x123456
    assert output[0]['bit_phase'] == phase
    assert output[1]['payload'] == b'after idle'


def test_corruption_is_reported_and_later_frame_can_resynchronize():
    broken = bytearray(frame(0, 1, b'corrupt this payload'))
    broken[-5] ^= 1
    d = BitFramer()
    got = d.feed(b'\xff' * 100 + broken + b'\xff' * 32 + frame(1, 2, b'good'))
    assert len(got) == 1 and got[0]['sequence'] == 2
    assert d.errors[0]['error'] == 'crc_mismatch'

