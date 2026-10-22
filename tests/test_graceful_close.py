"""End-session negotiation through real PER/SRP/H.223 and delayed wire data."""
from collections import deque
from types import SimpleNamespace
import pytest
from h324.h245 import decode, encode, pdu
from h324.session import StreamingSession
from h324.v80 import shield
from h324.worker import Worker
from h324.worker import Cancelled
from h324.h223 import PDU, FLAG, Slot, encode_pdu, octets
from h324.srp import encode_command
import io


class DelayedPair:
    def __init__(self):
        self.now = 0
        self.events = [[], []]
        self.endpoints = [StreamingSession(lambda _i=i, **e: self.events[_i].append(e),
            status_number=i+1, drain_allowance=.6) for i in range(2)]
        self.wire = [deque(), deque()]
        for session in self.endpoints:
            session.start(0)
        for _ in range(1250):
            self.step()
            if all(s.ready for s in self.endpoints):
                break
        assert all(s.ready for s in self.endpoints)

    def step(self):
        self.now += .02
        for index, session in enumerate(self.endpoints):
            session.tick(self.now)
            self.wire[1-index].append((self.now+.5, shield(session.link.pull(36))))
        for index, incoming in enumerate(self.wire):
            while incoming and incoming[0][0] <= self.now:
                self.endpoints[index].feed(incoming.popleft()[1], self.now)
        for session in self.endpoints:
            assert session.failure is None, session.failure
            assert not session.link.framer.errors
            assert not session.link.demux.events


@pytest.mark.parametrize('initiator', [0, 1])
def test_both_channel_owners_end_without_truncating_admitted_video(initiator):
    pair = DelayedPair()
    picture = b'\0\0\x80'+bytes(range(100))*3
    for session in pair.endpoints:
        session.start_live(pair.now)
        assert session.send_live(2, picture, pair.now)
        assert session.send_live(1, bytes(24), pair.now)
    sender = pair.endpoints[initiator]
    sender.begin_close(pair.now)
    assert not sender.ready
    assert pair.events[initiator][-1]['state'] == 'hanging_up'
    for _ in range(450):
        pair.step()
        if all(s.close_ready for s in pair.endpoints):
            break
    assert all(s.close_ready for s in pair.endpoints)
    assert all(s.close_outcome != 'timeout' for s in pair.endpoints), [s.summary()['close'] for s in pair.endpoints]
    assert all(s.h245.closing for s in pair.endpoints)
    assert any(s.h245.end_received for s in pair.endpoints)
    for events in pair.events:
        received = b''.join(e['payload'] for e in events if e['event'] == 'media' and e['lcn'] == 2)
        assert received == picture


def test_nonresponding_peer_has_explicit_bounded_close_fallback():
    pair = DelayedPair()
    sender = pair.endpoints[0]
    sender.begin_close(pair.now, timeout=2)
    before = pair.now
    # Drain local bytes while deliberately withholding all peer responses.
    for _ in range(110):
        pair.now += .02
        sender.tick(pair.now)
        sender.link.pull(36)
        if sender.close_ready:
            break
    assert sender.close_ready and sender.close_outcome == 'timeout'
    assert pair.now-before < 2.1


def test_simultaneous_close_handles_crossed_bidirectional_close_and_request_ack():
    pair = DelayedPair()
    for session in pair.endpoints:
        session.begin_close(pair.now)
    for _ in range(450):
        pair.step()
        if all(s.close_ready for s in pair.endpoints):
            break
    assert all(s.close_ready and s.close_outcome != 'timeout' for s in pair.endpoints)
    assert all(s.h245.end_sent and s.h245.end_received for s in pair.endpoints)


def test_close_rejects_nonfinite_clock():
    with pytest.raises(ValueError, match='Close timeout'):
        StreamingSession().begin_close(float('nan'))


def test_end_session_is_canonical_and_stops_further_h245_transmission():
    pair = DelayedPair()
    endpoint = pair.endpoints[0].h245
    update = endpoint.end_session()
    assert decode(update.outgoing[0]) == pdu('command', 'endSessionCommand', ('disconnect', None))
    assert endpoint.end_session().outgoing == ()
    incoming = encode(pdu('request', 'closeLogicalChannel',
                          {'forwardLogicalChannelNumber': 1, 'source': ('user', None)}))
    assert endpoint.receive(incoming, pair.now).outgoing == ()
    assert endpoint.tick(pair.now+100).outgoing == ()


def test_worker_announces_close_before_rejecting_stale_queued_media():
    events = []
    worker = Worker('test', lambda **event: events.append(event))
    worker.session = StreamingSession(worker._session_event)
    worker.modem = SimpleNamespace()
    worker.state = 'connected'
    worker._command(dict(op='hangup'))
    assert worker.session.closing
    assert worker.state == 'hanging_up'
    for _ in range(250):
        worker._command(dict(op='live_chunk', payload='invalid but obsolete'))
    assert worker.stale_media_commands == 250
    assert not any(e['event'] == 'error' for e in events)
    worker._command(dict(op='shutdown'))
    assert worker.shutdown_after_close and not worker.shutdown_requested


@pytest.mark.parametrize('timeout', [0, -1, float('inf'), float('nan')])
def test_close_timeout_is_finite_and_positive(timeout):
    with pytest.raises(ValueError, match='Close timeout'):
        StreamingSession().begin_close(0, timeout=timeout)


def end_bits():
    message = encode(pdu('command', 'endSessionCommand', ('disconnect', None)))
    return (encode_pdu(PDU(0, False, encode_command(0, message)))+
            encode_pdu(PDU(0, True, b'')))


def wire(value):
    return shield(octets(value+'0'*((-len(value)) % 8)))


def receive_worker():
    worker = Worker('test', lambda **e: None)
    worker.session = StreamingSession(worker._session_event)
    worker.session.start(0)
    worker.files = {'rx': io.BytesIO()}
    return worker


@pytest.mark.parametrize('terminal', [b'', b'\x19\xba', b'\x19\x66'])
def test_only_framing_tail_after_valid_received_end_is_recorded(terminal):
    worker = receive_worker()
    data = wire(end_bits()+FLAG+'001111111111')+terminal
    if terminal:
        with pytest.raises(Cancelled):
            worker._feed(data, 1)
    else:
        worker._feed(data, 1)
    session = worker.session
    assert session.h245.end_received
    assert session.termination_tail_errors == 1
    assert session.link.framer.errors[0]['error'] == 'abort_sequence'
    assert session.link.framer.first_error_bit_offset >= session.end_received_bit_boundary


@pytest.mark.parametrize('terminal', [b'', b'\x19\xba', b'\x19\x66'])
def test_earlier_corruption_remains_fatal_even_with_end_and_carrier_status(terminal):
    worker = receive_worker()
    corrupt = FLAG+'001111111111'+FLAG+encode_pdu(PDU(0, True, b''))
    with pytest.raises(RuntimeError, match='framing|reassembly'):
        worker._feed(wire(corrupt+end_bits())+terminal, 1)
    assert worker.session.link.framer.error_count >= 1
    assert worker.session.termination_tail_errors == 0


def test_under_and_codec_crc_errors_remain_fatal_after_received_end():
    worker = receive_worker()
    with pytest.raises(RuntimeError, match='underrun'):
        worker._feed(wire(end_bits())+b'\x19\xb4', 1)
    worker = receive_worker()
    worker.session.link.rx_table.install(1, [Slot(1)])
    worker.session.link.demux.open_channels.add(1)
    bad_audio = encode_pdu(PDU(1, False, bytes(24)+b'\xff'))
    with pytest.raises(RuntimeError, match='bad_al2_crc'):
        worker._feed(wire(end_bits()+bad_audio), 1)


@pytest.mark.parametrize('status', [b'\x19\xba', b'\x19\x66'])
def test_carrier_end_before_valid_end_cannot_enable_tail_exception(status):
    worker = receive_worker()
    data = wire(FLAG+'001111111111')+status+wire(end_bits()+FLAG+'001111111111')
    with pytest.raises(RuntimeError, match='framing|reassembly'):
        worker._feed(data, 1)
    assert worker.session.termination_tail_errors == 0


def test_dcd_off_before_end_in_same_read_cannot_reclassify_later_corruption():
    worker = receive_worker()
    with pytest.raises(RuntimeError, match='framing|reassembly'):
        worker._feed(b'\x19\x66'+wire(end_bits()+FLAG+'001111111111'), 1)
    assert worker.session.h245.end_received
    assert worker.session.termination_tail_errors == 0


def test_call_cannot_start_new_media_after_terminal_state_without_active_sender():
    from h324 import Call, H324Error
    modem = SimpleNamespace(_active=True)
    call = Call(modem)
    modem._call = call
    call._disconnected('peer is hanging up')
    with pytest.raises(H324Error, match='hanging up'):
        call._require_connected()


@pytest.mark.parametrize('write_delay', [0, 3])
def test_slow_responsive_at_commands_share_one_cleanup_deadline(monkeypatch, write_delay):
    import h324.worker as module
    from h324.lifecycle import CLEANUP_BUDGET, SERIAL_WRITE_TIMEOUT, API_HANGUP_TIMEOUT, SESSION_CLOSE_TIMEOUT
    clock = SimpleNamespace(now=0.0)
    monkeypatch.setattr(module.time, 'monotonic', lambda: clock.now)
    monkeypatch.setattr(module.time, 'sleep', lambda seconds: setattr(clock, 'now', clock.now+seconds))
    events, records = [], []
    worker = Worker('test', lambda **event: events.append(event))
    class SlowResponsive(module.ResponsiveModem):
        def __init__(self):
            self.check_cancel = worker._check_cancel
            self.name = 'synthetic'
            self.state = 'data'
            self.logfile = None
            self.port = SimpleNamespace(closed=False, rtscts=True, xonxoff=False, dtr=True,
                                        close=lambda: setattr(self.port, 'closed', True))
            self.sent = []
        def send(self, data):
            self.sent.append(data)
            if data.startswith(b'AT'):
                clock.now += write_delay
            self.response_at = clock.now+3.5  # Each response meets its 4s AT limit.
        def lines(self):
            return ['OK'] if clock.now >= self.response_at else []
        def read(self): return b''
        def log(self, name, **fields): records.append((name, fields))
        def line_status(self): return {}
    modem = worker.modem = SlowResponsive()
    worker._cleanup()
    assert clock.now <= CLEANUP_BUDGET+write_delay+.021
    if write_delay:
        assert clock.now > CLEANUP_BUDGET  # Exactly one in-flight write can overrun.
    assert modem.port.closed and modem.port.dtr is False
    assert modem.restore_failed and modem.cleanup_ok is False
    assert any('cleanup budget' in str(fields) for _, fields in records)
    result = next(e for e in events if e['event'] == 'cleanup')
    assert result['cleanup_ok'] is False
    assert result['cleanup_timed_out'] is True
    assert result['error']
    assert len([data for data in modem.sent if data.startswith(b'AT')]) < 11
    assert API_HANGUP_TIMEOUT > SESSION_CLOSE_TIMEOUT+CLEANUP_BUDGET+SERIAL_WRITE_TIMEOUT


def test_first_error_position_survives_bounded_history_rollover():
    from h324.h223 import Framer
    framer = Framer()
    for _ in range(300):
        framer.feed_bits(FLAG+'001111111111'+FLAG)
    assert len(framer.errors) == 256
    assert framer.error_count >= 300
    assert framer.first_error_bit_offset < framer.errors[0]['bit_offset']


@pytest.mark.parametrize('initiator', [0, 1])
def test_first_ready_endpoint_stops_and_carrier_tail_is_safe_at_the_other(initiator):
    pair = DelayedPair()
    pair.endpoints[initiator].begin_close(pair.now)
    for _ in range(450):
        pair.step()
        if any(s.close_ready for s in pair.endpoints):
            break
    stopped = next(i for i,s in enumerate(pair.endpoints) if s.close_ready)
    later = pair.endpoints[1-stopped]
    assert later.h245.end_received  # No dependence on continued writes by stopped peer.
    worker = Worker('test', lambda **e: None)
    worker.session = later
    worker.files = {'rx': io.BytesIO()}
    worker._feed(wire(FLAG+'001111111111'), pair.now+.01)
    assert later.termination_tail_errors >= 1
    assert later.failure is None
