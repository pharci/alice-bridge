import json
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from alice_bridge.config import defaults
from alice_bridge.core import Bridge
from alice_bridge.http_api import create_server
from modules.client import BridgeClient
from modules.ollama import run
from tests.test_api import FakeNative

class ModuleTests(unittest.TestCase):
    def test_ollama_is_an_independent_worker_using_the_public_http_contract(self):
        calls = []
        class Provider(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                calls.append(value)
                result = {'message': {'content': 'Test answer'}, 'done_reason': 'stop'}
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        provider = ThreadingHTTPServer(('127.0.0.1', 0), Provider)
        provider_thread = threading.Thread(target=provider.serve_forever, kwargs={'poll_interval': .02})
        provider_thread.start()
        config = defaults()
        config['bridge']['token_env'] = ''
        config['ollama'].update(url='http://127.0.0.1:' + str(provider.server_port), model='test-model')
        native = FakeNative()
        bridge = Bridge(config, native)
        server = create_server(bridge, host='127.0.0.1', port=0)
        server_thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02})
        server_thread.start()
        url = 'http://127.0.0.1:' + str(server.server_port)
        stop = threading.Event()
        worker = threading.Thread(target=run, args=(config, stop, url), daemon=True)
        try:
            with patch.object(BridgeClient, 'poll', lambda self: self.request('/v1/requests?timeout=1')):
                worker.start()
                bridge.enqueue('Hello', 'test-turn', cancel=False)
                deadline = time.monotonic() + 5
                while not native.spoken and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertEqual(native.spoken, ['Test answer'])
                self.assertEqual(bridge.status()['module'], 'ollama')
                self.assertEqual(calls[0]['messages'], [])
                self.assertEqual(calls[1]['messages'][-1], {'role': 'user', 'content': 'Hello'})
                self.assertEqual(calls[1]['keep_alive'], -1)
                self.assertFalse(calls[1]['think'])
                stop.set()
                worker.join(3)
                self.assertFalse(worker.is_alive())
        finally:
            stop.set()
            bridge.close()
            server.shutdown()
            server.server_close()
            server_thread.join(2)
            provider.shutdown()
            provider.server_close()
            provider_thread.join(2)
