import asyncio, sys
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from client import Client
import json
import os

def stdio_parameters():
    # MCP's default environment is deliberately limited; forward bridge config
    # explicitly so Linux uses its tunnel and not Windows' local port.
    env={key:value for key,value in os.environ.items() if key.startswith('DESKTOP_BRIDGE_')}
    return StdioServerParameters(command=sys.executable,args=[str(Path(__file__).with_name('mcp_adapter.py'))],env=env)

async def main():
    params=stdio_parameters()
    async with stdio_client(params) as (reader,writer):
        async with ClientSession(reader,writer) as session:
            await session.initialize()
            tools=await session.list_tools()
            assert {t.name for t in tools.tools} == {'desktop_status','desktop_windows','desktop_observe','desktop_inspect','desktop_wait_for','desktop_lease','desktop_action','desktop_actions','desktop_release'}
            result=await session.call_tool('desktop_status',{})
            assert not result.isError
            windows=await session.call_tool('desktop_windows',{'include_popups':True})
            assert not windows.isError
            hwnd=next(w['id'] for w in json.loads(windows.content[0].text)['windows'] if w['active'])
            waited=await session.call_tool('desktop_wait_for',{'window_id':hwnd,'selector':{'name':'__bridge_smoke_missing_element__'},'timeout':0})
            assert not waited.isError
            assert not any(c.type=='image' for c in waited.content)
            assert json.loads(waited.content[0].text)['status']=='timeout'
            result=await session.call_tool('desktop_observe',{})
            assert not result.isError
            assert any(c.type=='image' for c in result.content), [c.type for c in result.content]
            lease_response=await session.call_tool('desktop_lease',{'ttl':30})
            lease=json.loads(lease_response.content[0].text)['lease_id']
            point=Client(os.environ.get('DESKTOP_BRIDGE_URL','http://127.0.0.1:8765')).call('observe',{})['cursor']
            action={'type':'move','x':point['x'],'y':point['y']}
            try:
                for name,args in [('desktop_action',{'action':action}),('desktop_actions',{'actions':[action,action]})]:
                    result=await session.call_tool(name,{'lease_id':lease,**args})
                    assert not result.isError
                    assert not any(c.type=='image' for c in result.content), 'Actions must not return an implicit screenshot'
            finally:
                await session.call_tool('desktop_release',{'lease_id':lease})
            rejected=await session.call_tool('desktop_action',{'lease_id':lease,'action':action})
            assert rejected.isError and 'lease_required' in rejected.content[0].text
            print('MCP: 9 tools, native screenshot, no implicit images, structured error details OK')

if __name__=='__main__': asyncio.run(main())
