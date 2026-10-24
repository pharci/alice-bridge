"""Small stdlib client for any agent using the bridge's public HTTP API."""
import json
import urllib.error
import urllib.request

class APIError(RuntimeError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Bridge HTTP {status}')

class BridgeClient:
    def __init__(self, url='http://127.0.0.1:19876', token=''):
        self.url = url.rstrip('/')
        self.token = token
        self.http = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, path, body=None, timeout=35):
        headers = {'Content-Type': 'application/json'}
        if self.token:
            headers['Authorization'] = 'Bearer ' + self.token
        request = urllib.request.Request(self.url + path,
            data=json.dumps(body, ensure_ascii=False).encode('utf-8') if body is not None else None,
            headers=headers)
        try:
            with self.http.open(request, timeout=timeout) as response:
                return None if response.status == 204 else json.load(response)
        except urllib.error.HTTPError as error:
            status = error.code
            error.close()
            raise APIError(status) from None

    def poll(self, timeout=25):
        return self.request(f'/v1/requests?timeout={timeout}', timeout=timeout + 8)

    def respond(self, request_id, text):
        # Retrying a lost acknowledgement is safe: the core deduplicates replies.
        for attempt in range(2):
            try:
                return self.request('/v1/responses', {'request_id': request_id, 'text': text})
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt:
                    raise

    def fail(self, request_id, error):
        return self.request('/v1/errors', {'request_id': request_id, 'error': error}, timeout=4)

    def register(self, name, ready):
        return self.request('/v1/module', {'name': name, 'ready': ready}, timeout=4)
