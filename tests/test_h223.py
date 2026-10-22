"""Normative vectors, independent CRC arithmetic, and stream impairment checks."""
import random

import pytest

from h324.h223 import (PDU, FLAG, WireEncoder, Framer, Table, Slot, Repeat,
                        Multiplexer, Demultiplexer, SDU, bits, octets, encode_pdu,
                        hec, crc8, crc16, stuff, unstuff,
                        al1_encode, al1_decode, al2_encode, al2_decode,
                        al3_encode, al3_decode)


def polynomial_crc(data, width, polynomial, initial=0, invert=False):
    # Independent MSB polynomial long division of the time-ordered bit string.
    message = bits(data)
    value = int(message or '0', 2) << width
    value ^= initial << len(message)
    while value.bit_length() > width:
        value ^= polynomial << (value.bit_length() - width - 1)
    if invert:
        value ^= (1 << width) - 1
    return int(f'{value:0{width}b}'[::-1], 2)


def lab_table():
    table = Table()
    table.install(1, [Slot(1)])
    table.install(2, [Slot(2)])
    table.install(3, [Slot(1, 4), Repeat((Slot(0, 1), Slot(2, 2)))])
    return table


def test_all_normative_hec_values_and_pm_not_covered():
    # Table 1/H.223 (03/1996), columns 8..6; independent published vector.
    expected = [0, 5, 7, 2, 3, 6, 4, 1, 6, 3, 1, 4, 5, 0, 2, 7]
    assert [hec(n) for n in range(16)] == expected
    for mc in range(16):
        for pm in (False, True):
            f = Framer()
            assert f.feed_bits(encode_pdu(PDU(mc, pm, b'x'))) == [PDU(mc, pm, b'x')]
    assert encode_pdu(PDU(0, False, b'A')) == bits(bytes.fromhex('7e00417e'))
    assert encode_pdu(PDU(1, True, b'')) == bits(bytes.fromhex('7ea37e'))


def test_zero_insertion_includes_header_and_spans_octets():
    assert encode_pdu(PDU(0, False, b'\xff')) == FLAG + '00000000' + '111110111' + FLAG
    for payload in [bytes(range(256)), b'\xff'*1024, b'\x1f\xf8'*128]:
        wire = stuff(bits(payload))
        assert '111111' not in wire
        assert unstuff(wire) == bits(payload)
    with pytest.raises(ValueError):
        unstuff('11111')
    with pytest.raises(ValueError):
        unstuff('1111110')


@pytest.mark.parametrize('phase', range(8))
@pytest.mark.parametrize('chunk', [1, 7, 31])
def test_arbitrary_bit_alignment_and_usb_fragmentation(phase, chunk):
    pdus = [PDU(1, False, bytes(range(256))), PDU(2, True, b'\xff'*23), PDU(0, True, b'')]
    encoder = WireEncoder()
    stream = b''.join([encoder.idle(16), *(encoder.pdu(p) for p in pdus), encoder.idle(2)])
    shifted = '1'*phase + bits(stream) + encoder.pending + FLAG*2
    shifted = shifted[:len(shifted)//8*8]
    framer, found = Framer(), []
    raw = octets(shifted)
    for i in range(0, len(raw), chunk):
        found.extend(framer.feed(raw[i:i+chunk]))
    assert found == pdus
    assert found[0].bit_offset % 8 == phase
    assert not framer.errors


def test_shared_flags_idle_flags_and_optional_information_xor():
    a, b = PDU(5, False, b'\x7e\xff\x19'), PDU(0, True, b'')
    stream = FLAG*16 + encode_pdu(a, True) + encode_pdu(b, True)[8:] + FLAG*3
    f = Framer(xor_information=True)
    assert f.feed_bits(stream) == [a, b]
    assert f.counts['idle_flags'] >= 18


def test_corrupt_header_and_non_octet_frame_recover_at_next_flag():
    bad = FLAG + stuff(bits(b'\x20wrong')) + FLAG  # MC0, invalid HEC1
    malformed = FLAG + '010' + FLAG
    good = PDU(0, False, b'next')
    f = Framer()
    found = f.feed_bits(bad + malformed + encode_pdu(good))
    assert found == [good]
    assert found[0].discontinuity
    assert f.counts['bad_hec'] == f.counts['non_octet_pdu'] == 1


def test_inserted_deleted_bits_and_bit_error_do_not_prevent_resync():
    original = encode_pdu(PDU(2, False, b'\xff\x7e\xa5'*20))
    next_pdu = PDU(0, True, b'recovered')
    for damaged in (original[:19] + original[20:], original[:19] + '0' + original[19:],
                    original[:19] + str(1-int(original[19])) + original[20:]):
        f = Framer()
        found = f.feed_bits(damaged + FLAG*2 + encode_pdu(next_pdu))
        assert found[-1] == next_pdu
        # A valid-looking payload bit error may pass the MUX header; AL CRC must catch it.


def test_decoder_and_reassembly_have_explicit_memory_bounds():
    f = Framer(max_information=8)
    f.feed_bits(FLAG + '0'*100000)
    assert len(f.buffer) <= f.max_bits and f.counts['oversized_pdu'] == 1
    f.feed_bits(FLAG + '1'*100000)
    assert len(f.buffer) <= f.max_bits
    d = Demultiplexer(Table(), {0}, {0}, max_sdu=8)
    assert d.feed(PDU(0, False, b'123456789')) == []
    assert not d.partial and 0 in d.blocked
    assert d.feed(PDU(0, True, b'')) == []
    assert not d.blocked
    assert d.feed(PDU(0, False, b'ok')) == []
    assert d.feed(PDU(0, True, b'')) == [SDU(0, b'ok')]


def test_pm_finishes_previous_channel_even_when_current_code_changes():
    table = lab_table()
    mux = Multiplexer(table, {0, 2})
    d = Demultiplexer(table, {0, 2}, {0, 1, 2})
    assert d.feed(mux.send(0, b'con')) == []
    assert d.feed(mux.send(0, b'trol', end_sdu=True)) == []
    # PM on audio PDU completes the preceding control SDU; audio is nonsegmentable.
    assert d.feed(mux.send(1, b'audio')) == [SDU(0, b'control'), SDU(1, b'audio')]
    assert d.feed(mux.send(2, b'video', end_sdu=True)) == []
    assert d.feed(mux.finish()) == [SDU(2, b'video')]
    assert mux.finish() is None


def test_normative_table2_row5_layout_and_figure5_boundary():
    table = Table()
    table.install(5, [Slot(1, 4), Repeat((Slot(2, 1), Slot(3, 2)))])
    assert table.layout(5, 9) == [(1, 4), (2, 1), (3, 2), (2, 1), (3, 1)]
    d = Demultiplexer(table, {0, 2, 3}, {0, 1, 2, 3})
    assert d.feed(PDU(5, False, b'aaaadVVdV')) == [SDU(1, b'aaaa')]
    assert d.feed(PDU(0, True, b'C')) == [SDU(3, b'VVV')]
    assert d.partial[2] == b'dd'
    assert d.feed(PDU(0, True, b'')) == [SDU(0, b'C')]


def test_inactive_entries_closed_channels_abort_and_missing_pdu():
    d = Demultiplexer(Table(), {0}, {0})
    assert d.feed(PDU(0, False, b'incomplete')) == []
    assert d.feed(PDU(0, False, b'')) == []
    assert d.counts['mux_abort'] == 1 and not d.partial
    d.feed(PDU(0, False, b'first'))
    assert d.feed(PDU(0, True, b'tail', discontinuity=True)) == []
    assert d.feed(PDU(0, True, b'')) == []  # Dropped through the next known boundary.
    assert d.feed(PDU(1, False, b'unnegotiated')) == []
    assert d.counts['inactive_entry'] == 1
    table = lab_table()
    d = Demultiplexer(table, {0, 2}, {0})
    assert d.feed(PDU(1, False, b'closed')) == []
    assert d.counts['closed_channel'] == 1


def test_table_zero_is_immutable_and_bad_descriptors_rejected():
    t = Table()
    with pytest.raises(ValueError):
        t.install(0, [Slot(1)])
    with pytest.raises(ValueError):
        t.install(1, [Slot(1), Slot(2)])
    with pytest.raises(ValueError):
        t.install(1, [Repeat((Slot(2),), 2)])
    t.install(1, [Slot(1, 4)])
    with pytest.raises(ValueError, match='pattern_exhausted'):
        t.layout(1, 5)


def test_crcs_against_independent_polynomial_division_and_external_vectors():
    assert crc16(b'123456789') == 0x906E  # CRC-16/IBM-SDLC check value.
    # First entries in upstream Wireshark h223_al2_crc8bit's independent table.
    assert [crc8(bytes((i,))) for i in range(4)] == [0, 0x91, 0xE3, 0x72]
    for payload in [b'', b'123456789', bytes(range(256)), random.Random(223).randbytes(1024)]:
        assert crc8(payload) == polynomial_crc(payload, 8, 0x107)
        assert crc16(payload) == polynomial_crc(payload, 16, 0x11021, 0xFFFF, True)


def test_adaptation_layers_preserve_octets_and_reject_corruption():
    payload = bytes(range(256))
    assert al1_decode(al1_encode(payload)) == payload
    for seq in (None, 0, 255):
        p = al2_encode(payload, seq)
        assert al2_decode(p, seq is not None) == (payload, seq)
        damaged = p[:10] + bytes((p[10] ^ 1,)) + p[11:]
        with pytest.raises(ValueError, match='bad_al2_crc'):
            al2_decode(damaged, seq is not None)
    for size, sequence in [(0, None), (1, 0), (1, 127), (2, 0), (2, 32767), (2, 0x1234)]:
        p = al3_encode(payload, sequence, size)
        assert al3_decode(p, size) == (payload, sequence)
        with pytest.raises(ValueError, match='bad_al3_crc'):
            al3_decode(p[:-1] + bytes((p[-1] ^ 1,)), size)
    with pytest.raises(ValueError):
        al3_encode(payload, 128, 1)
