import json
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

from alice_bridge.config import defaults
from alice_bridge.core import Bridge
from alice_bridge.http_api import create_server
from modules.client import APIError, BridgeClient

class FakeNative:
    def __init__(self):
        self.spoken = []
        self.cancellations = 0
    def audio_status(self):
        return {'volume_raw': 8, 'muted': False}
    def cancel(self):
        self.cancellations += 1
    def speak(self, text):
        self.spoken.append(text)
    def close(self):
        pass

class APITests(unittest.TestCase):
    def setUp(self):
        self.config = defaults()
        self.config['bridge']['token_env'] = ''
        self.native = FakeNative()
        self.bridge = Bridge(self.config, native=self.native)
        self.server = create_server(self.bridge, host='127.0.0.1', port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .02})
        self.thread.start()
        self.url = 'http://127.0.0.1:' + str(self.server.server_port)
        self.client = BridgeClient(self.url)

    def tearDown(self):
        self.bridge.close()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(2)

    def ask(self, text='Question'):
        self.client.request('/v1/ask', {'text': text})
        return self.client.poll(0)

    def test_external_agent_round_trip_and_retry_deduplication(self):
        request = self.ask()
        self.assertEqual(request['text'], 'Question')
        self.assertNotIn('_generation', request)
        self.assertEqual(self.client.respond(request['request_id'], 'Answer'),
                         {'submitted': True, 'duplicate': False})
        self.assertTrue(self.client.respond(request['request_id'], 'Answer')['duplicate'])
        self.assertEqual(self.native.spoken, ['Answer'])
        self.assertEqual(self.bridge.status()['answers'], 1)
        self.assertIsNone(self.client.poll(0))

    def test_new_turn_rejects_old_agent_reply(self):
        old = self.ask('First')
        new = self.ask('Second')
        with self.assertRaises(APIError) as caught:
            self.client.respond(old['request_id'], 'Too late')
        self.assertEqual(caught.exception.status, 409)
        self.client.respond(new['request_id'], 'Current')
        self.assertEqual(self.native.spoken, ['Current'])

    def test_stop_command_invalidates_a_claimed_request(self):
        request = self.ask()
        self.bridge.on_recognition('Стоп', 'stop-turn')
        with self.assertRaises(APIError) as caught:
            self.client.respond(request['request_id'], 'Late')
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(self.native.spoken)

    def test_prefix_native_turn_invalidates_a_claimed_agent_reply(self):
        self.bridge.args.mode = 'prefix'
        request = self.ask('qwen, first question')
        self.bridge.on_recognition('a native request without the agent prefix', 'native-turn')
        with self.assertRaises(APIError) as caught:
            self.client.respond(request['request_id'], 'Late answer')
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(self.native.spoken)
        self.assertEqual(self.native.cancellations, 1)
        self.assertEqual(self.bridge.status()['last_route'], 'native')

    def test_expired_reply_cannot_be_spoken(self):
        request = self.ask()
        self.bridge.tasks[request['request_id']]['_started'] -= 200
        with self.assertRaises(APIError) as caught:
            self.client.respond(request['request_id'], 'Expired')
        self.assertEqual(caught.exception.status, 409)
        self.assertFalse(self.native.spoken)
        self.assertFalse(self.bridge.status()['busy'])

    def test_conflicting_duplicate_does_not_speak_twice(self):
        request = self.ask()
        self.client.respond(request['request_id'], 'First')
        with self.assertRaises(APIError) as caught:
            self.client.respond(request['request_id'], 'Different')
        self.assertEqual(caught.exception.status, 409)
        self.assertEqual(self.native.spoken, ['First'])

    def test_agent_errors_are_silent(self):
        request = self.ask()
        self.client.fail(request['request_id'], 'provider_unavailable')
        self.assertFalse(self.native.spoken)
        self.assertFalse(self.bridge.status()['busy'])
        self.assertEqual(self.bridge.status()['last_error'], 'provider_unavailable')

    def test_failed_native_cancel_does_not_leave_a_busy_request(self):
        with patch.object(self.native, 'cancel', side_effect=OSError('unavailable')):
            with self.assertRaises(APIError) as caught:
                self.client.request('/v1/ask', {'text': 'Question'})
        self.assertEqual(caught.exception.status, 503)
        self.assertFalse(self.bridge.status()['busy'])
        self.assertTrue(self.bridge.jobs.empty())

    def test_core_operates_without_a_model_or_worker(self):
        self.assertFalse(self.config['ollama']['model'])
        self.assertFalse(self.bridge.status()['module_ready'])
        self.assertIsNone(self.client.poll(0))
        self.assertFalse(self.native.spoken)

    def test_bad_json_types_are_rejected(self):
        for body in ([], {'request_id': 123, 'text': 'bad'}, {'request_id': 'a', 'text': []}):
            with self.subTest(body=body), self.assertRaises(APIError) as caught:
                self.client.request('/v1/responses', body)
            self.assertEqual(caught.exception.status, 400)

    def test_browser_origin_is_rejected(self):
        request = urllib.request.Request(self.url + '/v1/health', headers={'Origin': 'https://example.com'})
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.client.http.open(request)
        self.assertEqual(caught.exception.code, 403)
        caught.exception.close()

    def test_long_poll_waits_for_a_new_request(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.client.poll(2)))
        thread.start()
        time.sleep(.05)
        self.bridge.enqueue('Queued later', 'new-turn', cancel=False)
        thread.join(3)
        self.assertEqual(result[0]['text'], 'Queued later')

    def test_token_is_required_when_configured(self):
        self.config['bridge']['token_env'] = 'TEST_BRIDGE_TOKEN'
        with patch.dict('os.environ', {'TEST_BRIDGE_TOKEN': 'test-only-token'}):
            server = create_server(self.bridge, host='127.0.0.1', port=0)
        thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': .02})
        thread.start()
        try:
            url = 'http://127.0.0.1:' + str(server.server_port)
            for secret in ('', 'wrong'):
                with self.assertRaises(APIError) as caught:
                    BridgeClient(url, secret).request('/v1/health')
                self.assertEqual(caught.exception.status, 401)
            self.assertIn('connected', BridgeClient(url, 'test-only-token').request('/v1/health'))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
