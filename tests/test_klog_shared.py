import ast
import asyncio
from datetime import datetime, timezone
import errno
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = 'ps4_goldhen'

def signal_klog_line(entry_id): return f'{DOMAIN}_klog_line_{entry_id}'

class Dispatcher:
    """Stands in for homeassistant.helpers.dispatcher (klog lines go here, not on the bus)."""
    def __init__(self): self.listeners=[];self.sent=[]
    def connect(self,hass,signal,fn):
        item=(signal,fn);self.listeners.append(item)
        def unsub():
            if item in self.listeners:self.listeners.remove(item)
        return unsub
    def send(self,hass,signal,*args):
        self.sent.append((signal,args))
        for s,f in list(self.listeners):
            if s==signal:f(*args)

class Bus:
    """Nothing may fire a klog line on the event bus (the recorder stores every event)."""
    def async_listen(self,*a):raise AssertionError('klog must not use the event bus')
    def async_fire(self,*a):raise AssertionError('klog must not use the event bus')

def load_function(path, name, extra):
    source = (ROOT / path).read_text()
    node = next(n for n in ast.parse(source).body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name == name)
    node.decorator_list = []
    ns = {'Any':Any,'HomeAssistant':object,'DataUpdateCoordinator':object,
          'websocket_api':SimpleNamespace(ActiveConnection=object),
          'callback':lambda f:f,'DOMAIN':DOMAIN,'signal_klog_line':signal_klog_line,
          'dt_util':SimpleNamespace(utcnow=lambda:datetime.now(timezone.utc)),
          '_LOGGER':logging.getLogger('klog-test'),'asyncio':asyncio,'errno':errno}
    ns.update(extra)
    exec(compile(ast.Module(body=[node],type_ignores=[]),'isolated-function','exec'),ns)
    return ns[name]

class Connection:
    def __init__(self):self.subscriptions={};self.results=[];self.errors=[];self.messages=[]
    def send_result(self,*args):self.results.append(args)
    def send_error(self,*args):self.errors.append(args)
    def send_message(self,msg):self.messages.append(msg)

class SubscriptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.disp=Dispatcher();self.hass=SimpleNamespace(data={DOMAIN:{'one':{'klog_data':{'klog_connected':True}}}},bus=Bus())
        self.fn=load_function('custom_components/ps4_goldhen/websocket.py','ws_klog_subscribe',
                              {'async_dispatcher_connect':self.disp.connect})
    def send(self,entry_id,line):self.disp.send(self.hass,signal_klog_line(entry_id),line)
    async def test_no_ps4_tcp_connection(self):
        c=Connection()
        with patch.object(asyncio,'open_connection',side_effect=AssertionError('Unexpected TCP')):
            await self.fn(self.hass,c,{'id':1,'entry_id':'one'})
        self.assertTrue(c.results[0][1]['subscribed'])
    async def test_entry_filter_and_event_shape(self):
        c=Connection();await self.fn(self.hass,c,{'id':1,'entry_id':'one'})
        self.send('two','ignore')
        self.send('one','raw line')
        self.assertEqual(len(c.messages),1)
        self.assertEqual(c.messages[0]['event']['line'],'raw line')
        self.assertEqual(c.messages[0]['type'],'event')
        datetime.fromisoformat(c.messages[0]['event']['time'])
    async def test_unsubscribe_stops_delivery(self):
        c=Connection();await self.fn(self.hass,c,{'id':1,'entry_id':'one'})
        c.subscriptions[1]()
        self.send('one','ignore')
        self.assertFalse(c.messages)
    async def test_two_subscribers_share_the_signal(self):
        a,b=Connection(),Connection()
        await self.fn(self.hass,a,{'id':1,'entry_id':'one'})
        await self.fn(self.hass,b,{'id':2,'entry_id':'one'})
        self.send('one','line')
        self.assertEqual(len(a.messages),1);self.assertEqual(len(b.messages),1)
    async def test_missing_entry(self):
        c=Connection();await self.fn(self.hass,c,{'id':1,'entry_id':'missing'})
        self.assertEqual(c.errors[0][1],'not_found');self.assertFalse(self.disp.listeners)
    async def test_missing_line_ignored(self):
        c=Connection();await self.fn(self.hass,c,{'id':1,'entry_id':'one'})
        self.send('one',None)
        self.assertFalse(c.messages)

class ListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_cleanup_and_raw_stream(self):
        import contextlib
        disp=Dispatcher()
        sm=SimpleNamespace(klog_connected=False,on_klog_connected=lambda now:None,tick=lambda now:False,
                       on_klog_heard=lambda:False)
        data={'klog_state_machine':sm,'klog_data':{'klog_connected':False}}
        hass=SimpleNamespace(data={DOMAIN:{'one':data}},bus=Bus(),loop=asyncio.get_running_loop())
        coordinator=SimpleNamespace(data={},async_set_updated_data=lambda d:None)
        ready=asyncio.Event()
        class Reader:
            first=True
            async def read(self,n):
                if self.first:
                    self.first=False
                    return b'noise line\nreal line\n'
                ready.set()
                await asyncio.Event().wait()
        class Writer:
            closed=False
            def close(self):self.closed=True
            async def wait_closed(self):pass
            def get_extra_info(self,name):return None
        writer=Writer()
        async def connect(*args,**kwargs):return Reader(),writer
        noop=SimpleNamespace(async_on_klog_connected=lambda *a:None,async_handle_line=lambda *a:False)
        fn=load_function('custom_components/ps4_goldhen/__init__.py','_klog_listener_task',{
            'contextlib':contextlib,'_parse_klog_line':lambda *args:False,'async_dispatcher_send':disp.send,
            'console_info':noop,'live_sensors':noop,'_STALE_KLOG_CHECK':300})
        with patch.object(asyncio,'open_connection',connect):
            t=asyncio.create_task(fn(hass,'one','ps4',3232,coordinator))
            await asyncio.wait_for(ready.wait(),timeout=1)
            self.assertTrue(data['klog_data']['klog_connected'])
            t.cancel()
            with self.assertRaises(asyncio.CancelledError):await t
        self.assertTrue(writer.closed)
        self.assertEqual(disp.sent,[(signal_klog_line('one'),('noise line',)),(signal_klog_line('one'),('real line',))])
    def test_pending_line_buffer_is_bounded(self):
        src=(ROOT/'custom_components/ps4_goldhen/__init__.py').read_text()
        self.assertIn('lines[-1][-65536:]',src)
    def test_no_klog_line_reaches_the_event_bus(self):
        # the integration fired ps4_goldhen_klog_stream + ps4_goldhen_klog_event per line; the recorder kept both
        tree=ast.parse((ROOT/'custom_components/ps4_goldhen/__init__.py').read_text())
        for name in ('_klog_listener_task','_parse_klog_line'):
            fn=next(n for n in tree.body if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name==name)
            self.assertNotIn('async_fire',ast.unparse(fn),name)
        self.assertNotIn('ps4_goldhen_klog_',(ROOT/'custom_components/ps4_goldhen/const.py').read_text())

if __name__=='__main__':unittest.main(verbosity=2)
