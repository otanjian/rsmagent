"""Private IPC worker using sap-connect's installed MCP SDK; no project edits."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from Scene.sap_workbench.backend.mcp_login import SapIdentity, SapMcpLogin, McpLoginError


async def main():
    initial = json.loads(await asyncio.to_thread(sys.stdin.readline))
    identity = SapIdentity(**initial['identity'])
    async def revalidate():
        return identity  # owner process revalidates platform/config on each IPC call
    login = SapMcpLogin(identity, revalidate=revalidate)
    try:
        ready = await login.connect(initial['connections'], initial['url'], initial.pop('password'))
        print(json.dumps(ready), flush=True)
        while True:
            line = await asyncio.to_thread(sys.stdin.readline)
            if not line:
                break
            try:
                message = json.loads(line)
                if message.get('action') == 'close':
                    break
                if message.get('action') == 'read_json':
                    result = await login.call_json(message['connection'], message['tool'], message['arguments'])
                    print(json.dumps({'data': result}, ensure_ascii=False, separators=(',', ':')), flush=True)
                    continue
                if message.get('action') == 'call_business':
                    # The scene-mediated business channel. It shares this
                    # transport's scrubbing and truncation, but draws its tool
                    # allowlist from `BUSINESS_TOOLS` rather than `READ_TOOLS`.
                    result = await login.call_business(message['connection'], message['tool'], message['arguments'])
                else:
                    result = await login.call(message['connection'], message['tool'], message['arguments'])
                # Transport identity is never part of the returned model data.
                text = '\n'.join(item.text for item in result.content if getattr(item, 'type', '') == 'text')
                for entry in login._connections.values():
                    text = text.replace(entry[1], '[connection]')
                print(json.dumps({'output': text[:32000]}, ensure_ascii=False), flush=True)
            except Exception:
                print(json.dumps({'error': 'mcp_call_failed'}), flush=True)
    except Exception as error:
        print(json.dumps({'error': error.code if isinstance(error, McpLoginError) else 'mcp_login_failed'}), flush=True)
    finally:
        await login.close()


if __name__ == '__main__':
    asyncio.run(main())
