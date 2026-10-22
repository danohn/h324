"""A call-scoped macOS activity assertion, with no GUI dependency.

Constants are from Foundation/NSProcessInfo.h. This changes neither global
App Nap preferences nor display-sleep settings. Other platforms are no-ops.
"""
from __future__ import annotations
import ctypes
import sys

NS_ACTIVITY_USER_INITIATED = 0x00FFFFFF | (1 << 20)
NS_ACTIVITY_LATENCY_CRITICAL = 0xFF00000000
CALL_ACTIVITY_OPTIONS = NS_ACTIVITY_USER_INITIATED | NS_ACTIVITY_LATENCY_CRITICAL


class _MacActivity:
    def __init__(self):
        self.foundation = ctypes.CDLL('/System/Library/Frameworks/Foundation.framework/Foundation')
        self.objc = ctypes.CDLL('/usr/lib/libobjc.A.dylib')
        self.objc.objc_getClass.argtypes = [ctypes.c_char_p]
        self.objc.objc_getClass.restype = ctypes.c_void_p
        self.objc.sel_registerName.argtypes = [ctypes.c_char_p]
        self.objc.sel_registerName.restype = ctypes.c_void_p
        self.address = ctypes.cast(self.objc.objc_msgSend, ctypes.c_void_p).value
        self.process = self.send(self.objc.objc_getClass(b'NSProcessInfo'), b'processInfo')

    def send(self, receiver, selector, *arguments, argument_types=(), result=ctypes.c_void_p):
        function = ctypes.CFUNCTYPE(result, ctypes.c_void_p, ctypes.c_void_p, *argument_types)(self.address)
        return function(receiver, self.objc.sel_registerName(selector), *arguments)

    def begin(self):
        pool = self.send(self.send(self.objc.objc_getClass(b'NSAutoreleasePool'), b'alloc'), b'init')
        try:
            reason = self.send(self.objc.objc_getClass(b'NSString'), b'stringWithUTF8String:',
                               b'H.324 synchronous modem audio/video call', argument_types=(ctypes.c_char_p,))
            token = self.send(self.process, b'beginActivityWithOptions:reason:', CALL_ACTIVITY_OPTIONS, reason,
                              argument_types=(ctypes.c_uint64, ctypes.c_void_p))
            if not token:
                raise RuntimeError('NSProcessInfo did not return a call activity token')
            # The worker owns this token beyond the temporary autorelease pool.
            return self.send(token, b'retain')
        finally:
            self.send(pool, b'drain', result=None)

    def end(self, token):
        try:
            self.send(self.process, b'endActivity:', token, argument_types=(ctypes.c_void_p,), result=None)
        finally:
            self.send(token, b'release', result=None)


class CallActivity:
    """Pair start()/close() around modem ownership, including failure cleanup."""
    def __init__(self, *, platform=None, backend=None):
        self.platform = platform or sys.platform
        self.backend = backend
        self.token = None
        self.report = dict(supported=self.platform == 'darwin', active=False,
                           source='NSProcessInfo' if self.platform == 'darwin' else 'not-required')

    def start(self):
        if self.token is not None or self.platform != 'darwin':
            return dict(self.report)
        self.backend = self.backend or _MacActivity()
        self.token = self.backend.begin()
        self.report.update(active=True, options=CALL_ACTIVITY_OPTIONS)
        return dict(self.report)

    def close(self):
        if self.token is not None:
            token, self.token = self.token, None
            try:
                self.backend.end(token)
            finally:
                self.report['active'] = False
        return dict(self.report)
