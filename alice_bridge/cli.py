"""CLI and optional process supervisor. The core runs without an agent module."""
import argparse
import json
import logging
import logging.handlers
import subprocess
import sys
import threading
import urllib.request
from .config import DEFAULT_CONFIG, ROOT, api_url, load, token
from .core import Bridge
from .http_api import create_server

def serve(config, config_path, module='external'):
    (ROOT / 'logs').mkdir(exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(ROOT / 'logs' / 'bridge.log',
        maxBytes=1_000_000, backupCount=1, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler, logging.StreamHandler()],
        format='%(asctime)s %(levelname)s %(message)s')
    bridge = Bridge(config)
    server = create_server(bridge)
    worker = None
    try:
        threading.Thread(target=bridge.observe, daemon=True, name='native-events').start()
        logging.info('Bridge API: %s; module=%s', api_url(config), module)
        if module != 'external':
            # Launching a worker is optional; core and worker communicate only by HTTP.
            worker = subprocess.Popen([sys.executable, '-m', 'modules.' + module,
                '--config', str(config_path)], cwd=ROOT,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.close()
        server.server_close()
        if worker and worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait(timeout=2)

def main():
    parser = argparse.ArgumentParser(description='Native Alice speech <-> any HTTP agent')
    parser.add_argument('command', choices=('run', 'up', 'status', 'ask', 'speak', 'stop'))
    parser.add_argument('text', nargs='?')
    parser.add_argument('--config', default=str(DEFAULT_CONFIG))
    parser.add_argument('--module', choices=('external', 'ollama'))
    args = parser.parse_args()
    try:
        config = load(args.config)
        if args.command in ('run', 'up'):
            module = args.module or (config['module'] if args.command == 'up' else 'external')
            serve(config, args.config, module)
            return
        if args.command in ('ask', 'speak') and not args.text:
            parser.error('text is required')
        headers = {'Content-Type': 'application/json'}
        if token(config):
            headers['Authorization'] = 'Bearer ' + token(config)
        body = None if args.command == 'status' else json.dumps({'text': args.text}).encode('utf-8')
        path = '/v1/health' if args.command == 'status' else '/v1/' + args.command
        request = urllib.request.Request(api_url(config) + path, data=body, headers=headers)
        http = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with http.open(request, timeout=10) as response:
            print(json.dumps(json.load(response), ensure_ascii=True, indent=2))
    except (ValueError, OSError) as error:
        parser.exit(1, f'{error}\n')
