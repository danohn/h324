"""Bounded H.324 laboratory negotiation using genuine H.245 aligned PER.

This deliberately finite profile uses only baseline root alternatives from the
vendored ITU/Wireshark H.245 v15 schema. It is not a complete H.245 terminal.
SRP delivers one encoded MultimediaSystemControlMessage to receive().
"""
from dataclasses import dataclass
from functools import lru_cache
from importlib.resources import files
import secrets

import asn1tools

SCHEMA = files('h324').joinpath('data/MULTIMEDIA-SYSTEM-CONTROL.asn')
PROTOCOL_IDENTIFIER = '0.0.8.245.0.1'
MAX_MESSAGE = 4096


@lru_cache(maxsize=1)
def codec():
    return asn1tools.compile_string(SCHEMA.read_text(encoding='utf-8'), codec='per')


def encode(message):
    """Encode ASN.1 choices as (name,value), with constrained values checked."""
    result = codec().encode('MultimediaSystemControlMessage', message, check_constraints=True)
    if len(result) > MAX_MESSAGE:
        raise ValueError('H.245 message exceeds bounded lab limit')
    return bytes(result)


def decode(payload):
    """Decode exactly one canonical PER message; reject trailing/truncated data."""
    if not payload or len(payload) > MAX_MESSAGE:
        raise ValueError('invalid H.245 message length')
    try:
        message = codec().decode('MultimediaSystemControlMessage', bytes(payload), check_constraints=True)
        if encode(message) != bytes(payload):
            raise ValueError('noncanonical or trailing H.245 data')
        return message
    except (asn1tools.Error, IndexError, TypeError) as exc:
        raise ValueError(f'invalid H.245 PER: {exc}') from exc


def pdu(category, name, value):
    return (category, (name, value))


def audio_capability():
    return ('g7231', {'maxAl-sduAudioFrames': 1, 'silenceSuppression': False})


def video_capability():
    return ('h263VideoCapability', {
        'sqcifMPI': 15, 'maxBitRate': 48, 'unrestrictedVector': False,
        'arithmeticCoding': False, 'advancedPrediction': False, 'pbFrames': False,
        'temporalSpatialTradeOffCapability': False,
    })


def capabilities(sequence=0):
    h223 = {name: False for name in (
        'transportWithI-frames', 'videoWithAL1', 'videoWithAL2', 'videoWithAL3',
        'audioWithAL1', 'audioWithAL2', 'audioWithAL3', 'dataWithAL1', 'dataWithAL2', 'dataWithAL3')}
    h223.update(videoWithAL3=True, audioWithAL2=True, maximumAl2SDUSize=1024,
                maximumAl3SDUSize=1024, maximumDelayJitter=120,
                h223MultiplexTableCapability=('basic', None))
    return pdu('request', 'terminalCapabilitySet', {
        'sequenceNumber': sequence, 'protocolIdentifier': PROTOCOL_IDENTIFIER,
        'multiplexCapability': ('h223Capability', h223),
        'capabilityTable': [
            {'capabilityTableEntryNumber': 1, 'capability': ('receiveAndTransmitAudioCapability', audio_capability())},
            {'capabilityTableEntryNumber': 2, 'capability': ('receiveAndTransmitVideoCapability', video_capability())}],
        'capabilityDescriptors': [{'capabilityDescriptorNumber': 0, 'simultaneousCapabilities': [[1], [2]]}],
    })


def open_channel(channel):
    if channel not in (1, 2):
        raise ValueError('lab profile has only audio channel 1 and video channel 2')
    audio = channel == 1
    value = {
        'forwardLogicalChannelNumber': channel,
        'forwardLogicalChannelParameters': {
            'dataType': ('audioData', audio_capability()) if audio else ('videoData', video_capability()),
            'multiplexParameters': ('h223LogicalChannelParameters', {
                'adaptationLayerType': ('al2WithoutSequenceNumbers', None) if audio else
                    ('al3', {'controlFieldOctets': 1, 'sendBufferSize': 4096}),
                'segmentableFlag': not audio,
            }),
        },
    }
    if not audio:
        value['reverseLogicalChannelParameters'] = {
            'dataType': ('videoData', video_capability()),
            'multiplexParameters': ('h223LogicalChannelParameters', {
                'adaptationLayerType': ('al3', {'controlFieldOctets': 1, 'sendBufferSize': 4096}),
                'segmentableFlag': True,
            }),
        }
    return pdu('request', 'openLogicalChannel', value)


def multiplex_entries(sequence=0):
    return pdu('request', 'multiplexEntrySend', {
        'sequenceNumber': sequence,
        'multiplexEntryDescriptors': [
            {'multiplexTableEntryNumber': n, 'elementList': [
                {'type': ('logicalChannelNumber', n), 'repeatCount': ('untilClosingFlag', None)}]}
            for n in (1, 2)],
    })


@dataclass(frozen=True)
class Event:
    kind: str
    channel: int | None = None
    entry: int | None = None
    media: str | None = None
    adaptation: str | None = None
    control_field_octets: int | None = None
    elements: tuple[tuple[int, int | None], ...] = ()


@dataclass(frozen=True)
class Update:
    outgoing: tuple[bytes, ...] = ()
    events: tuple[Event, ...] = ()
    failure: str | None = None


def channel_event(kind, channel):
    return Event(kind, channel=channel, media='audio' if channel == 1 else 'video',
                 adaptation='al2WithoutSequenceNumbers' if channel == 1 else 'al3',
                 control_field_octets=None if channel == 1 else 1)


class Endpoint:
    """Single-call finite profile. Timers fail cleanly; SRP owns retransmissions.

    Caller applies events in order *before* enqueueing outgoing responses.
    In particular RX entries are installed before ACK; TX entries only after ACK.
    Monotonic ``now`` seconds are supplied by the caller for deterministic tests.
    """
    def __init__(self, status_number=None, terminal_type=128, timeout=15.0):
        self.status_number = secrets.randbelow(1 << 24) if status_number is None else status_number
        if not 0 <= self.status_number < (1 << 24) or not 0 <= terminal_type <= 255 or timeout <= 0:
            raise ValueError('invalid H.245 endpoint configuration')
        self.terminal_type, self.timeout = terminal_type, timeout
        self.failure = None
        self.started = False
        self.role = None
        self.remote_capabilities = False
        self.local_capabilities_acked = False
        self.msd_acked = False
        self.pending = {}
        self.requests = {}
        self.tx_channels, self.rx_channels = set(), set()
        self.tx_entries, self.rx_entries = {}, {}
        self._opening = False
        self._mux_sent = False
        self._ready_emitted = False
        self.closing = self._close_started = False
        self.end_sent = self.end_received = False
        self.close_requests = {}

    @property
    def ready(self):
        return not self.closing and not self.failure and self.local_capabilities_acked and self.remote_capabilities and self.msd_acked and self.role is not None and self.tx_channels == {1, 2} and self.rx_channels == {1, 2} and set(self.tx_entries) == {1, 2} and set(self.rx_entries) == {1, 2}

    @property
    def close_complete(self):
        return self._close_started and not self.tx_channels

    def begin_close(self, now):
        """Close owned outgoing channels after the application drains media."""
        if self._close_started or self.end_sent or self.end_received:
            return Update()
        self.closing = self._close_started = True
        self.pending.clear()
        outgoing = []
        for channel in (2, 1):
            if channel not in self.tx_channels:
                continue
            request = 'requestChannelClose' if channel == 2 and self.role == 'slave' else 'closeLogicalChannel'
            value = {'forwardLogicalChannelNumber': channel}
            if request == 'closeLogicalChannel':
                value['source'] = ('user', None)
            outgoing.append(encode(pdu('request', request, value)))
            self.close_requests[channel] = request
            self.pending[f'close{channel}'] = now+self.timeout
        return Update(outgoing=tuple(outgoing))

    def end_session(self):
        if self.end_sent:
            return Update()
        self.closing = self.end_sent = True
        self.pending.clear()
        return Update(outgoing=(encode(pdu('command', 'endSessionCommand', ('disconnect', None))),),
                      events=(Event('end_session_sent'),))

    @property
    def deadline(self):
        return min(self.pending.values(), default=None)

    def _fail(self, reason):
        self.failure = reason
        self.pending.clear()
        return Update(events=(Event('failure'),), failure=reason)

    def start(self, now):
        if self.end_sent or self.end_received:
            return Update()
        if self.started:
            return Update(failure=self.failure)
        self.started = True
        self.pending.update(tcs=now+self.timeout, msd=now+self.timeout)
        return Update(outgoing=(encode(capabilities()), encode(pdu('request', 'masterSlaveDetermination', {
            'terminalType': self.terminal_type, 'statusDeterminationNumber': self.status_number}))))

    def tick(self, now):
        if self.failure:
            return Update(failure=self.failure)
        expired = [name for name, deadline in self.pending.items() if now >= deadline]
        return self._fail('H.245 timeout: '+','.join(expired)) if expired else Update()

    def _progress(self, now, outgoing, events):
        if self.closing:
            return
        if self.remote_capabilities and self.local_capabilities_acked and self.msd_acked and self.role and not self._opening:
            self._opening = True
            for channel in ((1, 2) if self.role == 'master' else (1,)):
                outgoing.append(encode(open_channel(channel)))
                self.pending[f'olc{channel}'] = now + self.timeout
            if self.role == 'slave' and 2 not in self.rx_channels:
                self.pending['video_request'] = now + self.timeout
        if self.tx_channels == {1, 2} and not self._mux_sent:
            self._mux_sent = True
            outgoing.append(encode(multiplex_entries()))
            self.pending['mux'] = now + self.timeout
        if self.ready and not self._ready_emitted:
            self._ready_emitted = True
            events.append(Event('ready'))

    def _supports_remote(self, value):
        mux = value.get('multiplexCapability', (None, {}))
        if not value['protocolIdentifier'].startswith('0.0.8.245.0.') or mux[0] != 'h223Capability':
            return False
        h = mux[1]
        if not h['audioWithAL2'] or not h['videoWithAL3'] or h['maximumAl2SDUSize'] < 25 or h['maximumAl3SDUSize'] < 128:
            return False
        acceptable = {'audio': set(), 'video': set()}
        for entry in value.get('capabilityTable', []):
            choice, cap = entry.get('capability', (None, (None, None)))
            if choice == 'receiveAndTransmitAudioCapability' and cap[0] == 'g7231':
                acceptable['audio'].add(entry['capabilityTableEntryNumber'])
            if choice == 'receiveAndTransmitVideoCapability' and cap[0] == 'h263VideoCapability' and 'sqcifMPI' in cap[1] and cap[1]['sqcifMPI'] <= 15 and cap[1]['maxBitRate'] >= 48:
                acceptable['video'].add(entry['capabilityTableEntryNumber'])
        for descriptor in value.get('capabilityDescriptors', []):
            groups = descriptor.get('simultaneousCapabilities', [])
            if any(set(audio) & acceptable['audio'] and set(video) & acceptable['video']
                   for i, audio in enumerate(groups) for j, video in enumerate(groups) if i != j):
                return True
        return False

    def receive(self, payload, now):
        if self.failure:
            return Update(failure=self.failure)
        if not self.started:
            return self._fail('start H.245 before receiving peer data')
        try:
            category, (name, value) = decode(payload)
        except (ValueError, asn1tools.Error) as exc:
            return self._fail(str(exc))
        outgoing, events = [], []
        if category == 'command' and name == 'endSessionCommand':
            if value != ('disconnect', None):
                return self._fail('unsupported end-session mode')
            self.closing = self.end_received = True
            self.pending.clear()
            return Update(events=(Event('end_session_received'),))
        if self.end_sent or self.end_received:
            # SRP remains live, but no further H.245 is transmitted after End.
            return Update()
        # Reliable SRP normally removes duplicates; cached requests make replay safe.
        if category == 'request':
            identity = (name, value.get('sequenceNumber', value.get('forwardLogicalChannelNumber', 0)))
            if identity in self.requests:
                old, reply = self.requests[identity]
                if old != bytes(payload):
                    return self._fail('conflicting duplicate H.245 request')
                return Update(outgoing=reply)
            if len(self.requests) >= 32:
                return self._fail('H.245 request cache capacity exceeded')
            if name == 'closeLogicalChannel':
                channel = value['forwardLogicalChannelNumber']
                if value.get('source') != ('user', None) or channel not in self.rx_channels or channel == 2 and self.role != 'slave':
                    return self._fail('invalid logical-channel close')
                self.closing = True
                self.rx_channels.discard(channel)
                if channel == 2:
                    self.tx_channels.discard(2)
                    self.pending.pop('close2', None)
                events.append(Event('peer_closing', channel=channel))
                outgoing.append(encode(pdu('response', 'closeLogicalChannelAck',
                                           {'forwardLogicalChannelNumber': channel})))
            elif name == 'requestChannelClose':
                channel = value['forwardLogicalChannelNumber']
                if (channel != 2 or self.role != 'master' or
                        channel not in self.tx_channels and not (self.closing and channel in self.tx_entries)):
                    return self._fail('invalid bidirectional-channel close request')
                self.closing = True
                events.append(Event('peer_closing', channel=channel))
                outgoing.append(encode(pdu('response', 'requestChannelCloseAck',
                                           {'forwardLogicalChannelNumber': channel})))
            elif name == 'terminalCapabilitySet':
                seq = value['sequenceNumber']
                if not self._supports_remote(value):
                    reply = pdu('response', 'terminalCapabilitySetReject', {'sequenceNumber': seq, 'cause': ('unspecified', None)})
                else:
                    self.remote_capabilities = True
                    events.append(Event('capabilities_received'))
                    reply = pdu('response', 'terminalCapabilitySetAck', {'sequenceNumber': seq})
                outgoing.append(encode(reply))
            elif name == 'masterSlaveDetermination':
                diff = (value['statusDeterminationNumber'] - self.status_number) % (1 << 24)
                if value['terminalType'] == self.terminal_type and diff in (0, 1 << 23):
                    outgoing.append(encode(pdu('response', 'masterSlaveDeterminationReject', {'cause': ('identicalNumbers', None)})))
                else:
                    local_master = self.terminal_type > value['terminalType'] if self.terminal_type != value['terminalType'] else diff > (1 << 23)
                    role = 'master' if local_master else 'slave'
                    if self.role and self.role != role:
                        return self._fail('inconsistent master/slave determination')
                    self.role = role
                    events.append(Event(role))
                    outgoing.append(encode(pdu('response', 'masterSlaveDeterminationAck', {'decision': ('slave' if local_master else 'master', None)})))
            elif name == 'openLogicalChannel':
                channel = value['forwardLogicalChannelNumber']
                expected = open_channel(channel)[1][1] if channel in (1, 2) else None
                if not self.remote_capabilities or not self.role or value != expected or (channel == 2 and self.role != 'slave'):
                    outgoing.append(encode(pdu('response', 'openLogicalChannelReject', {'forwardLogicalChannelNumber': channel, 'cause': ('dataTypeALCombinationNotSupported', None)})))
                else:
                    self.rx_channels.add(channel)
                    events.append(channel_event('rx_channel_open', channel))
                    acknowledgement = {'forwardLogicalChannelNumber': channel}
                    if channel == 2:
                        self.pending.pop('video_request', None)
                        self.pending['video_confirm'] = now + self.timeout
                        acknowledgement['reverseLogicalChannelParameters'] = {'reverseLogicalChannelNumber': 2}
                    outgoing.append(encode(pdu('response', 'openLogicalChannelAck', acknowledgement)))
            elif name == 'multiplexEntrySend':
                seq = value['sequenceNumber']
                expected = multiplex_entries(seq)[1][1]
                if value != expected or self.rx_channels != {1, 2}:
                    outgoing.append(encode(pdu('response', 'multiplexEntrySendReject', {'sequenceNumber': seq, 'rejectionDescriptions': [{'multiplexTableEntryNumber': d['multiplexTableEntryNumber'], 'cause': ('descriptorTooComplex', None)} for d in value['multiplexEntryDescriptors']]})))
                else:
                    for entry in (1, 2):
                        self.rx_entries[entry] = ((entry, None),)
                        events.append(Event('rx_entry_install', entry=entry, elements=((entry, None),)))
                    outgoing.append(encode(pdu('response', 'multiplexEntrySendAck', {'sequenceNumber': seq, 'multiplexTableEntryNumber': [1, 2]})))
            else:
                return self._fail(f'unsupported H.245 request: {name}')
            self.requests[identity] = (bytes(payload), tuple(outgoing))
        elif category == 'response':
            if name.endswith('Reject'):
                return self._fail(f'peer rejected {name}: {value.get("cause", value)}')
            if name in ('closeLogicalChannelAck', 'requestChannelCloseAck'):
                channel = value['forwardLogicalChannelNumber']
                expected = 'requestChannelClose' if name == 'requestChannelCloseAck' else 'closeLogicalChannel'
                if not self.closing or self.close_requests.get(channel) != expected:
                    return self._fail('unsolicited channel-close acknowledgement')
                if f'close{channel}' not in self.pending and channel not in self.tx_channels:
                    return Update()  # Crossed CLC can finish before its earlier RCC ACK.
                if name == 'closeLogicalChannelAck':
                    self.tx_channels.discard(channel)
                    if channel == 2:
                        self.rx_channels.discard(2)
                    self.pending.pop(f'close{channel}', None)
                elif channel != 2 or self.role != 'slave':
                    return self._fail('unexpected request-channel-close acknowledgement')
            elif name == 'terminalCapabilitySetAck':
                if value['sequenceNumber'] != 0:
                    return self._fail('unexpected capability acknowledgement sequence')
                self.local_capabilities_acked = True
                self.pending.pop('tcs', None)
            elif name == 'masterSlaveDeterminationAck':
                role = value['decision'][0]
                if self.role and self.role != role:
                    return self._fail('conflicting master/slave acknowledgement')
                self.role = role
                if not self.msd_acked:
                    self.msd_acked = True
                    self.pending.pop('msd', None)
                    events.append(Event(role))
                    outgoing.append(encode(pdu('response', 'masterSlaveDeterminationAck', {'decision': ('slave' if role == 'master' else 'master', None)})))
            elif name == 'openLogicalChannelAck':
                channel = value['forwardLogicalChannelNumber']
                if channel not in (1, 2) or (f'olc{channel}' not in self.pending and channel not in self.tx_channels):
                    return self._fail('unsolicited logical-channel acknowledgement')
                expected = {'forwardLogicalChannelNumber': channel}
                if channel == 2:
                    if self.role != 'master':
                        return self._fail('only the master initiates the bidirectional video channel')
                    expected['reverseLogicalChannelParameters'] = {'reverseLogicalChannelNumber': 2}
                if value != expected:
                    return self._fail('unsupported logical-channel acknowledgement parameters')
                if channel not in self.tx_channels:
                    self.pending.pop(f'olc{channel}', None)
                    self.tx_channels.add(channel)
                    events.append(channel_event('tx_channel_open', channel))
                    if channel == 2:
                        self.rx_channels.add(channel)
                        events.append(channel_event('rx_channel_open', channel))
                if channel == 2:
                    outgoing.append(encode(pdu('indication', 'openLogicalChannelConfirm', {'forwardLogicalChannelNumber': 2})))
            elif name == 'multiplexEntrySendAck':
                if not self._mux_sent or value['sequenceNumber'] != 0 or set(value['multiplexTableEntryNumber']) != {1, 2} or len(value['multiplexTableEntryNumber']) != 2:
                    return self._fail('unexpected multiplex-entry acknowledgement')
                self.pending.pop('mux', None)
                for entry in (1, 2):
                    if entry not in self.tx_entries:
                        self.tx_entries[entry] = ((entry, None),)
                        events.append(Event('tx_entry_install', entry=entry, elements=((entry, None),)))
            else:
                return self._fail(f'unsupported H.245 response: {name}')
        elif category == 'indication' and name == 'openLogicalChannelConfirm':
            if value != {'forwardLogicalChannelNumber': 2} or self.role != 'slave' or 2 not in self.rx_channels:
                return self._fail('unsolicited bidirectional channel confirmation')
            if 'video_confirm' in self.pending:
                self.pending.pop('video_confirm')
                self.tx_channels.add(2)
                events.append(channel_event('tx_channel_open', 2))
            elif 2 not in self.tx_channels:
                return self._fail('bidirectional channel confirmation without pending request')
        else:
            return self._fail(f'unsupported H.245 {category}: {name}')
        self._progress(now, outgoing, events)
        return Update(tuple(outgoing), tuple(events), self.failure)
