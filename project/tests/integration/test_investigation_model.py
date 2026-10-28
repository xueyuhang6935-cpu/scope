"""Model transport boundaries, using local HTTP mock responses only."""
import json
import unittest
from unittest.mock import patch

import httpx

from backend.investigation_agent import investigate
from backend.investigation_model import model_session


class ModelTransportTests(unittest.TestCase):
    def test_failure_diagnostics_classify_errors_without_exposing_payloads(self):
        env = {'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'SECRET-KEY',
               'SUPPLY_MODEL_BASE_URL': 'https://model.test'}
        cases = [(error('SECRET-KEY PRIVATE-BODY'), reason, 'sending', None) for error, reason in (
            (httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
            (httpx.WriteTimeout, 'write_timeout'), (httpx.PoolTimeout, 'pool_timeout'),
            (httpx.ConnectError, 'connect_error'), (httpx.RemoteProtocolError, 'remote_protocol_error'),
            (RuntimeError, 'unexpected_exception'))]
        for status in (429, 503):
            cases.append((httpx.Response(status, content=b'PRIVATE-BODY', headers={'x-request-id': 'req-123'}),
                          'http_status_error', 'checking_status', status))
        cases.append((httpx.Response(200, content=b'PRIVATE-BODY'), 'invalid_response_json', 'parsing_json', 200))
        for response, reason, stage, status in cases:
            with self.subTest(reason=reason, status=status):
                seen, diagnostics = [], []
                def handler(request):
                    seen.append(request)
                    if isinstance(response, Exception):
                        raise response
                    return response
                client = httpx.Client(transport=httpx.MockTransport(handler))
                with patch('backend.investigation_model.httpx.Client', return_value=client), \
                     patch('backend.investigation_model.time.monotonic', return_value=10):
                    with model_session(env, on_failure=diagnostics.append) as request:
                        with self.assertRaises(type(response) if isinstance(response, Exception) else
                                              httpx.HTTPStatusError if status != 200 else json.JSONDecodeError) as caught:
                            request({'messages': [{'content': 'PRIVATE-BODY'}]}, 0, 60)
                self.assertEqual(len(seen), 1)
                self.assertTrue(client.is_closed)
                self.assertEqual(len(diagnostics), 1)
                diagnostic = diagnostics[0]
                self.assertEqual(diagnostic['failure_reason'], reason)
                self.assertEqual(diagnostic['stage'], stage)
                self.assertEqual(diagnostic['http_status'], status)
                self.assertEqual(diagnostic['timeout_seconds'], 15)
                self.assertEqual(diagnostic['elapsed_ms'], 0)
                self.assertEqual(diagnostic['received_bytes'], len(b'PRIVATE-BODY') if status == 200 else 0)
                self.assertEqual(diagnostic['provider_request_id'], 'req-123' if status in (429, 503) else None)
                self.assertNotIn('SECRET-KEY', json.dumps(diagnostic))
                self.assertNotIn('PRIVATE-BODY', json.dumps(diagnostic))
                if isinstance(response, Exception):
                    self.assertIs(caught.exception, response)

    def test_partial_read_records_bytes_and_bounded_cause_types(self):
        diagnostic = []
        original = httpx.ReadTimeout('PRIVATE-BODY')
        original.__cause__ = OSError('SECRET-KEY')
        class BrokenStream(httpx.SyncByteStream):
            def __iter__(self):
                yield b'partial'
                raise original
        client = httpx.Client(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, stream=BrokenStream(), headers={'x-request-id': 'SECRET-KEY'})))
        with patch('backend.investigation_model.httpx.Client', return_value=client), \
             patch('backend.investigation_model.time.monotonic', return_value=0):
            with model_session({'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'SECRET-KEY'},
                               on_failure=diagnostic.append) as request:
                with self.assertRaises(httpx.ReadTimeout) as caught:
                    request({}, 0, 60)
        self.assertIs(caught.exception, original)
        self.assertEqual(diagnostic[0]['stage'], 'reading_body')
        self.assertEqual(diagnostic[0]['received_bytes'], 7)
        self.assertEqual(diagnostic[0]['cause_types'], ['OSError'])
        self.assertIsNone(diagnostic[0]['provider_request_id'])
        self.assertNotIn('SECRET-KEY', json.dumps(diagnostic))
        self.assertNotIn('PRIVATE-BODY', json.dumps(diagnostic))

    def test_diagnostic_callback_failure_preserves_original_error(self):
        original = httpx.ConnectError('original')
        def handler(request):
            raise original
        def broken_callback(diagnostic):
            raise RuntimeError('callback failed')
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('backend.investigation_model.httpx.Client', return_value=client):
            with model_session({'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'fixture'},
                               on_failure=broken_callback) as request:
                with self.assertRaises(httpx.ConnectError) as caught:
                    request({}, 0, 60)
        self.assertIs(caught.exception, original)
        self.assertTrue(client.is_closed)

    def test_timeout_uses_call_budget_but_elapsed_uses_attempt_start(self):
        diagnostics, requests = [], []
        def handler(request):
            requests.append(request)
            raise httpx.ReadTimeout('timeout')
        client = httpx.Client(transport=httpx.MockTransport(handler))
        with patch('backend.investigation_model.httpx.Client', return_value=client), \
             patch('backend.investigation_model.time.monotonic', side_effect=[18, 20]):
            with model_session({'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'fixture'},
                               on_failure=diagnostics.append) as request:
                with self.assertRaises(httpx.ReadTimeout):
                    request({}, 0, 20)
        self.assertEqual(diagnostics[0]['timeout_seconds'], 2)
        self.assertEqual(diagnostics[0]['elapsed_ms'], 2000)
        self.assertEqual(requests[0].extensions['timeout']['read'], 2)

    def test_provider_request_ids_are_bounded_and_allowlisted(self):
        for headers, expected in (({'request-id': 'req:123'}, 'req:123'),
                                  ({'x-request-id': 'x'*129}, None),
                                  ({'x-request-id': 'private data'}, None),
                                  ({'authorization': 'SECRET-KEY'}, None)):
            with self.subTest(headers=headers):
                diagnostics = []
                client = httpx.Client(transport=httpx.MockTransport(lambda request:
                    httpx.Response(503, headers=headers)))
                with patch('backend.investigation_model.httpx.Client', return_value=client):
                    with model_session({'SUPPLY_MODEL': 'fixture', 'SUPPLY_MODEL_API_KEY': 'fixture'},
                                       on_failure=diagnostics.append) as request:
                        with self.assertRaises(httpx.HTTPStatusError):
                            request({}, 0, 60)
                self.assertEqual(diagnostics[0]['provider_request_id'], expected)
                self.assertNotIn('SECRET-KEY', json.dumps(diagnostics))

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
        for content, clock, reason in [(b'x' * 100001, [0, 1], 'response_too_large'),
                                       (b'{}', [0, 61, 62], 'call_time_exceeded')]:
            with self.subTest(size=len(content)):
                requests, diagnostics = [], []

                def handler(request):
                    requests.append(request)
                    return httpx.Response(200, content=content)

                client = httpx.Client(transport=httpx.MockTransport(handler))
                with patch('backend.investigation_model.httpx.Client', return_value=client):
                    with patch('backend.investigation_model.time.monotonic', side_effect=clock):
                        with self.assertRaisesRegex(ValueError, 'response budget'):
                            with model_session({'SUPPLY_MODEL': 'fixture', 'DEEPSEEK_API_KEY': 'fixture'},
                                               on_failure=diagnostics.append) as request:
                                request({}, 0, 60)
                self.assertEqual(len(requests), 1)
                self.assertTrue(client.is_closed)
                self.assertEqual(diagnostics[0]['failure_reason'], reason)
                self.assertEqual(diagnostics[0]['stage'], 'reading_body')
                self.assertEqual(diagnostics[0]['received_bytes'], len(content))

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
