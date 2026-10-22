"""The desktop endpoints use independent state and real protocol encoding."""
import base64
import io
import json
from pathlib import Path
import queue
from types import SimpleNamespace

import pytest

from h324.session import StreamingSession
from h324.worker import Worker, Cancelled, read_commands
from h324.media import MediaSDU, load_fixture
from h324.v80 import shield


class Pair:
    def __init__(self):
        self.events = [[], []]
        self.endpoints = [StreamingSession(lambda _i=i, **event: self.events[_i].append(event),
                                          status_number=i + 1) for i in range(2)]
        self.now = 0
        for s in self.endpoints:
            s.start(0)
        self.pump(4)
        assert all(s.ready for s in self.endpoints)

    def pump(self, seconds):
        for _ in range(round(seconds / .02)):
            self.now += .02
            for s in self.endpoints:
                s.tick(self.now)
            outgoing = [s.link.pull(36) for s in self.endpoints]
            for index in range(2):
                self.endpoints[1-index].feed(shield(outgoing[index]), self.now)
            assert all(s.failure is None for s in self.endpoints)

    def received(self, endpoint, lcn):
        return [e['payload'] for e in self.events[endpoint] if e['event'] == 'media' and e['lcn'] == lcn]


def video_source(marker, count, step=40):
    # These tests exercise transport sequencing, not H.263 decoding. Every SDU
    # has distinct content and no receiver is supplied either source list.
    return [MediaSDU('h263', 2, i*step, step, bytes((marker, i % 256)) * 8)
            for i in range(count)]


def test_independent_sources_different_duration_wrap_and_repeat():
    pair = Pair()
    a = video_source(0x51, 150)
    b = video_source(0xA2, 57, 60)
    pair.endpoints[0].send_media(a, 6000, pair.now, 'A')
    pair.endpoints[1].send_media(b, 3420, pair.now, 'B')
    pair.pump(7)
    assert pair.received(1, 2) == [s.payload for s in a]
    assert pair.received(0, 2) == [s.payload for s in b]
    assert all(s.ready and not s.media_active for s in pair.endpoints)
    assert pair.endpoints[1].video_next == 150 % 128
    assert pair.endpoints[0].video_next == 57
    # Idle continues to carry flags and keeps negotiated channels available.
    pair.pump(2)
    again = video_source(0xD4, 140)
    pair.endpoints[0].send_media(again, 5600, pair.now, 'Again')
    pair.pump(7)
    assert pair.received(1, 2) == [s.payload for s in a + again]
    assert pair.endpoints[1].video_next == 290 % 128
    assert len([e for e in pair.events[0] if e['event'] == 'media_finished']) == 2
    assert pair.endpoints[1].source is None  # Receiving needs no selected local file.


def test_actual_fixture_payloads_are_received_without_oracle():
    pair = Pair()
    fixture = load_fixture(Path(__file__).parent / 'fixtures/media')
    pair.endpoints[0].send_media(fixture, 6000, pair.now, 'Fixture')
    pair.pump(7)
    for lcn in (1, 2):
        assert pair.received(1, lcn) == [s.payload for s in fixture if s.lcn == lcn]
    assert pair.endpoints[1].counts['audio_frames'] == 200
    assert pair.endpoints[1].counts['video_frames'] == 12
    assert pair.received(0, 1) == []


def test_pause_resume_and_stop_drain_complete_fragmented_sdus():
    pair = Pair()
    source = [MediaSDU('h263', 2, i*60, 60, bytes((i,)) * 96) for i in range(20)]
    sender = pair.endpoints[0]
    sender.send_media(source, 1200, pair.now)
    pair.pump(.4)
    sender.pause(pair.now)
    position = sender.media_position(pair.now)
    pair.pump(.6)
    assert sender.media_position(pair.now) == position
    cursor = sender.cursor
    sender.resume(pair.now)
    pair.pump(.2)
    assert sender.cursor > cursor
    sender.stop_media(pair.now)
    # A complete already queued SDU is drained, so the next file cannot be
    # concatenated into an abandoned SAR partial buffer.
    pair.pump(1)
    before = len(pair.received(1, 2))
    assert before < len(source)
    sender.send_media(video_source(0x99, 5), 200, pair.now)
    pair.pump(1)
    assert len(pair.received(1, 2)) == before + 5
    assert not pair.endpoints[1].link.demux.events


def test_session_refuses_unnegotiated_unsorted_and_concurrent_sources():
    fresh = StreamingSession()
    with pytest.raises(RuntimeError, match='connected'):
        fresh.send_media(video_source(1, 1), 40, 0)
    pair = Pair()
    with pytest.raises(ValueError, match='unsorted'):
        pair.endpoints[0].send_media(list(reversed(video_source(1, 2))), 80, pair.now)
    pair.endpoints[0].send_media(video_source(1, 1), 40, pair.now)
    with pytest.raises(RuntimeError, match='already'):
        pair.endpoints[0].send_media(video_source(2, 1), 40, pair.now)


def test_stdin_reader_rejects_invalid_input_and_closes_on_eof():
    inbox = queue.Queue()
    read_commands(io.StringIO('nonsense\n[]\n{"op":"hangup"}\n'), inbox)
    commands = [inbox.get_nowait() for _ in range(4)]
    assert [c['op'] for c in commands] == ['_invalid', '_invalid', 'hangup', 'shutdown']


def test_connect_parser_preserves_binary_following_connect(tmp_path, monkeypatch):
    events = []
    worker = Worker('1001', lambda **event: events.append(event))
    sent = []
    worker.modem = SimpleNamespace(state='dialling', log=lambda *a, **kw: None, send=sent.append)
    worker.session = StreamingSession(worker._session_event, status_number=1)
    worker.files = {name: io.BytesIO() for name in ('audio', 'video', 'rx', 'tx')}
    # A complete V.80 rate response can share the USB read containing CONNECT.
    worker._negotiate(b'\r\n+MCR: V32B\r\nCONNECT 14400\r\n\x19\xbe\x26', 10)
    assert worker.connected_at == 10
    assert worker.session.started
    assert bytes(worker.session.link.decoder.pending) == b'\x19\xbe\x26'
    worker._feed(b'\x26', 10.02)
    assert worker.rate == 14400
    assert sent[0] == b'\x19\xb0'
    assert len(worker.files['tx'].getvalue()) == 216
    assert worker.state == 'negotiating'


def test_unknown_rate_stays_unknown_and_remote_eot_closes():
    events = []
    worker = Worker('1001', lambda **event: events.append(event))
    worker.session = StreamingSession(worker._session_event, status_number=1)
    worker.files = {'rx': io.BytesIO()}
    worker._feed(b'\x19\xbe\x00\x00', 0)
    assert worker.rate is None
    assert any('unspecified' in e.get('message', '') for e in events)
    with pytest.raises(Cancelled):
        worker._feed(b'\x19\xba', .1)


def test_setup_cancel_and_cleanup_release_the_selected_modem(tmp_path, monkeypatch):
    events = []
    worker = Worker('1002', lambda **event: events.append(event))
    worker.inbox.put({'op': 'shutdown'})
    with pytest.raises(Cancelled):
        worker._check_cancel()
    assert worker.shutdown_requested
    worker.directory = tmp_path
    modem = SimpleNamespace(state='data', send=lambda data: None)
    worker.modem = modem
    worker.session = StreamingSession()
    streams = [io.BytesIO(), io.BytesIO()]
    worker.files = dict(audio=streams[0], video=streams[1])
    restored = []
    def restore(m):
        restored.append(m)
        m.cleanup_ok = True
    monkeypatch.setattr('h324.worker.restore_close', restore)
    worker._cleanup()
    assert restored == [modem]
    assert worker.modem is None and worker.session is None
    assert all(s.closed for s in streams)
    assert worker.state == 'idle'
    assert json.loads((tmp_path / 'results.json').read_text())['cleanup_ok']
    assert any(e.get('state') == 'hanging_up' for e in events)


def test_received_media_ipc_is_base64_and_saved_to_worker_log():
    events = []
    worker = Worker('1001', lambda **event: events.append(event))
    stream = io.BytesIO()
    worker.files = {'video': stream}
    worker._session_event(event='media', lcn=2, payload=b'\0\0\x80\xff')
    assert stream.getvalue() == b'\0\0\x80\xff'
    assert base64.b64decode(events[-1]['payload']) == stream.getvalue()
