import asyncio
import json
import os
import pathlib
import sys
import tempfile
import types
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import plugins


async def main():
    with tempfile.TemporaryDirectory() as folder:
        root = pathlib.Path(folder)
        secret = root / 'other-tenant-secret'
        secret.write_text(json.dumps({'secret': 'private-test-value'}))
        package = root / 'plugin'
        package.mkdir()
        (package / 'main.py').write_text('''import json, os, pathlib, sys
import lostos_sdk
p = pathlib.Path(os.environ['LOSTOS_PLUGIN_DIR']) / 'result.json'
r = {'secret_env': 'OPENAI_API_KEY' in os.environ, 'parent_env': 'LOSTOS_INTERNAL_KEY' in os.environ,
     'secret_file': pathlib.Path(''' + repr(str(secret)) + ''').exists(),
     'server_file': pathlib.Path(''' + repr(plugins.__file__) + ''').exists(),
     'sdk': callable(lostos_sdk.call)}
lostos_sdk.call('storage.set', key='roundtrip', value='ok')
r['rpc'] = lostos_sdk.call('storage.get', key='roundtrip')
p.write_text(json.dumps(r))
''')
        srv = types.SimpleNamespace(log=lambda *args: None)
        manager = plugins.Manager(srv)
        plugin = plugins.Plugin({'id': 'ru.test.isolation', 'entry': 'main.py', 'builtin': '', 'system': False, 'permissions': ['storage']}, str(package), 'user')
        manager.state[plugin.id] = {'granted': ['storage']}
        with patch.dict(os.environ, OPENAI_API_KEY='fake-key-for-test', LOSTOS_INTERNAL_KEY='fake-tenant-key'):
            await manager.run(plugin)
        assert plugin.proc is not None, plugin.error
        await asyncio.wait_for(plugin.proc.wait(), 10)
        assert plugin.proc.returncode == 0, plugin.error
        result = json.loads((package / 'data/result.json').read_text())
        assert not result['secret_env'], result
        assert not result['parent_env'], result
        assert not result['secret_file'], result
        assert not result['server_file'], result
        assert result['sdk'], result
        assert result['rpc'] == 'ok', result
        await manager.halt(plugin)
        store = package / 'data/storage.json'
        store.unlink()
        store.symlink_to(secret)
        assert await manager._call(plugin, 'storage.get', {'key': 'secret'}) is None
        await manager._call(plugin, 'storage.set', {'key': 'secret', 'value': 'changed'})
        assert json.loads(secret.read_text())['secret'] == 'private-test-value'
        assert not store.is_symlink()
        with patch('plugins.shutil.which', return_value=None):
            await manager.run(plugin)
        assert plugin.proc is None or plugin.proc.returncode is not None
        assert plugin.error, 'sandbox unavailable must not fall back to unsandboxed Python'
    print('ALL PASS: plugin cannot inherit credentials or read server/tenant files; SDK and data remain usable')


asyncio.run(main())
