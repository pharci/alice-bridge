"""Optional Ollama worker connected to the public bridge HTTP API."""
from __future__ import annotations
import json
import argparse
import logging
import threading
import urllib.request
from alice_bridge.config import DEFAULT_CONFIG, api_url, load, token
from alice_bridge.speech import speech_text
from .client import APIError, BridgeClient

SYSTEM_PROMPT = (
    'Ты локальный голосовой помощник Квен, работающий на ПК через Ollama. '
    'Отвечай по-русски кратко и естественно, '
    'обычно одним-тремя предложениями. Ответ будет произнесён вслух. '
    'Не используй Markdown, списки, эмодзи, теги и рассуждения вслух. '
    'Если не знаешь ответ, скажи об этом. У тебя нет доступа к инструментам или устройствам.'
)

class Ollama:
    def __init__(self, url: str, model: str, keep_alive=-1,
                 num_ctx=4096, num_predict=256, temperature=0.5):
        self.url = url.rstrip('/')
        self.model = model
        self.keep_alive = int(keep_alive) if str(keep_alive) == '-1' else keep_alive
        self.history: list[dict] = []
        self.options = {'num_ctx': num_ctx, 'num_predict': num_predict, 'temperature': temperature}
        # Loopback requests must not be sent to a configured HTTP proxy.
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def preload(self):
        request = urllib.request.Request(self.url + '/api/chat',
            data=json.dumps({'model': self.model, 'messages': [],
                             'stream': False, 'keep_alive': self.keep_alive}).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with self.http.open(request, timeout=60) as response:
            result = json.load(response)
        if result.get('error'):
            raise RuntimeError(result['error'])

    def answer(self, question: str) -> tuple[str, dict]:
        messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
        messages += self.history[-8:] + [{'role': 'user', 'content': question}]
        payload = {'model': self.model, 'messages': messages, 'stream': False,
                   'think': False, 'keep_alive': self.keep_alive,
                   'options': self.options}
        request = urllib.request.Request(self.url + '/api/chat',
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'}, method='POST')
        with self.http.open(request, timeout=60) as response:
            result = json.load(response)
        if result.get('error'):
            raise RuntimeError(result['error'])
        answer = speech_text(result.get('message', {}).get('content', ''))
        if not answer:
            raise RuntimeError('Ollama returned an empty spoken answer')
        return answer, {'eval_count': result.get('eval_count'),
                        'done_reason': result.get('done_reason'),
                        'load_s': round(result.get('load_duration', 0) / 1e9, 3),
                        'generation_s': round(result.get('eval_duration', 0) / 1e9, 3)}

    def commit(self, question: str, answer: str):
        self.history.extend([{'role': 'user', 'content': question},
                             {'role': 'assistant', 'content': answer}])
        self.history = self.history[-8:]

def run(config, stop=None, bridge_url=None):
    stop = stop or threading.Event()
    client = BridgeClient(bridge_url or api_url(config), token(config))
    settings = config['ollama']
    if not settings['model']:
        raise ValueError('Set ollama.model in the private config')
    model = Ollama(**settings)
    ready = False
    log = logging.getLogger('module.ollama')
    try:
        while not stop.is_set():
            request = None
            try:
                if not ready:
                    log.info('Preloading Ollama model')
                    model.preload()
                    ready = True
                client.register('ollama', True)
                request = client.poll()
                if not request or stop.is_set():
                    continue
                question = request['text']
                if question.lower().strip('.!?') in ('новый диалог', 'забудь разговор', 'очисти историю'):
                    model.history.clear()
                    answer, metadata = 'Начинаем новый разговор.', {}
                else:
                    answer, metadata = model.answer(question)
                if stop.is_set():
                    break
                client.respond(request['request_id'], answer)
                model.commit(question, answer)
                log.info('Answer submitted; load=%s s, generation=%s s',
                         metadata.get('load_s'), metadata.get('generation_s'))
            except APIError as error:
                if error.status == 409:
                    log.info('Discarded superseded answer')
                else:
                    log.warning('Bridge unavailable: HTTP %d', error.status)
                    stop.wait(2)
            except Exception as error:
                # Provider bodies or URLs can contain credentials; do not log them.
                log.warning('Module request failed: %s', type(error).__name__)
                ready = False
                try:
                    client.register('ollama', False)
                    if request:
                        client.fail(request['request_id'], 'ollama_request_failed')
                except Exception:
                    pass
                stop.wait(3)
    finally:
        try:
            client.register('ollama', False)
        except Exception:
            pass

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--bridge-url')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    try:
        run(load(args.config), bridge_url=args.bridge_url)
    except KeyboardInterrupt:
        pass

if __name__ == '__main__':
    main()

