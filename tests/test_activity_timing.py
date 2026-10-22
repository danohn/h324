"""Power assertions are scoped and pacing evidence remains bounded."""
import json
from types import SimpleNamespace
import pytest
from h324.activity import CallActivity, CALL_ACTIVITY_OPTIONS
from h324.timing import PacerMetrics
from h324.worker import Worker
import h324.worker as worker_module


class Backend:
    def __init__(self): self.actions = []
    def begin(self):
        self.actions.append('begin')
        return object()
    def end(self, token):
        assert token is not None
        self.actions.append('end')


def test_macos_activity_pairs_once_and_never_prevents_display_sleep():
    backend = Backend()
    activity = CallActivity(platform='darwin', backend=backend)
    assert activity.start()['active']
    assert activity.start()['active']
    assert not CALL_ACTIVITY_OPTIONS & (1 << 40)
    assert CALL_ACTIVITY_OPTIONS & (1 << 20)
    assert not activity.close()['active']
    activity.close()
    assert backend.actions == ['begin', 'end']


@pytest.mark.parametrize('platform', ['linux', 'win32'])
def test_other_platforms_do_not_load_objc(platform):
    activity = CallActivity(platform=platform, backend=Backend())
    assert activity.start() == dict(supported=False, active=False, source='not-required')
    assert activity.close()['active'] is False
    assert activity.backend.actions == []


def test_factory_failure_releases_started_activity(monkeypatch):
    backend = Backend()
    monkeypatch.setattr(worker_module, 'CallActivity', lambda: CallActivity(platform='darwin', backend=backend))
    def fail(*a, **kw): raise PermissionError('port unavailable')
    worker = Worker('test', lambda **e: None, modem_factory=fail)
    monkeypatch.setattr(worker, '_ownership_check', lambda port: None)
    with pytest.raises(PermissionError):
        worker._initialize(dict(op='listen', port='COM3'))
    worker._cleanup('port unavailable')
    assert backend.actions == ['begin', 'end']
    assert worker.activity is None
    assert worker.activity_report['active'] is False


def test_activity_begin_failure_does_not_open_port(monkeypatch):
    class BadBackend(Backend):
        def begin(self): raise RuntimeError('assertion unavailable')
    monkeypatch.setattr(worker_module, 'CallActivity', lambda: CallActivity(platform='darwin', backend=BadBackend()))
    worker = Worker('test', lambda **e: None, modem_factory=lambda *a, **k: pytest.fail('opened port'))
    monkeypatch.setattr(worker, '_ownership_check', lambda port: None)
    with pytest.raises(RuntimeError, match='timing activity protection'):
        worker._initialize(dict(op='listen', port='COM3'))
    worker._cleanup()
    assert worker.activity is None


def test_cleanup_releases_activity_after_modem_restoration(monkeypatch):
    order = []
    class Activity:
        def close(self):
            order.append('activity ended')
            return dict(active=False)
    worker = Worker('test', lambda **e: None)
    worker.activity = Activity()
    worker.modem = SimpleNamespace(state='data', send=lambda data: None)
    def restore(modem):
        order.append('modem restored')
        modem.cleanup_ok = True
    monkeypatch.setattr(worker_module, 'restore_close', restore)
    worker._cleanup()
    assert order == ['modem restored', 'activity ended']


def test_pacer_records_gap_write_delay_and_separates_estimate_from_observation():
    meter = PacerMetrics()
    meter.start(10)
    meter.loop(10.002)
    meter.sent(10.002, 10.003, 216, 216, None,
               dict(cts=True, os_out_waiting=0), dict(cts=True, os_out_waiting=0))
    meter.loop(10.203)
    meter.sent(10.203, 10.253, 36, 37, 10.02,
               dict(cts=False, os_out_waiting=10), dict(cts=True, os_out_waiting=5))
    result = meter.summary(history=True)
    assert result['max_loop_gap_ms'] == 201
    assert result['max_write_ms'] == 50
    assert result['max_send_lateness_ms'] == 183
    assert result['minimum_nominal_lead_ms'] < 0
    assert any(e['kind']=='loop_gap' for e in result['recent'])
    assert result['serial']['os_out_waiting'] == 5
    assert 'not measured' in result['nominal_lead_note']


def test_pacer_history_and_telemetry_size_stay_bounded_over_long_call():
    meter = PacerMetrics()
    meter.start(0)
    for i in range(30000):
        t = i*.02
        meter.loop(t)
        meter.sent(t, t, 36, 40, t, dict(cts=True, os_out_waiting=0), dict(cts=True, os_out_waiting=0))
    assert len(meter.history) == 64
    assert len(json.dumps(meter.summary())) < 2000
    assert len(json.dumps(meter.summary(history=True))) < 25000


def test_gc_observer_records_duration_and_uninstalls_without_disabling_gc(monkeypatch):
    import gc
    import time
    from h324.timing import CollectionMetrics
    meter = CollectionMetrics()
    before = list(gc.callbacks)
    enabled = gc.isenabled()
    meter.install()
    assert meter.callback in gc.callbacks
    meter.callback('start', dict(generation=2))
    time.sleep(.001)
    meter.callback('stop', dict(generation=2, collected=3, uncollectable=0))
    result = meter.summary(history=True)
    assert result['max_pause_ms'] >= 1
    assert result['counts']['2'] >= 1
    assert result['recent'][-1]['collected'] == 3
    meter.close()
    meter.close()
    assert gc.callbacks == before
    assert gc.isenabled() == enabled


def test_gc_observer_history_is_bounded():
    from h324.timing import CollectionMetrics
    meter = CollectionMetrics()
    for i in range(100):
        meter.callback('start', dict(generation=0))
        meter.callback('stop', dict(generation=0, collected=0))
    assert len(meter.summary(history=True)['recent']) == 32
