"""Minimal custom agent. Run from the repository: python -m examples.agent."""
import os
from modules.client import APIError, BridgeClient

def answer(text):
    # Replace this function with your agent, HTTP API or model call.
    return 'Внешний агент получил ваш вопрос.'

def main():
    client = BridgeClient(os.environ.get('ALICE_BRIDGE_URL', 'http://127.0.0.1:19876'),
                          os.environ.get('ALICE_BRIDGE_TOKEN', ''))
    client.register('custom-agent', True)
    while True:
        request = client.poll()
        if request:
            try:
                client.respond(request['request_id'], answer(request['text']))
            except APIError as error:
                if error.status != 409:
                    raise

if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
