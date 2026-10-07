"""Model transport boundaries, using local HTTP mock responses only."""
import json
import unittest
from unittest.mock import patch

import httpx

from .investigation_agent import investigate
from .investigation_model import model_session


class ModelTransportTests(unittest.TestCase):
    def test_request_contract_and_session_cleanup(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={'choices': []})

        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('backend.investigation_model.httpx.Client', return_value=client) as factory:
            with patch('backend.investigation_model.time.monotonic', return_value=0):
                with model_session({'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'fixture',
                                    'SUPPLY_MODEL_BASE_URL': 'https://model.test/'}) as request:
                    self.assertEqual(request({'messages': [], 'stream': False}, 0, 60), {'choices': []})
        factory.assert_called_once_with(timeout=15, follow_redirects=False)
        self.assertTrue(client.is_closed)
        self.assertEqual(str(requests[0].url), 'https://model.test/chat/completions')
        self.assertEqual(requests[0].headers['Authorization'], 'Bearer fixture')
        self.assertEqual(json.loads(requests[0].content), {'model': 'fixture', 'messages': [], 'stream': False})

    def test_response_byte_and_time_limits_close_without_retry(self):
        for content, clock in [(b'x' * 100001, [0, 0]), (b'{}', [0, 61])]:
            with self.subTest(size=len(content)):
                requests = []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, content=content)

                client = httpx.Client(transport=httpx.MockTransport(handler))
                with patch('backend.investigation_model.httpx.Client', return_value=client):
                    with patch('backend.investigation_model.time.monotonic', side_effect=clock):
                        with self.assertRaisesRegex(ValueError, 'response budget'):
                            with model_session({'SUPPLY_MODEL': 'fixture', 'DEEPSEEK_API_KEY': 'fixture'}) as request:
                                request({}, 0, 60)
                self.assertEqual(len(requests), 1)
                self.assertTrue(client.is_closed)

    def test_invalid_config_does_not_create_http_client(self):
        for base in ('http://model.test', 'https://user:secret@model.test', 'https://model.test?secret=x'):
            with self.subTest(base=base), patch('backend.investigation_model.httpx.Client') as client:
                report = investigate({'target': None, 'missing': [], 'candidates': []}, [], {
                    'SUPPLY_INVESTIGATOR': 'model', 'SUPPLY_MODEL': 'fixture',
                    'SUPPLY_MODEL_API_KEY': 'fixture', 'SUPPLY_MODEL_BASE_URL': base})
                client.assert_not_called()
                self.assertEqual(report['modelCalls'], 0)
                self.assertTrue(report['manualRequired'])
                self.assertNotIn('secret', str(report))
