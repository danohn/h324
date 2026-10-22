"""H.223 03/1996 AL3 mandatory transmit-side retransmission procedures.

The receiver may elect not to request retransmission (H.3246.6.1). This module
implements supervisory framing and the bounded transmitter, not receiver SREJ
exception timers or automatic intra-picture encoding after AL-DRTX.indication.
"""
from collections import Counter, OrderedDict
from dataclasses import dataclass
from h324.h223 import al3_encode, crc16

SREJ, DRTX = 0, 255


@dataclass(frozen=True)
class AL3PDU:
    information: bool
    sequence: int
    payload: bytes


def decode(pdu, control_size=1):
    if control_size not in (1, 2) or len(pdu) < control_size + 3:
        raise ValueError('invalid AL3 size/control')
    if crc16(pdu[:-2]) != int.from_bytes(pdu[-2:], 'little'):
        raise ValueError('bad AL3 CRC')
    sequence = pdu[0] >> 1
    if control_size == 2:
        sequence = (sequence << 8) | pdu[1]
    payload = pdu[control_size:-2]
    information = bool(pdu[0] & 1)
    if not information and len(payload) != 1:
        raise ValueError('AL3 supervisory payload must be one octet')
    return AL3PDU(information, sequence, payload)


def supervisory(sequence, code, control_size=1):
    modulus = {1: 128, 2: 32768}.get(control_size)
    if modulus is None or not 0 <= sequence < modulus or not 0 <= code <= 255:
        raise ValueError('invalid AL3 supervisory field')
    control = bytes((sequence << 1,)) if control_size == 1 else bytes(((sequence >> 8) << 1, sequence & 255))
    body = control + bytes((code,))
    return body + crc16(body).to_bytes(2, 'little')


class Transmitter:
    """Retain latest I-PDUs and respond to valid, ordered SREJ requests.

    send_buffer_size is in stored I-PDU octets, conservatively including control
    and CRC. At most modulus-1 records are retained to avoid sequence ambiguity.
    receive returns response bytes or None; errors never change V(S).
    AL-DRTX.indication is exposed as a bounded event and force_intra flag. The
    codec owner must act on that flag for its next newly encoded picture.
    """
    def __init__(self, control_size=1, send_buffer_size=1024, max_sdu=4096):
        if control_size not in (1, 2) or send_buffer_size < 1024 or max_sdu < 1:
            raise ValueError('AL3 requires 1/2 control octets, >=1024 buffer, positive SDU bound')
        self.control_size = control_size
        self.modulus = 128 if control_size == 1 else 32768
        self.send_buffer_size = send_buffer_size
        self.max_sdu = max_sdu
        self.vs = self.sent = self.stored_bytes = 0
        self.history = OrderedDict()
        self.last_srej = None
        self.last_srej_absolute = None
        self.force_intra = False
        self.counts = Counter()

    def send(self, payload):
        if not 1 <= len(payload) <= self.max_sdu:
            raise ValueError('AL3 SDU outside configured bound')
        pdu = al3_encode(payload, self.vs, self.control_size)
        if len(pdu) > self.send_buffer_size:
            raise ValueError('AL3 I-PDU exceeds transmit buffer')
        if self.vs in self.history:
            self.stored_bytes -= len(self.history.pop(self.vs))
        self.history[self.vs] = pdu
        self.stored_bytes += len(pdu)
        while self.stored_bytes > self.send_buffer_size or len(self.history) >= self.modulus:
            _, discarded = self.history.popitem(last=False)
            self.stored_bytes -= len(discarded)
        self.vs = (self.vs + 1) % self.modulus
        self.sent += 1
        self.counts['sent'] += 1
        return pdu

    def receive(self, pdu):
        try:
            value = decode(pdu, self.control_size)
        except ValueError:
            self.counts['invalid_pdu'] += 1
            return None
        if value.information:
            self.counts['information_ignored'] += 1
            return None
        code = value.payload[0]
        if code != SREJ:
            self.counts['unsolicited_drtx' if code == DRTX else 'reserved_supervisory'] += 1
            return None
        age = (self.vs - value.sequence) % self.modulus
        absolute = self.sent - age
        # H.2237.4.6.4.3: repeated/backwards N(R) is invalid. A sequence which
        # has never been sent or denotes next V(S) cannot request an I-PDU.
        if (age == 0 or age > min(self.sent, self.modulus-1)
                or self.last_srej_absolute is not None and absolute <= self.last_srej_absolute):
            self.counts['invalid_nr'] += 1
            return None
        self.last_srej = value.sequence
        self.last_srej_absolute = absolute
        if value.sequence in self.history:
            self.counts['retransmitted'] += 1
            return self.history[value.sequence]
        self.force_intra = True
        self.counts['declined_retransmission'] += 1
        return supervisory(value.sequence, DRTX, self.control_size)

    def consume_force_intra(self):
        """Consume AL-DRTX.indication; caller must encode next picture INTRA."""
        value, self.force_intra = self.force_intra, False
        return value
