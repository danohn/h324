"""Async facade over the isolated, timing-sensitive modem worker.

Media chunks are local metadata; only standard encoded codec bytes cross the
telephone connection. Applications must consume events while transmitting.
"""
from __future__ import annotations

import asyncio
import contextlib
import base64
from collections.abc import AsyncIterable, AsyncIterator, Mapping, Sequence
from dataclasses import dataclass, field
import enum
import json
from pathlib import Path
import re
import sys
from typing import Any

PROTOCOL_VERSION = 1


class H324Error(Exception):
    """Worker, modem or protocol failure."""


class OverloadError(H324Error):
    """An application did not keep up with the bounded media/event queues."""


class ModemProfile(str, enum.Enum):
    CX93001_V80_14400 = 'cx93001-v80-14400'


@dataclass(frozen=True)
class ModemConfig:
    port: str
    profile: ModemProfile = ModemProfile.CX93001_V80_14400
    timeout: float = 120
    capture_directory: str | Path | None = None
    station: str = 'station'

    def __post_init__(self) -> None:
        if not isinstance(self.port, str) or not self.port.strip():
            raise ValueError('An explicit serial port is required')
        try:
            object.__setattr__(self, 'profile', ModemProfile(self.profile))
        except ValueError:
            raise ValueError('Unsupported modem profile') from None
        if not 5 <= self.timeout <= 180:
            raise ValueError('Call timeout must be between 5 and 180 seconds')


@dataclass(frozen=True)
class MediaChunk:
    """One G.723.1 frame or complete octet-aligned H.263 picture.

    timestamp_ms is source-relative. The worker schedules live chunks upon
    arrival; producers should pace capture/audio in real time. File sources can
    use send_bundle() for the prepared bundle's release-time scheduler.
    """
    lcn: int
    payload: bytes
    timestamp_ms: int = 0
    duration_ms: int = 30

    def __post_init__(self) -> None:
        if self.lcn not in (1, 2) or not isinstance(self.payload, bytes) or not self.payload:
            raise ValueError('Media requires channel 1 or 2 and nonempty bytes')
        if self.timestamp_ms < 0 or self.duration_ms <= 0:
            raise ValueError('Media timing must be nonnegative with positive duration')
        if self.lcn == 1 and (len(self.payload) != 24 or self.payload[0] & 3):
            raise ValueError('Audio requires one 24-byte G.723.1 6.3 kbit/s frame')
        if self.lcn == 2 and (len(self.payload) > 4096 or not self.payload.startswith(b'\x00\x00') or len(self.payload) < 3 or self.payload[2] & 0xfc != 0x80):
            raise ValueError('Video requires one complete baseline H.263 picture of at most 4096 bytes')
        if self.lcn == 2 and any(self.payload[i:i+2] == b'\x00\x00' and self.payload[i+2] & 0xfc == 0x80 for i in range(1, len(self.payload)-2)):
            raise ValueError('Pass one complete H.263 picture per chunk')


@dataclass(frozen=True)
class CallEvent:
    kind: str
    data: Mapping[str, Any] = field(default_factory=dict)
    payload: bytes | None = None


class Call:
    """A call owned by an AsyncModem; consume events in one task."""
    def __init__(self, modem: AsyncModem):
        self._modem = modem
        self._send_active = False
        self._sender = None
        self._disconnect_error: H324Error | None = None
        self._resume_gate = asyncio.Event()
        self._resume_gate.set()

    async def events(self) -> AsyncIterator[CallEvent]:
        while True:
            event = await self._modem._next_event()
            yield event
            if event.kind == 'closed' or event.kind == 'state' and event.data.get('state') in ('idle', 'error'):
                break

    def _disconnected(self, message: str) -> None:
        if self._sender is not None and not self._sender.done():
            self._disconnect_error = H324Error('Call disconnected: ' + message)
            self._sender.cancel()

    def _require_connected(self) -> None:
        if not self._modem._active or self._modem._call is not self:
            raise H324Error('This call is no longer connected')

    async def send(self, source: AsyncIterable[MediaChunk], *, label: str = 'Live media') -> None:
        self._require_connected()
        if self._send_active:
            raise H324Error('A media source is already active')
        self._send_active = True
        self._sender = asyncio.current_task()
        try:
            await self._modem._command('live_start', label=label)
            async for chunk in source:
                await self._resume_gate.wait()
                self._require_connected()
                if not isinstance(chunk, MediaChunk):
                    raise TypeError('send() expects MediaChunk values')
                await self._modem._command('live_chunk', lcn=chunk.lcn,
                    payload=base64.b64encode(chunk.payload).decode('ascii'),
                    timestamp_ms=chunk.timestamp_ms, duration_ms=chunk.duration_ms)
        except asyncio.CancelledError:
            if self._disconnect_error is not None:
                raise self._disconnect_error from None
            raise
        finally:
            self._send_active = False
            self._sender = None
            if self._disconnect_error is None and self._modem._process is not None and self._modem._process.returncode is None:
                # Preserve a source/worker exception; cleanup must not replace it
                # with a secondary broken pipe. Context exit still owns teardown.
                with contextlib.suppress(H324Error, BrokenPipeError, ConnectionResetError):
                    await asyncio.shield(self._modem._command('live_stop'))

    async def send_bundle(self, directory: str | Path) -> None:
        self._require_connected()
        await self._modem._command('send', bundle=str(directory))

    async def pause(self) -> None:
        self._require_connected()
        self._resume_gate.clear()
        await self._modem._command('pause')

    async def resume(self) -> None:
        self._require_connected()
        self._resume_gate.set()
        await self._modem._command('resume')

    async def stop(self) -> None:
        if self._sender is not None and self._sender is not asyncio.current_task():
            self._sender.cancel()
            await asyncio.gather(self._sender, return_exceptions=True)
        self._resume_gate.set()
        if self._modem._active and self._modem._call is self:
            await self._modem._command('stop_media')

    async def hangup(self) -> None:
        await self.stop()
        if self._modem._call is self:
            await self._modem._hangup()


class AsyncModem:
    """Own one worker. Context exit waits for DTR/profile cleanup.

    worker_command supports applications shipping a dedicated frozen helper.
    The default invokes the installed Python worker module. event_queue_size is
    bounded; overflow terminates the call instead of blocking serial draining.
    """
    def __init__(self, config: ModemConfig, *, worker_command: Sequence[str] | None = None,
                 event_queue_size: int = 256):
        if event_queue_size < 4:
            raise ValueError('event_queue_size must be at least 4')
        self.config = config
        self.worker_command = list(worker_command or [sys.executable, '-m', 'h324.worker'])
        self._events: asyncio.Queue[CallEvent] = asyncio.Queue(event_queue_size)
        self._process = None
        self._reader = self._stderr_reader = None
        self._write_lock = asyncio.Lock()
        self._connected = asyncio.Event()
        self._idle = asyncio.Event()
        self._ready = asyncio.Event()
        self._failure: H324Error | None = None
        self._active = False
        self._closing = False
        self._call: Call | None = None

    async def __aenter__(self) -> AsyncModem:
        if self._process is not None:
            raise H324Error('Modem context is already open')
        if self._closing:
            raise H324Error('A closed AsyncModem context cannot be reopened')
        self._process = await asyncio.create_subprocess_exec(*self.worker_command, '--station', self.config.station,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=65536)
        self._reader = asyncio.create_task(self._read_events())
        self._stderr_reader = asyncio.create_task(self._drain_stderr())
        try:
            await asyncio.wait_for(self._ready.wait(), 45)
            if self._failure:
                raise self._failure
            return self
        except BaseException:
            await self.close()
            raise

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    async def _drain_stderr(self) -> None:
        # Never let an unused diagnostic pipe fill and stall the modem worker.
        assert self._process is not None and self._process.stderr is not None
        import logging
        log = logging.getLogger('h324.worker')
        while data := await self._process.stderr.read(4096):
            log.debug('%s', data.decode(errors='replace').rstrip())

    async def _read_events(self) -> None:
        assert self._process is not None and self._process.stdout is not None
        try:
            while line := await self._process.stdout.readline():
                value = json.loads(line)
                if value.pop('protocol_version', None) != PROTOCOL_VERSION:
                    raise H324Error('Incompatible worker IPC version')
                kind = value.pop('event')
                payload = base64.b64decode(value['payload'], validate=True) if kind == 'media' else None
                event = CallEvent(kind, value, payload)
                if kind == 'state':
                    if value.get('state') in ('hanging_up', 'idle', 'error') and self._call is not None:
                        self._call._disconnected(str(value.get('message', value['state'])))
                    if value.get('state') == 'connected':
                        self._connected.set()
                    if value.get('state') in ('idle', 'error'):
                        self._idle.set()
                        self._ready.set()
                        self._active = False
                    if value.get('state') == 'error':
                        self._failure = H324Error(str(value.get('message', 'Call failed')))
                        self._connected.set()
                if kind == 'error' and self._active and not self._connected.is_set():
                    self._failure = H324Error(str(value.get('message', 'Call failed')))
                    self._connected.set()
                if kind == 'closed':
                    self._idle.set()
                    self._ready.set()
                    self._connected.set()
                if isinstance(self._failure, OverloadError):
                    continue
                try:
                    self._events.put_nowait(event)
                except asyncio.QueueFull:
                    # Telemetry is replaceable; encoded media and lifecycle are not.
                    if kind in ('telemetry', 'log'):
                        continue
                    self._failure = OverloadError('Event consumer exceeded the bounded queue')
                    self._connected.set()
                    while not self._events.empty():
                        self._events.get_nowait()
                    self._events.put_nowait(CallEvent('error', {'message': str(self._failure)}))
                    await self._command('hangup')
                    continue
            if not self._closing:
                self._failure = self._failure or H324Error('Modem worker exited before the call closed')
        except (ValueError, KeyError, OSError, H324Error) as exc:
            self._failure = H324Error(str(exc))
        finally:
            if self._call is not None:
                self._call._disconnected(str(self._failure or 'worker output closed'))
            self._ready.set()
            self._connected.set()
            if self._failure:
                # Wake a waiting iterator even if its queue was full.
                while not self._events.empty():
                    self._events.get_nowait()
                self._events.put_nowait(CallEvent('error', {'message': str(self._failure)}))

    async def _next_event(self) -> CallEvent:
        if self._failure and self._events.empty():
            raise self._failure
        return await self._events.get()

    async def _command(self, op: str, **fields: Any) -> None:
        process = self._process
        if process is None or process.returncode is not None or process.stdin is None:
            if self._closing:
                return
            raise H324Error('Modem worker is not running')
        payload = json.dumps({'protocol_version': PROTOCOL_VERSION, 'op': op, **fields}, separators=(',', ':')).encode() + b'\n'
        if len(payload) > 65536:
            raise ValueError('IPC command exceeds 64 KiB')
        async with self._write_lock:
            process.stdin.write(payload)
            await process.stdin.drain()

    async def _connect(self, op: str, **fields: Any) -> Call:
        if self._active:
            raise H324Error('A call is already active')
        self._failure = None
        self._connected.clear()
        self._idle.clear()
        self._active = True
        # Startup telemetry must not precede this call's lifecycle events.
        while not self._events.empty():
            self._events.get_nowait()
        command = dict(port=self.config.port, profile=self.config.profile.value, timeout=self.config.timeout, **fields)
        if self.config.capture_directory is not None:
            command['log_dir'] = str(self.config.capture_directory)
        try:
            await self._command(op, **command)
            await asyncio.wait_for(self._connected.wait(), self.config.timeout + 45)
            if self._failure:
                raise self._failure
            if not self._active:
                raise H324Error('Call ended during connection')
            self._call = Call(self)
            return self._call
        except BaseException:
            # A worker can close stdout before asyncio has reaped its exit.
            # Preserve the original connection/cancellation failure if the
            # cleanup write encounters an already closed Windows pipe.
            with contextlib.suppress(H324Error, BrokenPipeError, ConnectionResetError):
                await asyncio.shield(self._hangup())
            raise

    async def dial(self, number: str) -> Call:
        if not re.fullmatch(r'[0-9*#]+', number):
            raise ValueError('Dial number must contain digits, * or #')
        return await self._connect('dial', number=number)

    async def answer(self) -> Call:
        return await self._connect('listen')

    async def _hangup(self) -> None:
        if not self._active:
            return
        await self._command('hangup')
        try:
            await asyncio.wait_for(self._idle.wait(), 15)
        except asyncio.TimeoutError:
            raise H324Error('Worker did not confirm modem restoration within 15 seconds') from None
        finally:
            self._active = False

    async def close(self) -> None:
        process = self._process
        if process is None:
            return
        self._closing = True
        try:
            if self._call is not None:
                with contextlib.suppress(H324Error, BrokenPipeError, ConnectionResetError):
                    await self._call.stop()
            # Proactor transports may report ConnectionResetError while the
            # worker has already exited but returncode is not populated yet.
            # Reap it below even when no shutdown command can be delivered.
            with contextlib.suppress(H324Error, BrokenPipeError, ConnectionResetError):
                await self._command('shutdown')
            try:
                await asyncio.wait_for(process.wait(), 18)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        finally:
            for task in (self._reader, self._stderr_reader):
                if task is not None:
                    task.cancel()
            await asyncio.gather(*(t for t in (self._reader, self._stderr_reader) if t is not None), return_exceptions=True)
            self._active = False
            self._process = None
            self._call = None
