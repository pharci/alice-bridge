"""Native speech routing and agent request broker. No inference dependencies."""
from __future__ import annotations
import collections
import logging
import queue
import re
import socket
import subprocess
import threading
import time
import uuid
import json
from types import SimpleNamespace
from .config import ROOT
from .native import NativeCLI
from .speech import speech_text
from .protocol import FrameReader, action_directives, alice_state, child, first, native_response, text

LOG = logging.getLogger('alice-bridge')

class RequestError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)

NATIVE_CONTROLS = re.compile(
    r'^(?:стоп|хватит|отмена|замолчи|пауза|тише|громче|'
    r'(?:сделай |поставь |установи )?громкость(?:\s.*)?|выключи(?:сь)?|'
    r'выключи (?:музыку|звук)|продолжи музыку)[.!?]*$', re.I)

class Bridge:
    def __init__(self, config, native=None):
        self.config = config
        self.args = SimpleNamespace(**config['bridge'])
        self.native = native or NativeCLI(self.args.adb, self.args.serial)
        self.stop = threading.Event()
        self.lock = threading.RLock()
        self.jobs = queue.Queue(maxsize=1)
        self.seen = collections.OrderedDict()
        self.pending = {}
        self.seen_responses = collections.OrderedDict()
        self.tasks = collections.OrderedDict()
        self.generation = 0
        self.prefix = re.compile(r'^(?:' + '|'.join(re.escape(p) for p in self.args.prefixes)
                                 + r')[, :—-]+(.+)$', re.I)
        self.state = {'connected': False, 'serial': self.args.serial,
                      'mode': self.args.mode, 'requests': 0, 'answers': 0,
                      'last_question': '', 'last_answer': '', 'last_native_text': '',
                      'last_error': '', 'last_latency_s': None, 'busy': False,
                      'native_requests': 0, 'last_route': '', 'last_route_reason': '',
                      'last_routed_text': '', 'project_dir': str(ROOT),
                      'speaker_volume_raw': None, 'speaker_muted': None,
                      'audio_warning': '', 'last_speech_status': '',
                      'current_request_id': None, 'module': None, 'module_ready': False}

    def refresh_audio_status(self):
        try:
            audio = self.native.audio_status()
        except Exception as error:
            with self.lock:
                self.state.update(speaker_volume_raw=None, speaker_muted=None,
                                  audio_warning='speaker_controls_unavailable')
            LOG.warning('Cannot check speaker controls: %s', error)
            return
        warning = ('speaker_muted' if audio['muted'] else
                   'speaker_volume_zero' if audio['volume_raw'] == 0 else '')
        with self.lock:
            self.state.update(speaker_volume_raw=audio['volume_raw'],
                              speaker_muted=audio['muted'], audio_warning=warning)
        if warning:
            LOG.warning('%s: text delivery does not mean audible playback; '
                        'volume is left unchanged', warning)

    def adb(self, *parts):
        result = subprocess.run([self.args.adb, '-s', self.args.serial, *parts],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace').strip())
        return result.stdout.decode('utf-8', errors='replace').strip()

    def remember(self, request_id: str) -> bool:
        if request_id in self.seen:
            return False
        self.seen[request_id] = None
        while len(self.seen) > 256:
            self.seen.popitem(last=False)
        return True

    def keep_native(self, question: str, reason: str):
        """Leave the original request alone. Never replay physical actions."""
        with self.lock:
            self.generation += 1
            self.state.update(busy=False, last_route='native',
                last_route_reason=reason, last_routed_text=question)
            self.state['native_requests'] += 1
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                pass
        LOG.info('Native route: %s', reason)

    def on_recognition(self, question: str, request_id: str):
        if self.args.mode != 'auto':
            return self.enqueue(question, request_id)
        question = ' '.join(question.split()).strip()
        with self.lock:
            if not question or not self.remember(request_id):
                return False
            # An older turn must not speak over a new native request.
            self.generation += 1
            self.pending.clear()
            self.state.update(busy=False, last_route='waiting',
                last_route_reason='waiting_for_native_directives', last_routed_text=question)
        if NATIVE_CONTROLS.fullmatch(question):
            self.keep_native(question, 'native_control')
            return False
        explicit = self.prefix.match(question)
        if explicit:
            return self.enqueue(explicit.group(1), request_id, remember=False)
        with self.lock:
            self.pending[request_id] = question
        return True

    def on_response(self, response: dict, origin_request_id: str = ''):
        if self.args.mode != 'auto':
            return
        header = response.get('header', {})
        request_id = header.get('request_id') if isinstance(header, dict) else None
        fingerprint = json.dumps(response, ensure_ascii=True, sort_keys=True)
        with self.lock:
            if fingerprint in self.seen_responses:
                return
            self.seen_responses[fingerprint] = None
            while len(self.seen_responses) > 256:
                self.seen_responses.popitem(last=False)
            question = self.pending.pop(request_id, None)
            linked = False
            if question is None and origin_request_id and request_id != origin_request_id:
                # App scenarios can create a child request ID. AliceState keeps
                # the originating voice ID. Preserve these native child turns.
                question = self.pending.pop(origin_request_id, None)
                linked = question is not None
        if question is None:
            return
        actions = action_directives(response)
        if linked:
            self.keep_native(question, 'native_linked_scenario_request')
        elif actions:
            self.keep_native(question, 'native_actions:' + ','.join(actions))
        else:
            self.enqueue(question, request_id, remember=False)

    def enqueue(self, question: str, request_id: str, cancel: bool = True,
                remember: bool = True):
        question = ' '.join(question.split()).strip()
        if not question:
            return False
        with self.lock:
            if remember and not self.remember(request_id):
                return False
            if NATIVE_CONTROLS.fullmatch(question):
                # Preserve stop/volume controls and discard a pending model answer.
                self.generation += 1
                self.state['busy'] = False
                self.pending.clear()
                return False
            if self.args.mode == 'prefix':
                match = self.prefix.match(question)
                if not match:
                    self.keep_native(question, 'no_agent_prefix')
                    return False
                question = match.group(1)
            self.generation += 1
            generation = self.generation
            self.pending.clear()
            self.state.update(last_question=question, last_error='', busy=True,
                last_route='agent', last_route_reason='speech_only_or_explicit_agent',
                last_routed_text=question)
            self.state['requests'] += 1
        if cancel:
            try:
                self.native.cancel()
            except Exception:
                with self.lock:
                    if generation == self.generation:
                        self.state.update(busy=False, last_error='native_cancel_failed')
                raise
        with self.lock:
            if generation != self.generation:
                return False
            try:
                self.jobs.get_nowait()
            except queue.Empty:
                pass
            task_id = uuid.uuid4().hex
            task = {'request_id': task_id, 'text': question,
                    'created_at': time.time(), 'timeout_s': self.args.request_timeout_s,
                    '_generation': generation, '_started': time.monotonic(),
                    '_status': 'queued'}
            self.tasks[task_id] = task
            while len(self.tasks) > 256:
                self.tasks.popitem(last=False)
            self.state['current_request_id'] = task_id
            self.jobs.put_nowait(task_id)
        LOG.info('Accepted text request (%d characters)', len(question))
        return True

    def _current(self, task):
        return (task['_generation'] == self.generation and not self.stop.is_set()
                and time.monotonic() - task['_started'] <= self.args.request_timeout_s)

    def claim(self, timeout=25):
        """Long poll: one consumer receives each request, never cached ASR."""
        deadline = time.monotonic() + timeout
        while not self.stop.is_set():
            try:
                task_id = self.jobs.get(timeout=max(0, min(0.25, deadline - time.monotonic())))
            except queue.Empty:
                if time.monotonic() >= deadline:
                    return None
                continue
            with self.lock:
                task = self.tasks.get(task_id)
                if task and self._current(task) and task['_status'] == 'queued':
                    task['_status'] = 'claimed'
                    return {k: v for k, v in task.items() if not k.startswith('_')}
            if time.monotonic() >= deadline:
                return None
        return None

    def deliver(self, request_id, value):
        """Accept one answer for the current turn; reject late/cancelled replies."""
        if not isinstance(request_id, str) or not isinstance(value, str):
            raise RequestError(400, 'request_id and text must be strings')
        answer = speech_text(value)
        if not answer:
            raise RequestError(400, 'text must contain spoken content')
        with self.lock:
            task = self.tasks.get(request_id)
            if not task:
                raise RequestError(404, 'unknown request_id')
            # A lost HTTP acknowledgement can be retried without repeating sound.
            if task['_status'] == 'submitted' and task.get('_answer') == answer:
                return {'submitted': True, 'duplicate': True}
            if not self._current(task) or task['_status'] != 'claimed':
                raise RequestError(409, 'request is stale, expired or already answered')
            self.refresh_audio_status()
            self.native.speak(answer)
            task.update(_status='submitted', _answer=answer)
            self.state.update(last_answer=answer, busy=False, last_error='',
                last_latency_s=round(time.monotonic() - task['_started'], 2),
                last_speech_status='submitted_to_native_engine')
            self.state['answers'] += 1
        LOG.info('Submitted agent answer to native speech (audible playback is not verified)')
        return {'submitted': True, 'duplicate': False}

    def fail(self, request_id, message):
        if not isinstance(request_id, str) or not isinstance(message, str):
            raise RequestError(400, 'request_id and error must be strings')
        with self.lock:
            task = self.tasks.get(request_id)
            if not task:
                raise RequestError(404, 'unknown request_id')
            if not self._current(task) or task['_status'] != 'claimed':
                raise RequestError(409, 'request is no longer active')
            task['_status'] = 'failed'
            self.state.update(busy=False, last_error=message[:300])
        # Never announce failures unexpectedly through the speaker.
        LOG.warning('Agent reported a failed request')

    def speak(self, value):
        if not isinstance(value, str) or not speech_text(value):
            raise RequestError(400, 'text must contain spoken content')
        with self.lock:
            self.refresh_audio_status()
            self.native.speak(value)
            self.state['last_speech_status'] = 'submitted_to_native_engine'

    def observe(self):
        while not self.stop.is_set():
            try:
                self.adb('forward', f'tcp:{self.args.ipc_port}', 'tcp:9875')
                self.native.start()
                self.refresh_audio_status()
                with socket.create_connection(('127.0.0.1', self.args.ipc_port), timeout=4) as sock:
                    sock.settimeout(1)
                    reader = FrameReader(sock)
                    initial = True
                    with self.lock:
                        self.state.update(connected=True, last_error='')
                    LOG.info('Connected to native Alice text events')
                    while not self.stop.is_set():
                        try:
                            payload = reader.receive()
                        except socket.timeout:
                            continue
                        current = alice_state(payload)
                        if not current:
                            continue
                        request = child(current, 15)
                        phrase = text(request, 1)
                        final = first(request, 2) == 1
                        request_id = text(current, 7)
                        if initial:
                            # A new subscriber receives cached state. Never replay it.
                            initial = False
                            if final and request_id:
                                with self.lock:
                                    self.remember(request_id)
                        elif final and phrase and request_id:
                            self.on_recognition(phrase, request_id)
                        response = native_response(current)
                        if response:
                            self.on_response(response, text(current, 7))
                        response_text = text(child(current, 14), 1)
                        if response_text:
                            with self.lock:
                                self.state['last_native_text'] = response_text
            except Exception as error:
                with self.lock:
                    self.state.update(connected=False, last_error=str(error))
                LOG.warning('Native connection interrupted: %s', error)
                with self.lock:
                    self.generation += 1
                    self.pending.clear()
                    self.state['busy'] = False
                self.native.close()
                self.stop.wait(3)

    def status(self):
        with self.lock:
            task = self.tasks.get(self.state['current_request_id'])
            if (task and task['_status'] in ('queued', 'claimed')
                    and task['_generation'] == self.generation and not self._current(task)):
                task['_status'] = 'expired'
                self.state.update(busy=False, last_error='agent_request_expired')
            return dict(self.state)

    def close(self):
        self.stop.set()
        with self.lock:
            self.generation += 1
        self.native.close()

