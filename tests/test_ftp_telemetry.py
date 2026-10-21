import asyncio
import ast
import importlib.util
import json
import pathlib
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components/ps4_goldhen/ftp_telemetry.py"
spec = importlib.util.spec_from_file_location("tested_ftp_telemetry", MODULE)
ftp = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = ftp
spec.loader.exec_module(ftp)

class Reader:
    def __init__(self, frames, stall=False):
        self.frames = list(frames)
        self.stall = stall
    async def take(self):
        if self.frames:
            return self.frames.pop(0)
        if self.stall:
            await asyncio.Event().wait()
        return b""
    async def readline(self):
        return await self.take()
    async def read(self, size):
        return await self.take()

class Writer:
    def __init__(self, hang_close=False):
        self.closed = False
        self.commands = []
        self.hang_close = hang_close
    def write(self, data): self.commands.append(data)
    async def drain(self): pass
    def close(self): self.closed = True
    async def wait_closed(self):
        if self.hang_close:
            await asyncio.Event().wait()
    def get_extra_info(self, name): return ("127.0.0.1", 2121)

class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def fetch(self, body=b'{"cpu_temp":63,"soc_power_w":12.5}', replies=None,
                    control_stall=False, data_stall=False, max_bytes=65536,
                    hang_close=False):
        if replies is None:
            replies = [b"220 ready\r\n", b"230 ok\r\n", b"200 binary\r\n",
                       b"227 PASV (192,0,2,1,10,1)\r\n", b"150 data\r\n", b"226 done\r\n"]
        control = Reader(replies, control_stall)
        data = Reader([body] if body is not None else [], data_stall)
        self.writers = [Writer(hang_close), Writer(hang_close)]
        streams = [(control, self.writers[0]), (data, self.writers[1])]
        self.calls = []
        async def connect(host, port, **kwargs):
            self.calls.append((host, port))
            return streams.pop(0)
        with patch.object(ftp.asyncio, 'open_connection', connect):
            return await ftp.async_fetch_telemetry('ps4.test', 2121, '/data/GoldHEN/ps4_state.json',
                       operation_timeout=0.025, total_timeout=0.15, max_bytes=max_bytes)

    async def test_success_and_peer_address(self):
        r = await self.fetch()
        self.assertEqual(r.status, 'ok')
        self.assertEqual(r.values['cpu_temp'], 63)
        self.assertEqual(r.values['soc_power_w'], 12.5)
        self.assertEqual(self.calls[1], ('127.0.0.1', 2561))
        self.assertTrue(all(w.closed for w in self.writers))

    async def test_password_required(self):
        r = await self.fetch(replies=[b'220 ready\r\n', b'331 password\r\n', b'230 ok\r\n',
                       b'200 binary\r\n', b'227 PASV (0,0,0,0,10,1)\r\n', b'150 data\r\n', b'226 done\r\n'])
        self.assertEqual(r.status, 'ok')
        self.assertIn(b'PASS anonymous@\r\n', self.writers[0].commands)

    async def test_multiline_banner(self):
        r = await self.fetch(replies=[b'220-first\r\n', b'hello\r\n', b'220 ready\r\n', b'230 ok\r\n',
                       b'200 binary\r\n', b'227 PASV (0,0,0,0,10,1)\r\n', b'150 data\r\n', b'226 done\r\n'])
        self.assertEqual(r.status, 'ok')

    async def test_missing_file_does_not_read_hanging_data_socket(self):
        r = await self.fetch(body=None, data_stall=True, replies=[b'220 ready\r\n',b'230 ok\r\n',b'200 binary\r\n',
                       b'227 PASV (0,0,0,0,10,1)\r\n',b'550 missing\r\n'])
        self.assertEqual(r.status, 'ftp_550')
        self.assertTrue(r.ftp_reachable)
        self.assertTrue(all(w.closed for w in self.writers))

    async def test_stalled_data_socket_times_out(self):
        r = await self.fetch(body=None, data_stall=True)
        self.assertEqual(r.status, 'timeout')
        self.assertTrue(all(w.closed for w in self.writers))

    async def test_stalled_banner_times_out(self):
        r = await self.fetch(replies=[], control_stall=True)
        self.assertEqual(r.status, 'timeout')
        self.assertFalse(r.ftp_reachable)
        self.assertTrue(self.writers[0].closed)

    async def test_missing_completion_times_out(self):
        r = await self.fetch(replies=[b'220 ready\r\n',b'230 ok\r\n',b'200 binary\r\n',
                       b'227 PASV (0,0,0,0,10,1)\r\n',b'150 data\r\n'], control_stall=True)
        self.assertEqual(r.status, 'timeout')

    async def test_size_limit(self):
        r = await self.fetch(body=b'x'*17, max_bytes=16)
        self.assertEqual(r.status, 'protocol_error')
        self.assertTrue(all(w.closed for w in self.writers))

    async def test_invalid_json(self):
        for body in [b'', b'{', b'[]', b'{}', b'{"unknown":1}', b'\xff']:
            with self.subTest(body=body):
                r = await self.fetch(body=body)
                self.assertEqual(r.status, 'invalid_json')
                self.assertTrue(r.ftp_reachable)

    async def test_invalid_pasv(self):
        for value in [b'227 wrong\r\n',b'227 (0,0,0,0,300,1)\r\n',b'227 (0,0,0,0,0,0)\r\n']:
            r = await self.fetch(replies=[b'220 ready\r\n',b'230 ok\r\n',b'200 binary\r\n',value])
            self.assertEqual(r.status,'protocol_error')

    async def test_connection_error(self):
        with patch.object(ftp.asyncio, 'open_connection', side_effect=OSError('offline')):
            r = await ftp.async_fetch_telemetry('ps4.test',2121,'/state.json')
        self.assertEqual(r.status,'connection_error')
        self.assertFalse(r.ftp_reachable)

    async def test_cancellation_closes_both_connections(self):
        control = Reader([b'220 ready\r\n',b'230 ok\r\n',b'200 binary\r\n',
                          b'227 (0,0,0,0,10,1)\r\n',b'150 data\r\n'])
        writers=[Writer(),Writer()]
        connected = asyncio.Event()
        streams=[(control,writers[0]),(Reader([],True),writers[1])]
        async def connect(*args,**kwargs):
            stream=streams.pop(0)
            if not streams: connected.set()
            return stream
        with patch.object(ftp.asyncio,'open_connection',connect):
            task=asyncio.create_task(ftp.async_fetch_telemetry('ps4.test',2121,'/state.json'))
            await connected.wait()
            await asyncio.sleep(0)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
        self.assertTrue(all(w.closed for w in writers))

    async def test_close_wait_is_bounded(self):
        r = await asyncio.wait_for(self.fetch(hang_close=True),timeout=0.6)
        self.assertEqual(r.status,'ok')

    async def test_control_rejection(self):
        r=await self.fetch(replies=[b'220 ready\r\n',b'530 denied\r\n'])
        self.assertEqual(r.status,'ftp_530')
        self.assertFalse(r.ftp_reachable)

class ParserTests(unittest.TestCase):
    def test_invalid_measurements_are_unknown(self):
        result=ftp.parse_telemetry(b'{"cpu_temp":18823,"soc_temp":true,"soc_power_w":NaN,"fan_duty":101}')
        for key in ('cpu_temp','soc_temp','soc_power_w','fan_duty'):
            self.assertIsNone(result[key])
    def test_null_measurements(self):
        self.assertIsNone(ftp.parse_telemetry(b'{"cpu_temp":null}')['cpu_temp'])

class WrapperTests(unittest.IsolatedAsyncioTestCase):
    def wrapper(self, enabled):
        import logging
        from typing import Any
        source=(ROOT/'custom_components/ps4_goldhen/__init__.py').read_text()
        node=next(n for n in ast.parse(source).body if isinstance(n,ast.AsyncFunctionDef) and n.name=='_poll_ftp_json')
        ns={'Any':Any,'HomeAssistant':object,'DataUpdateCoordinator':object,'_LOGGER':logging.getLogger('test')}
        exec((ROOT/'custom_components/ps4_goldhen/const.py').read_text(),ns)
        root={'entry':{'klog_data':{'cpu_temp':63,'current_game':'CUSA00001'}}}
        ns['_ensure_domain_root']=lambda hass: root
        ns['_PS4STATE_JSON_PATH']='/state.json'
        ns['_FTP_TELEMETRY_ENABLED']=enabled
        self.fetches=[]
        async def fetch(*args):
            self.fetches.append(args)
            return ftp.TelemetryResult(True,None,'ftp_550')
        ns['async_fetch_telemetry']=fetch
        self.locks=[]
        def lock(data):
            self.locks.append(asyncio.Lock());return self.locks[-1]
        ns['ftp_session']=SimpleNamespace(lock=lock)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'wrapper','exec'),ns)
        return ns['_poll_ftp_json']

    async def test_bad_telemetry_clears_dynamic_values_without_erasing_game(self):
        result=await self.wrapper(True)('ps4',2121,'entry',object(),object())
        self.assertIsNone(result['cpu_temp'])
        self.assertEqual(result['current_game'],'CUSA00001')
        self.assertTrue(result['ftp_reachable'])
        self.assertEqual(len(self.locks),1,'the poll takes the FTP lock')

    async def test_disabled_telemetry_opens_no_ftp_session(self):
        result=await self.wrapper(False)('ps4',2121,'entry',object(),object())
        self.assertEqual(self.fetches,[])
        self.assertEqual(result['telemetry_status'],'disabled')
        self.assertIsNone(result['ftp_reachable'])
        self.assertEqual(result['cpu_temp'],63)
        self.assertEqual(result['current_game'],'CUSA00001')

if __name__=='__main__': unittest.main(verbosity=2)
