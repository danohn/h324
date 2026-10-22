"""Bounded timing observations. Headroom is an estimate, not a DCE counter."""
from collections import Counter, deque
from functools import wraps
import threading


def _meter_synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return locked


class PacerMetrics:
    def __init__(self, history_size=64):
        self._lock = threading.RLock()
        self.history = deque(maxlen=history_size)
        self.counts = Counter()
        self.started = self.last_loop = None
        self.wire_bytes = 0
        self.max_gap_ms = self.max_write_ms = self.max_read_ms = self.max_protocol_ms = 0.0
        self.max_late_ms = 0.0
        self.minimum_nominal_lead_ms = None
        self.last_serial = {}

    @_meter_synchronized
    def start(self, now):
        self.started = now
        self.last_loop = now

    @_meter_synchronized
    def loop(self, now):
        if self.started is None:
            return
        gap = max(0.0, (now-self.last_loop)*1000)
        self.last_loop = now
        self.max_gap_ms = max(self.max_gap_ms, gap)
        self.counts['loops'] += 1
        for limit in (5, 20, 50, 100):
            if gap > limit:
                self.counts[f'loop_gaps_over_{limit}_ms'] += 1
        if gap > 20:
            self.history.append(dict(at_s=round(now-self.started, 6), kind='loop_gap', gap_ms=round(gap, 3)))

    @_meter_synchronized
    def read(self, before, after):
        self.max_read_ms = max(self.max_read_ms, (after-before)*1000)

    @_meter_synchronized
    def protocol(self, before, after):
        self.max_protocol_ms = max(self.max_protocol_ms, (after-before)*1000)

    @_meter_synchronized
    def sent(self, before, after, wire_bytes, serial_bytes, deadline, serial_before, serial_after):
        if self.started is None:
            return
        self.wire_bytes += wire_bytes
        write_ms = max(0.0, (after-before)*1000)
        late_ms = max(0.0, (before-deadline)*1000) if deadline is not None else 0.0
        self.max_write_ms = max(self.max_write_ms, write_ms)
        self.max_late_ms = max(self.max_late_ms, late_ms)
        # Uses nominal 14,400 bps against host monotonic time. USB/firmware
        # occupancy, actual oscillator drift and CTS delays are not measured by
        # this estimate. They remain separately reported observations.
        lead_ms = (self.wire_bytes/1800 - (after-self.started))*1000
        self.minimum_nominal_lead_ms = lead_ms if self.minimum_nominal_lead_ms is None else min(self.minimum_nominal_lead_ms, lead_ms)
        if self.last_serial and serial_after.get('cts') != self.last_serial.get('cts'):
            self.counts['cts_transitions'] += 1
        self.last_serial = serial_after
        if self.counts['writes'] and wire_bytes > 36:
            self.counts['debt_batch_writes'] += 1
        self.counts['writes'] += 1
        self.history.append(dict(at_s=round(after-self.started, 6), kind='write',
            write_ms=round(write_ms, 3), late_ms=round(late_ms, 3),
            wire_bytes=wire_bytes, serial_bytes=serial_bytes,
            nominal_lead_ms=round(lead_ms, 3), before=serial_before, after=serial_after))

    @_meter_synchronized
    def summary(self, history=False):
        result = dict(algorithm='independent-transmit-thread-36-per20ms-max108-plus900-prefill',
            clock='monotonic', counts=dict(self.counts),
            max_loop_gap_ms=round(self.max_gap_ms, 3), max_write_ms=round(self.max_write_ms, 3),
            max_read_ms=round(self.max_read_ms, 3), max_protocol_ms=round(self.max_protocol_ms, 3),
            max_send_lateness_ms=round(self.max_late_ms, 3),
            minimum_nominal_lead_ms=round(self.minimum_nominal_lead_ms, 3) if self.minimum_nominal_lead_ms is not None else None,
            serial=self.last_serial,
            nominal_lead_note='Host-time/nominal-rate estimate; not measured modem buffer occupancy')
        if history:
            result['recent'] = list(self.history)
        return result


class TransmitClock:
    """Repay overdue line quanta in bounded batches without changing the rate.

    Integer epoch-relative deadlines avoid accumulated floating-point error.
    At most three 36-octet quanta are selected per service, leaving a chance to
    drain receive data between batches. No nominal rate bias or buffer feedback
    is inferred from CTS or the host's queue estimate.
    """
    QUANTUM = 36
    PERIOD_NS = 20_000_000
    MAX_QUANTA = 3

    def __init__(self, epoch_ns):
        self.epoch_ns = int(epoch_ns)
        self.next_tick = 1

    @property
    def deadline_ns(self):
        return self.epoch_ns + self.next_tick*self.PERIOD_NS

    def due(self, now_ns):
        overdue = (int(now_ns)-self.epoch_ns)//self.PERIOD_NS-self.next_tick+1
        return max(0, min(self.MAX_QUANTA, overdue))*self.QUANTUM

    def advance(self, octets):
        if not 0 < octets <= self.MAX_QUANTA*self.QUANTUM or octets % self.QUANTUM:
            raise ValueError('Transmit clock advances require one to three complete quanta')
        self.next_tick += octets//self.QUANTUM


class CollectionMetrics:
    """Measure actual GC stop intervals without disabling or forcing collection."""
    def __init__(self):
        import threading
        self._lock = threading.RLock()
        self._started = {}
        self.recent = deque(maxlen=32)
        self.counts = Counter()
        self.max_ms = 0.0
        self.total_ms = 0.0
        self.epoch_ns = None
        self.installed = False

    def callback(self, phase, info):
        import time
        now = time.monotonic_ns()
        generation = info.get('generation', -1)
        with self._lock:
            if phase == 'start':
                self._started[generation] = now
            elif phase == 'stop':
                started = self._started.pop(generation, None)
                if started is None:
                    return
                elapsed = (now-started)/1_000_000
                self.max_ms = max(self.max_ms, elapsed)
                self.total_ms += elapsed
                self.counts[str(generation)] += 1
                self.recent.append(dict(generation=generation,
                    start_s=round((started-(self.epoch_ns or started))/1_000_000_000, 6),
                    duration_ms=round(elapsed, 3), collected=info.get('collected', 0),
                    uncollectable=info.get('uncollectable', 0)))

    def install(self):
        import gc
        import time
        if not self.installed:
            self.epoch_ns = time.monotonic_ns()
            gc.callbacks.append(self.callback)
            self.installed = True

    def close(self):
        import gc
        if self.installed:
            gc.callbacks.remove(self.callback)
            self.installed = False

    def summary(self, history=False):
        with self._lock:
            result = dict(counts=dict(self.counts), max_pause_ms=round(self.max_ms, 3),
                          total_pause_ms=round(self.total_ms, 3),
                          clock='monotonic; offsets since worker GC observer installation')
            if history:
                result['recent'] = list(self.recent)
            return result
