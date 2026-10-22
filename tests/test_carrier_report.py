"""Actual initial AT bearer evidence is distinct from V.80's current rate."""
import io
from types import SimpleNamespace
import pytest
from h324.modem import parse_carrier_report
from h324.session import StreamingSession
from h324.worker import Worker


class PrimeOnlyTransmitter:
    """Keep parser tests synchronous; separate tests exercise the actual owner."""
    failure = None
    def __init__(self, prime, *args, **kwargs): self.prime = prime
    def start(self):
        self.prime()
        return dict(active=False)
    def stop(self, **kwargs): return True
    def summary(self): return dict(active=False)

VALID = ['+MCR: V32B', '+MRR: 14400,14400', '+ER: NONE', 'CONNECT 14400']


def test_valid_actual_initial_reports_are_named_and_preserved():
    report = parse_carrier_report(VALID)
    assert report == dict(source='AT+MRR', validated=True, phase='connect', modulation='V32B',
        tx_bps=14400, rx_bps=14400, connect_bps=14400,
        raw_mcr=VALID[0], raw_mrr=VALID[1], raw_connect=VALID[3])


@pytest.mark.parametrize('lines', [[], ['CONNECT 14400'], VALID[:-1],
    ['+MS: V32B,0,14400,14400,14400,14400'],
    ['+MCR: V32B', '+MRR: 14400', 'CONNECT 14400'],
    ['+MCR: V32B', '+MRR: 14400,9600', 'CONNECT 14400'],
    ['+MCR: V32B', '+MRR: 14400,14400', 'CONNECT 9600'],
    ['+MCR: V34', '+MRR: 14400,14400', 'CONNECT 14400'],
    ['+MCR: V32B', '+MRR: 0,0', 'CONNECT 14400'],
    ['+MRR: 9600,9600']+VALID, VALID+['CONNECT 9600']])
def test_missing_unknown_asymmetric_or_conflicting_reports_cannot_verify(lines):
    assert parse_carrier_report(lines) is None


def test_telemetry_keeps_unspecified_inband_rate_unknown_with_verified_at_report():
    events = []
    worker = Worker('test', lambda **event: events.append(event), transmitter_factory=PrimeOnlyTransmitter)
    worker.modem = SimpleNamespace(state='dialling', log=lambda *a, **k: None, send=lambda data: None)
    worker.session = StreamingSession(status_number=1)
    worker.files = {name: io.BytesIO() for name in ('audio', 'video', 'rx', 'tx')}
    # The standard unspecified V.80 rate is never replaced with the AT result.
    worker._negotiate(('\r\n'.join(VALID)+'\r\n').encode()+b'\x19\xbe\x00\x00', 10)
    worker.telemetry(10.1)
    telemetry = events[-1]
    assert telemetry['rate'] is None
    assert telemetry['carrier_report']['source'] == 'AT+MRR'
    assert telemetry['carrier_report']['tx_bps'] == telemetry['carrier_report']['rx_bps'] == 14400


def test_known_wrong_inband_rate_is_still_fatal_despite_initial_at_target():
    worker = Worker('test', lambda **event: None)
    worker.session = StreamingSession(status_number=1)
    worker.files = {'rx': io.BytesIO()}
    worker.carrier_report = parse_carrier_report(VALID)
    with pytest.raises(RuntimeError, match='Unsupported actual modem rate 9600'):
        worker._feed(b'\x19\xbe\x24\x24', 0)
