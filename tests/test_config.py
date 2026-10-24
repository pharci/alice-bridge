import json
from pathlib import Path
import unittest
from unittest.mock import patch
from alice_bridge.config import load

class ConfigTests(unittest.TestCase):
    def load_value(self, value):
        with patch('alice_bridge.config.Path.read_text', return_value=json.dumps(value)):
            return load('test-config.json')

    def test_private_config_overrides_defaults(self):
        config = self.load_value({'bridge': {'serial': '192.0.2.10:5555'}})
        self.assertEqual(config['bridge']['serial'], '192.0.2.10:5555')
        self.assertEqual(config['module'], 'external')
        self.assertEqual(config['bridge']['port'], 19876)

    def test_missing_station_address_is_rejected(self):
        with self.assertRaises(ValueError):
            self.load_value({})

    def test_typo_in_config_is_not_silently_ignored(self):
        with self.assertRaises(ValueError):
            self.load_value({'bridge': {'serial': '192.0.2.10:5555', 'modde': 'auto'}})

    def test_network_binding_requires_a_secret(self):
        config = {'bridge': {'serial': '192.0.2.10:5555', 'host': '0.0.0.0',
                             'token_env': 'TEST_BRIDGE_TOKEN'}}
        with patch.dict('os.environ', {'TEST_BRIDGE_TOKEN': ''}):
            with self.assertRaises(ValueError):
                self.load_value(config)
        with patch.dict('os.environ', {'TEST_BRIDGE_TOKEN': 'test-only-secret'}):
            self.assertEqual(self.load_value(config)['bridge']['host'], '0.0.0.0')

    def test_public_template_is_valid_and_does_not_require_a_model(self):
        config = load(Path(__file__).resolve().parents[1] / 'config' / 'config.example.json')
        self.assertEqual(config['module'], 'external')
