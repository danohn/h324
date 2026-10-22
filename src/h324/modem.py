"""Serial adapter and volatile CX93001 V.80 modem configuration."""
import contextlib
import datetime as dt
import json
import logging
import os
import re
import sys
import time
import threading
from pathlib import Path
import serial
from serial.tools import list_ports
FAILURES = {"NO CARRIER", "BUSY", "NO DIALTONE", "NO DIAL TONE", "NO ANSWER", "ERROR"}
MODES = {"V32B": (14400, 14400)}

def classify(line):
    if re.fullmatch(r"CONNECT(?:\s.*)?", line):
        return "connect"
    if line == "RING":
        return "ring"
    if line in FAILURES:
        return "failure"
    return "response"

class Modem:
    def __init__(self, device, name, directory=None):
        self.name, self.device = name, device
        self._log_lock = threading.RLock()
        self.logfile = (directory / "modem.jsonl").open("w") if directory else None
        self.port = serial.Serial(port=None, baudrate=115200, bytesize=8,
                                  parity="N", stopbits=1, timeout=0,
                                  write_timeout=3, rtscts=False, xonxoff=False,
                                  dsrdtr=False, **({"exclusive": True} if os.name == "posix" else {}))
        self.port.port = device
        self.port.dtr = True
        self.port.rts = True
        self.buffer = b""
        self.state = "unknown"
        try:
            self.port.open()
        except BaseException:
            self.logfile.close() if self.logfile else None
            raise
        self.log("open", device=device)

    def log(self, event, **details):
        record = dict(time=dt.datetime.now(dt.timezone.utc).isoformat(),
                      monotonic=time.monotonic(), event=event, **details)
        if self.logfile:
            with self._log_lock:
                self.logfile.write(json.dumps(record) + "\n")
                self.logfile.flush()
        else:
            logging.getLogger("h324.modem").debug("%s", event)

    def send(self, data):
        self.log("tx", hex=data.hex(), text=data.decode("ascii", errors="backslashreplace"))
        written = self.port.write(data)
        if written != len(data):
            raise OSError(f"Serial write accepted only {written} of {len(data)} octets")

    def read(self):
        data = self.port.read(self.port.in_waiting)
        if data:
            self.log("rx", hex=data.hex(), text=data.decode("ascii", errors="backslashreplace"))
        return data

    def line_status(self):
        try:
            status = {label: getattr(self.port, attr, None)
                      for label, attr in [("dcd", "cd"), ("cts", "cts"), ("dsr", "dsr")]}
        except (OSError, serial.SerialException) as exc:
            status = {"unsupported": str(exc)}
        self.log("line_status", **status)
        return status

    def lines(self):
        self.buffer += self.read()
        lines = []
        while b"\r" in self.buffer or b"\n" in self.buffer:
            parts = re.split(b"[\r\n]", self.buffer, maxsplit=1)
            self.buffer = parts[1]
            line = parts[0].decode("ascii", errors="replace").strip()
            if not line:
                continue
            kind = classify(line)
            if kind == "connect":
                self.state = "data"
            elif kind == "failure":
                self.state = "command"
            self.log("line", text=line, kind=kind)
            logging.getLogger("h324.modem").debug("%s: %s", self.name, line)
            lines.append(line)
        return lines

    def command(self, command, timeout=4, required=True):
        if self.state != "command":
            raise RuntimeError(f"Refusing AT command in {self.state} mode")
        self.send(command.encode("ascii") + b"\r")
        deadline, result = time.monotonic() + timeout, []
        while time.monotonic() < deadline:
            for line in self.lines():
                result.append(line)
                if line == "OK" or line in FAILURES:
                    if required and line != "OK":
                        raise RuntimeError(f"{self.name}: {command}: {line}")
                    return result
            time.sleep(.02)
        raise TimeoutError(f"{self.name}: no final response to {command}")

    def idle(self):
        # &D2 is explicitly configured before calls. Initially it may be unknown.
        self.port.rtscts = False
        self.log("dtr", value=False, purpose="return_to_idle")
        self.port.dtr = False
        time.sleep(1.2)
        self.port.dtr = True
        self.log("dtr", value=True)
        time.sleep(1.2)
        self.read()
        self.buffer = b""
        if self.state != "command":
            # Last-resort guarded escape: silence on BOTH sides of the exact +++.
            self.send(b"+++")
            time.sleep(1.2)
            self.lines()
        self.state = "command"
        self.command("AT")  # prove command mode before proceeding
        self.command("ATH0")
        self.command("ATS0=0")
        self.line_status()

    def close(self):
        try:
            self.idle()
            self.cleanup_ok = True
            self.log("cleanup", status="idle_autoanswer_off")
        except Exception as exc:
            self.cleanup_ok = False
            self.log("cleanup_error", error=str(exc))
            self.port.dtr = False
            print(f"{self.name}: cleanup warning: {exc}", file=sys.stderr)
        finally:
            self.port.close()
            self.logfile.close() if self.logfile else None

def configure(m, mode, receive_flow='rts'):
    m.port.xonxoff = False
    m.idle()
    for cmd in ['AT&F', 'ATE0', 'ATQ0', 'ATV1', 'AT&D2', 'AT&C1', 'AT&K3',
                f'AT+MS={mode},0,{MODES[mode][0]},{MODES[mode][1]},{MODES[mode][0]},{MODES[mode][1]}',
                'AT+MR=1', 'ATW2', 'ATS7=60', 'AT%C0',
                f'AT+IFC={"1,1" if receive_flow == "xonxoff" else "2,2"}', 'AT+IBC=0',
                'ATS17=0', 'AT+ESA=0,0,0,,0,0,255', 'AT+ITF=255,64,0', 'AT+ER=1',
                'AT+ES=6,0,8']:
        m.command(cmd)
    result = {cmd: m.command(cmd) for cmd in ['AT+MS?', 'AT+ES?', 'AT+ESA?', 'AT+ITF?',
                                             'AT+IFC?', 'AT+IBC?', 'ATS17?', 'AT+DS?']}
    if '+ES: 6,0,8' not in result['AT+ES?']:
        raise RuntimeError(f'{m.name}: synchronous access readback did not match')
    expected_ifc = '+IFC: ' + ('1,1' if receive_flow == 'xonxoff' else '2,2')
    if expected_ifc not in result['AT+IFC?']:
        raise RuntimeError(f'{m.name}: flow-control readback did not match')
    m.port.rtscts = receive_flow == 'rts'
    m.port.xonxoff = receive_flow == 'xonxoff'
    return result

def restore_close(m):
    try:
        m.port.xonxoff = False
        m.idle()
        # Leave the previous ordinary-data recipe available in volatile settings.
        for cmd in ['AT+ES=3,0,2', 'AT%C0', 'AT+IFC=2,2',
                    'AT+MS=V32B,0,14400,14400,14400,14400', 'ATS17=0']:
            m.command(cmd)
        m.log('restored', status='ordinary_v32b_lapm_no_compression')
    except Exception as exc:
        m.log('restore_error', error=str(exc))
        m.restore_failed = True
    finally:
        m.close()


def parse_carrier_report(lines):
    """Verify the initial bearer from independent CX93001 AT result codes.

    Only the supported V.32bis/14400 profile is validated. This never uses
    configured speeds or fills an unspecified V.80 rate. Missing, malformed,
    asymmetric or conflicting reports return None. The phase is initial
    connection reporting; later V.80 rate reports retain their own meaning.
    """
    wanted = {'raw_mcr': (r'\+MCR:\s*(V32B)', ('V32B',)),
              'raw_mrr': (r'\+MRR:\s*([0-9]+)\s*,\s*([0-9]+)', ('14400', '14400')),
              'raw_connect': (r'CONNECT\s+([0-9]+)', ('14400',))}
    reports = {}
    for raw in lines:
        if not isinstance(raw, str):
            return None
        text = raw.strip()
        key = ('raw_mcr' if text.upper().startswith('+MCR:') else
               'raw_mrr' if text.upper().startswith('+MRR:') else
               'raw_connect' if text.upper().startswith('CONNECT') else None)
        if key is None:
            continue
        pattern, expected = wanted[key]
        match = re.fullmatch(pattern, text, re.IGNORECASE)
        if match is None or tuple(value.upper() for value in match.groups()) != expected:
            return None
        # Repeated identical reports are harmless; conflicting ones were
        # rejected above, even if a later line happens to match the target.
        reports[key] = text
    if set(reports) != set(wanted):
        return None
    return dict(source='AT+MRR', validated=True, phase='connect', modulation='V32B',
                tx_bps=14400, rx_bps=14400, connect_bps=14400, **reports)
