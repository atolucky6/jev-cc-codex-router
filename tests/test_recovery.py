"""Recovery regression tests using isolated config and a controlled upstream."""
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        source = Path(self.temp.name) / 'jev_router.py'
        shutil.copy2(ROOT / 'jev_router.py', source)
        spec = importlib.util.spec_from_file_location('isolated_router', source)
        self.router = importlib.util.module_from_spec(spec)
        with patch.dict(os.environ, {'TYPESAFE_API_KEY': 'test', 'JEV_UPSTREAM_KEY': 'test'}, clear=True):
            spec.loader.exec_module(self.router)
        self.router.RETRY_DELAY = 0
        self.router.OPT_ENABLED = False
        self.router.DEFAULT_MODEL = 'healthy-model'
        self.router.MAP['luna'] = 'broken-model'
        self.meta = {'new': True, 'tier': 'luna', 'out': 'broken-model', 'sid': 'test-session'}
        self.req = urllib.request.Request('http://localhost/v1/responses',
            data=json.dumps({'model': 'broken-model', 'input': 'hello'}).encode(),
            headers={'Content-Type': 'application/json', 'Authorization': 'Bearer test'})

    def error(self, status, code='', message=''):
        return urllib.error.HTTPError(self.req.full_url, status, 'test', {},
            io.BytesIO(json.dumps({'error': {'code': code, 'message': message}}).encode()))

    def test_invalid_model_is_not_retried_and_body_is_preserved(self):
        error = self.error(400, 'model_not_found')
        with patch.object(self.router.urllib.request, 'urlopen', side_effect=error) as call:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.router.open_with_retry(self.req, 'POST', '/v1/responses')
        self.assertEqual(call.call_count, 1)
        self.assertEqual(json.loads(caught.exception.read())['error']['code'], 'model_not_found')

    def test_auth_unavailable_is_not_retried(self):
        with patch.object(self.router.urllib.request, 'urlopen', side_effect=self.error(503, message='auth_unavailable: no auth available')) as call:
            with self.assertRaises(urllib.error.HTTPError):
                self.router.open_with_retry(self.req, 'POST', '/v1/responses')
        self.assertEqual(call.call_count, 1)

    def test_transient_retry_recovers(self):
        response = object()
        with patch.object(self.router.urllib.request, 'urlopen', side_effect=[self.error(503), response]) as call:
            self.assertIs(self.router.open_with_retry(self.req, 'POST', '/v1/responses'), response)
        self.assertEqual(call.call_count, 2)

    def test_fallback_uses_configured_model_and_preserves_auth(self):
        response = object()
        with patch.object(self.router.urllib.request, 'urlopen', side_effect=[self.error(400, 'model_not_found'), response]) as call:
            up, model = self.router.open_routed_request(self.req, 'POST', '/v1/responses', self.meta)
        self.assertIs(up, response)
        self.assertEqual(model, 'healthy-model')
        fallback = call.call_args_list[1].args[0]
        self.assertEqual(json.loads(fallback.data)['model'], model)
        self.assertEqual(fallback.get_header('Authorization'), 'Bearer test')

    def test_fallback_failure_does_not_recurse(self):
        with patch.object(self.router.urllib.request, 'urlopen', side_effect=[self.error(400, 'model_not_found'), self.error(503, 'auth_unavailable')]) as call:
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.router.open_routed_request(self.req, 'POST', '/v1/responses', self.meta)
        self.assertEqual(call.call_count, 2)
        self.assertEqual(json.loads(caught.exception.read())['error']['code'], 'auth_unavailable')

    def test_bad_payload_auth_and_generic_404_do_not_fallback(self):
        for status, code in [(400, 'invalid_request_error'), (401, 'unauthorized'), (404, '')]:
            with self.subTest(status=status):
                with patch.object(self.router.urllib.request, 'urlopen', side_effect=self.error(status, code)) as call:
                    with self.assertRaises(urllib.error.HTTPError):
                        self.router.open_routed_request(self.req, 'POST', '/v1/responses', self.meta)
                self.assertEqual(call.call_count, 1)

    def test_continuation_and_passthrough_do_not_fallback(self):
        for changes in [{'new': False}, {'tier': 'reuse'}, {'tier': None}]:
            self.assertIsNone(self.router.fallback_request(self.req, 'POST', '/v1/responses', dict(self.meta, **changes)))
        self.router.REWRITE = False
        self.assertIsNone(self.router.fallback_request(self.req, 'POST', '/v1/responses', self.meta))

    def test_provider_state_and_tool_history_do_not_fallback(self):
        for extra in [{'previous_response_id': 'resp_1'}, {'conversation': 'conv_1'},
                      {'input': [{'type': 'function_call_output', 'call_id': '1', 'output': 'ok'}]}]:
            body = dict(json.loads(self.req.data), **extra)
            req = urllib.request.Request(self.req.full_url, data=json.dumps(body).encode())
            self.assertIsNone(self.router.fallback_request(req, 'POST', '/v1/responses', self.meta))
        self.req.add_header('Content-Encoding', 'zstd')
        self.assertIsNone(self.router.fallback_request(self.req, 'POST', '/v1/responses', self.meta))

    def test_blank_model_rejected_without_mutation(self):
        before = dict(self.router.MAP)
        for value in ['', '   ', None, 12]:
            ok, _ = self.router.save_settings_data({'models': {'luna': 'changed', 'sol': value}})
            self.assertFalse(ok)
            self.assertEqual(self.router.MAP, before)

    def test_classification_timeout_uses_fallback(self):
        with patch.object(self.router, 'ask_jev', side_effect=TimeoutError('classification timeout')):
            raw, meta = self.router.maybe_rewrite(json.dumps({'model': 'original', 'input': 'hello'}).encode(), '/v1/responses')
        self.assertEqual(json.loads(raw)['model'], 'healthy-model')
        self.assertEqual(meta['err'], 'classification timeout')

    def test_http_fallback_updates_session_and_continuation(self):
        seen = []
        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                seen.append(payload['model'])
                if payload['model'] == 'broken-model':
                    status, result = 503, {'error': {'code': 'auth_unavailable'}}
                else:
                    status, result = 200, {'model': payload['model']}
                data = json.dumps(result).encode()
                self.send_response(status)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
        router = ThreadingHTTPServer(('127.0.0.1', 0), self.router.Handler)
        for server in [upstream, router]:
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
        self.router.UPSTREAM_ORIGIN = 'http://127.0.0.1:%d' % upstream.server_port
        self.router.init_db()
        base = 'http://127.0.0.1:%d/v1/responses' % router.server_port
        with patch.object(self.router, 'ask_jev', return_value=('luna', None, 'medium', None)):
            for text in ['hello', 'continue']:
                req = urllib.request.Request(base,
                    data=json.dumps({'model': 'original', 'input': text}).encode(),
                    headers={'Content-Type': 'application/json', 'session-id': 'test-session'})
                with urllib.request.urlopen(req, timeout=3) as response:
                    self.assertEqual(json.load(response)['model'], 'healthy-model')
        self.assertEqual(seen, ['broken-model', 'healthy-model', 'healthy-model'])
        self.assertEqual(self.router.LEASE['test-session'], 'healthy-model')
