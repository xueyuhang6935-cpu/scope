"""Backend model transport; credentials never enter the Agent core."""
from contextlib import contextmanager
import json
import re
import time

import httpx


def failure_reason(exc):
    for error_type, reason in (
        (httpx.ConnectTimeout, 'connect_timeout'), (httpx.ReadTimeout, 'read_timeout'),
        (httpx.WriteTimeout, 'write_timeout'), (httpx.PoolTimeout, 'pool_timeout'),
        (httpx.ConnectError, 'connect_error'), (httpx.HTTPStatusError, 'http_status_error'),
        (httpx.RemoteProtocolError, 'remote_protocol_error'), (json.JSONDecodeError, 'invalid_response_json'),
    ):
        if isinstance(exc, error_type):
            return reason
    return 'unexpected_exception'


@contextmanager
def model_session(env, *, on_failure=None):
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        def request(body, started, seconds):
            attempt_started = time.monotonic()
            timeout = min(15, max(1, seconds - (attempt_started - started)))
            stage, reason = 'sending', None
            status, provider_request_id = None, None
            raw = bytearray()
            try:
                body = {'model': env['SUPPLY_MODEL'], **body}
                with client.stream('POST', env.get('SUPPLY_MODEL_BASE_URL', 'https://api.deepseek.com').rstrip('/') + '/chat/completions',
                    json=body, timeout=timeout, headers={'Authorization': 'Bearer ' + (env.get('SUPPLY_MODEL_API_KEY') or env['DEEPSEEK_API_KEY'])}) as response:
                    stage = 'checking_status'
                    status = response.status_code
                    identifier = response.headers.get('x-request-id') or response.headers.get('request-id')
                    if identifier and re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', identifier) and not any(
                        secret and secret in identifier for secret in (env.get('SUPPLY_MODEL_API_KEY'), env.get('DEEPSEEK_API_KEY'))
                    ):
                        provider_request_id = identifier
                    response.raise_for_status()
                    stage = 'reading_body'
                    for chunk in response.iter_bytes():
                        raw.extend(chunk)
                        if len(raw) > 100000:
                            reason = 'response_too_large'
                            raise ValueError('response budget')
                        if time.monotonic() - started > seconds:
                            reason = 'call_time_exceeded'
                            raise ValueError('response budget')
                stage = 'parsing_json'
                return json.loads(raw)
            except Exception as exc:
                if on_failure is not None:
                    # Only bounded identifiers and exception types; never exception text, URLs or bodies.
                    causes = []
                    cause = exc.__cause__ or (None if exc.__suppress_context__ else exc.__context__)
                    while cause is not None and len(causes) < 3:
                        causes.append(type(cause).__name__)
                        cause = cause.__cause__ or (None if cause.__suppress_context__ else cause.__context__)
                    try:
                        on_failure({'exception_type': type(exc).__name__, 'failure_reason': reason or failure_reason(exc),
                            'stage': stage, 'elapsed_ms': round(max(0, time.monotonic()-attempt_started)*1000, 3),
                            'timeout_seconds': timeout, 'http_status': status, 'received_bytes': len(raw),
                            'provider_request_id': provider_request_id, 'cause_types': causes})
                    except Exception:
                        # Diagnostics must not replace the original request failure.
                        pass
                raise
        yield request
