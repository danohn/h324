"""Integrated bounded lab H.223/SRP/H.245 session, independent of serial I/O.

Only LCN0/entry0 exists initially. Media entries are activated by H.245 events.
This finite profile is an engineering prototype, not full H.324 conformance.
"""
from collections import Counter, deque
from functools import wraps
import threading
from dataclasses import asdict
import hashlib

from h324.h223 import Table, Slot, Multiplexer, Demultiplexer, WireEncoder, Framer, al2_decode
from h324.v80 import Decoder
from h324.srp import Endpoint as SRP, COMMAND, RESPONSE


def _tx_synchronized(method):
    @wraps(method)
    def locked(self, *args, **kwargs):
        with self._tx_lock:
            return method(self, *args, **kwargs)
    return locked


def digest(data):
    return hashlib.sha256(data).hexdigest()


class Link:
    """Choose a complete MUX fragment at a time: control, audio, then video.

    Keep bit packing continuous while allowing audio between video fragments.
    The encoded residual is at most one fragment; abstract queues are bounded.
    """
    def __init__(self, log, max_queued=32768, *, startup_hunt=False):
        self.log = log
        self._tx_lock = threading.RLock()
        self.tx_table, self.rx_table = Table(), Table()
        self.mux = Multiplexer(self.tx_table, {0, 2})
        self.demux = Demultiplexer(self.rx_table, {0, 2}, {0})
        self.encoder, self.framer, self.decoder = WireEncoder(), Framer(startup_hunt=startup_hunt), Decoder()
        self.queues = [deque(), deque(), deque()]
        self.encoded = bytearray(self.encoder.idle(16))
        self.max_queued = max_queued
        self.status, self.pdu_counts, self.phases = [], Counter(), Counter()
        self.queued_bytes = 0
        self.high_water = 0
        self.last_decoded = b''
        self.active_underruns = 0

    @property
    @_tx_synchronized
    def pending(self):
        return bool(self.encoded or any(self.queues) or self.mux.pending_end)

    @_tx_synchronized
    def queue_sdu(self, mc, data, priority, segmentable):
        if not data or not 0 <= priority <= 2:
            raise ValueError('nonempty SDU and priority0..2 required')
        self.tx_table.layout(mc, len(data))  # No use of an unacknowledged entry.
        if self.queued_bytes + len(self.encoded) + len(data) > self.max_queued:
            raise BufferError('bounded H.223 transmit queue full')
        n = 48 if segmentable else len(data)
        for i in range(0,len(data),n):
            chunk = data[i:i+n]
            self.queues[priority].append((mc,chunk,segmentable and i+n >= len(data)))
            self.queued_bytes += len(chunk)
        self.high_water = max(self.high_water,self.queued_bytes)

    @_tx_synchronized
    def pull(self, count):
        if not 1 <= count <= 255:
            raise ValueError('line pull must be1..255 octets')
        out = bytearray()
        while len(out) < count:
            if self.encoded:
                n = min(count-len(out),len(self.encoded))
                out.extend(self.encoded[:n])
                del self.encoded[:n]
                continue
            selected = next((q for q in self.queues if q), None)
            if selected is not None:
                mc,data,end = selected.popleft()
                self.queued_bytes -= len(data)
                self.encoded.extend(self.encoder.pdu(self.mux.send(mc,data,end)))
            elif self.mux.pending_end:
                self.encoded.extend(self.encoder.pdu(self.mux.finish()))
            else:
                out.extend(self.encoder.idle(count-len(out)))
        return bytes(out)

    def feed(self, raw):
        data, events = self.decoder.feed(raw)
        self.last_decoded = data
        for event in events:
            if event['name']=='under' and self.pdu_counts:
                self.active_underruns += 1
            self.status.append(event)
            self.log('v80_status',**event)
        # Drive higher layers before decoding the next PDU: an H.245 message may
        # activate the very table entry used by the following PDU in this read.
        for pdu in self.framer.feed(data):
            self.pdu_counts[pdu.mc] += 1
            self.phases[pdu.bit_offset % 8] += 1
            self.log('h223_pdu',mc=pdu.mc,pm=pdu.pm,bytes=len(pdu.information),
                     bit_offset=pdu.bit_offset,discontinuity=pdu.discontinuity)
            for sdu in self.demux.feed(pdu):
                yield sdu

    @_tx_synchronized
    def summary(self):
        return dict(tx_entries=sorted(self.tx_table.entries),rx_entries=sorted(self.rx_table.entries),
            rx_open_channels=sorted(self.demux.open_channels),pdu_codes=dict(self.pdu_counts),
            bit_phases=dict(self.phases),status_counts=dict(Counter(s['name'] for s in self.status)),
            statuses=self.status,mux_counts=dict(self.framer.counts),mux_errors=list(self.framer.errors),
            demux_counts=dict(self.demux.counts),demux_events=list(self.demux.events),
            partial_sdu_bytes={str(k):len(v) for k,v in self.demux.partial.items()},
            queue_high_water_bytes=self.high_water,queued_bytes=self.queued_bytes,
            active_underruns=self.active_underruns,
            startup_acquisition=dict(acquiring=self.framer.acquiring,
                locked_bit_offset=self.framer.startup_locked_offset,
                rejected_candidates=sum(self.framer.startup_counts.values()),
                rejection_counts=dict(self.framer.startup_counts),
                rejections=list(self.framer.startup_errors),
                timed_out=self.framer.startup_timed_out))


def control_payloads(sender, count=8):
    return [b'SRP lab '+bytes((sender,i))+bytes(range(256))[:[31,64,128,256][i%4]]
            for i in range(count)]


class Session:
    def __init__(self, name, index, stage='srp', log=None, faults=False, fixture=None,
                 status_number=None):
        if stage not in ('srp','h245','media') or index not in (0,1):
            raise ValueError('stage srp/h245/media and index0/1 required')
        self.name,self.index,self.stage = name,index,stage
        self.log = log or (lambda *args,**kwargs: None)
        self.link = Link(self.log)
        self.srp = SRP(timeout=2.0,max_retries=5,max_pending=32,initial_sequence=index*128)
        self.failure = None
        self.started = False
        self.received_control = []
        self.acknowledged = []
        self.protocol_events = []
        self.faults = Counter(drop_command=int(faults),corrupt_command=int(faults),drop_ack=int(faults))
        self.injected = Counter()
        self.h245 = None
        self.fixture = list(fixture or [])
        self.media_started_at = None
        self.media_cursor = 0
        self.received_media = []
        self.media_errors = []
        self.video_received = {}
        self.video_next = 0
        self.requested_retransmission = False
        self.retransmission_verified = False
        self.al3_tx = None
        if stage != 'srp':
            from h324.h245 import Endpoint
            self.h245 = Endpoint(status_number=status_number,timeout=30.0)
        if stage == 'media':
            from h324.al3 import Transmitter
            self.al3_tx = Transmitter(control_size=1,send_buffer_size=4096,max_sdu=96)
            if not self.fixture:
                raise ValueError('media stage requires timestamped codec fixture')

    def _fail(self,reason):
        self.failure = reason
        self.log('session_failure',reason=reason)

    def _srp_update(self, update, now):
        for event in update.events:
            self.log('srp_event',name=event)
        if update.failure:
            self._fail('SRP: '+update.failure)
        self.acknowledged.extend(update.acknowledged)
        for frame in update.outgoing:
            self.log('srp_tx',header=frame[0],bytes=len(frame),sha256=digest(frame))
            self.link.queue_sdu(0,frame,0,True)
        for payload in update.delivered:
            self.received_control.append(payload)
            self.log('control_delivered',bytes=len(payload),sha256=digest(payload))
            if self.h245 is not None:
                self._h245_update(self.h245.receive(payload,now),now)

    def _h245_update(self,update,now):
        for event in update.events:
            value=asdict(event)
            self.protocol_events.append(value)
            self.log('h245_event',**value)
            if event.kind == 'rx_channel_open':
                self.link.demux.open_channels.add(event.channel)
            elif event.kind in ('rx_entry_install','tx_entry_install'):
                table=self.link.rx_table if event.kind.startswith('rx') else self.link.tx_table
                table.install(event.entry,[Slot(lcn,count) for lcn,count in event.elements])
        if update.failure:
            self._fail('H.245: '+update.failure)
        for payload in update.outgoing:
            self.log('h245_tx',bytes=len(payload),hex=payload.hex())
            self._srp_update(self.srp.send(payload,now),now)

    def start(self,now):
        if self.started:
            raise RuntimeError('single-call session already started')
        self.started=True
        if self.h245 is None:
            for payload in control_payloads(self.index):
                self._srp_update(self.srp.send(payload,now),now)
        else:
            self._h245_update(self.h245.start(now),now)

    def _filter(self,frame):
        command = frame and frame[0] == COMMAND
        ack = frame and frame[0] == RESPONSE
        if command and self.faults['drop_command']:
            fault='drop_command'
        elif command and self.faults['corrupt_command']:
            fault='corrupt_command'
        elif ack and self.faults['drop_ack']:
            fault='drop_ack'
        else:
            return frame
        self.faults[fault]-=1
        self.injected[fault]+=1
        self.log('fault_injected',name=fault,location='receiver_after_AL1',sha256=digest(frame))
        if fault=='corrupt_command':
            return frame[:2]+bytes((frame[2]^1,))+frame[3:]
        return None

    def feed(self,raw,now):
        for sdu in self.link.feed(raw):
            if sdu.lcn==0:
                frame=self._filter(sdu.payload)
                if frame is not None:
                    self._srp_update(self.srp.receive(frame,now),now)
            else:
                self._media_receive(sdu.lcn,sdu.payload)

    def _media_receive(self,lcn,pdu):
        try:
            if lcn==1:
                payload,sequence=al2_decode(pdu,False)
            elif lcn==2:
                from h324.al3 import decode
                value=decode(pdu,1)
                if not value.information:
                    response=self.al3_tx.receive(pdu)
                    if response is not None:
                        self.link.queue_sdu(2,response,2,True)
                    if self.al3_tx.consume_force_intra():
                        raise ValueError('AL3 retransmission unavailable; offline encoder cannot force next INTRA')
                    return
                payload,sequence=value.payload,value.sequence
                if self.requested_retransmission and sequence==0 and sequence in self.video_received:
                    if self.video_received[sequence]!=payload:
                        raise ValueError('AL3 retransmission content differs')
                    self.retransmission_verified=True
                    self.log('al3_retransmission_verified',sequence=sequence,sha256=digest(payload))
                    return
                if sequence != self.video_next:
                    raise ValueError('unexpected AL3 sequence')
                self.video_received[sequence]=payload
                self.video_next=(self.video_next+1)%128
            else:
                raise ValueError('unknown media channel')
            self.received_media.append(dict(lcn=lcn,payload=payload,sequence=sequence))
            self.log('media_received',lcn=lcn,bytes=len(payload),sequence=sequence,sha256=digest(payload))
        except ValueError as exc:
            self.media_errors.append(str(exc))
            self._fail(str(exc))

    def tick(self,now):
        if self.failure:
            return
        self._srp_update(self.srp.tick(now),now)
        if self.h245 is not None:
            self._h245_update(self.h245.tick(now),now)
        if self.stage=='media' and self.h245.ready:
            if self.media_started_at is None:
                self.media_started_at=now+.3
                self.log('media_scheduled',start=self.media_started_at)
            while self.media_cursor<len(self.fixture) and self.fixture[self.media_cursor].timestamp_ms/1000 <= now-self.media_started_at:
                from h324.media import adapt
                sdu=self.fixture[self.media_cursor]
                encoded=adapt(sdu) if sdu.lcn==1 else self.al3_tx.send(sdu.payload)
                self.link.queue_sdu(sdu.lcn,encoded,sdu.lcn,sdu.lcn==2)
                self.media_cursor+=1
            if len(self.received_media)==len(self.fixture) and not self.requested_retransmission:
                from h324.al3 import supervisory,SREJ
                self.requested_retransmission=True
                self.link.queue_sdu(2,supervisory(0,SREJ,1),2,True)
                self.log('al3_srej_test_request',sequence=0)

    @property
    def complete(self):
        if self.failure or not self.started or not self.srp.idle or self.link.pending:
            return False
        if self.stage=='srp':
            return self.received_control==control_payloads(1-self.index)
        if not self.h245.ready:
            return False
        return self.stage=='h245' or (self.media_cursor==len(self.fixture) and
            len(self.received_media)==len(self.fixture) and self.retransmission_verified)

    def summary(self):
        media_hashes={str(lcn):digest(b''.join(m['payload'] for m in self.received_media if m['lcn']==lcn))
                      for lcn in (1,2)}
        return dict(name=self.name,stage=self.stage,complete=self.complete,failure=self.failure,
            srp_counts=dict(self.srp.counts),srp_pending=self.srp.pending_count,
            control_deliveries=len(self.received_control),acknowledged=len(self.acknowledged),
            injected_faults=dict(self.injected),h245_ready=self.h245.ready if self.h245 else None,
            h245_role=self.h245.role if self.h245 else None,h245_events=self.protocol_events,
            link=self.link.summary(),media_sdus=len(self.received_media),media_hashes=media_hashes,
            media_errors=self.media_errors,al3_retransmission_verified=self.retransmission_verified,
            al3_tx_counts=dict(self.al3_tx.counts) if self.al3_tx else {})
