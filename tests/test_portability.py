import io
from types import SimpleNamespace
import pytest
from h324 import modem as serial_modem
from h324.worker import Worker
import h324.worker as worker_module


def test_windows_serial_does_not_pass_posix_exclusive(monkeypatch):
    options = []
    class Port:
        def __init__(self, **kwargs): options.append(kwargs)
        def open(self): pass
        def close(self): pass
    monkeypatch.setattr(serial_modem, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(serial_modem.serial, 'Serial', Port)
    modem = serial_modem.Modem('COM12', 'test')
    assert 'exclusive' not in options[0]
    assert modem.port.port == 'COM12'
    assert modem.logfile is None


def test_posix_serial_requests_exclusivity(monkeypatch):
    options = []
    class Port:
        def __init__(self, **kwargs): options.append(kwargs)
        def open(self): pass
    monkeypatch.setattr(serial_modem, 'os', SimpleNamespace(name='posix'))
    monkeypatch.setattr(serial_modem.serial, 'Serial', Port)
    serial_modem.Modem('/dev/ttyACM0', 'test')
    assert options[0]['exclusive'] is True


@pytest.mark.parametrize('port', ['COM1', 'com12', r'\\.\COM255'])
def test_windows_port_names_bypass_lsof(monkeypatch, port):
    monkeypatch.setattr(worker_module, 'os', SimpleNamespace(name='nt'))
    monkeypatch.setattr(worker_module.subprocess, 'run', lambda *a, **k: pytest.fail('lsof on Windows'))
    Worker('test', lambda **e: None)._ownership_check(port)


def test_invalid_windows_name_is_clear(monkeypatch):
    monkeypatch.setattr(worker_module, 'os', SimpleNamespace(name='nt'))
    with pytest.raises(ValueError, match='COM port'):
        Worker('test', lambda **e: None)._ownership_check('/dev/ttyACM0')


def test_lsof_optional(monkeypatch):
    monkeypatch.setattr(worker_module, 'os', SimpleNamespace(name='posix'))
    def unavailable(*a, **kw): raise FileNotFoundError()
    monkeypatch.setattr(worker_module.subprocess, 'run', unavailable)
    Worker('test', lambda **e: None)._ownership_check('/dev/ttyACM0')


def test_ipc_slow_consumer_still_restores_modem(monkeypatch):
    def emit(**event): raise BufferError('consumer blocked')
    worker = Worker('test', emit)
    worker.modem = SimpleNamespace(state='data', send=lambda data: None)
    restored = []
    def restore(modem):
        restored.append(modem)
        modem.cleanup_ok = True
    monkeypatch.setattr(worker_module, 'restore_close', restore)
    worker._cleanup()
    assert len(restored) == 1
    assert worker.modem is None
    assert worker.ipc_overload


def test_cleanup_event_without_recording(monkeypatch):
    events = []
    worker = Worker('test', lambda **event: events.append(event))
    worker.modem = SimpleNamespace(state='data', send=lambda data: None)
    def restore(modem): modem.cleanup_ok = True
    monkeypatch.setattr(worker_module, 'restore_close', restore)
    worker._cleanup()
    cleanup = next(e for e in events if e['event'] == 'cleanup')
    assert cleanup['cleanup_ok'] is True and cleanup['error'] is None
    assert worker.directory is None
    assert events.index(cleanup) < next(i for i,e in enumerate(events) if e.get('state') == 'idle')
