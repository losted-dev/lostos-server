import http.client
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    code, data = root / 'app', root / 'data'
    subprocess.run(['bash', str(Path(__file__).resolve().parent.parent / 'tools/stage-server.sh'), str(code)], check=True, stdout=subprocess.DEVNULL)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    env = dict(os.environ, LOSTOS_LITE='1', LOSTOS_AUTH='1', LOSTOS_BIND='0.0.0.0',
               LOSTOS_PUBLIC_URL=f'http://127.0.0.1:{port}', LOSTOS_DATA=str(data), TTS_PORT=str(port), LOSTOS_NO_LOCAL_ENGINES='1')
    with (root / 'server.log').open('w') as log:
        process = subprocess.Popen([sys.executable, '-u', 'voice_server.py'], cwd=code, env=env, stdout=log, stderr=log)
        try:
            deadline = time.monotonic() + 45
            while True:
                try:
                    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
                    connection.request('GET', '/auth/whoami')
                    response = connection.getresponse()
                    assert response.status == 200
                    response.read()
                    connection.close()
                    break
                except OSError:
                    if process.poll() is not None or time.monotonic() > deadline:
                        raise AssertionError('home HTTP server did not start')
                    time.sleep(0.2)
            assert not (data / 'certs').exists()
            assert not (code / 'certs').exists()
            setup = urlsplit((data / 'setup-link.txt').read_text().strip())
            connection = http.client.HTTPConnection('127.0.0.1', port, timeout=2)
            connection.request('GET', setup.path + '?' + setup.query)
            response = connection.getresponse()
            assert response.status == 302
            cookie = response.getheader('Set-Cookie', '')
            assert 'lostos_session=' in cookie and 'Secure' not in cookie
            connection.close()
        finally:
            process.terminate()
            try:
                process.wait(10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
print('ALL PASS: home HTTP starts without certificates and permits normal panel login')
