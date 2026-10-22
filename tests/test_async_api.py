"""Lifecycle tests use a real subprocess, never modem hardware."""
import asyncio
import json
from pathlib import Path
import sys
import pytest
from h324 import AsyncModem, H324Error, MediaChunk, ModemConfig, OverloadError

FAKE = '''import json,sys
from pathlib import Path
mode, record = sys.argv[1:3]
def emit(event, **kw):
 print(json.dumps(dict(protocol_version=1,event=event,**kw)),flush=True)
if mode == 'early_eof': sys.exit(0)
emit('state',state='idle',message='ready')
for line in sys.stdin:
 c=json.loads(line)
 with Path(record).open('a') as f: f.write(json.dumps(c)+'\\n')
 op=c['op']
 if op in ('dial','listen'):
  emit('state',state='initializing',message='initializing')
  if mode == 'error': emit('error',message='permission denied')
  elif mode != 'cancel':
   emit('state',state='connected',message='connected')
   if mode == 'overflow':
    for i in range(40): emit('media',lcn=1,payload='AAAA')
 elif op == 'hangup': emit('state',state='idle',message='restored')
 elif op == 'shutdown':
  emit('closed')
  break
'''


def worker(tmp_path, mode='normal'):
    path = tmp_path/'worker with spaces.py'
    path.write_text(FAKE)
    record = tmp_path/'commands.jsonl'
    return [sys.executable, str(path), mode, str(record)], record


def commands(record):
    return [json.loads(l)['op'] for l in record.read_text().splitlines()]


async def test_call_send_pause_stop_hangup_and_context_shutdown(tmp_path):
    command, record = worker(tmp_path)
    async with AsyncModem(ModemConfig('COM10'), worker_command=command) as modem:
        call = await modem.dial('1234')
        async def source():
            yield MediaChunk(1, bytes(24))
            await asyncio.Event().wait()
        task = asyncio.create_task(call.send(source()))
        await asyncio.sleep(.03)
        await call.pause()
        await call.resume()
        await call.stop()
        assert task.cancelled()
        await call.hangup()
    ops = commands(record)
    assert ops[:3] == ['dial', 'live_start', 'live_chunk']
    assert 'pause' in ops and 'resume' in ops and 'live_stop' in ops
    assert 'hangup' in ops and ops[-1] == 'shutdown'


async def test_cancel_during_initialization_waits_for_restore(tmp_path):
    command, record = worker(tmp_path, 'cancel')
    async with AsyncModem(ModemConfig('/dev/ttyACM0'), worker_command=command) as modem:
        task = asyncio.create_task(modem.answer())
        await asyncio.sleep(.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not modem._active
    assert commands(record) == ['listen', 'hangup', 'shutdown']


async def test_connect_error_immediately_wakes_waiter(tmp_path):
    command, record = worker(tmp_path, 'error')
    async with AsyncModem(ModemConfig('COM3'), worker_command=command) as modem:
        with pytest.raises(H324Error, match='permission denied'):
            await asyncio.wait_for(modem.dial('1'), 2)
    assert 'hangup' in commands(record)


async def test_early_worker_eof_fails_context_without_call(tmp_path):
    command, _ = worker(tmp_path, 'early_eof')
    with pytest.raises(H324Error, match='exited'):
        async with AsyncModem(ModemConfig('COM3'), worker_command=command):
            pytest.fail('worker EOF must not appear ready')


async def test_event_overload_requests_hangup_without_blocking_worker(tmp_path):
    command, record = worker(tmp_path, 'overflow')
    async with AsyncModem(ModemConfig('COM3'), worker_command=command, event_queue_size=4) as modem:
        try:
            await modem.dial('1')
        except OverloadError:
            pass  # The producer may flood before dial() resumes.
        await asyncio.sleep(.03)
        assert isinstance(modem._failure, OverloadError)
        assert 'hangup' in commands(record)
        assert modem._events.qsize() <= 4


async def test_invalid_source_releases_send_slot(tmp_path):
    command, _ = worker(tmp_path)
    async with AsyncModem(ModemConfig('COM3'), worker_command=command) as modem:
        call = await modem.dial('1')
        async def bad():
            yield 'not a MediaChunk'
        with pytest.raises(TypeError):
            await call.send(bad())
        assert not call._send_active
        async def empty():
            if False:
                yield None
        await call.send(empty())
        await call.hangup()


def test_public_input_validation():
    with pytest.raises(ValueError): ModemConfig('')
    with pytest.raises(ValueError): MediaChunk(1, b'bad')
    with pytest.raises(ValueError): MediaChunk(2, b'not h263')
    assert ModemConfig('COM255').port == 'COM255'


async def test_context_exit_cancels_active_source(tmp_path):
    command, record = worker(tmp_path)
    async with AsyncModem(ModemConfig('COM3'), worker_command=command) as modem:
        call = await modem.dial('1')
        started = asyncio.Event()
        async def source():
            started.set()
            await asyncio.Event().wait()
            if False:
                yield None
        task = asyncio.create_task(call.send(source()))
        await started.wait()
    assert task.cancelled()
    assert commands(record)[-1] == 'shutdown'


def test_profile_string_is_normalized():
    config = ModemConfig('COM3', profile='cx93001-v80-14400')
    assert config.profile.value == 'cx93001-v80-14400'


@pytest.mark.parametrize('pipe_error', [BrokenPipeError, ConnectionResetError])
async def test_close_reaps_worker_after_pipe_reset(pipe_error):
    class Stdin:
        def write(self, data): pass
        async def drain(self): raise pipe_error('pipe closed before exit was reaped')
    class Process:
        returncode = None
        stdin = Stdin()
        waited = False
        async def wait(self):
            self.waited = True
            self.returncode = 0
            return 0
    process = Process()
    modem = AsyncModem(ModemConfig('COM3'))
    modem._process = process
    await modem.close()
    assert process.waited
    assert modem._process is None


async def test_connection_cleanup_pipe_reset_preserves_original_error():
    modem = AsyncModem(ModemConfig('COM3'))
    async def command(op, **fields):
        if op == 'dial': raise H324Error('original connection failure')
        raise ConnectionResetError('secondary cleanup pipe reset')
    modem._command = command
    with pytest.raises(H324Error, match='original connection failure'):
        await modem.dial('1')
