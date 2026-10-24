import json
import pathlib
import socket
import subprocess
import threading
import time
import unittest
from unittest.mock import Mock, patch

from alice_bridge.core import Bridge
from alice_bridge.native import NativeCLI
from alice_bridge.speech import speech_text
from alice_bridge.config import defaults
from alice_bridge.protocol import (FrameReader, action_directives, alice_state, child,
                        fields, first, frame, text)

ROOT = pathlib.Path(__file__).parent / "fixtures"


def test_config(mode="auto"):
    config = defaults()
    config["bridge"].update(adb="unused", serial="station.invalid:5555", mode=mode)
    return config

class ProtocolTests(unittest.TestCase):
    def test_recorded_station_final_asr(self):
        raw = bytes.fromhex((ROOT / 'final-asr.hex').read_text().strip())
        state = alice_state(raw)
        self.assertEqual(first(child(state, 15), 2), 1)
        self.assertEqual(text(child(state, 15), 1), 'Сколько будет 2 + 2')

    def test_fragmented_frame_survives_timeout(self):
        left, right = socket.socketpair()
        try:
            right.settimeout(0.02)
            reader = FrameReader(right)
            payload = b'protobuf payload'
            encoded = frame(payload)
            left.sendall(encoded[:6])
            with self.assertRaises(socket.timeout):
                reader.receive()
            left.sendall(encoded[6:] + frame(b'next'))
            self.assertEqual(reader.receive(), payload)
            self.assertEqual(reader.receive(), b'next')
        finally:
            left.close()
            right.close()

    def test_invalid_protobuf_rejected(self):
        for raw in (b'\x00', b'\x0a\xff', b'\x0a\x03x', b'\x08' + b'\xff' * 11):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                fields(raw)

    def test_speech_has_no_cli_newlines_or_thinking(self):
        result = speech_text('<think>private</think> **Привет**\nx\nq\nМир!')
        self.assertEqual(result, 'Привет x q Мир!')
        self.assertNotIn('\n', result)

    def test_same_native_request_processed_once(self):
        class Args:
            adb = 'unused'
            serial = 'unused'
            ollama_url = 'http://127.0.0.1:11434'
            model = 'qwen3.5:9b-q4_K_M'
            mode = 'all'
        bridge = Bridge(test_config(Args.mode))
        self.assertTrue(bridge.enqueue('Сколько будет 2 + 2', 'same-id', cancel=False))
        self.assertFalse(bridge.enqueue('Сколько будет 2 + 2', 'same-id', cancel=False))
        self.assertEqual(bridge.state['requests'], 1)
        self.assertEqual(bridge.jobs.qsize(), 1)

    def test_stop_discards_pending_generation(self):
        class Args:
            adb = 'unused'
            serial = 'unused'
            ollama_url = 'http://127.0.0.1:11434'
            model = 'qwen3.5:9b-q4_K_M'
            mode = 'all'
        bridge = Bridge(test_config(Args.mode))
        bridge.enqueue('Расскажи историю', 'question', cancel=False)
        generation = bridge.generation
        self.assertFalse(bridge.enqueue('Хватит', 'stop', cancel=False))
        self.assertGreater(bridge.generation, generation)
        self.assertFalse(bridge.state['busy'])

class AutoRoutingTests(unittest.TestCase):
    def make_bridge(self):
        class Args:
            adb = 'unused'
            serial = 'unused'
            ollama_url = 'http://127.0.0.1:11434'
            model = 'qwen3.5:9b-q4_K_M'
            mode = 'auto'
        class Native:
            cancellations = 0
            def cancel(self):
                self.cancellations += 1
        result = Bridge(test_config(Args.mode))
        result.native = Native()
        return result

    def native_response(self, request_id):
        value = json.loads((ROOT / 'native-actions.json').read_text(encoding='utf-8'))[0]
        value['header']['request_id'] = request_id
        return value

    def test_zero_volume_warns_without_changing_speaker_controls(self):
        bridge = self.make_bridge()
        bridge.native = Mock()
        bridge.native.audio_status.return_value = {'volume_raw': 0, 'muted': False}
        bridge.refresh_audio_status()
        self.assertEqual(bridge.state['audio_warning'], 'speaker_volume_zero')
        self.assertEqual(bridge.native.mock_calls, [unittest.mock.call.audio_status()])
        bridge.native.audio_status.return_value = {'volume_raw': 8, 'muted': False}
        bridge.refresh_audio_status()
        self.assertEqual(bridge.state['audio_warning'], '')

    def test_muted_speaker_is_reported_separately(self):
        bridge = self.make_bridge()
        bridge.native = Mock()
        bridge.native.audio_status.return_value = {'volume_raw': 8, 'muted': True}
        bridge.refresh_audio_status()
        self.assertEqual(bridge.state['audio_warning'], 'speaker_muted')
        self.assertTrue(bridge.state['speaker_muted'])

    def test_vendor_volume_index_and_mute_flag_are_parsed_without_percent_assumptions(self):
        native = NativeCLI('unused', 'unused')
        with patch('alice_bridge.native.subprocess.run') as run:
            for raw, volume, muted in ((b'00', 0, False), (b'80', 8, False),
                                       (b'201', 20, True)):
                run.return_value = subprocess.CompletedProcess([], 0, raw, b'')
                self.assertEqual(native.audio_status(), {'volume_raw': volume, 'muted': muted})
            run.return_value = subprocess.CompletedProcess([], 1, b'0', b'missing file')
            with self.assertRaises(RuntimeError):
                native.audio_status()

    def test_custom_scene_uses_observed_actions_without_cancelling_or_replaying(self):
        bridge = self.make_bridge()
        bridge.on_recognition('Я ухожу', 'scene')
        self.assertEqual(bridge.state['last_route'], 'waiting')
        self.assertEqual(bridge.native.cancellations, 0)
        bridge.on_response(self.native_response('scene'))
        self.assertEqual(bridge.state['last_route'], 'native')
        self.assertEqual(bridge.state['native_requests'], 1)
        self.assertEqual(bridge.native.cancellations, 0)
        self.assertTrue(bridge.jobs.empty())

    def test_unknown_action_is_preserved_without_any_phrase_rule(self):
        bridge = self.make_bridge()
        bridge.on_recognition('Любое новое название из приложения', 'new-scene')
        bridge.on_response({'header': {'request_id': 'new-scene'}, 'response': {
            'directives': [{'name': 'future_smart_home_action_987'}]}})
        self.assertEqual(bridge.state['last_route'], 'native')
        self.assertEqual(bridge.native.cancellations, 0)

    def test_recorded_app_scene_child_id_uses_originating_voice_id(self):
        bridge = self.make_bridge()
        fixture = json.loads((ROOT / 'native-scene.json').read_text(encoding='utf-8'))
        voice_id = fixture['voice_request_id']
        bridge.on_recognition('Произвольное название сценария', voice_id)
        bridge.on_response(fixture['response'], voice_id)
        self.assertEqual(bridge.state['last_route'], 'native')
        self.assertEqual(bridge.state['last_route_reason'], 'native_linked_scenario_request')
        self.assertEqual(bridge.native.cancellations, 0)
        self.assertTrue(bridge.jobs.empty())

    def test_speech_only_child_scene_is_not_sent_to_model(self):
        bridge = self.make_bridge()
        bridge.on_recognition('Сценарий с голосовым ответом', 'voice')
        bridge.on_response({'header': {'request_id': 'child'}, 'response': {
            'directives': [{'name': 'tts_play_placeholder'}]}}, 'voice')
        self.assertEqual(bridge.state['last_route'], 'native')
        self.assertEqual(bridge.state['requests'], 0)

    def test_cached_speech_does_not_claim_a_new_voice_turn(self):
        bridge = self.make_bridge()
        old = {'header': {'request_id': 'old-speech'}, 'response': {
            'directives': [{'name': 'tts_play_placeholder'}]}}
        bridge.on_response(old, 'old-voice')
        bridge.on_recognition('Новый вопрос', 'new-voice')
        bridge.on_response(old, 'new-voice')
        self.assertEqual(bridge.state['last_route'], 'waiting')
        self.assertIn('new-voice', bridge.pending)

    def test_speech_only_response_goes_to_model_once(self):
        bridge = self.make_bridge()
        bridge.on_recognition('Придумай имя роботу', 'chat')
        response = {'header': {'request_id': 'chat'}, 'response': {
            'directives': [{'name': 'tts_play_placeholder'}], 'is_streaming': True}}
        bridge.on_response(response)
        bridge.on_response(response)
        self.assertEqual(bridge.state['last_route'], 'agent')
        self.assertEqual(bridge.state['requests'], 1)
        self.assertEqual(bridge.native.cancellations, 1)
        self.assertEqual(bridge.jobs.qsize(), 1)

    def test_explicit_agent_prefix_overrides_native_routing(self):
        bridge = self.make_bridge()
        bridge.on_recognition('Квен, объясни как включить свет', 'explicit')
        self.assertEqual(bridge.state['last_route'], 'agent')
        self.assertEqual(bridge.state['last_question'], 'объясни как включить свет')
        self.assertEqual(bridge.native.cancellations, 1)

    def test_old_response_cannot_answer_a_new_turn(self):
        bridge = self.make_bridge()
        bridge.on_recognition('Первый вопрос', 'old')
        bridge.on_recognition('Второй вопрос', 'new')
        bridge.on_response({'header': {'request_id': 'old'}, 'response': {'directives': []}})
        self.assertEqual(bridge.state['requests'], 0)
        self.assertEqual(bridge.state['last_route'], 'waiting')

    def test_local_agent_request_cannot_be_overwritten_by_an_older_native_response(self):
        bridge = self.make_bridge()
        bridge.on_recognition('Первый вопрос', 'old-voice')
        bridge.enqueue('Вопрос с компьютера', 'new-local')
        request_id = bridge.state['current_request_id']
        bridge.on_response({'header': {'request_id': 'old-voice'}, 'response': {'directives': []}})
        self.assertEqual(bridge.state['current_request_id'], request_id)
        self.assertEqual(bridge.state['requests'], 1)

    def test_native_speech_request_does_not_feed_back_to_model(self):
        bridge = self.make_bridge()
        bridge.on_response({'header': {'request_id': 'synthetic-speech'}, 'response': {
            'directives': [{'name': 'tts_play_placeholder'}]}})
        self.assertEqual(bridge.state['requests'], 0)
        self.assertTrue(bridge.jobs.empty())

if __name__ == '__main__':
    unittest.main()
