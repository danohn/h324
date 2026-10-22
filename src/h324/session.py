"""One independently negotiated H.324 endpoint with a bounded streaming source.

No receiver is told which file its peer selected. Codec payloads alone cross the
standard H.223 media channels; local file metadata stays on the IPC connection.
"""
from dataclasses import asdict
from collections import Counter, deque
import math
from h324.api import MediaChunk
from h324.lifecycle import SESSION_CLOSE_TIMEOUT

from h324.h223 import Slot, al2_decode, al2_encode
from h324.link import Link
from h324.srp import Endpoint as SRP
from h324.h245 import Endpoint as H245
from h324.al3 import Transmitter, decode as decode_al3


class StreamingSession:
    def __init__(self, emit=None, log=None, *, status_number=None, max_queued=8192,
                 startup_hunt=False, drain_allowance=.2):
        self.drain_allowance = float(drain_allowance)
        if not math.isfinite(self.drain_allowance) or self.drain_allowance < 0:
            raise ValueError('Transport drain allowance must be finite and nonnegative')
        self.emit = emit or (lambda **event: None)
        self.log = log or (lambda *args, **fields: None)
        self.link = Link(self.log, max_queued=max_queued, startup_hunt=startup_hunt)
        self.srp = SRP(timeout=2, max_retries=5, max_pending=32)
        self.h245 = H245(status_number=status_number, timeout=30)
        self.al3_tx = Transmitter(control_size=1, send_buffer_size=4096, max_sdu=96)
        self.video_next = 0
        self.last_video = None
        self.failure = None
        self.started = self.ready_announced = False
        self.counts = Counter()
        self.source = None
        self.cursor = 0
        self.source_start = self.pause_start = self.drained_at = None
        self.paused_seconds = 0
        self.duration_ms = 0
        self.source_label = ''
        self.stopped = False
        self.live = False
        self.video_suspended = False
        self.video_resume_at = 0
        self.video_tokens = 900.0
        self.video_token_time = 0
        self.closing = self.close_ready = False
        self.close_phase = None
        self.close_deadline = self.close_drained_at = None
        self.close_outcome = None
        self.end_sent_at = None
        self.end_received_bit_boundary = None

    @property
    def ready(self):
        return self.h245.ready and not self.closing and not self.failure

    @property
    def media_active(self):
        return self.source is not None or self.live

    def _fail(self, message):
        if self.failure is None:
            self.failure = message
            self.log('session_failure', reason=message)
            self.emit(event='error', message=message)

    def _srp_update(self, update, now):
        if update.failure:
            self._fail('SRP: ' + update.failure)
        for frame in update.outgoing:
            self.link.queue_sdu(0, frame, 0, True)
        for payload in update.delivered:
            self._h245_update(self.h245.receive(payload, now), now)

    def _h245_update(self, update, now):
        for event in update.events:
            self.log('h245_event', **asdict(event))
            if event.kind == 'rx_channel_open':
                self.link.demux.open_channels.add(event.channel)
            elif event.kind in ('rx_entry_install', 'tx_entry_install'):
                table = self.link.rx_table if event.kind.startswith('rx') else self.link.tx_table
                table.install(event.entry, [Slot(lcn, count) for lcn, count in event.elements])
            elif event.kind in ('peer_closing', 'end_session_received'):
                if event.kind == 'end_session_received' and self.end_received_bit_boundary is None:
                    end = self.link.current_pdu_end_bit
                    if end is not None:
                        # A closing flag can also open the next candidate PDU.
                        self.end_received_bit_boundary = end-8
                self.begin_close(now)
        if update.failure:
            self._fail('H.245: ' + update.failure)
        for payload in update.outgoing:
            self._srp_update(self.srp.send(payload, now), now)
        if self.ready and not self.ready_announced:
            self.ready_announced = True
            self.emit(event='state', state='connected', message='Audio and video channels ready')

    def begin_close(self, now, timeout=SESSION_CLOSE_TIMEOUT):
        if not math.isfinite(timeout) or timeout <= 0 or not math.isfinite(now):
            raise ValueError('Close timeout must be finite and positive')
        if self.closing:
            return
        self.closing = True
        self.close_phase = 'media_drain'
        self.close_deadline = now+timeout
        self.live = False
        self.source = None  # Already admitted complete pictures remain queued.
        self.pause_start = None
        self.emit(event='state', state='hanging_up', message='Ending the multimedia session')

    def _close_tick(self, now):
        if self.close_ready:
            return
        if now >= self.close_deadline:
            self.close_ready = True
            self.close_outcome = 'timeout'
            self.emit(event='log', message='Graceful session close timed out; restoring modem')
            return
        if self.h245.end_received and not self.h245.end_sent and self.close_phase != 'reply_drain':
            self.close_phase = 'reply_drain'
            self.close_drained_at = None
        if self.close_phase in ('media_drain', 'reply_drain', 'end_drain'):
            if self.link.pending or self.close_phase == 'end_drain' and not self.srp.idle:
                self.close_drained_at = None
                return
            if self.close_drained_at is None:
                self.close_drained_at = now
            if now-self.close_drained_at < self.drain_allowance:
                return
            if self.close_phase == 'end_drain':
                # A disconnect need not await peer EndSession (H.324 7.6), but
                # permit its optional reply briefly for coordinated cleardown.
                if not self.h245.end_received and now-self.end_sent_at < 2:
                    return
                self.close_ready = True
                self.close_outcome = 'peer_end_received' if self.h245.end_received else 'end_delivered'
            elif self.close_phase == 'reply_drain':
                self._h245_update(self.h245.end_session(), now)
                self.end_sent_at = now
                self.close_phase = 'end_drain'
                self.close_drained_at = None
            else:
                self._h245_update(self.h245.begin_close(now), now)
                self.close_phase = 'channels'
        if self.close_phase == 'channels' and self.h245.close_complete and self.srp.idle:
            self._h245_update(self.h245.end_session(), now)
            self.end_sent_at = now
            self.close_phase = 'end_drain'
            self.close_drained_at = None

    def start(self, now):
        if self.started:
            raise RuntimeError('session can only be started once')
        self.started = True
        self._h245_update(self.h245.start(now), now)

    def feed(self, raw, now):
        if self.failure:
            return
        try:
            for sdu in self.link.feed(raw):
                if sdu.lcn == 0:
                    self._srp_update(self.srp.receive(sdu.payload, now), now)
                else:
                    self._receive_media(sdu.lcn, sdu.payload)
        except (ValueError, BufferError) as exc:
            self._fail(str(exc))

    def _receive_media(self, lcn, pdu):
        if lcn == 1:
            payload, _ = al2_decode(pdu, False)
            self.counts['audio_frames'] += 1
        elif lcn == 2:
            value = decode_al3(pdu, 1)
            if not value.information:
                response = self.al3_tx.receive(pdu)
                if response is not None:
                    self.link.queue_sdu(2, response, 2, True)
                if self.al3_tx.consume_force_intra():
                    if self.live:
                        self.video_suspended = True
                        self.video_resume_at = self.video_token_time + 1
                        self.emit(event='video_overload', message='Peer requested a fresh INTRA picture')
                    else:
                        raise ValueError('Peer requested expired video; this prepared-file sender cannot force an INTRA picture')
                return
            payload, sequence = value.payload, value.sequence
            if sequence != self.video_next:
                # An immediately repeated cached I-PDU is harmless. This check
                # works across sequence wrap without retaining unbounded history.
                if self.last_video == (sequence, payload):
                    self.counts['video_duplicates'] += 1
                    return
                raise ValueError(f'Video AL3 sequence discontinuity: expected {self.video_next}, got {sequence}')
            self.last_video = (sequence, payload)
            self.video_next = (sequence + 1) % 128
            self.counts['video_sdus'] += 1
            if payload[:2] == b'\0\0' and len(payload) >= 3 and payload[2] & 0xfc == 0x80:
                self.counts['video_frames'] += 1
        else:
            raise ValueError(f'Unexpected media channel {lcn}')
        self.counts['rx_media_bytes'] += len(payload)
        self.emit(event='media', lcn=lcn, payload=payload)

    def send_media(self, sdus, duration_ms, now, label=''):
        if not self.ready:
            raise RuntimeError('Call must be connected before sending a movie')
        if self.media_active:
            raise RuntimeError('A movie is already being sent; wait for it to finish')
        source = tuple(sdus)
        if not source or duration_ms <= 0:
            raise ValueError('Movie must contain codec data and have positive duration')
        previous = -1
        for item in source:
            if item.lcn not in (1, 2) or not item.payload or item.timestamp_ms < previous:
                raise ValueError('Invalid or unsorted movie SDU')
            if item.timestamp_ms < 0 or item.timestamp_ms >= duration_ms:
                raise ValueError('Movie timestamp outside duration')
            if item.lcn == 2 and len(item.payload) > 96:
                raise ValueError('Movie video SDU exceeds negotiated sender bound')
            previous = item.timestamp_ms
        self.source = source
        self.cursor = 0
        self.source_start = now + .1
        self.pause_start = self.drained_at = None
        self.paused_seconds = 0
        self.duration_ms = duration_ms
        self.source_label = label
        self.stopped = False
        self.emit(event='media_started', label=label, duration_ms=duration_ms)

    def start_live(self, now, label='Live media'):
        if not self.ready:
            raise ValueError('Call must be connected before transmitting')
        if self.media_active:
            raise ValueError('A media source is already active')
        self.live = True
        self.source_start = now
        self.pause_start = None
        self.paused_seconds = 0
        self.duration_ms = 0
        self.source_label = label
        self.video_tokens = 900.0
        self.video_token_time = now
        self.video_suspended = False
        self.emit(event='media_started', label=label, duration_ms=0, live=True)

    def send_live(self, lcn, payload, now, timestamp_ms=0, duration_ms=30):
        if not self.live:
            raise ValueError('Start a live source before sending chunks')
        MediaChunk(lcn, payload, timestamp_ms, duration_ms)
        if self.pause_start is not None:
            return False
        if lcn == 1:
            # Video is admitted below a separate low watermark, leaving room for
            # audio and SRP/H.245 even during high-motion capture.
            pdu = al2_encode(payload)
            self.link.queue_sdu(1, pdu, 1, False)
            self.counts['tx_media_bytes'] += len(payload)
            return True
        self.video_tokens = min(900.0, self.video_tokens + max(0, now-self.video_token_time)*600)
        self.video_token_time = now
        needed = len(payload) + 3*((len(payload)+95)//96)
        if self.video_suspended:
            self.counts['video_pictures_rejected'] += 1
            return False
        if len(payload) > self.video_tokens or self.link.queued_bytes + needed > 1536:
            self.video_suspended = True
            self.video_resume_at = now + 1
            self.counts['video_pictures_rejected'] += 1
            self.emit(event='video_overload', message='Video exceeded the bounded dial-up budget; audio continues')
            return False
        self.video_tokens -= len(payload)
        # Complete-picture admission is atomic. Once accepted, every AL3 SDU and
        # MUX fragment drains; subsequent overload never truncates this picture.
        for offset in range(0, len(payload), 96):
            part = payload[offset:offset+96]
            self.link.queue_sdu(2, self.al3_tx.send(part), 2, True)
            self.counts['tx_media_bytes'] += len(part)
        return True

    def pause(self, now):
        if self.media_active and self.pause_start is None:
            self.pause_start = now

    def resume(self, now):
        if self.pause_start is not None:
            self.paused_seconds += now - self.pause_start
            self.pause_start = None

    def stop_media(self, now):
        if self.live:
            self.live = False
            self.pause_start = None
            self.emit(event='media_finished', label=self.source_label, stopped=True, duration_ms=0)
            return
        if self.media_active:
            # Drain complete already queued SDUs. Discarding remaining fragments
            # of a segmentable SDU would corrupt the next movie's first picture.
            self.cursor = len(self.source)
            self.resume(now)
            self.stopped = True

    def media_position(self, now):
        if self.source_start is None:
            return 0
        endpoint = self.pause_start if self.pause_start is not None else now
        position = max(0, int((endpoint - self.source_start - self.paused_seconds) * 1000))
        return position if self.live else min(self.duration_ms, position)

    def tick(self, now):
        if self.failure:
            return
        try:
            self._srp_update(self.srp.tick(now), now)
            self._h245_update(self.h245.tick(now), now)
            if self.closing:
                self._close_tick(now)
                return
            if self.live and self.video_suspended and now >= self.video_resume_at and self.link.queued_bytes < 256:
                self.video_suspended = False
                self.video_tokens = min(900.0, self.video_tokens + max(0, now-self.video_token_time)*600)
                self.video_token_time = now
                self.emit(event='video_reset', message='Restart video encoder with a fresh INTRA picture')
            if self.source is None or self.pause_start is not None:
                return
            position = self.media_position(now)
            while self.cursor < len(self.source) and self.source[self.cursor].timestamp_ms <= position:
                if now < self.source_start:
                    break
                item = self.source[self.cursor]
                pdu = al2_encode(item.payload) if item.lcn == 1 else self.al3_tx.send(item.payload)
                self.link.queue_sdu(item.lcn, pdu, item.lcn, item.lcn == 2)
                self.counts['tx_media_bytes'] += len(item.payload)
                self.cursor += 1
            if self.cursor == len(self.source) and not self.link.pending:
                if self.drained_at is None:
                    self.drained_at = now
                # Transport-supplied nominal time for queued wire/USB data to
                # drain. This elapsed allowance is not a modem or remote ACK.
                if now - self.drained_at >= self.drain_allowance and (self.stopped or position >= self.duration_ms):
                    self.source = None
                    self.emit(event='media_finished', label=self.source_label, stopped=self.stopped,
                              duration_ms=self.duration_ms)
            else:
                self.drained_at = None
        except (ValueError, BufferError) as exc:
            self._fail(str(exc))

    def summary(self):
        return dict(failure=self.failure, ready=self.ready, role=self.h245.role,
                    drain_allowance_seconds=self.drain_allowance,
                    close=dict(phase=self.close_phase, ready=self.close_ready,
                               outcome=self.close_outcome, end_sent=self.h245.end_sent,
                               end_received=self.h245.end_received,
                               received_end_bit_boundary=self.end_received_bit_boundary,
                               termination_tail_errors=self.termination_tail_errors),
                    counts=dict(self.counts), srp_counts=dict(self.srp.counts),
                    al3_counts=dict(self.al3_tx.counts), transport=self.link.summary())

    @property
    def termination_tail_errors(self):
        framer = self.link.framer
        boundary = self.end_received_bit_boundary
        carrier_ended_earlier = boundary is not None and any(
            event['name'] in ('eot', 'dcd_off') and event.get('data_octet_offset', -1)*8 < boundary
            for event in self.link.status)
        if boundary is not None and not carrier_ended_earlier and framer.error_count and framer.first_error_bit_offset >= boundary:
            return framer.error_count
        return 0
