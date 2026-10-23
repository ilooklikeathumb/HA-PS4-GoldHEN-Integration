import asyncio
import importlib.util
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT=Path(__file__).resolve().parents[1]
MODULE=ROOT/'custom_components/ps4_goldhen/websocket.py'

class Connection:
    def __init__(self):self.results=[];self.errors=[];self.subscriptions={}
    def send_result(self,*args):self.results.append(args)
    def send_error(self,*args):self.errors.append(args)

class FTPHandlerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        vol=ModuleType('voluptuous')
        vol.Required=lambda key,**kw:key
        vol.Optional=lambda key,**kw:key
        vol.All=lambda *args:object()
        vol.Coerce=lambda *args:object()
        vol.Range=lambda **kw:object()
        ha=ModuleType('homeassistant');ha.__path__=[]
        components=ModuleType('homeassistant.components');components.__path__=[]
        api=ModuleType('homeassistant.components.websocket_api')
        api.websocket_command=lambda schema:lambda f:f
        api.async_response=lambda f:f
        api.require_admin=lambda f:f
        api.ActiveConnection=object
        components.websocket_api=api
        core=ModuleType('homeassistant.core')
        core.HomeAssistant=object;core.callback=lambda f:f
        helpers=ModuleType('homeassistant.helpers');helpers.__path__=[]
        dispatcher=ModuleType('homeassistant.helpers.dispatcher')
        dispatcher.async_dispatcher_connect=lambda *args:lambda:None
        util=ModuleType('homeassistant.util');util.__path__=[]
        dt=ModuleType('homeassistant.util.dt');util.dt=dt
        package=ModuleType('_ps4_handler_test');package.__path__=[str(MODULE.parent)]
        const=ModuleType('_ps4_handler_test.const')
        exec((MODULE.parent/'const.py').read_text(),const.__dict__)
        fake={'voluptuous':vol,'homeassistant':ha,'homeassistant.components':components,
              'homeassistant.components.websocket_api':api,'homeassistant.core':core,
              'homeassistant.helpers':helpers,'homeassistant.helpers.dispatcher':dispatcher,
              'homeassistant.util':util,'homeassistant.util.dt':dt,
              '_ps4_handler_test':package,'_ps4_handler_test.const':const}
        self.modules=patch.dict(sys.modules,fake);self.modules.start();self.addCleanup(self.modules.stop)
        spec=importlib.util.spec_from_file_location('_ps4_handler_test.websocket',MODULE)
        self.ws=importlib.util.module_from_spec(spec)
        sys.modules[spec.name]=self.ws
        self.addCleanup(lambda:sys.modules.pop(spec.name,None))
        spec.loader.exec_module(self.ws)
        self.hass=SimpleNamespace(data={'ps4_goldhen':{'entry':{'host':'ps4.test','ftp_port':2121}}},
                                 async_add_executor_job=lambda f,*a:asyncio.get_running_loop().run_in_executor(None,f,*a))

    async def exercise(self,handler,helper,msg,value,expected):
        c=Connection()
        with patch.object(self.ws,helper,return_value=value) as call:
            await getattr(self.ws,handler)(self.hass,c,{'id':1,'entry_id':'entry',**msg})
        self.assertFalse(c.errors,c.errors)
        self.assertEqual(c.results,[(1,expected)])
        call.assert_called_once()
        self.assertEqual(call.call_args.args[:2],('ps4.test',2121))

    async def test_list_dir(self):
        await self.exercise('ws_list_dir','_ftp_list_dir',{'path':'/'},[{'name':'data'}],{'path':'/','entries':[{'name':'data'}]})
    async def test_delete(self):
        await self.exercise('ws_delete','_ftp_delete',{'path':'/test','is_dir':False},None,{'success':True})
    async def test_rename(self):
        await self.exercise('ws_rename','_ftp_rename',{'from_path':'/a','to_path':'/b'},None,{'success':True})
    async def test_mkdir(self):
        await self.exercise('ws_mkdir','_ftp_mkdir',{'path':'/a'},None,{'success':True})
    async def test_get_text(self):
        await self.exercise('ws_get_text','_ftp_get_text',{'path':'/a'},'example',{'content':'example'})
    async def test_put_text(self):
        await self.exercise('ws_put_text','_ftp_put_text',{'path':'/a','content':'example'},None,{'success':True})
    async def test_a_sleeping_console_gets_no_ftp(self):
        for power in ('rest','off'):
            self.hass.data['ps4_goldhen']['entry']['coordinator']=SimpleNamespace(data={'power_state':power})
            for handler,helper,msg in (('ws_list_dir','_ftp_list_dir',{'path':'/'}),('ws_put_text','_ftp_put_text',{'path':'/a','content':'x'})):
                c=Connection()
                with patch.object(self.ws,helper) as call:
                    await getattr(self.ws,handler)(self.hass,c,{'id':1,'entry_id':'entry',**msg})
                call.assert_not_called()
                self.assertEqual(c.errors[0][1],'console_asleep')
                self.assertIn('rest mode or off: turn it on first',c.errors[0][2])
        self.hass.data['ps4_goldhen']['entry']['coordinator']=SimpleNamespace(data={'power_state':None})
        await self.exercise('ws_list_dir','_ftp_list_dir',{'path':'/'},[],{'path':'/','entries':[]})  # unknown still tries
    def test_asyncio_is_imported_by_module(self):
        self.assertIs(self.ws.asyncio,asyncio)

if __name__=='__main__':unittest.main(verbosity=2)
