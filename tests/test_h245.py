"""Finite-profile H.245 tests: wire vectors, state ordering and failure paths."""
import json
from pathlib import Path

import pytest

from h324.h245 import (Endpoint, capabilities, channel_event, decode, encode,
                        multiplex_entries, open_channel, pdu)


def exchange(a=None, b=None):
    a, b = a or Endpoint(1), b or Endpoint(2)
    queue = [(b, x) for x in a.start(0).outgoing] + [(a, x) for x in b.start(0).outgoing]
    transcript = []
    for count in range(100):
        if not queue:
            break
        endpoint, payload = queue.pop(0)
        update = endpoint.receive(payload, count * .01)
        transcript.append((endpoint, decode(payload), update))
        assert update.failure is None
        queue.extend((b if endpoint is a else a, x) for x in update.outgoing)
    assert not queue
    return a, b, transcript


def test_negotiation_both_directions_and_ack_activation():
    a, b, log = exchange()
    assert a.ready and b.ready
    assert a.role == 'slave' and b.role == 'master'
    for endpoint, message, update in log:
        category, (name, _) = message
        for event in update.events:
            if event.kind == 'tx_entry_install':
                assert (category, name) == ('response', 'multiplexEntrySendAck')
            if event.kind == 'rx_entry_install':
                assert (category, name) == ('request', 'multiplexEntrySend')
        if name == 'multiplexEntrySend':
            assert [x.kind for x in update.events] == ['rx_entry_install'] * 2
            assert decode(update.outgoing[0])[1][0] == 'multiplexEntrySendAck'
    assert a.deadline is None and b.deadline is None
    assert a.tx_entries == a.rx_entries == b.tx_entries == b.rx_entries == {1: ((1, None),), 2: ((2, None),)}


@pytest.mark.parametrize('local,remote,expected', [(1, 2, 'slave'), (2, 1, 'master'), (0xFFFFFF, 1, 'slave'), (1, 0xFFFFFF, 'master')])
def test_modulo_master_slave_matches_1996_figure_4(local, remote, expected):
    a = Endpoint(local)
    a.start(0)
    u = a.receive(encode(pdu('request', 'masterSlaveDetermination', {'terminalType': 128, 'statusDeterminationNumber': remote})), 0)
    assert a.role == expected
    assert decode(u.outgoing[0])[1][1]['decision'][0] != expected


@pytest.mark.parametrize('local,remote', [(1, 1), (1, 1 + (1 << 23))])
def test_indeterminate_status_rejected_and_bounded(local, remote):
    a, b = Endpoint(local), Endpoint(remote)
    a.start(0)
    update = a.receive(b.start(0).outgoing[1], 0)
    assert decode(update.outgoing[0])[1][0] == 'masterSlaveDeterminationReject'
    assert not a.msd_acked and not a.ready
    assert a.tick(15).failure.startswith('H.245 timeout')


def test_terminal_type_takes_priority():
    a = Endpoint(1, terminal_type=129)
    a.start(0)
    a.receive(Endpoint(2, terminal_type=128).start(0).outgoing[1], 0)
    assert a.role == 'master'


def test_duplicate_requests_replay_without_reinstall():
    a, b, log = exchange()
    request = next(message for endpoint, message, update in log if endpoint is a and message[1][0] == 'multiplexEntrySend')
    update = a.receive(encode(request), 2)
    assert len(update.outgoing) == 1
    assert not update.events
    assert decode(update.outgoing[0])[1][0] == 'multiplexEntrySendAck'
    ack = encode(pdu('response', 'multiplexEntrySendAck', {'sequenceNumber': 0, 'multiplexTableEntryNumber': [1, 2]}))
    assert not a.receive(ack, 3).events


def test_conflicting_request_sequence_is_failure():
    a, b, _ = exchange()
    altered = multiplex_entries()
    altered[1][1]['multiplexEntryDescriptors'][0]['elementList'][0]['type'] = ('logicalChannelNumber', 2)
    assert a.receive(encode(altered), 2).failure == 'conflicting duplicate H.245 request'


@pytest.mark.parametrize('alter', ['no_mux', 'no_audio_al2', 'no_video', 'alternatives_only', 'too_small_video_sdu', 'receive_only_video'])
def test_incompatible_peer_capabilities_rejected(alter):
    endpoint = Endpoint(1)
    endpoint.start(0)
    message = capabilities()
    value = message[1][1]
    if alter == 'no_mux':
        value.pop('multiplexCapability')
    elif alter == 'no_audio_al2':
        value['multiplexCapability'][1]['audioWithAL2'] = False
    elif alter == 'no_video':
        value['capabilityTable'].pop()
    elif alter == 'alternatives_only':
        value['capabilityDescriptors'][0]['simultaneousCapabilities'] = [[1, 2]]
    elif alter == 'too_small_video_sdu':
        value['multiplexCapability'][1]['maximumAl3SDUSize'] = 1
    else:
        value['capabilityTable'][1]['capability'] = ('receiveVideoCapability', value['capabilityTable'][1]['capability'][1])
    update = endpoint.receive(encode(message), 0)
    assert not endpoint.remote_capabilities
    assert decode(update.outgoing[0])[1][0] == 'terminalCapabilitySetReject'
    assert not endpoint.ready


def test_no_tx_media_before_matching_acks():
    a = Endpoint(1)
    a.start(0)
    assert not a.tx_channels and not a.tx_entries
    update = a.receive(encode(pdu('response', 'multiplexEntrySendAck', {'sequenceNumber': 0, 'multiplexTableEntryNumber': [1, 2]})), 0)
    assert update.failure and not a.tx_entries


def test_video_uses_one_master_bidirectional_request_and_confirm():
    a, b, log = exchange()
    video_requests = [(endpoint, message) for endpoint, message, _ in log
                      if message[0] == 'request' and message[1][0] == 'openLogicalChannel'
                      and message[1][1]['forwardLogicalChannelNumber'] == 2]
    assert len(video_requests) == 1
    assert video_requests[0][0].role == 'slave'  # receiver of master's request
    assert 'reverseLogicalChannelParameters' in video_requests[0][1][1][1]
    confirmations = [(endpoint, message, update) for endpoint, message, update in log
                     if message[1][0] == 'openLogicalChannelConfirm']
    assert len(confirmations) == 1
    assert confirmations[0][0].role == 'slave'
    assert any(e.kind == 'tx_channel_open' and e.channel == 2 for e in confirmations[0][2].events)
    for endpoint, message, update in log:
        if endpoint.role == 'slave' and message[1][0] == 'openLogicalChannel':
            assert not any(e.kind == 'tx_channel_open' and e.channel == 2 for e in update.events)


def pause_before_video_confirm():
    slave, master = Endpoint(1, timeout=2), Endpoint(2, timeout=2)
    queue = [(master, x) for x in slave.start(0).outgoing] + [(slave, x) for x in master.start(0).outgoing]
    for index in range(100):
        endpoint, payload = queue.pop(0)
        if decode(payload)[1][0] == 'openLogicalChannelConfirm':
            return slave, master, payload
        update = endpoint.receive(payload, index * .001)
        assert not update.failure
        queue.extend((master if endpoint is slave else slave, x) for x in update.outgoing)
    raise AssertionError('no video confirmation')


def test_missing_confirm_never_opens_slave_video_transmit_and_times_out():
    slave, master, confirm = pause_before_video_confirm()
    assert 2 in slave.rx_channels
    assert 2 not in slave.tx_channels
    assert not slave.tx_entries
    assert 'video_confirm' in slave.pending
    assert slave.tick(3).failure and not slave.ready
    assert 2 not in slave.tx_channels


def test_confirm_opens_slave_reverse_once_and_duplicate_is_safe():
    slave, master, confirm = pause_before_video_confirm()
    update = slave.receive(confirm, .5)
    assert 2 in slave.tx_channels
    assert any(e.kind == 'tx_channel_open' and e.channel == 2 for e in update.events)
    assert 'video_confirm' not in slave.pending
    assert not slave.receive(confirm, .6).events


def test_unsolicited_confirm_fails_without_opening_transmit():
    endpoint = Endpoint(1)
    endpoint.start(0)
    update = endpoint.receive(encode(pdu('indication', 'openLogicalChannelConfirm', {'forwardLogicalChannelNumber': 2})), .1)
    assert update.failure and not endpoint.tx_channels


@pytest.mark.parametrize('reverse', [None, 3])
def test_bidirectional_ack_requires_expected_reverse_channel(reverse):
    slave, master = Endpoint(1), Endpoint(2)
    queue = [(master, x) for x in slave.start(0).outgoing] + [(slave, x) for x in master.start(0).outgoing]
    for index in range(100):
        endpoint, payload = queue.pop(0)
        message = decode(payload)
        if message[1][0] == 'openLogicalChannelAck' and message[1][1]['forwardLogicalChannelNumber'] == 2:
            assert endpoint is master
            value = message[1][1]
            if reverse is None:
                value.pop('reverseLogicalChannelParameters')
            else:
                value['reverseLogicalChannelParameters']['reverseLogicalChannelNumber'] = reverse
            update = master.receive(encode(message), index * .001)
            assert update.failure and 2 not in master.tx_channels and 2 not in master.rx_channels
            return
        update = endpoint.receive(payload, index * .001)
        assert not update.failure
        queue.extend((master if endpoint is slave else slave, x) for x in update.outgoing)
    raise AssertionError('video ACK not found')


def test_unsupported_channel_and_mux_rejected_without_install():
    endpoint = Endpoint(1)
    endpoint.start(0)
    update = endpoint.receive(encode(open_channel(1)), 0)
    assert decode(update.outgoing[0])[1][0] == 'openLogicalChannelReject'
    assert not endpoint.rx_channels
    update = endpoint.receive(encode(multiplex_entries()), 0)
    assert decode(update.outgoing[0])[1][0] == 'multiplexEntrySendReject'
    assert not endpoint.rx_entries


@pytest.mark.parametrize('name,value', [
    ('terminalCapabilitySetReject', {'sequenceNumber': 0, 'cause': ('unspecified', None)}),
    ('masterSlaveDeterminationReject', {'cause': ('identicalNumbers', None)}),
    ('openLogicalChannelReject', {'forwardLogicalChannelNumber': 1, 'cause': ('dataTypeNotSupported', None)}),
    ('multiplexEntrySendReject', {'sequenceNumber': 0, 'rejectionDescriptions': [{'multiplexTableEntryNumber': 1, 'cause': ('descriptorTooComplex', None)}]}),
])
def test_explicit_peer_reject_terminates_negotiation(name, value):
    endpoint = Endpoint(1)
    endpoint.start(0)
    assert endpoint.receive(encode(pdu('response', name, value)), 0).failure.startswith('peer rejected')
    assert not endpoint.ready


def test_timeout_does_not_activate_media():
    endpoint = Endpoint(1, timeout=2)
    endpoint.start(3)
    assert endpoint.tick(4.99).failure is None
    assert endpoint.tick(5).failure
    assert not endpoint.tx_channels and not endpoint.tx_entries


def test_decode_rejects_trailing_truncated_and_noncanonical():
    value = encode(capabilities())
    for invalid in (b'', value[:-1], value + b'\x00', b'\xff' * 4097):
        with pytest.raises(ValueError):
            decode(invalid)
    with pytest.raises(Exception):
        encode(pdu('request', 'masterSlaveDetermination', {'terminalType': 256, 'statusDeterminationNumber': 0}))


def test_independently_decoded_wire_vectors():
    vectors = json.loads(Path(__file__).parent.joinpath('fixtures/codec-vectors.json').read_text())
    for vector in vectors:
        encoded = bytes.fromhex(vector['hex'])
        message = decode(encoded)
        assert message[1][0] == vector['name']
        assert encode(message) == encoded
        assert vector['tshark_decoded_name'] == vector['name']
