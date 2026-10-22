# Async calls and encoded media

This is the experimental 0.1 API. The only supported modem profile is
`ModemProfile.CX93001_V80_14400`, with V.80 transparent synchronous access and a
14,400 bps V.32bis bearer. The protocol classes can be used independently of
serial I/O. No Qt, camera, FFmpeg or audio playback dependency is imported.

## Ownership and lifecycle

```python
import asyncio
from h324 import AsyncModem, ModemConfig

async def receive(call):
    async for event in call.events():
        if event.kind == "media":
            # event.data["lcn"]: 1 = G.723.1, 2 = H.263 fragment
            # event.payload: decoded bytes, ready for an incremental decoder
            print(event.data["lcn"], len(event.payload))
        elif event.kind == "error":
            print(event.data["message"])

async def main(port, number):
    async with AsyncModem(ModemConfig(port=port)) as modem:
        call = await modem.dial(number)  # await modem.answer() to receive calls
        await receive(call)
```

A context starts one subprocess; only that subprocess owns the serial port and
pacer. Supply an explicit OS serial device path or Windows COM port. No phone
number, device, PBX or local address is selected automatically. `dial()` and
`answer()` return after negotiated audio/video channels are ready; they raise
`H324Error` for worker, transport, negotiation or modem failure. Cancelling
connection requests hangup and waits for restoration. Context exit requests
shutdown and waits for cleanup; termination and kill are bounded fallbacks.

`Call.hangup()` first cancels its active async media producer, then waits for an
idle confirmation. Worker idle means the selected modem's volatile ordinary
V.32bis/LAPM profile was restored, auto-answer disabled and serial port closed.
Restoration failures are reported as an error state. No NVRAM profile is saved.
An unexpectedly killed process cannot promise AT-command restoration; port
close/DTR is the final fallback.

Use one event-consumer task per call. `Call.events()` yields immutable
`CallEvent(kind, data, payload)` values. Principal kinds are `state`, `media`,
`telemetry`, `media_started`, `media_finished`, `video_overload`, `video_reset`,
`error`, `cleanup`, and `closed`. The cleanup event always reports restoration
status, actual rate evidence and bounded session counts, even when recording is
disabled. State transitions include initializing, listening or
dialing, negotiating, connected, hanging_up, and idle/error. Stop iterating on
a terminal lifecycle state. Call-local source completion does not terminate
receiving or close negotiated channels.

The default event queue is 256 entries. Telemetry may be discarded if full.
Media/lifecycle overflow raises `OverloadError`, requests call teardown and
keeps reading worker output until restoration. Applications must consume media
promptly; do not decode synchronously in the serial process.

## Sending and source timing

`await call.send(source)` consumes an asynchronous iterable of `MediaChunk`:

- `MediaChunk(1, payload, timestamp_ms, duration_ms)` contains one native
  **24-byte G.723.1 6.3 kbit/s frame**, representing 30 ms of 8 kHz mono audio.
- `MediaChunk(2, payload, timestamp_ms, duration_ms)` contains one complete
  octet-aligned baseline **H.263 picture**, at most 4096 bytes. Include its
  picture-start code. The worker atomically admits the picture and segments it
  into AL3 SDUs of at most 96 bytes. Do not pass arbitrary picture fragments.

Live source timestamps are source-local metadata. Pace source production in
real time; live chunks are admitted upon arrival. `send()` applies pipe
backpressure and source cancellation but does not buffer an entire recording
or impose a duration limit. Run `send()` concurrently with `events()`.

`await call.send_bundle(directory)` validates a prepared bundle and schedules
its timestamped SDUs. Bundles contain `audio.g723`, `video.h263` and
`manifest.json`, with exact file sizes/hashes and duration. File preparation
and FFmpeg execution are application responsibilities. Bundle validation
requires matching audio/video duration, at most 60 seconds, the negotiated
codec profile, and average/burst bandwidth limits. A bundle's local title and
source path are never sent to the remote endpoint.

`pause()` gates a live async producer and pauses the worker's file schedule.
`resume()` resumes them. `stop()` cancels the active async producer and stops
the current source; already admitted complete SDUs drain. Sending again uses
the same call and ongoing AL3 sequence numbers. Continuous sequence wrap is
supported. A remote source does not need to match the local file or duration.

Control, audio and video use separate priorities. Live video admission uses a
bounded queue watermark and a 600-byte/second token budget with a bounded
initial burst. Excess video is rejected as a **whole picture**: a
`video_overload` event pauses video admission while audio/control continue.
After the queue drains and a cooldown, `video_reset` requests an encoder restart
with a fresh INTRA picture. Senders must react to this event; resuming arbitrary
predicted pictures would require reference pictures the receiver never saw.
No accepted AL3/MUX fragment is discarded on overload. Optional receive-side
ARQ timers and generalized loss recovery remain outside this profile.

## Worker IPC and packaged helpers

The supported local interface is JSON lines over stdin/stdout. Every event has
`protocol_version: 1`; clients should send that version on every command.
Source commands launch `python -m h324.worker --station NAME`. Compiled clients
must launch their dedicated helper executable, with that helper dispatching to
`h324.worker.main()`; a frozen executable is not a Python interpreter.
`AsyncModem(..., worker_command=[...])` accepts this helper command.

Commands are `dial` (port, number, optional timeout/profile), `listen` (port,
optional timeout/profile), `send` (bundle), `pause`, `resume`, `stop_media`,
`live_start` (label), `live_chunk` (lcn, base64 payload, timestamp_ms,
duration_ms), `live_stop`, `hangup`, and `shutdown`. Media events contain lcn
and base64 payload; only the high-level API converts it to bytes. An omitted
profile means the single supported CX93001 profile. Unknown versions fail.

Input commands are limited to 64 KiB, the input/deferred queues to 100 entries,
and output to 256 entries. Input reading and output writing use separate
threads. IPC congestion cannot block serial draining; an unconsumed critical
event ends the call and produces an explicit overload error. EOF requests
shutdown. Cancellation during initialization and connected operation follows
the same restoration path on macOS, Linux and Windows.

Raw wire, received codec streams and raw AT logs are disabled by default.
`ModemConfig(capture_directory=...)` or IPC `log_dir` explicitly enables them in
a **new directory**. They can contain sensitive audio/video and local device
information. Ordinary diagnostics use Python logging instead.

## Verification and limits

The public core initially migrated 141 portable tests from the lab: H.223,
H.245/PER, SRP, AL3, real synthetic media, integrated link/session behavior and
13 pure V.80 tests. Eighteen new async-process, queue, live-session and platform
adapter tests raised that baseline to 159. The lab's complete original
191-test suite remains in the private lab archive; diagnostic-only PBX,
recorded-PCAP and historical V.34 experiment tests were not copied into the
public library. New release tests add to these counts.

Platform support distinguishes software tests from hardware verification.
macOS and Windows physical modem validation is recorded by the release owner.
Linux hardware/capture validation remains unverified until physical devices
are available. The CI matrix tests Python 3.11–3.14 on macOS, Windows and Linux;
it does not substitute for real modem/driver testing or independent terminal
interoperability. This finite profile has no channel renegotiation,
end-session handshake or universal modem compatibility claim.

## Physical modem startup acquisition

The physical worker sends **one second of consecutive full H.223 idle flags**
before releasing its queued SRP traffic. H.223 (03/1996), clause 6.3.1, permits
consecutive flags and defines the complete flag as `01111110`; the implementation
does not reinterpret a seven-bit interval as a valid flag or repair payload bits.

The worker enables a bounded initial hunt. It requires eight consecutive full
flags followed by a structurally valid, nonempty MC0 control PDU. Rejected
acquisition candidates are recorded separately in the `acquisition` events and
`transport.startup_acquisition` summary. The first accepted PDU immediately
locks strict framing, including for any later error in the same USB read.
Post-lock framing errors remain fatal. The ordinary `Framer()` API retains its
strict behavior unless `startup_hunt=True` is explicitly requested.

Acquisition fails after **five seconds of wall time or 72,000 received bits**,
whichever is reached first. Empty reads cannot postpone the wall-time limit.
This is an explicit startup policy for the tested modem profile, rather than
a claim that all H.324 peers must send an eight-flag training sequence.

The async media producer is automatically cancelled when the remote call starts
hanging up or enters idle/error, including when the producer is awaiting a new
capture frame. `send()` then raises `H324Error`; the event reader requests this
cancellation without awaiting producer teardown, so serial/IPC draining can
continue. No new chunks or source-end writes are sent after that disconnect.

## Independent initial bearer reports

`telemetry.rate` and `cleanup.rate` continue to represent the **V.80 in-band
rate** only. An unspecified in-band report remains `None`; the configured
14,400 bps target is never substituted.

The separate `carrier_report` field is `None` unless the modem's actual initial
result codes consistently report V.32bis, transmit/receive rates of 14,400 bps,
and `CONNECT 14400`. A verified report has `source="AT+MRR"`, `validated=True`,
`phase="connect"`, `modulation="V32B"`, `tx_bps`, `rx_bps`, `connect_bps`, and the
original `raw_mcr`, `raw_mrr` and `raw_connect` strings. The worker emits it in
a `carrier_report` event and includes it in telemetry and cleanup. Applications
can independently validate captured result lines with
`h324.modem.parse_carrier_report(list_of_lines)`.

These are independently reported **initial negotiated bearer rates**, not
configuration readbacks. The Conexant reference manual describes +MR's actual
negotiated/renegotiated result codes and +MRR's transmit/receive fields on
pages 3-56 through 3-58 of the
[StarTech-hosted CX930xx manual](https://sgcdn.startech.com/005329/media/sets/USB56KEMH2_Manual/AT%20Commands%20for%20USB56KEMH2.pdf#page=82).
Missing, malformed, unknown, asymmetric or conflicting reports cannot create a
verified carrier report. Later in-band reports remain independent: a known
unsupported in-band rate is still fatal even when the initial AT report was
14,400 bps. Reports and acceptance results must name which evidence source they
used rather than describe an unspecified in-band status as verified.
