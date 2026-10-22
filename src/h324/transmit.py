"""Independent serial-transmit ownership and scoped thread scheduling requests.

Scheduling preferences improve isolation; they are not real-time guarantees.
No modem occupancy is inferred from OS CTS or output queue observations.
"""
from __future__ import annotations
import ctypes
import sys
import threading
import time


class ThreadScheduling:
    def __init__(self, platform=None):
        self.platform = platform or sys.platform
        self.report = dict(platform=self.platform, active=False, supported=False)
        self.lib = self.kernel = self.winmm = None
        self.previous = None
        self.timer_started = False

    def start(self):
        if self.platform == 'darwin':
            self.lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            self.lib.pthread_set_qos_class_self_np.argtypes = [ctypes.c_uint, ctypes.c_int]
            self.lib.pthread_set_qos_class_self_np.restype = ctypes.c_int
            self.lib.qos_class_self.argtypes = []
            self.lib.qos_class_self.restype = ctypes.c_uint
            self.previous = self.lib.qos_class_self()
            result = self.lib.pthread_set_qos_class_self_np(0x21, 0)
            observed = self.lib.qos_class_self()
            self.report.update(source='pthread_qos', supported=True,
                requested_class=0x21, observed_class=observed, set_result=result)
            if result or observed != 0x21:
                raise RuntimeError(f'Transmit thread QoS request failed: {self.report}')
            self.report['active'] = True
        elif self.platform == 'win32':
            self.kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            self.winmm = ctypes.WinDLL('winmm')
            self.kernel.GetCurrentThread.restype = ctypes.c_void_p
            self.kernel.GetThreadPriority.argtypes = [ctypes.c_void_p]
            self.kernel.GetThreadPriority.restype = ctypes.c_int
            self.kernel.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
            self.kernel.SetThreadPriority.restype = ctypes.c_int
            handle = self.kernel.GetCurrentThread()
            self.previous = self.kernel.GetThreadPriority(handle)
            result = self.kernel.SetThreadPriority(handle, 2)  # THREAD_PRIORITY_HIGHEST
            observed = self.kernel.GetThreadPriority(handle)
            self.winmm.timeBeginPeriod.argtypes = [ctypes.c_uint]
            self.winmm.timeBeginPeriod.restype = ctypes.c_uint
            self.winmm.timeEndPeriod.argtypes = [ctypes.c_uint]
            self.winmm.timeEndPeriod.restype = ctypes.c_uint
            timer_result = self.winmm.timeBeginPeriod(1)
            self.timer_started = timer_result == 0
            self.report.update(source='SetThreadPriority/timeBeginPeriod', supported=True,
                requested_priority=2, observed_priority=observed,
                set_result=bool(result), timer_result=timer_result)
            if not result or observed != 2 or timer_result:
                raise RuntimeError(f'Transmit thread scheduling request failed: {self.report}')
            self.report['active'] = True
        else:
            self.report.update(source='default-platform-scheduler')
        return dict(self.report)

    def close(self):
        try:
            if self.platform == 'darwin' and self.lib is not None and self.previous:
                self.report['restore_result'] = self.lib.pthread_set_qos_class_self_np(self.previous, 0)
            elif self.platform == 'win32' and self.kernel is not None and self.previous is not None:
                self.report['restore_result'] = bool(self.kernel.SetThreadPriority(self.kernel.GetCurrentThread(), self.previous))
        finally:
            if self.timer_started:
                self.report['timer_end_result'] = self.winmm.timeEndPeriod(1)
                self.timer_started = False
            self.report['active'] = False
        return dict(self.report)


def scheduling_self_test():
    result = {}
    def run():
        scheduling = ThreadScheduling()
        try:
            result['started'] = scheduling.start()
        except Exception as exc:
            result['error'] = str(exc)
        finally:
            result['released'] = scheduling.close()
    thread = threading.Thread(target=run, name='h324-scheduling-self-test')
    thread.start()
    thread.join(3)
    if thread.is_alive() or 'error' in result:
        raise RuntimeError('Transmit scheduling self-test failed: ' + str(result))
    return result


class DataTransmitter:
    """Only this thread writes serial data between CONNECT and cleanup.

    prime() establishes the continuous encoder and clock. service() submits a
    bounded batch. deadline() returns the next integer monotonic deadline.
    stop() must finish before the caller sends EOT/AT commands or closes files.
    """
    def __init__(self, prime, service, deadline, *, cancel_write=None,
                 scheduling_factory=ThreadScheduling):
        self.prime = prime
        self.service = service
        self.deadline = deadline
        self.cancel_write = cancel_write
        self.scheduling_factory = scheduling_factory
        self.stop_event = threading.Event()
        self.ready = threading.Event()
        self.thread = None
        self.failure = None
        self.scheduling = None
        self.started_scheduling = None
        self.ended_scheduling = None
        self.max_service_gap_ms = 0.0
        self.services = 0

    def start(self):
        if self.thread is not None:
            raise RuntimeError('Transmit owner is already started')
        self.thread = threading.Thread(target=self._run, name='h324-serial-transmit', daemon=True)
        self.thread.start()
        if not self.ready.wait(4):
            self.stop()
            raise RuntimeError('Transmit owner did not initialize within four seconds')
        if self.failure is not None:
            self.stop()
            raise RuntimeError('Transmit owner initialization failed: ' + str(self.failure))
        return self.summary()

    def _run(self):
        previous = None
        try:
            self.scheduling = self.scheduling_factory()
            self.started_scheduling = self.scheduling.start()
            self.prime()
            self.ready.set()
            while not self.stop_event.is_set():
                now = time.monotonic_ns()
                if previous is not None:
                    self.max_service_gap_ms = max(self.max_service_gap_ms, (now-previous)/1_000_000)
                previous = now
                self.service()
                self.services += 1
                remaining = (self.deadline()-time.monotonic_ns())/1_000_000_000
                if remaining > 0:
                    self.stop_event.wait(min(.02, remaining))
        except BaseException as exc:
            if not self.stop_event.is_set():
                self.failure = exc
        finally:
            try:
                if self.scheduling is not None:
                    self.ended_scheduling = self.scheduling.close()
            except Exception as exc:
                self.failure = self.failure or exc
            self.ready.set()

    def stop(self, timeout=4):
        self.stop_event.set()
        if self.thread is None:
            return True
        self.thread.join(.05)
        if self.thread.is_alive() and self.cancel_write is not None:
            try:
                self.cancel_write()
            except (OSError, AttributeError):
                pass
        self.thread.join(timeout)
        return not self.thread.is_alive()

    def summary(self):
        return dict(active=self.thread is not None and self.thread.is_alive(),
                    services=self.services, max_service_gap_ms=round(self.max_service_gap_ms, 3),
                    failure=str(self.failure) if self.failure is not None else None,
                    scheduling_started=self.started_scheduling, scheduling_released=self.ended_scheduling)
