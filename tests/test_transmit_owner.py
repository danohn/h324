import io
import threading
import time
from types import SimpleNamespace
from h324.transmit import DataTransmitter, scheduling_self_test
from h324.worker import Worker
from h324.session import StreamingSession
import h324.worker as worker_module
from h324.timing import TransmitClock
from h324.v80 import shield
import pytest


class Scheduling:
    def __init__(self): self.active = False
    def start(self): self.active = True; return dict(active=True)
    def close(self): self.active = False; return dict(active=False)


def test_blocked_writer_does_not_block_receive_owner_and_cancel_stops_it():
    entered, release = threading.Event(), threading.Event()
    writer_threads = []
    def service():
        writer_threads.append(threading.get_ident())
        entered.set()
        release.wait(2)
    pump = DataTransmitter(lambda: None, service, lambda: time.monotonic_ns()+20_000_000,
                           cancel_write=release.set, scheduling_factory=Scheduling)
    pump.start()
    assert entered.wait(1)
    # Assert progress while the transmitter remains blocked. A fixed number
    # of iterations in 30 ms measures CI scheduling speed instead of isolation.
    try:
        incoming = io.BytesIO(b'received while transmit is blocked')
        assert incoming.read(8) == b'received'
        assert incoming.read() == b' while transmit is blocked'
        assert pump.thread.is_alive() and not release.is_set()
    finally:
        assert pump.stop(timeout=1)
    assert set(writer_threads) != {threading.get_ident()}
    assert pump.summary()['scheduling_released']['active'] is False
    assert pump.failure is None


def test_transmit_owner_keeps_running_when_protocol_owner_stalls():
    writes = []
    progress = threading.Event()
    clock = TransmitClock(time.monotonic_ns())
    def service():
        due = clock.due(time.monotonic_ns())
        if due:
            writes.append(due)
            clock.advance(due)
            if sum(writes) >= 108:
                progress.set()
    pump = DataTransmitter(lambda: None, service, lambda: clock.deadline_ns,
                           scheduling_factory=Scheduling)
    pump.start()
    # Simulated blocking protocol work releases the GIL, just as serial reads,
    # subprocess waits and timer waits do. The independent owner still paces.
    try:
        assert progress.wait(2)
    finally:
        assert pump.stop()
    assert sum(writes) >= 108
    assert max(writes) <= 108


def test_owner_start_failure_is_reported_and_releases_scheduling():
    def fail(): raise OSError('prime failed')
    pump = DataTransmitter(fail, lambda: None, lambda: 0, scheduling_factory=Scheduling)
    with pytest.raises(RuntimeError, match='prime failed'):
        pump.start()
    assert pump.summary()['active'] is False
    assert pump.summary()['scheduling_released']['active'] is False


def test_scheduling_constructor_failure_is_reported_without_initialization_timeout():
    def fail(): raise RuntimeError('scheduling constructor failed')
    pump = DataTransmitter(lambda: None, lambda: None, lambda: 0, scheduling_factory=fail)
    with pytest.raises(RuntimeError, match='scheduling constructor failed'):
        pump.start()
    assert pump.ready.is_set()
    assert pump.summary()['active'] is False


def test_prime_has_bounded_half_second_wire_lead_and_continuous_flags():
    sent = []
    worker = Worker('test', lambda **event: None)
    worker.session = StreamingSession(status_number=1)
    worker.session.start(0)
    worker.modem = SimpleNamespace(send=sent.append)
    worker.files = {'tx': io.BytesIO()}
    worker._prime_transmit()
    assert sent == [b'\x19\xb0', b'\x7e'*900]
    assert worker.files['tx'].getvalue() == b'\x7e'*900
    assert worker.session.link.queued_bytes > 0
    assert len(shield(bytes([0x19])*900)) <= 1800
    assert worker.transmit_clock is not None


def test_cleanup_joins_owner_before_eot_and_at_restore(monkeypatch):
    order = []
    class Pump:
        failure = None
        def stop(self, **kwargs): order.append('stop/join'); return True
        def summary(self): return dict(active=False)
    worker = Worker('test', lambda **event: None)
    worker.transmitter = Pump()
    worker.modem = SimpleNamespace(state='data', send=lambda data: order.append('eot'))
    def restore(modem):
        order.append('restore')
        modem.cleanup_ok = True
    monkeypatch.setattr(worker_module, 'restore_close', restore)
    worker._cleanup()
    assert order == ['stop/join', 'eot', 'restore']
    assert worker.transmitter is None


def test_failed_writer_stop_quarantines_worker_and_retains_owned_resources(monkeypatch):
    order, events = [], []
    class Pump:
        failure = None
        def stop(self, **kwargs): order.append('stop'); return False
        def summary(self): return dict(active=True)
    worker = Worker('test', lambda **event: events.append(event))
    worker.transmitter = Pump()
    modem = SimpleNamespace(state='data', port=SimpleNamespace(close=lambda: order.append('close')))
    worker.modem = modem
    capture = io.BytesIO()
    worker.files = {'tx': capture}
    monkeypatch.setattr(worker_module, 'restore_close', lambda modem: pytest.fail('Unsafe AT restoration'))
    worker._cleanup()
    assert order == ['stop', 'close', 'stop']
    assert worker.fatal_transmit_stop and worker.shutdown_requested
    assert worker.modem is modem and not capture.closed
    cleanup = next(event for event in events if event['event'] == 'cleanup')
    assert cleanup['cleanup_ok'] is False
    assert cleanup['transmit_owner']['active'] is True
    with pytest.raises(RuntimeError, match='quarantined'):
        worker._initialize(dict(op='dial'))
    worker._cleanup()  # run()'s final cleanup must not reuse or repeat shutdown.
    assert order == ['stop', 'close', 'stop']
    capture.close()


def test_worker_self_test_verifies_real_thread_scheduling_without_modem():
    result = scheduling_self_test()
    assert result['released']['active'] is False
    if result['started']['supported']:
        assert result['started']['active'] is True
        if result['started']['platform'] == 'darwin':
            assert result['started']['observed_class'] == 0x21


def test_half_second_wire_lead_survives_process_wide_pause_model():
    # Composite USB/driver/firmware acceptance exceeded 2 KB in physical burst
    # tests. This model uses a conservative 900-wire-octet budget and explicitly
    # subtracts 30 ms delivery lag. It does not infer the DCE FIFO capacity.
    lead = 900-1800*.030
    lead -= 1800*.108404
    assert lead > 600
    # Even a hypothetical 400 ppm faster line over ten minutes leaves positive
    # margin for the same pause. Feedback is still needed for indefinite calls.
    lead -= 1800*.0004*600
    assert lead > 200
