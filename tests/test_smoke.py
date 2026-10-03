"""Run an isolated router against a local fake upstream; never load live config."""
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        payload = self.rfile.read(int(self.headers['Content-Length']))
        body = json.dumps({'path': self.path, 'received': json.loads(payload)}).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class RouterSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        for name in ('jev_router.py', 'dashboard.html'):
            shutil.copy2(ROOT / name, cls.temp.name)
        (Path(cls.temp.name) / 'settings.json').write_text(
            json.dumps({'optimizer': {'enabled': False}}))
        cls.upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        cls.addClassCleanup(cls.upstream.server_close)
        cls.addClassCleanup(cls.upstream.shutdown)
        threading.Thread(target=cls.upstream.serve_forever, daemon=True).start()
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        cls.base = 'http://127.0.0.1:%d' % port
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('JEV_', 'TYPESAFE_', 'OPENROUTER_'))}
        env.update(JEV_LISTEN_HOST='127.0.0.1', JEV_LISTEN_PORT=str(port),
                   JEV_UPSTREAM='http://127.0.0.1:%d' % cls.upstream.server_port,
                   JEV_UPSTREAM_KEY='test', TYPESAFE_API_KEY='test',
                   JEV_REWRITE='0')
        cls.log = tempfile.TemporaryFile()
        cls.addClassCleanup(cls.log.close)
        cls.proc = subprocess.Popen([sys.executable, 'jev_router.py'],
                                    cwd=cls.temp.name, env=env,
                                    stdout=cls.log, stderr=cls.log)
        cls.addClassCleanup(cls.stop_router)
        for _ in range(100):
            try:
                cls.read('/api/stats')
                return
            except OSError:
                if cls.proc.poll() is not None:
                    break
                time.sleep(.1)
        cls.log.seek(0)
        raise AssertionError('Router did not start: ' + cls.log.read().decode())

    @classmethod
    def stop_router(cls):
        cls.proc.terminate()
        try:
            cls.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            cls.proc.kill()
            cls.proc.wait()

    @classmethod
    def read(cls, path):
        with urllib.request.urlopen(cls.base + path, timeout=3) as response:
            return response.read()

    def test_dashboard_and_prefix(self):
        expected = (ROOT / 'dashboard.html').read_bytes()
        for path in ('/dashboard', '/jev/dashboard'):
            self.assertEqual(self.read(path), expected)

    def test_empty_database_and_settings(self):
        stats = json.loads(self.read('/api/stats'))
        self.assertNotIn('error', stats)
        self.assertEqual(stats['total_records'], 0)
        for path in ('/api/decisions', '/api/diffs'):
            self.assertEqual(json.loads(self.read(path)), [])
        self.assertIsInstance(json.loads(self.read('/api/settings')), dict)

    def test_passthrough_to_local_upstream(self):
        payload = {'model': 'test-model', 'input': 'CI smoke test', 'stream': False}
        request = urllib.request.Request(self.base + '/v1/responses',
                                         data=json.dumps(payload).encode(),
                                         headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=5) as response:
            result = json.load(response)
        self.assertEqual(result['received'], payload)
        self.assertEqual(result['path'], '/v1/responses')
