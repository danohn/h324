import pytest
from h324.al3 import decode, supervisory, Transmitter, SREJ, DRTX


@pytest.mark.parametrize('control_size,sequence', [(1,127),(2,32767)])
def test_supervisory_and_cached_retransmission(control_size, sequence):
    for code in (SREJ, DRTX, 42):
        value = decode(supervisory(sequence,code,control_size),control_size)
        assert not value.information and value.sequence == sequence and value.payload == bytes((code,))
    tx = Transmitter(control_size)
    originals = [tx.send(bytes((i,))*80) for i in range(3)]
    assert tx.receive(supervisory(1,SREJ,control_size)) == originals[1]
    assert tx.vs == 3
    assert tx.receive(supervisory(1,SREJ,control_size)) is None
    assert tx.receive(supervisory(0,SREJ,control_size)) is None
    assert tx.receive(supervisory(2,SREJ,control_size)) == originals[2]


def test_evicted_frame_declined_and_intra_indication():
    tx = Transmitter(max_sdu=100)
    for i in range(20):
        tx.send(bytes((i,))*100)
    assert tx.stored_bytes <= 1024
    assert 0 not in tx.history
    answer = decode(tx.receive(supervisory(0,SREJ)))
    assert not answer.information and answer.sequence == 0 and answer.payload == bytes((DRTX,))
    assert tx.consume_force_intra()
    assert not tx.consume_force_intra()


def test_corruption_reserved_unsolicited_and_future_request():
    tx = Transmitter()
    tx.send(b'picture')
    bad = bytearray(supervisory(0,SREJ)); bad[-1] ^= 1
    for pdu in (bad,supervisory(0,42),supervisory(0,DRTX),supervisory(1,SREJ)):
        assert tx.receive(pdu) is None
    assert tx.vs == 1 and tx.last_srej is None
    assert tx.receive(supervisory(0,SREJ)) == tx.history[0]


def test_sequence_wrap_uses_current_epoch_payload_and_bounded_history():
    tx = Transmitter(send_buffer_size=65536)
    for i in range(132):
        tx.send(i.to_bytes(2,'big'))
    assert tx.vs == 4 and len(tx.history) == 127
    assert decode(tx.receive(supervisory(0,SREJ))).payload == (128).to_bytes(2,'big')
    assert decode(tx.receive(supervisory(1,SREJ))).payload == (129).to_bytes(2,'big')


def test_reject_oversized_sdu_without_advancing_sequence():
    tx = Transmitter(max_sdu=100)
    with pytest.raises(ValueError): tx.send(bytes(101))
    assert tx.vs == 0
    with pytest.raises(ValueError): Transmitter(send_buffer_size=100)


def test_srej_ordering_does_not_block_new_epoch_after_sequence_wrap():
    tx = Transmitter()
    tx.send(b'first')
    assert decode(tx.receive(supervisory(0,SREJ))).payload == b'first'
    for _ in range(127):
        tx.send(b'fill')
    tx.send(b'new epoch')
    assert decode(tx.receive(supervisory(0,SREJ))).payload == b'new epoch'
