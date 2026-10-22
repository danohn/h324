"""H.223 (03/1996) base/Level 0 framing and a bounded static-table MUX.

Octets are LSB-first. PM completes the previous PDU's last segmentable SDU.
Only table 0 / LCN0 exists at startup. Installing other entries here is local
provisioning, not H.245 negotiation. AL3 codecs do not implement its ARQ engine.
"""
from collections import Counter, deque
from dataclasses import dataclass, field

FLAG = '01111110'


def bits(data):
    return ''.join(f'{b:08b}'[::-1] for b in data)


def octets(value):
    if len(value) % 8:
        raise ValueError('integral octets required')
    return bytes(int(value[i:i+8][::-1], 2) for i in range(0, len(value), 8))


def crc(data, width, reflected_polynomial, initial=0, xor_out=0):
    value = initial
    for b in data:
        for i in range(8):
            low = (value ^ (b >> i)) & 1
            value >>= 1
            if low:
                value ^= reflected_polynomial
    return (value ^ xor_out) & ((1 << width) - 1)


def hec(mc):
    if not 0 <= mc <= 15:
        raise ValueError('multiplex code must be 0..15')
    value = 0
    for i in range(4):
        low = (value ^ (mc >> i)) & 1
        value >>= 1
        if low:
            value ^= 0b110
    return value


def crc8(data):
    return crc(data, 8, 0xE0)


def crc16(data):
    return crc(data, 16, 0x8408, 0xFFFF, 0xFFFF)


def stuff(value):
    out, ones = [], 0
    for bit in value:
        out.append(bit)
        ones = ones + 1 if bit == '1' else 0
        if ones == 5:
            out.append('0')
            ones = 0
    return ''.join(out)


def unstuff(value):
    out, ones, i = [], 0, 0
    while i < len(value):
        bit = value[i]
        out.append(bit)
        ones = ones + 1 if bit == '1' else 0
        i += 1
        if ones == 5:
            if i == len(value) or value[i] != '0':
                raise ValueError('invalid zero-bit insertion')
            i += 1
            ones = 0
    return ''.join(out)


@dataclass(frozen=True)
class PDU:
    mc: int
    pm: bool
    information: bytes
    bit_offset: int = field(default=0, compare=False)
    discontinuity: bool = field(default=False, compare=False)
    end_bit_offset: int = field(default=0, compare=False)


def encode_pdu(pdu, xor_information=False):
    """Return exact line bits, with flags; no padding between PDUs."""
    header = hec(pdu.mc) << 5 | pdu.mc << 1 | int(pdu.pm)
    information = pdu.information
    if xor_information:
        information = bytes(b ^ (pdu.mc << 1) for b in information)
    return FLAG + stuff(bits(bytes((header,)) + information)) + FLAG


class WireEncoder:
    """Preserve partial octets across PDUs and idle flags for V.80 byte I/O."""
    def __init__(self, xor_information=False):
        self.pending = ''
        self.xor_information = xor_information

    def _write(self, value):
        self.pending += value
        n = len(self.pending) // 8 * 8
        out = octets(self.pending[:n])
        self.pending = self.pending[n:]
        return out

    def pdu(self, pdu):
        return self._write(encode_pdu(pdu, self.xor_information))

    def idle(self, flags=1):
        if not 1 <= flags <= 4096:
            raise ValueError('idle flags must be 1..4096')
        return self._write(FLAG * flags)


class Framer:
    """Incremental, arbitrary-bit-phase HDLC deframer with bounded storage.

    A seven-one run loses framing. Pure all-one modem underrun fill is counted
    separately; no received payload is repaired. Other invalid candidates are
    explicit errors. Error history is bounded, counters are cumulative.
    Opt-in startup_hunt records acquisition rejects separately until eight full
    flags and a valid initial control PDU; all subsequent errors remain strict.
    """
    def __init__(self, max_information=4096, xor_information=False, *, startup_hunt=False, max_startup_bits=72000):
        if not 1 <= max_information <= 65535:
            raise ValueError('max_information must be 1..65535')
        self.max_information = max_information
        self.max_bits = ((max_information + 1) * 8 * 6 + 4) // 5 + 8
        self.xor_information = xor_information
        self.buffer, self.offset, self.opening = '', 0, None
        self.errors = deque(maxlen=256)
        self.error_count = 0
        self.first_error_bit_offset = None
        self.counts = Counter()
        self.discontinuity = False
        if max_startup_bits < 64:
            raise ValueError('startup bit budget must allow at least eight flags')
        self.acquiring = bool(startup_hunt)
        self.max_startup_bits = max_startup_bits
        self.startup_errors = deque(maxlen=256)
        self.startup_counts = Counter()
        self.startup_flag_run = 0
        self.startup_locked_offset = None
        self.startup_timed_out = False

    def _error(self, name):
        if self.acquiring:
            self.startup_errors.append(dict(error=name, bit_offset=self.opening))
            self.startup_counts[name] += 1
            self.counts['startup_rejected_candidates'] += 1
            self.startup_flag_run = 0
            return
        self.errors.append(dict(error=name, bit_offset=self.opening))
        self.error_count += 1
        if self.first_error_bit_offset is None:
            self.first_error_bit_offset = self.opening if self.opening is not None else self.offset
        self.counts[name] += 1
        self.discontinuity = True

    def expire_startup(self, reason='startup_acquisition_timeout'):
        if self.acquiring:
            self.acquiring = False
            self.startup_timed_out = True
            self._error(reason)
            self.opening = None
            self.buffer = ''

    def feed(self, data):
        return self.feed_bits(bits(data))

    def feed_bits(self, value):
        if set(value) - {'0', '1'}:
            raise ValueError('binary bits required')
        if self.startup_timed_out:
            return []
        # Bound each working chunk even if a caller supplies a giant buffer.
        result = []
        for start in range(0, len(value), 2048):
            self.buffer += value[start:start+2048]
            result.extend(self._consume())
            if self.startup_timed_out:
                break
            if self.acquiring and self.offset + len(self.buffer) >= self.max_startup_bits:
                self.expire_startup()
                break
        return result

    def _consume(self):
        output = []
        while True:
            i = self.buffer.find(FLAG)
            if i < 0:
                if self.opening is None:
                    n = max(0, len(self.buffer) - 7)
                elif len(self.buffer) > self.max_bits:
                    self._error('oversized_pdu')
                    self.opening = None
                    self.startup_flag_run = 0
                    n = max(0, len(self.buffer) - 7)
                elif '1111111' in self.buffer:
                    if set(self.buffer) <= {'1'}:
                        self.counts['all_one_fill'] += 1
                    else:
                        self._error('abort_sequence')
                    self.opening = None
                    self.startup_flag_run = 0
                    n = max(0, len(self.buffer) - 7)
                else:
                    break
                self.buffer = self.buffer[n:]
                self.offset += n
                break
            candidate = self.buffer[:i]
            if self.opening is not None and candidate:
                try:
                    if len(candidate) > self.max_bits:
                        raise ValueError('oversized_pdu')
                    decoded = unstuff(candidate)
                    if len(decoded) % 8:
                        raise ValueError('non_octet_pdu')
                    raw = octets(decoded)
                    if len(raw) > self.max_information + 1:
                        raise ValueError('oversized_pdu')
                    mc, pm = (raw[0] >> 1) & 15, bool(raw[0] & 1)
                    if raw[0] >> 5 != hec(mc):
                        raise ValueError('bad_hec')
                    info = raw[1:]
                    if self.xor_information:
                        info = bytes(b ^ (mc << 1) for b in info)
                    if self.acquiring:
                        if self.offset + i + 8 > self.max_startup_bits:
                            self.expire_startup()
                            return output
                        if self.startup_flag_run < 8:
                            raise ValueError('startup_flag_train_required')
                        if mc != 0 or not info:
                            raise ValueError('startup_control_pdu_required')
                        self.acquiring = False
                        self.startup_locked_offset = self.opening
                        self.counts['startup_acquired'] += 1
                    output.append(PDU(mc, pm, info, self.opening, self.discontinuity,
                                      self.offset+i+8))
                    self.discontinuity = False
                    self.counts['pdus'] += 1
                except ValueError as exc:
                    if set(candidate) <= {'1'}:
                        self.counts['all_one_fill'] += 1
                    else:
                        self._error(str(exc))
            elif self.opening is not None:
                self.counts['idle_flags'] += 1
            if self.acquiring:
                self.startup_flag_run = self.startup_flag_run + 1 if self.opening is not None and not candidate else 1
            self.opening = self.offset + i
            self.buffer = self.buffer[i+8:]
            self.offset += i + 8
        return output


@dataclass(frozen=True)
class Slot:
    lcn: int
    count: int | None = None  # None = untilClosingFlag


@dataclass(frozen=True)
class Repeat:
    elements: tuple
    count: int | None = None


class Table:
    """Locally installed subset of H.245 MultiplexEntryDescriptor structures.

    Logical slots and nested finite/untilClosingFlag repeats are supported.
    Descriptor validation bounds expansion depth and rejects unreachable tails.
    It does not decode ASN.1 or activate entries via H.245.
    """
    def __init__(self):
        self.entries = {0: (Slot(0),)}

    def install(self, mc, elements):
        if not 1 <= mc <= 15:
            raise ValueError('entry 0 is permanently assigned to LCN0')
        elements = tuple(elements)
        self._validate(elements)
        self.entries[mc] = elements

    def _validate(self, elements, depth=0):
        if depth > 4 or not 1 <= len(elements) <= 16:
            raise ValueError('descriptor size/depth exceeds local bound')
        for i, element in enumerate(elements):
            if not isinstance(element, (Slot, Repeat)):
                raise ValueError('Slot or Repeat required')
            if element.count is not None and not 1 <= element.count <= 65535:
                raise ValueError('repeat count must be 1..65535 or None')
            if element.count is None and i != len(elements) - 1:
                raise ValueError('untilClosingFlag must be last')
            if isinstance(element, Slot):
                if not 0 <= element.lcn <= 65535:
                    raise ValueError('LCN must be 0..65535')
            else:
                self._validate(element.elements, depth+1)
                if element.count is not None and any(e.count is None for e in element.elements):
                    raise ValueError('unbounded child in finite repeat')

    def layout(self, mc, length):
        if mc not in self.entries:
            raise ValueError('inactive_entry')
        if not 0 <= length <= 65535:
            raise ValueError('information length exceeds local bound')
        def slots(elements):
            for e in elements:
                if isinstance(e, Slot):
                    yield e.lcn, e.count
                else:
                    n = 0
                    while e.count is None or n < e.count:
                        yield from slots(e.elements)
                        n += 1
        result, remaining = [], length
        for lcn, count in slots(self.entries[mc]):
            if not remaining:
                break
            n = remaining if count is None else min(remaining, count)
            result.append((lcn, n))
            remaining -= n
        if remaining:
            raise ValueError('pattern_exhausted')
        return result


class Multiplexer:
    def __init__(self, table, segmentable):
        self.table, self.segmentable = table, set(segmentable)
        self.pending_end, self.last_mc = False, 0

    def send(self, mc, information, end_sdu=False):
        if not information:
            raise ValueError('use finish/abort for empty PDUs')
        layout = self.table.layout(mc, len(information))
        if end_sdu and layout[-1][0] not in self.segmentable:
            raise ValueError('only a segmentable last slot uses PM')
        out = PDU(mc, self.pending_end, bytes(information))
        self.pending_end, self.last_mc = bool(end_sdu), mc
        return out

    def finish(self):
        if not self.pending_end:
            return None
        self.pending_end = False
        return PDU(self.last_mc, True, b'')

    def abort(self):
        if self.pending_end:
            raise ValueError('already ended SDU cannot be aborted')
        return PDU(self.last_mc, False, b'')


@dataclass(frozen=True)
class SDU:
    lcn: int
    payload: bytes


class Demultiplexer:
    def __init__(self, table, segmentable, open_channels, max_sdu=4096):
        if not 1 <= max_sdu <= 65535:
            raise ValueError('max_sdu must be 1..65535')
        self.table, self.segmentable = table, set(segmentable)
        self.open_channels = set(open_channels)
        self.max_sdu = max_sdu
        self.partial, self.blocked = {}, set()
        self.last_lcn, self.last_mc = None, None
        self.events = deque(maxlen=256)
        self.counts = Counter()

    def _event(self, name, **fields):
        self.events.append(dict(name=name, **fields))
        self.counts[name] += 1

    def lost(self):
        self.partial.clear()
        self.blocked.update(self.segmentable)
        self.last_lcn, self.last_mc = None, None
        self._event('lost_framing')

    def feed(self, pdu):
        if pdu.discontinuity:
            self.lost()
        output = []
        # The current PM refers to the last octet of the PREVIOUS PDU.
        if pdu.pm and self.last_lcn in self.segmentable:
            lcn = self.last_lcn
            if lcn in self.partial and lcn not in self.blocked:
                output.append(SDU(lcn, bytes(self.partial[lcn])))
            self.partial.pop(lcn, None)
            self.blocked.discard(lcn)
        elif pdu.pm:
            self._event('orphan_pm')
        if not pdu.information and not pdu.pm and pdu.mc == self.last_mc:
            self.partial.pop(self.last_lcn, None)
            self.blocked.discard(self.last_lcn)
            self._event('mux_abort', lcn=self.last_lcn)
        try:
            layout = self.table.layout(pdu.mc, len(pdu.information))
            if any(lcn not in self.open_channels for lcn, _ in layout):
                raise ValueError('closed_channel')
        except ValueError as exc:
            self._event(str(exc), mc=pdu.mc)
            self.lost()
            return output
        offset = 0
        for lcn, count in layout:
            data = pdu.information[offset:offset+count]
            offset += count
            if lcn not in self.segmentable:
                if len(data) > self.max_sdu:
                    self._event('oversized_sdu', lcn=lcn)
                else:
                    output.append(SDU(lcn, data))
            elif lcn not in self.blocked:
                self.partial.setdefault(lcn, bytearray()).extend(data)
                if len(self.partial[lcn]) > self.max_sdu:
                    self.partial.pop(lcn)
                    self.blocked.add(lcn)
                    self._event('oversized_sdu', lcn=lcn)
        self.last_lcn = layout[-1][0] if layout else None
        self.last_mc = pdu.mc
        self.counts['sdus'] += len(output)
        return output


def al1_encode(payload):
    return bytes(payload)


def al1_decode(pdu):
    return bytes(pdu)


def al2_encode(payload, sequence=None):
    if not payload or sequence is not None and not 0 <= sequence <= 255:
        raise ValueError('nonempty payload and optional uint8 sequence required')
    body = (bytes((sequence,)) if sequence is not None else b'') + payload
    return body + bytes((crc8(body),))


def al2_decode(pdu, sequenced=False):
    if len(pdu) < (3 if sequenced else 2):
        raise ValueError('short_al2')
    if crc8(pdu[:-1]) != pdu[-1]:
        raise ValueError('bad_al2_crc')
    return (pdu[1:-1], pdu[0]) if sequenced else (pdu[:-1], None)


def al3_encode(payload, sequence=None, control_size=1):
    if not payload or control_size not in (0, 1, 2):
        raise ValueError('nonempty payload and control_size 0/1/2 required')
    if control_size:
        if sequence is None or not 0 <= sequence < (128 if control_size == 1 else 32768):
            raise ValueError('7/15-bit sequence required')
        control = bytes(((sequence << 1) | 1,)) if control_size == 1 else bytes((
            ((sequence >> 8) << 1) | 1, sequence & 255))
    else:
        if sequence is not None:
            raise ValueError('no sequence without control field')
        control = b''
    body = control + payload
    return body + crc16(body).to_bytes(2, 'little')


def al3_decode(pdu, control_size=1):
    if control_size not in (0, 1, 2) or len(pdu) < control_size + 3:
        raise ValueError('short_al3_or_invalid_control_size')
    if crc16(pdu[:-2]) != int.from_bytes(pdu[-2:], 'little'):
        raise ValueError('bad_al3_crc')
    if control_size and not pdu[0] & 1:
        raise ValueError('al3_supervisory_not_implemented')
    sequence = None
    if control_size:
        sequence = pdu[0] >> 1
        if control_size == 2:
            sequence = (sequence << 8) | pdu[1]
    return pdu[control_size:-2], sequence
