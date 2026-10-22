"""Annex A.2 vectors, timer semantics, and framed-AL1 impairment tests."""
from collections import deque

import pytest

from h324.h223 import (Table, Multiplexer, Demultiplexer, WireEncoder,
                        Framer, al1_encode, al1_decode, crc16)
from h324.srp import (Endpoint, Failed, Frame, decode_frame, encode_command,
                       encode_response)


def independent_fcs(data):
    # MSB polynomial division over the transmitted LSB-first bit order. This is
    # independent of h223.crc16's reflected bit-at-a-time recurrence.
    bits = ''.join(f'{octet:08b}'[::-1] for octet in data)
    remainder = (int(bits, 2) << 16) ^ (0xffff << len(bits))
    while remainder.bit_length() > 16:
        remainder ^= 0x11021 << (remainder.bit_length() - 17)
    return int(f'{remainder ^ 0xffff:016b}'[::-1], 2).to_bytes(2, 'little')


def test_exact_wire_fields_and_independent_v42_fcs():
    for sequence, payload in [(0, b'hello'), (255, bytes(range(256))*8)]:
        frame = encode_command(sequence, payload)
        assert frame[:2] == bytes((249, sequence))
        assert frame[2:-2] == payload
        assert frame[-2:] == independent_fcs(frame[:-2])
        assert crc16(frame) == 0x0f47
        assert decode_frame(frame) == Frame(sequence, payload)
    response = encode_response()
    assert len(response) == 3 and response[0] == 251
    assert response[1:] == independent_fcs(b'\xfb')
    assert decode_frame(response).is_response


@pytest.mark.parametrize('payload', [b'', b'x'*2049])
def test_information_limits(payload):
    with pytest.raises(ValueError):
        encode_command(0, payload)


@pytest.mark.parametrize('frame', [b'', b'\xf9', b'\xf9\x00\x00\x00',
                                  b'\xfb\x00', b'\xfb\x00\x00\x00',
                                  b'\xf9\x00'+b'x'*2051, b'\x00xx'])
def test_invalid_frames_are_ignored_without_ack_or_delivery(frame):
    endpoint = Endpoint()
    result = endpoint.receive(frame, 0)
    assert not result.outgoing and not result.delivered
    assert endpoint.counts['invalid_frames'] == 1


def test_every_single_bit_error_is_detected():
    for original in [encode_command(42, b'control message'), encode_response()]:
        for bit in range(len(original)*8):
            damaged = bytearray(original)
            damaged[bit//8] ^= 1 << (bit % 8)
            with pytest.raises(ValueError):
                decode_frame(damaged)


def test_bounded_queue_stops_and_waits_and_copies_mutable_payload():
    endpoint = Endpoint(max_pending=2, initial_sequence=255)
    value = bytearray(b'first')
    first = endpoint.send(value, 0).outgoing[0]
    value[:] = b'other'
    assert endpoint.send(b'second', 0).outgoing == ()
    with pytest.raises(BufferError):
        endpoint.send(b'third', 0)
    assert endpoint.pending_count == 2
    assert decode_frame(first) == Frame(255, b'first')
    result = endpoint.receive(encode_response(), 1)
    assert result.acknowledged == (255,)
    assert decode_frame(result.outgoing[0]) == Frame(0, b'second')
    assert endpoint.deadline == 4
    endpoint.receive(encode_response(), 2)
    assert endpoint.idle and endpoint.deadline is None


def test_retry_budget_and_terminal_failure_preserve_unconfirmed_work():
    endpoint = Endpoint(timeout=1, max_retries=5)
    original = endpoint.send(b'first', 0).outgoing
    endpoint.send(b'queued', 0)
    assert not endpoint.tick(.999).outgoing
    for second in range(1, 6):
        result = endpoint.tick(second)
        assert result.outgoing == original and result.failure is None
    result = endpoint.tick(6)
    assert not result.outgoing and result.failure == 'retransmission_limit_exhausted'
    assert endpoint.pending_count == 2 and endpoint.deadline is None
    assert endpoint.counts['retransmissions'] == 5
    assert endpoint.tick(100).failure == result.failure
    assert endpoint.receive(encode_response(), 101).failure == result.failure
    with pytest.raises(Failed):
        endpoint.send(b'no restart', 102)


def test_late_scheduler_wakeup_does_not_send_retry_burst():
    endpoint = Endpoint(timeout=1)
    endpoint.send(b'hello', 0)
    assert len(endpoint.tick(100).outgoing) == 1
    assert endpoint.deadline == 101
    assert not endpoint.tick(100).outgoing


def test_duplicate_command_acknowledged_again_but_not_redelivered():
    endpoint = Endpoint()
    command = encode_command(89, b'a')
    assert endpoint.receive(command, 0).delivered == (b'a',)
    duplicate = endpoint.receive(command, 1)
    assert duplicate.delivered == () and duplicate.outgoing == (encode_response(),)
    # Annex A.2 compares only sequence, even if a broken peer changes the body.
    assert not endpoint.receive(encode_command(89, b'b'), 2).delivered
    assert endpoint.receive(encode_command(90, b'b'), 3).delivered == (b'b',)


def test_full_sequence_wrap_and_unchanged_sequence_on_retry():
    sender, receiver = Endpoint(initial_sequence=254), Endpoint()
    forward, reverse = AL1Wire(), AL1Wire()
    for number in range(258):
        now = number*4
        command = sender.send(str(number).encode(), now).outgoing[0]
        assert decode_frame(command).sequence == (254 + number) % 256
        incoming, = forward.transfer(command)
        response = receiver.receive(incoming, now)
        assert response.delivered == (str(number).encode(),)
        if number == 1:
            assert sender.tick(now+3).outgoing == (command,)
            repeated, = forward.transfer(command)
            assert not receiver.receive(repeated, now+3).delivered
        ack, = reverse.transfer(response.outgoing[0])
        assert sender.receive(ack, now+3).acknowledged == (
            (254 + number) % 256,)
    assert sender.idle and receiver.counts['messages_delivered'] == 258


@pytest.mark.parametrize('kwargs', [dict(timeout=0), dict(timeout=float('nan')),
                                   dict(timeout=float('inf')), dict(max_retries=4),
                                   dict(max_pending=0), dict(initial_sequence=256)])
def test_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        Endpoint(**kwargs)


def test_clock_must_be_finite_and_monotonic():
    endpoint = Endpoint()
    endpoint.tick(10)
    for now in [9, float('nan'), float('inf')]:
        with pytest.raises(ValueError):
            endpoint.tick(now)


class AL1Wire:
    """Persistent real H.223 bit framing, segmentation and USB-size chunks."""
    def __init__(self):
        table = Table()  # Only mandated startup table 0 / LCN0.
        self.mux = Multiplexer(table, {0})
        self.demux = Demultiplexer(table, {0}, {0}, max_sdu=2052)
        self.encoder, self.framer = WireEncoder(), Framer()

    def transfer(self, srp_frame):
        information = al1_encode(srp_frame)
        pdus = [self.mux.send(0, information[i:i+37],
                             end_sdu=i+37 >= len(information))
                for i in range(0, len(information), 37)]
        pdus.append(self.mux.finish())
        wire = b''.join(self.encoder.pdu(pdu) for pdu in pdus)
        wire += self.encoder.idle(2)
        received = []
        for offset in range(0, len(wire), 7):
            for pdu in self.framer.feed(wire[offset:offset+7]):
                for sdu in self.demux.feed(pdu):
                    assert sdu.lcn == 0
                    received.append(al1_decode(sdu.payload))
        assert not self.framer.errors and not self.demux.events
        return received


def test_all_responses_lost_over_h223_ends_in_explicit_failure():
    sender, receiver = Endpoint(timeout=1), Endpoint()
    forward, reverse = AL1Wire(), AL1Wire()
    command, = sender.send(b'one application delivery', 0).outgoing
    deliveries = []
    for now in range(6):
        frame, = forward.transfer(command)
        update = receiver.receive(frame, now)
        deliveries.extend(update.delivered)
        # Verify a correctly framed response exists, then deliberately discard it.
        ack, = reverse.transfer(update.outgoing[0])
        assert decode_frame(ack).is_response
        retry = sender.tick(now+1)
        if now < 5:
            command, = retry.outgoing
        else:
            assert retry.failure == 'retransmission_limit_exhausted'
    assert deliveries == [b'one application delivery']
    assert receiver.counts['duplicate_commands'] == 5
    assert sender.pending_count == 1


def test_damaged_response_does_not_acknowledge_or_restart_timer():
    sender = Endpoint(timeout=1)
    original = sender.send(b'keep pending', 0).outgoing
    damaged = encode_response()[:-1] + bytes((encode_response()[-1] ^ 1,))
    update = sender.receive(damaged, .9)
    assert update.events == ('bad_srp_fcs',) and not update.acknowledged
    assert sender.deadline == 1
    assert sender.tick(1).outgoing == original


@pytest.mark.parametrize('impairment', ['lost_command', 'lost_ack', 'corrupt_command',
                                       'corrupt_ack', 'duplicate_command', 'none'])
def test_full_duplex_srp_over_h223_al1_with_impairments(impairment):
    endpoints = [Endpoint(timeout=1, initial_sequence=255), Endpoint(timeout=1)]
    wires = [AL1Wire(), AL1Wire()]
    expected = [[b'A'*2048, b'next A'], [bytes(range(256)), b'next B']]
    received = [[], []]
    queue = deque()
    acknowledgements = [[], []]
    impaired = False

    def emit(source, update):
        received[source].extend(update.delivered)
        acknowledgements[source].extend(update.acknowledged)
        assert update.failure is None
        queue.extend((source, frame) for frame in update.outgoing)

    for source in (0, 1):
        for payload in expected[source]:
            emit(source, endpoints[source].send(payload, 0))
    for step in range(100):
        now = step / 10
        while queue:
            source, frame = queue.popleft()
            is_ack = decode_frame(frame).is_response
            match = ('ack' if is_ack else 'command') in impairment
            action = impairment.split('_')[0]
            apply = source == (1 if is_ack else 0) and match and not impaired
            if apply:
                impaired = True
                if action == 'lost':
                    continue
                if action == 'corrupt':
                    frame = frame[:-1] + bytes((frame[-1] ^ 1,))
            copies = 2 if apply and action == 'duplicate' else 1
            for _ in range(copies):
                for sdu in wires[source].transfer(frame):
                    emit(1-source, endpoints[1-source].receive(sdu, now))
        for source in (0, 1):
            emit(source, endpoints[source].tick(now))
        if not queue and all(endpoint.idle for endpoint in endpoints):
            break
    assert all(endpoint.idle for endpoint in endpoints)
    assert received == [expected[1], expected[0]]
    assert acknowledgements == [[255, 0], [0, 1]]
    assert impaired == (impairment != 'none')
    if impairment.startswith(('lost', 'corrupt')):
        assert endpoints[0].counts['retransmissions'] == 1


def test_unsolicited_ack_is_ignored_but_delayed_ack_ambiguity_is_explicit():
    endpoint = Endpoint()
    assert endpoint.receive(encode_response(), 0).events == ('unsolicited_response',)
    endpoint.send(b'first', 1)
    endpoint.send(b'second', 1)
    first_ack = encode_response()
    assert endpoint.receive(first_ack, 2).acknowledged == (0,)
    # A delayed duplicate of that ACK is byte-for-byte identical to a true ACK
    # for the second command. Annex A.2 has no sequence number to distinguish it.
    assert endpoint.receive(first_ack, 3).acknowledged == (1,)
    assert endpoint.idle
