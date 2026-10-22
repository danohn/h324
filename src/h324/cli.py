"""Headless modem discovery, explicit probe and call commands."""
from __future__ import annotations
import argparse
import asyncio
import json
from pathlib import Path
from serial.tools import list_ports
from .api import AsyncModem, ModemConfig
from .modem import Modem


def devices() -> list[dict]:
    return [dict(device=p.device, description=p.description, manufacturer=p.manufacturer,
                 vid=p.vid, pid=p.pid, serial_number=p.serial_number, location=p.location)
            for p in list_ports.comports()]


async def _call(args):
    async with AsyncModem(ModemConfig(args.port, timeout=args.timeout)) as modem:
        call = await modem.answer() if args.answer else await modem.dial(args.number)
        if args.bundle:
            await call.send_bundle(args.bundle)
        try:
            async for event in call.events():
                print(json.dumps({'event': event.kind, **event.data}), flush=True)
        finally:
            await call.hangup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('devices', help='List available serial ports without opening them')
    probe = sub.add_parser('probe', help='Open the selected modem, query identity and restore idle')
    probe.add_argument('--port', required=True)
    call = sub.add_parser('call', help='Connect a call and print JSON events')
    call.add_argument('--port', required=True)
    direction = call.add_mutually_exclusive_group(required=True)
    direction.add_argument('--answer', action='store_true')
    direction.add_argument('--number')
    call.add_argument('--timeout', type=float, default=120)
    call.add_argument('--bundle', type=Path)
    sub.add_parser('worker', help='Start JSON-lines worker (prefer python -m h324.worker)')
    args = parser.parse_args()
    try:
        if args.command == 'devices':
            print(json.dumps(devices(), indent=2))
        elif args.command == 'probe':
            modem = Modem(args.port, 'probe')
            try:
                modem.idle()
                result = {command: modem.command(command, required=False)
                          for command in ('ATI', 'AT+GMI', 'AT+GMM', 'AT+GMR', 'AT+ES?', 'AT+MS?')}
                print(json.dumps(result, indent=2))
            finally:
                modem.close()
        elif args.command == 'worker':
            from .worker import main as worker_main
            import sys
            sys.argv = [sys.argv[0]]
            return worker_main()
        else:
            asyncio.run(_call(args))
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        parser.exit(1, f'h324: {exc}\n')
    return 0
