"""One modem per process. JSON-lines IPC is the only stdout output.

Run with ``python -m h324.worker --station station``. The desktop
controller owns stdin/stdout; this process alone owns its selected serial port.
"""
import argparse
import base64
from collections import deque
import contextlib
import json
from pathlib import Path
import queue
import re
import signal
import os
import io
import subprocess
import sys
import threading
import time

from h324.modem import Modem, FAILURES, parse_carrier_report
from h324.v80 import shield
from h324.modem import configure, restore_close
from h324.h245 import codec
from h324.session import StreamingSession
from h324.api import PROTOCOL_VERSION, ModemProfile


class NullCapture:
    def write(self, data):
        return len(data)
    def close(self):
        pass


class Cancelled(Exception):
    """Normal user cancellation, including during modem initialization."""


class ResponsiveModem(Modem):
    """Existing modem recipe with cooperative cancellation in command waits."""
    def __init__(self, *args, check_cancel=None, **kwargs):
        self.check_cancel = check_cancel or (lambda: None)
        super().__init__(*args, **kwargs)

    def _sleep(self, duration):
        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            self.check_cancel()
            time.sleep(min(.02, max(0, deadline - time.monotonic())))

    def command(self, command, timeout=4, required=True):
        if self.state != 'command':
            raise RuntimeError(f'Refusing AT command in {self.state} mode')
        self.check_cancel()
        self.send(command.encode('ascii') + b'\r')
        deadline, result = time.monotonic() + timeout, []
        while time.monotonic() < deadline:
            self.check_cancel()
            for line in self.lines():
                result.append(line)
                if line == 'OK' or line in FAILURES:
                    if required and line != 'OK':
                        raise RuntimeError(f'{self.name}: {command}: {line}')
                    return result
            self._sleep(.02)
        raise TimeoutError(f'{self.name}: no final response to {command}')

    def idle(self):
        self.port.rtscts = False
        self.port.dtr = False
        self.log('dtr', value=False, purpose='return_to_idle')
        self._sleep(1.2)
        self.port.dtr = True
        self.log('dtr', value=True)
        self._sleep(1.2)
        self.read()
        self.buffer = b''
        if self.state != 'command':
            self.send(b'+++')
            self._sleep(1.2)
            self.lines()
        self.state = 'command'
        self.command('AT')
        self.command('ATH0')
        self.command('ATS0=0')
        self.line_status()


def read_commands(stream, inbox):
    """The reader can block on stdin without blocking the serial pacer."""
    for line in stream:
        try:
            if len(line) > 65536:
                raise ValueError('IPC command exceeds 64 KiB')
            command = json.loads(line)
            if isinstance(command, dict) and command.get('protocol_version', 1) != PROTOCOL_VERSION:
                raise ValueError('Incompatible worker IPC version')
            if not isinstance(command, dict) or not isinstance(command.get('op'), str):
                raise ValueError('IPC command must be an object with an op string')
        except (ValueError, TypeError) as exc:
            command = {'op': '_invalid', 'message': str(exc)}
        inbox.put(command)
    inbox.put({'op': 'shutdown'})


class Worker:
    def __init__(self, station, emit, *, inbox=None, modem_factory=ResponsiveModem):
        self.station = str(station)
        self._emit_callback = emit
        self.ipc_overload = False
        self.inbox = inbox if inbox is not None else queue.Queue(maxsize=100)
        self.modem_factory = modem_factory
        self.modem = self.session = None
        self.directory = None
        self.files = {}
        self.state = 'idle'
        self.shutdown_requested = False
        self.cleanup_mode = False
        self.connected_at = self.last_tx = self.last_rx = None
        self.tx_bytes = self.rx_bytes = 0
        self.rate = None
        self.carrier_report = None
        self.last_rate_status = None
        self.negotiation = []
        self.connect_buffer = bytearray()
        self.deferred = deque()
        self.bundle_result = queue.Queue(maxsize=1)
        self.bundle_loading = False
        self.call_generation = 0
        self.last_error = None

    def emit(self, **event):
        if self.ipc_overload:
            return
        try:
            self._emit_callback(protocol_version=PROTOCOL_VERSION, **event)
        except (BufferError, BrokenPipeError, OSError):
            # Never let a blocked consumer bypass the DTR/profile finally path.
            self.ipc_overload = True

    def set_state(self, state, message):
        self.state = state
        self.emit(event='state', state=state, message=message)

    def _session_event(self, **event):
        if event['event'] == 'media':
            payload = event['payload']
            self.files['audio' if event['lcn'] == 1 else 'video'].write(payload)
            event['payload'] = base64.b64encode(payload).decode('ascii')
        elif event['event'] == 'state':
            self.state = event['state']
        self.emit(**event)

    def _check_cancel(self):
        if self.cleanup_mode:
            return
        if self.shutdown_requested or self.ipc_overload:
            raise Cancelled()
        try:
            while True:
                command = self.inbox.get_nowait()
                if command['op'] in ('hangup', 'shutdown'):
                    self.shutdown_requested |= command['op'] == 'shutdown'
                    raise Cancelled()
                if len(self.deferred) >= 100:
                    raise BufferError('Bounded command queue exceeded during modem initialization')
                self.deferred.append(command)
        except queue.Empty:
            pass

    def _ownership_check(self, port):
        if not isinstance(port, str) or not port.strip():
            raise ValueError('Select an explicit serial modem port')
        if os.name == 'nt':
            normalized = port[4:] if port.startswith('\\\\.\\') else port
            if not re.fullmatch(r'COM[1-9][0-9]*', normalized, re.IGNORECASE):
                raise ValueError('Select a Windows COM port')
            return
        if not port.startswith('/dev/'):
            raise ValueError('Select an explicit /dev/ serial modem port')
        # Serial(exclusive=True) remains the race-safe ownership check. lsof
        # additionally explains an already open port before changing any state.
        try:
            owners = subprocess.run(['lsof', '-nP', port], capture_output=True, text=True, timeout=5)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return
        if owners.returncode == 0:
            raise RuntimeError('Serial port is already in use: ' + owners.stdout.strip())

    def _initialize(self, command):
        if self.modem is not None:
            raise ValueError('This window already owns a modem call')
        port = command.get('port')
        number = str(command.get('number', ''))
        if command['op'] == 'dial' and not re.fullmatch(r'[0-9*#]+', number):
            raise ValueError('Dial number must contain digits, * or #')
        self._ownership_check(port)
        if command.get('profile', ModemProfile.CX93001_V80_14400.value) != ModemProfile.CX93001_V80_14400.value:
            raise ValueError('Unsupported modem profile')
        directory = Path(command['log_dir']).expanduser().resolve() if command.get('log_dir') else None
        if directory:
            directory.mkdir(parents=True, exist_ok=False)
        self.directory = directory
        self.call_generation += 1
        self.last_error = None
        self.rate = self.last_rate_status = self.connected_at = None
        self.carrier_report = None
        self.last_tx = self.last_rx = None
        self.tx_bytes = self.rx_bytes = 0
        self.negotiation = []
        self.connect_buffer.clear()
        self.set_state('initializing', 'Preparing V.80 synchronous modem access')
        self.modem = self.modem_factory(port, 'station', directory,
                                        check_cancel=self._check_cancel)
        configure(self.modem, 'V32B', 'rts')
        self.settings = {'configured_mode': 'V32B', 'configured_rate': 14400}
        self.session = StreamingSession(self._session_event, self.modem.log, startup_hunt=True)
        self.files = {name: (directory / filename).open('wb') if directory else NullCapture()
                      for name, filename in (('audio', 'received-audio.g723'),
                                             ('video', 'received-video.h263'),
                                             ('rx', 'wire-rx.bin'), ('tx', 'wire-tx.bin'))}
        if command['op'] == 'listen':
            self.modem.command('ATS0=1')
            self.modem.state = 'answering'
            self.set_state('listening', 'Waiting for an incoming call')
        else:
            self.modem.state = 'dialling'
            self.modem.send(f'ATDT{number}\r'.encode('ascii'))
            self.set_state('dialing', 'Calling ' + number)
        self.connect_deadline = time.monotonic() + min(180, max(5, float(command.get('timeout', 120))))

    def _negotiate(self, raw, now):
        """Text CONNECT parsing preserves every following binary octet."""
        self.connect_buffer.extend(raw)
        if len(self.connect_buffer) > 8192:
            raise ValueError('Oversized modem negotiation response')
        while b'\n' in self.connect_buffer:
            line, rest = bytes(self.connect_buffer).split(b'\n', 1)
            self.connect_buffer[:] = rest
            text = line.strip(b'\r').decode('ascii', errors='replace')
            if not text:
                continue
            self.negotiation.append(text)
            self.modem.log('negotiation', line=text)
            self.emit(event='log', message=text)
            if text in FAILURES:
                self.modem.state = 'command'
                raise RuntimeError('Modem: ' + text)
            if re.fullmatch(r'CONNECT(?:\s.*)?', text):
                self.carrier_report = parse_carrier_report(self.negotiation)
                self.emit(event='carrier_report', report=self.carrier_report)
                self.modem.state = 'data'
                self.connected_at = now
                self.set_state('negotiating', 'Carrier connected; negotiating audio and video')
                self.session.start(now)
                # H.223 6.3.1 permits consecutive full flags. Let V.80/remote
                # carrier acquisition settle before sending any queued SRP.
                self.protocol_release_at = now + 1.0
                self.acquisition_deadline = now + 5.0
                self.emit(event='acquisition', phase='hunt', idle_preamble_ms=1000, timeout_ms=5000)
                if self.connect_buffer:
                    self._feed(bytes(self.connect_buffer), now)
                    self.connect_buffer.clear()
                self.modem.send(b'\x19\xb0')  # Explicit transparent submode.
                self._send(216, now)
                self.next_send = now + .02
                return

    def _feed(self, raw, now):
        old = len(self.session.link.status)
        self.session.feed(raw, now)
        decoded = self.session.link.last_decoded
        self.files['rx'].write(decoded)
        self.rx_bytes += len(decoded)
        if decoded:
            self.last_rx = now
        for status in self.session.link.status[old:]:
            if status['name'] == 'rate':
                self.last_rate_status = status
                tx, rx = status['tx_bps'], status['rx_bps']
                self.rate = tx if tx == rx else None
                if self.rate is None:
                    self.emit(event='log', message='Modem rate report is unspecified; requested rate is 14,400 bps')
                elif self.rate != 14400:
                    raise RuntimeError(f'Unsupported actual modem rate {self.rate}; this profile requires 14,400 bps')
            elif status['name'] in ('eot', 'dcd_off'):
                raise Cancelled('Remote modem ended the call')
            elif status['name'] in ('abort', 'frame_error', 'tover', 'rover', 'unknown'):
                raise RuntimeError('V.80 transport error: ' + status['name'])
        if getattr(self, 'acquisition_deadline', None) is not None:
            if self.session.link.framer.startup_timed_out:
                raise TimeoutError('H.223 startup acquisition exceeded its received-bit budget')
            if not self.session.link.framer.acquiring:
                self.emit(event='acquisition', phase='locked',
                          **self.session.link.summary()['startup_acquisition'])
                self.acquisition_deadline = None
            elif now >= self.acquisition_deadline:
                self.session.link.framer.expire_startup('startup_wall_time_timeout')
                raise TimeoutError('H.223 startup acquisition did not lock within five seconds')
        if self.session.link.active_underruns:
            raise RuntimeError('Synchronous transmit underrun; call stopped to avoid corrupt playback')
        if self.session.link.framer.errors or self.session.link.demux.events:
            raise RuntimeError('H.223 framing or channel reassembly error')
        if self.session.failure:
            raise RuntimeError(self.session.failure)

    def _send(self, count, now):
        if now < getattr(self, 'protocol_release_at', 0):
            data = self.session.link.encoder.idle(count)
        else:
            data = self.session.link.pull(count)
        self.modem.send(shield(data))
        self.files['tx'].write(data)
        self.tx_bytes += len(data)
        self.last_tx = now

    def _load_bundle(self, path, generation):
        # File parsing/hash checks never occupy the tightly timed serial loop.
        try:
            from h324.media import load_bundle
            directory = Path(path).expanduser().resolve()
            if directory.is_file():
                directory = directory.parent
            sdus = load_bundle(directory)
            manifest = json.loads((directory / 'manifest.json').read_text())
            self.bundle_result.put((generation, sdus, manifest, None))
        except Exception as exc:
            self.bundle_result.put((generation, None, None, str(exc)))

    def _command(self, command):
        op = command['op']
        now = time.monotonic()
        if op in ('shutdown', 'hangup'):
            self.shutdown_requested |= op == 'shutdown'
            if self.modem is not None:
                raise Cancelled()
        elif op in ('dial', 'listen'):
            self._initialize(command)
        elif op == 'send':
            if self.state != 'connected' or self.session is None:
                raise ValueError('Connect a call before sending a movie')
            if self.bundle_loading or self.session.media_active:
                raise ValueError('A movie is already loading or sending')
            self.bundle_loading = True
            threading.Thread(target=self._load_bundle, args=(command['bundle'], self.call_generation), daemon=True).start()
        elif op == 'live_start':
            if self.state != 'connected' or self.session is None:
                raise ValueError('Connect a call before transmitting')
            self.session.start_live(now, str(command.get('label', 'Live media')))
        elif op == 'live_chunk':
            if self.session is None:
                raise ValueError('No connected call')
            payload = base64.b64decode(command['payload'], validate=True)
            self.session.send_live(int(command['lcn']), payload, now,
                                   int(command.get('timestamp_ms', 0)), int(command.get('duration_ms', 30)))
        elif op == 'live_stop':
            if self.session is not None:
                self.session.stop_media(now)
        elif op in ('pause', 'resume', 'stop_media'):
            if self.session is not None:
                getattr(self.session, op)(now)
        else:
            raise ValueError(command.get('message', 'Unknown command: ' + str(op)))

    def telemetry(self, now):
        s = self.session
        self.emit(event='telemetry', rate=self.rate, carrier_report=self.carrier_report,
                  elapsed=now-self.connected_at if self.connected_at is not None else 0,
                  tx_bytes=self.tx_bytes, rx_bytes=self.rx_bytes,
                  carrier=self.connected_at is not None,
                  tx_active=self.last_tx is not None and now-self.last_tx < .15,
                  rx_active=self.last_rx is not None and now-self.last_rx < .15,
                  video_frames=s.counts['video_frames'] if s else 0,
                  audio_frames=s.counts['audio_frames'] if s else 0,
                  media_position_ms=s.media_position(now) if s else 0,
                  media_duration_ms=s.duration_ms if s else 0,
                  media_transmitting=s.media_active if s else False,
                  paused=s.pause_start is not None if s else False)

    def _cleanup(self, error=None):
        if self.modem is None:
            if error:
                self.set_state('error', error)
            return
        self.set_state('hanging_up', 'Hanging up and restoring the modem')
        self.cleanup_mode = True
        try:
            # EOT is in-band modem control, followed by proven DTR/AT restoration.
            # Do not wait for DCD: these USB modems can leave it asserted.
            if self.modem.state == 'data':
                with contextlib.suppress(Exception):
                    self.modem.send(b'\x19\xba')
            restore_close(self.modem)
            cleanup_ok = bool(getattr(self.modem, 'cleanup_ok', False) and
                              not getattr(self.modem, 'restore_failed', False))
            if not cleanup_ok:
                error = (error + '; ' if error else '') + 'Modem idle/profile restoration was not confirmed'
            result = dict(station=self.station, error=error, cleanup_ok=cleanup_ok,
                          tx_bytes=self.tx_bytes, rx_bytes=self.rx_bytes, rate=self.rate,
                          last_rate_status=self.last_rate_status, carrier_report=self.carrier_report,
                          negotiation=self.negotiation,
                          session=self.session.summary() if self.session else None)
            self.emit(event='cleanup', **result)
            if self.directory:
                (self.directory / 'results.json').write_text(json.dumps(result, indent=2) + '\n')
        finally:
            for stream in self.files.values():
                stream.close()
            self.files = {}
            self.modem = self.session = None
            self.connected_at = None
            self.call_generation += 1
            self.cleanup_mode = False
            # A completed bundle load from the former call cannot enter a new one.
            if self.bundle_loading:
                self.bundle_loading = False
            self.set_state('error' if error else 'idle', error or 'Call closed; modem ready')
            self.telemetry(time.monotonic())

    def run(self):
        self.set_state('initializing', 'Loading conferencing protocols')
        codec()  # Compile PER before any modem is open or serial pacer is active.
        self.set_state('idle', 'Choose a modem, then Call or Answer')
        next_telemetry = 0
        try:
            while not self.shutdown_requested:
                try:
                    command = self.deferred.popleft() if self.deferred else self.inbox.get_nowait()
                except queue.Empty:
                    command = None
                try:
                    if self.ipc_overload:
                        self.shutdown_requested = True
                        raise Cancelled()
                    if command is not None:
                        owned_before_command = self.modem is not None
                        try:
                            self._command(command)
                        except (ValueError, KeyError) as exc:
                            self.emit(event='error', message=str(exc))
                            if command['op'] in ('dial', 'listen') and not owned_before_command:
                                self._cleanup(str(exc))
                    if self.modem is not None:
                        now = time.monotonic()
                        raw = self.modem.read()
                        if self.connected_at is None:
                            self._negotiate(raw, now)
                            if self.connected_at is None and now >= self.connect_deadline:
                                raise TimeoutError('No connection before the call timeout')
                        else:
                            self._feed(raw, now)
                            self.session.tick(now)
                            if self.session.failure:
                                raise RuntimeError(self.session.failure)
                            if now >= self.next_send:
                                self._send(36, now)
                                self.next_send += .02
                    try:
                        generation, sdus, manifest, load_error = self.bundle_result.get_nowait()
                    except queue.Empty:
                        pass
                    else:
                        if self.bundle_loading and generation == self.call_generation:
                            self.bundle_loading = False
                            if load_error:
                                self.emit(event='error', message='Cannot load prepared movie: ' + load_error)
                            elif self.session is not None and self.session.ready:
                                self.session.send_media(sdus, manifest['duration_ms'], time.monotonic(),
                                                        manifest.get('title', manifest.get('source_name', 'Movie')))
                    now = time.monotonic()
                    if now >= next_telemetry:
                        self.telemetry(now)
                        next_telemetry = now + .2
                except Cancelled:
                    self._cleanup()
                except Exception as exc:
                    self.emit(event='error', message=str(exc))
                    self._cleanup(str(exc))
                time.sleep(.002 if self.modem else .02)
        finally:
            self._cleanup()
            self.emit(event='closed', station=self.station)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--station', default='station')
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    if args.self_test:
        codec()
        print(json.dumps(dict(protocol_version=PROTOCOL_VERSION, event='self_test', ready=True)))
        return 0
    protocol_stdout = sys.stdout
    sys.stdout = sys.stderr  # Legacy AT diagnostics must not contaminate JSON IPC.
    output = queue.Queue(maxsize=256)
    def output_writer():
        while True:
            value = output.get()
            if value is None:
                return
            try:
                protocol_stdout.write(value)
                protocol_stdout.flush()
            except (BrokenPipeError, OSError):
                return
            finally:
                output.task_done()
    threading.Thread(target=output_writer, daemon=True).start()

    def emit(**event):
        event.setdefault('protocol_version', PROTOCOL_VERSION)
        encoded = json.dumps(event, separators=(',', ':')) + '\n'
        try:
            output.put_nowait(encoded)
        except queue.Full:
            if event.get('event') in ('telemetry', 'log'):
                return
            while True:
                try:
                    output.get_nowait()
                    output.task_done()
                except queue.Empty:
                    break
            fatal = dict(protocol_version=PROTOCOL_VERSION, event='error',
                         code='event_overload', message='IPC consumer exceeded bounded queue; call stopped')
            output.put_nowait(json.dumps(fatal, separators=(',', ':')) + '\n')
            raise BufferError('IPC event consumer is too slow; bounded output queue full')

    worker = Worker(args.station, emit)
    # QProcess.terminate() sends SIGTERM. Treat it as graceful shutdown so the
    # controller's fallback still restores DTR/profile and releases the port.
    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, lambda *_: setattr(worker, 'shutdown_requested', True))
    threading.Thread(target=read_commands, args=(sys.stdin, worker.inbox), daemon=True).start()
    try:
        worker.run()
    except KeyboardInterrupt:
        worker.shutdown_requested = True
        worker._cleanup()
        emit(event='closed', station=args.station)
    except Exception as exc:
        emit(event='error', message=str(exc))
        worker._cleanup(str(exc))
        emit(event='closed', station=args.station)
        return 1
    deadline = time.monotonic() + 1
    while output.unfinished_tasks and time.monotonic() < deadline:
        time.sleep(.01)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
