"""Private JSON configuration with portable public defaults."""
from copy import deepcopy
import ipaddress
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = ROOT / '.local' / 'config.json'
DEFAULTS = {
    'bridge': {
        'adb': 'adb', 'serial': '', 'mode': 'auto',
        'prefixes': ['агент', 'квен', 'qwen', 'нейро', 'нейрон'],
        'ipc_port': 19875, 'host': '127.0.0.1', 'port': 19876,
        'token_env': 'ALICE_BRIDGE_TOKEN', 'request_timeout_s': 180,
    },
    'module': 'external',
    'ollama': {
        'url': 'http://127.0.0.1:11434', 'model': '', 'keep_alive': -1,
        'num_ctx': 4096, 'num_predict': 256, 'temperature': 0.5,
    },
}

def defaults():
    return deepcopy(DEFAULTS)

def load(path=DEFAULT_CONFIG):
    config = defaults()
    try:
        supplied = json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except FileNotFoundError:
        raise ValueError('Copy config/config.example.json to .local/config.json and set bridge.serial') from None
    if not isinstance(supplied, dict):
        raise ValueError('Configuration must be a JSON object')
    for key, value in supplied.items():
        if key not in config:
            raise ValueError(f'Unknown configuration section: {key}')
        if isinstance(config[key], dict):
            if not isinstance(value, dict) or value.keys() - config[key].keys():
                raise ValueError(f'Invalid configuration section: {key}')
            config[key].update(value)
        else:
            config[key] = value
    bridge = config['bridge']
    if not isinstance(bridge['serial'], str) or not bridge['serial'].strip():
        raise ValueError('Set bridge.serial to the Station ADB address')
    if bridge['mode'] not in ('auto', 'all', 'prefix'):
        raise ValueError('bridge.mode must be auto, all or prefix')
    if not isinstance(bridge['prefixes'], list) or not bridge['prefixes'] or not all(
            isinstance(p, str) and p.strip() for p in bridge['prefixes']):
        raise ValueError('bridge.prefixes must contain nonempty strings')
    for key in ('port', 'ipc_port'):
        if type(bridge[key]) is not int or not 1 <= bridge[key] <= 65535:
            raise ValueError(f'Invalid bridge.{key}')
    if not isinstance(bridge['request_timeout_s'], (int, float)) or not 1 <= bridge['request_timeout_s'] <= 3600:
        raise ValueError('bridge.request_timeout_s must be between 1 and 3600')
    try:
        is_loopback = ipaddress.ip_address(bridge['host']).is_loopback
    except ValueError:
        raise ValueError('bridge.host must be an IP address') from None
    if not is_loopback and not token(config):
        raise ValueError('Set the environment variable bridge.token_env before binding outside loopback')
    if config['module'] not in ('external', 'ollama'):
        raise ValueError('module must be external or ollama')
    return config

def token(config):
    name = config['bridge']['token_env']
    if not isinstance(name, str):
        raise ValueError('bridge.token_env must be an environment variable name')
    return os.environ.get(name, '') if name else ''

def api_url(config):
    host = config['bridge']['host']
    if host in ('0.0.0.0', '::'):
        host = '127.0.0.1' if host == '0.0.0.0' else '::1'
    if ':' in host:
        host = f'[{host}]'
    return f'http://{host}:{config["bridge"]["port"]}'
