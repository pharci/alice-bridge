"""Bounded JSON HTTP API used equally by bundled and third-party agents."""
import hmac
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from .config import token
from .core import RequestError

MAX_BODY = 65536

def create_server(bridge, host=None, port=None):
    settings = bridge.config['bridge']
    secret = token(bridge.config)

    class HTTP(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(35)

        def log_message(self, *_):
            pass

        def reply(self, status, body=None):
            data = json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else b''
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if data:
                self.wfile.write(data)

        def authorized(self):
            if self.headers.get('Origin'):
                self.reply(403, {'error': 'non-browser clients only'})
                return False
            if secret and not hmac.compare_digest(
                    self.headers.get('Authorization', '').encode('utf-8'), ('Bearer ' + secret).encode('utf-8')):
                self.reply(401, {'error': 'valid bearer token required'})
                return False
            return True

        def do_GET(self):
            if not self.authorized():
                return
            path = urlsplit(self.path)
            try:
                if path.path in ('/health', '/v1/health'):
                    self.reply(200, bridge.status())
                elif path.path == '/v1/requests':
                    timeout = int(parse_qs(path.query).get('timeout', ['25'])[0])
                    if not 0 <= timeout <= 25:
                        raise ValueError('timeout must be between 0 and 25 seconds')
                    request = bridge.claim(timeout)
                    self.reply(200 if request else 204, request)
                else:
                    self.reply(404, {'error': 'not found'})
            except (ValueError, TypeError, UnicodeError) as error:
                self.reply(400, {'error': str(error)})

        def do_POST(self):
            if not self.authorized():
                return
            if self.headers.get_content_type() != 'application/json':
                self.reply(415, {'error': 'application/json required'})
                return
            try:
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= MAX_BODY:
                    raise RequestError(413, 'body must be between 1 and 65536 bytes')
                body = json.loads(self.rfile.read(size))
                if not isinstance(body, dict):
                    raise ValueError('JSON object required')
                path = urlsplit(self.path).path
                if path == '/v1/responses':
                    self.reply(200, bridge.deliver(body.get('request_id'), body.get('text')))
                elif path == '/v1/errors':
                    bridge.fail(body.get('request_id'), body.get('error'))
                    self.reply(200, {'accepted': True})
                elif path in ('/v1/speak', '/speak'):
                    bridge.speak(body.get('text'))
                    self.reply(200, {'submitted': True})
                elif path in ('/v1/ask', '/ask'):
                    value = body.get('text')
                    if not isinstance(value, str) or not value.strip():
                        raise ValueError('text is required')
                    accepted = bridge.enqueue(value, 'local-' + str(time.time_ns()))
                    self.reply(202, {'accepted': accepted,
                        'request_id': bridge.status()['current_request_id'] if accepted else None})
                elif path == '/v1/module':
                    name = body.get('name')
                    if not isinstance(name, str) or len(name) > 100 or type(body.get('ready')) is not bool:
                        raise ValueError('name and ready are required')
                    with bridge.lock:
                        bridge.state.update(module=name, module_ready=body['ready'])
                    self.reply(200, {'accepted': True})
                elif path in ('/v1/stop', '/stop'):
                    self.reply(200, {'stopping': True})
                    threading.Thread(target=lambda: (bridge.close(), server.shutdown()), daemon=True).start()
                else:
                    self.reply(404, {'error': 'not found'})
            except RequestError as error:
                self.reply(error.status, {'error': str(error)})
            except (ValueError, TypeError, UnicodeError) as error:
                self.reply(400, {'error': str(error)})
            except Exception:
                self.reply(503, {'error': 'native transport unavailable'})

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        if ':' in (host or settings['host']):
            address_family = socket.AF_INET6

    server = Server((host if host is not None else settings['host'],
                     port if port is not None else settings['port']), HTTP)
    return server
