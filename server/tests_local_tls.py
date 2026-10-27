import asyncio
import os
import pathlib
import ssl
import sys
import tempfile

from aiohttp import ClientSession, TCPConnector, web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


async def main():
    with tempfile.TemporaryDirectory() as folder:
        os.environ['LOSTOS_DATA'] = folder
        os.environ['LOSTOS_PUBLIC_URL'] = 'https://127.0.0.1'
        os.environ['LOSTOS_LOCAL_TLS'] = '1'
        import certs
        context = certs.local_context()
        assert isinstance(context, ssl.SSLContext)
        for path in (certs.CA_KEY, certs.SRV_KEY):
            assert pathlib.Path(path).stat().st_mode & 0o077 == 0, path
        original = pathlib.Path(certs.CA_CRT).read_bytes()
        certs.ensure({'127.0.0.1'})
        assert pathlib.Path(certs.CA_CRT).read_bytes() == original
        app = web.Application()
        app.router.add_get('/', lambda req: web.Response(text='secure'))
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0, ssl_context=context)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        trusted = ssl.create_default_context(cafile=certs.CA_CRT)
        try:
            async with ClientSession(connector=TCPConnector(ssl=trusted)) as session:
                async with session.get(f'https://127.0.0.1:{port}/') as response:
                    assert await response.text() == 'secure'
            async with ClientSession() as session:
                try:
                    await session.get(f'https://127.0.0.1:{port}/')
                except Exception:
                    pass
                else:
                    raise AssertionError('untrusted CA accepted')
        finally:
            await runner.cleanup()
        os.environ['LOSTOS_PUBLIC_URL'] = 'http://127.0.0.1'
        try:
            certs.local_context()
        except ValueError:
            pass
        else:
            raise AssertionError('local TLS downgraded to HTTP')
        os.environ.pop('LOSTOS_LOCAL_TLS')
        os.environ['LOSTOS_BIND'] = '0.0.0.0'
        try:
            certs.local_context()
        except ValueError:
            pass
        else:
            raise AssertionError('public plaintext listener accepted')
        os.environ['LOSTOS_BIND'] = '127.0.0.1'
        assert certs.local_context() is None
    print('ALL PASS: local HTTPS, persistent CA, private key permissions and downgrade refusal')


asyncio.run(main())
