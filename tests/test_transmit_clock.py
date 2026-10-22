"""Replay scheduler bursts through a finite, delayed modem-buffer model.

The delivery lag and effective occupancy are model parameters, not claims of
measured firmware occupancy. This reproduces the failure mechanism across a
range of plausible headroom rather than fitting one exact occupancy value.
"""
from h324.timing import TransmitClock
import pytest


def test_integer_deadlines_preserve_average_and_bound_debt_batch():
    clock = TransmitClock(12_345_678_901)
    assert clock.due(clock.epoch_ns+19_999_999) == 0
    assert clock.due(clock.epoch_ns+20_000_000) == 36
    assert clock.due(clock.epoch_ns+100_000_000) == 108
    clock.advance(108)
    assert clock.due(clock.epoch_ns+100_000_000) == 72
    clock.advance(72)
    assert clock.due(clock.epoch_ns+100_000_000) == 0
    assert clock.deadline_ns == clock.epoch_ns+120_000_000
    # Epoch and tick multiplication remain exact even after long operation.
    clock.next_tick = 30_000_001
    assert clock.deadline_ns-clock.epoch_ns == 600_000_020_000_000


def deliveries(batched):
    # Execution opportunities from the 04 failure around carrier second 263,
    # relative to second 260. The old scheduler sent only 36 at each opportunity.
    times = [3.003643, 3.054556, 3.086202, 3.098605, 3.102034,
             3.104831, 3.107741, 3.1105, 3.1133, 3.1161]
    times += [3.12+i*.002 for i in range(200)]
    clock = TransmitClock(0)
    clock.next_tick = 148  # Next deadline is 2.960 seconds.
    events = []
    for now in times:
        count = clock.due(round(now*1e9))
        if not batched:
            count = min(count, 36)
        if count:
            events.append((now+.025, count))  # Explicit USB/driver delivery lag.
            clock.advance(count)
    return events


def finite_buffer(events, headroom_ms):
    # Last regular 36-byte delivery was at host second 2.940 + 25 ms.
    now, occupancy = 2.965, headroom_ms*1.8
    maximum = occupancy
    for arrived, count in events:
        occupancy -= (arrived-now)*1800
        if occupancy < -1e-6:
            return False, maximum
        occupancy += count
        maximum = max(maximum, occupancy)
        assert occupancy <= 255  # Model checks overrun as well as underrun.
        now = arrived
    return True, maximum


@pytest.mark.parametrize('headroom_ms', [70, 80, 90, 100])
def test_clustered_real_scheduler_opportunities_old_fails_batched_recovers(headroom_ms):
    assert finite_buffer(deliveries(False), headroom_ms)[0] is False
    assert finite_buffer(deliveries(True), headroom_ms)[0] is True


def test_repeated_stalls_keep_service_bounded_without_rate_bias():
    clock = TransmitClock(0)
    transmitted = 0
    # 600 seconds at 2 ms service. Repeated 60 ms pauses and a second nearby
    # 40 ms pause exercise recovery; service continues between bounded batches.
    for millisecond in range(0, 600_001, 2):
        phase = millisecond % 30_000
        if 1000 <= phase < 1060 or 1064 <= phase < 1104:
            continue
        due = clock.due(millisecond*1_000_000)
        if due:
            assert due <= 108
            transmitted += due
            clock.advance(due)
    assert transmitted == 600*1800
    assert clock.deadline_ns == 600_020_000_000


@pytest.mark.parametrize('headroom_ms', [80, 100])
def test_ten_minute_repeated_stalls_with_delivery_lag_and_finite_occupancy(headroom_ms):
    clock = TransmitClock(0)
    events = []
    for millisecond in range(0, 600_001, 2):
        phase = millisecond % 30_000
        if 1000 <= phase < 1060 or 1064 <= phase < 1104:
            continue
        due = clock.due(millisecond*1_000_000)
        if due:
            events.append((millisecond/1000+.025, due))
            clock.advance(due)
    # Observe after initial prefill/transmission has settled. A 25 ms USB/driver
    # delay is explicitly distinct from the assumed effective modem occupancy.
    now, occupancy = .045, headroom_ms*1.8
    for arrived, count in events:
        if arrived <= now:
            continue
        occupancy -= (arrived-now)*1800
        assert occupancy >= -1e-5
        occupancy += count
        assert occupancy <= 255+1e-5
        now = arrived


def test_maximum_debt_batch_respects_worst_case_em_shielding():
    from h324.v80 import shield
    maximum = TransmitClock.MAX_QUANTA*TransmitClock.QUANTUM
    assert maximum == 108
    assert len(shield(bytes([0x19])*maximum)) == 216
    assert len(shield(bytes([0x99])*maximum)) == 216
    assert 216 < 255


def test_worker_services_transmit_before_receive_and_again_after_protocol(monkeypatch):
    from h324.worker import Worker
    from types import SimpleNamespace
    events = []
    worker = Worker('test', lambda **event: None)
    def read():
        events.append('read')
        worker.inbox.put(dict(op='shutdown'))
        return b''
    worker.modem = SimpleNamespace(read=read)
    worker.connected_at = 0
    worker.session = SimpleNamespace(tick=lambda now: events.append('tick'), failure=None)
    monkeypatch.setattr(worker, '_send_due', lambda: events.append('send_due'))
    monkeypatch.setattr(worker, '_feed', lambda raw, now: events.append('feed'))
    monkeypatch.setattr(worker, 'telemetry', lambda now: None)
    monkeypatch.setattr(worker, '_cleanup', lambda *a: setattr(worker, 'modem', None))
    worker.run()
    assert events[:5] == ['send_due', 'read', 'feed', 'tick', 'send_due']


def test_failed_submission_does_not_advance_deadline(monkeypatch):
    from h324.worker import Worker
    import h324.worker as module
    worker = Worker('test', lambda **event: None)
    worker.modem = object()
    worker.connected_at = 0
    worker.transmit_clock = TransmitClock(0)
    monkeypatch.setattr(module.time, 'monotonic_ns', lambda: 100_000_000)
    def fail(*a): raise OSError('serial write timed out')
    monkeypatch.setattr(worker, '_send', fail)
    with pytest.raises(OSError, match='timed out'):
        worker._send_due()
    assert worker.transmit_clock.next_tick == 1
    assert worker.transmit_clock.due(100_000_000) == 108
