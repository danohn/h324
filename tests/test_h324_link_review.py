"""Independent integration checks for SRP, negotiated tables and preemption."""
import pytest

from h324.h223 import Slot
from h324.link import Link, Session
from h324.v80 import shield


@pytest.mark.parametrize('stage,faults,pull', [
    ('srp', False, 1), ('srp', True, 95),
    ('h245', False, 31), ('h245', True, 95),
])
def test_two_sessions_complete_over_full_framing(stage, faults, pull):
    sessions = [Session('a', 0, stage, faults=faults, status_number=101),
                Session('b', 1, stage, faults=faults, status_number=102)]
    for session in sessions:
        session.start(0)
    for step in range(12000):
        now = step / 1000
        for source in (0, 1):
            raw = sessions[source].link.pull(pull)
            sessions[1-source].feed(shield(raw), now)
        for session in sessions:
            session.tick(now)
            assert session.failure is None
        if all(session.complete for session in sessions):
            break
    assert all(session.complete for session in sessions)
    for session in sessions:
        assert not session.link.framer.errors
        assert not session.link.demux.events
        assert not session.link.status
        if faults:
            assert session.injected == {
                'drop_command': 1, 'corrupt_command': 1, 'drop_ack': 1}
            assert session.srp.counts['retransmissions'] >= 3
        if stage == 'h245':
            assert set(session.link.tx_table.entries) == {0, 1, 2}
            assert set(session.link.rx_table.entries) == {0, 1, 2}
            assert session.link.demux.open_channels == {0, 1, 2}


def test_control_and_audio_preempt_video_at_fragment_boundary_without_corruption():
    tx, rx = Link(lambda *a, **k: None), Link(lambda *a, **k: None)
    for entry in (1, 2):
        tx.tx_table.install(entry, [Slot(entry)])
        rx.rx_table.install(entry, [Slot(entry)])
        rx.demux.open_channels.add(entry)
    video, audio, control = bytes(range(200)), b'audio'*4, b'urgent control'
    tx.queue_sdu(2, video, 2, True)
    # Consume initial flags and most of the first video fragment. The remaining
    # encoded bytes must complete before the new higher-priority fragments start.
    found = list(rx.feed(shield(tx.pull(65))))
    tx.queue_sdu(1, audio, 1, False)
    tx.queue_sdu(0, control, 0, True)
    while tx.pending:
        found.extend(rx.feed(shield(tx.pull(17))))
    found.extend(rx.feed(shield(tx.pull(17))))
    assert [(sdu.lcn, sdu.payload) for sdu in found] == [
        (0, control), (1, audio), (2, video)]
    assert not rx.framer.errors and not rx.demux.events


def test_queue_overflow_rejects_whole_sdu_without_partial_enqueue():
    tx = Link(lambda *a, **k: None, max_queued=128)
    tx.queue_sdu(0, b'x'*100, 0, True)
    before = tuple(tx.queues[0])
    with pytest.raises(BufferError):
        tx.queue_sdu(0, b'y'*100, 0, True)
    assert tuple(tx.queues[0]) == before
    assert tx.queued_bytes == 100
