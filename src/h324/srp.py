"""H.324 (03/1996) Annex A.2 Simple Retransmission Protocol.

Each encoded frame is one complete framed H.223 AL1 SDU on LCN0. This module
does not perform H.245 ASN.1 encoding, fragmentation, serial I/O or call teardown.
ACKs have no sequence field: transports must preserve ordering and bound frame
lifetimes. A sufficiently delayed duplicate ACK can acknowledge a later command;
Annex A.2 provides no wire-level way to distinguish it from the real response.
"""
from collections import Counter, deque
from dataclasses import dataclass
import math

from h324.h223 import crc16

COMMAND = 0xF9
RESPONSE = 0xFB
MAX_INFORMATION = 2048


def _payload(value):
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError('payload must be bytes-like')
    value = bytes(value)
    if not 1 <= len(value) <= MAX_INFORMATION:
        raise ValueError('SRP information must contain 1..2048 octets')
    return value


def _fcs(body):
    return body + crc16(body).to_bytes(2, 'little')


def encode_command(sequence, payload):
    if not isinstance(sequence, int) or not 0 <= sequence <= 255:
        raise ValueError('sequence must be uint8')
    return _fcs(bytes((COMMAND, sequence)) + _payload(payload))


def encode_response():
    return _fcs(bytes((RESPONSE,)))


@dataclass(frozen=True)
class Frame:
    sequence: int | None = None
    payload: bytes = b''

    @property
    def is_response(self):
        return self.sequence is None


def decode_frame(frame):
    """Reject malformed/unknown AL1 SDUs; caller may silently ignore them."""
    if not isinstance(frame, (bytes, bytearray, memoryview)):
        raise TypeError('frame must be bytes-like')
    frame = bytes(frame)
    if len(frame) < 3:
        raise ValueError('short_srp_frame')
    if frame[0] == RESPONSE:
        if len(frame) != 3:
            raise ValueError('invalid_response_length')
    elif frame[0] == COMMAND:
        if not 5 <= len(frame) <= MAX_INFORMATION + 4:
            raise ValueError('invalid_command_length')
    else:
        raise ValueError('unknown_srp_header')
    if crc16(frame[:-2]) != int.from_bytes(frame[-2:], 'little'):
        raise ValueError('bad_srp_fcs')
    return Frame() if frame[0] == RESPONSE else Frame(frame[1], frame[2:-2])


@dataclass(frozen=True)
class Update:
    """Immediate work/results; caller must consume outgoing frames and deliveries.

    `acknowledged` identifies local transactions, not a field on the ACK wire.
    `failure` requires call-level action: this endpoint cannot resume or reset.
    """
    outgoing: tuple[bytes, ...] = ()
    delivered: tuple[bytes, ...] = ()
    acknowledged: tuple[int, ...] = ()
    events: tuple[str, ...] = ()
    failure: str | None = None


class Failed(RuntimeError):
    pass


class Endpoint:
    """Bounded full-duplex stop-and-wait endpoint, driven by a monotonic clock.

    N400 is the number of retransmissions after the original transmission. On
    expiry after the final retry, failure becomes terminal. There is no automatic
    reconnect, implicit cancellation, or silent loss of queued application data.
    T401 should include the maximum AL1 frame serialization/queue/round-trip time;
    the default 3 seconds is a local lab policy, not a standardized value.
    """
    def __init__(self, timeout=3.0, max_retries=5, max_pending=16,
                 initial_sequence=0):
        if not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('timeout must be finite and positive')
        if not isinstance(max_retries, int) or not 5 <= max_retries <= 100:
            raise ValueError('max_retries must be 5..100')
        if not isinstance(max_pending, int) or not 1 <= max_pending <= 1024:
            raise ValueError('max_pending must be 1..1024')
        if not isinstance(initial_sequence, int) or not 0 <= initial_sequence <= 255:
            raise ValueError('initial_sequence must be uint8')
        self.timeout, self.max_retries, self.max_pending = timeout, max_retries, max_pending
        self.next_sequence = initial_sequence
        self._queued = deque()
        self._active = None
        self._deadline = None
        self._retries = 0
        self._last_received = None
        self._last_now = None
        self.failure = None
        self.counts = Counter()

    @property
    def pending_count(self):
        return len(self._queued) + int(self._active is not None)

    @property
    def idle(self):
        return not self.pending_count and self.failure is None

    @property
    def deadline(self):
        return self._deadline

    def _clock(self, now):
        if not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError('now must be a finite monotonic time')
        if self._last_now is not None and now < self._last_now:
            raise ValueError('clock moved backwards')
        self._last_now = now

    def _start(self, now):
        payload = self._queued.popleft()
        sequence = self.next_sequence
        frame = encode_command(sequence, payload)
        self.next_sequence = (sequence + 1) % 256
        self._active = (sequence, frame)
        self._deadline = now + self.timeout
        self._retries = 0
        self.counts['commands_sent'] += 1
        return frame

    def send(self, payload, now):
        """Enqueue a new message; BufferError leaves queue/sequence unchanged."""
        payload = _payload(payload)
        self._clock(now)
        if self.failure is not None:
            raise Failed(self.failure)
        if self.pending_count >= self.max_pending:
            raise BufferError('SRP pending-message limit reached')
        self._queued.append(payload)
        if self._active is None:
            return Update(outgoing=(self._start(now),), events=('command_sent',))
        return Update(events=('queued',))

    def receive(self, frame, now):
        """Consume exactly one AL1 SDU; invalid frames generate no response."""
        self._clock(now)
        if self.failure is not None:
            return Update(events=('ignored_after_failure',), failure=self.failure)
        try:
            decoded = decode_frame(frame)
        except ValueError as exc:
            self.counts['invalid_frames'] += 1
            return Update(events=(str(exc),))
        if decoded.is_response:
            if self._active is None:
                self.counts['unsolicited_responses'] += 1
                return Update(events=('unsolicited_response',))
            sequence, _ = self._active
            self._active, self._deadline = None, None
            self.counts['commands_acknowledged'] += 1
            outgoing = (self._start(now),) if self._queued else ()
            return Update(outgoing=outgoing, acknowledged=(sequence,),
                          events=('acknowledged',))
        self.counts['responses_sent'] += 1
        duplicate = decoded.sequence == self._last_received
        if duplicate:
            self.counts['duplicate_commands'] += 1
            return Update(outgoing=(encode_response(),), events=('duplicate_command',))
        self._last_received = decoded.sequence
        self.counts['messages_delivered'] += 1
        return Update(outgoing=(encode_response(),), delivered=(decoded.payload,),
                      events=('delivered',))

    def tick(self, now):
        """Check T401 once; no burst of retries when caller wakes up late."""
        self._clock(now)
        if self.failure is not None:
            return Update(failure=self.failure)
        if self._active is None or now < self._deadline:
            return Update()
        if self._retries >= self.max_retries:
            self.failure = 'retransmission_limit_exhausted'
            self._deadline = None
            self.counts['failures'] += 1
            return Update(events=('communication_lost',), failure=self.failure)
        self._retries += 1
        self._deadline = now + self.timeout
        self.counts['retransmissions'] += 1
        return Update(outgoing=(self._active[1],), events=('retransmitted',))
