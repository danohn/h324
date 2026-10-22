"""H.223 acquisition is opt-in, bounded and never repairs a data frame."""
from h324.h223 import FLAG, Framer, PDU, WireEncoder, bits, encode_pdu
from h324.session import StreamingSession
from h324.worker import Worker
from types import SimpleNamespace
import io
import pytest


def valid(payload=b'control'):
    return encode_pdu(PDU(0, False, payload))


def test_default_framer_keeps_startup_bit_slip_as_error():
    framer = Framer()
    framer.feed_bits(FLAG + FLAG[1:] + FLAG*12 + valid())
    assert framer.errors
    assert not framer.startup_counts


def test_hunt_records_seven_bit_flag_interval_then_locks_after_full_flags():
    framer = Framer(startup_hunt=True)
    stream = '1'*51 + FLAG + FLAG[1:] + FLAG*12 + valid()
    result = []
    for i in range(0, len(stream), 3):
        result.extend(framer.feed_bits(stream[i:i+3]))
    assert [p.information for p in result] == [b'control']
    assert not framer.errors
    assert framer.startup_counts['invalid zero-bit insertion'] == 1
    assert not framer.acquiring
    assert framer.startup_locked_offset is not None
    assert not result[0].discontinuity


def test_post_lock_bad_candidate_same_read_is_strict_error():
    framer = Framer(startup_hunt=True)
    result = framer.feed_bits(FLAG*12 + valid() + '1111110' + FLAG)
    assert len(result) == 1
    assert framer.errors
    assert not framer.startup_counts
    assert not framer.acquiring


def test_first_pdu_requires_control_entry_and_full_flag_train():
    framer = Framer(startup_hunt=True)
    framer.feed_bits(FLAG*4 + valid())
    assert framer.acquiring
    assert framer.startup_counts['startup_flag_train_required'] == 1
    wrong = encode_pdu(PDU(1, False, b'audio'))
    framer.feed_bits(FLAG*12 + wrong)
    assert framer.acquiring
    assert framer.startup_counts['startup_control_pdu_required'] == 1
    assert framer.feed_bits(FLAG*12 + valid())[0].mc == 0
    assert not framer.acquiring


@pytest.mark.parametrize('fill', ['0', '1'])
def test_hunt_exhaustion_is_fatal_and_storage_bounded(fill):
    framer = Framer(startup_hunt=True, max_startup_bits=128)
    assert not framer.feed_bits(fill*129)
    assert framer.startup_timed_out
    assert framer.errors[-1]['error'] == 'startup_acquisition_timeout'
    assert not framer.buffer
    assert not framer.feed_bits(FLAG*12 + valid())


def test_physical_pacer_sends_only_flags_before_release():
    worker = Worker('test', lambda **event: None)
    session = StreamingSession(status_number=1, startup_hunt=True)
    session.start(0)
    worker.session = session
    worker.modem = SimpleNamespace(send=lambda data: None)
    worker.files = {'tx': io.BytesIO()}
    worker.protocol_release_at = 1
    worker._send(216, .1)
    assert worker.files['tx'].getvalue() == b'\x7e'*216
    assert session.link.queued_bytes > 0
    worker._send(216, 1.1)
    assert worker.files['tx'].getvalue()[216:] != b'\x7e'*216


def test_wall_clock_acquisition_timeout_without_received_bits():
    worker = Worker('test', lambda **event: None)
    worker.session = StreamingSession(startup_hunt=True)
    worker.files = {'rx': io.BytesIO()}
    worker.acquisition_deadline = 5
    with pytest.raises(TimeoutError, match='five seconds'):
        worker._feed(b'', 5)


def test_valid_frame_after_bit_deadline_cannot_lock_from_one_large_read():
    framer = Framer(startup_hunt=True, max_startup_bits=128)
    assert not framer.feed_bits(FLAG*16 + valid())
    assert framer.startup_timed_out
    assert framer.startup_locked_offset is None
