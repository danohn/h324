# h324

Experimental Python H.324 dial-up multimedia protocols and async modem calling.
Python 3.11–3.14; no Qt or FFmpeg dependency. The desktop application is
[Modem Conference](https://github.com/danohn/modem-conference).

The initial finite profile supports CX93001 USB modems with V.80 synchronous
access, V.32bis at 14,400 bps, H.223 level 0, SRP, aligned-PER H.245, G.723.1
mono audio and H.263 SQCIF video. It is a tested prototype, not a complete
H.324 implementation or a promise of interoperability with arbitrary terminals.

Install the experimental preview with `pip install h324==0.1.0a1`. Development:
`uv sync --group dev`, `uv run pytest`. Enumerate ports with `h324 devices`;
inspect an explicitly selected modem using `h324 probe --port DEVICE`.

```python
import asyncio
from h324 import AsyncModem, ModemConfig

async def main():
    # Supply your own OS device path or Windows COM port and telephone number.
    async with AsyncModem(ModemConfig(port="COM3")) as modem:
        call = await modem.dial("1234")
        async for event in call.events():
            print(event.kind, event.data)

asyncio.run(main())
```

`await modem.answer()` waits for an incoming call. `Call.send()` consumes an
async iterable of `MediaChunk` values (24-byte G.723.1 frames or complete
H.263 pictures), with bounded backpressure. `pause`, `resume`, `stop`, and
`hangup` are async methods. Received media arrives through `Call.events()`.
Cancellation and context exit request worker cleanup and release the modem.

The worker owns all serial timing in a separate process. Local JSON-lines IPC
is version 1; `python -m h324.worker --self-test` verifies packaged resources
without opening hardware. `h324 call --port DEVICE --number NUMBER` and
`h324 call --port DEVICE --answer` expose the headless worker as JSON events.
No default modem, telephone number, PBX or network address is configured.

Raw captures are disabled by default. Supply an explicit capture directory only
when recording is intended. Use headphones with microphone calling; acoustic
echo cancellation is not implemented. See `docs/API.md` for event semantics,
source timing, overload and lifecycle behavior, and `THIRD_PARTY_NOTICES.md`
for the separately licensed ASN.1 resource.
