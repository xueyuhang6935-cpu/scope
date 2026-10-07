"""Backend model transport; credentials never enter the Agent core."""
from contextlib import contextmanager
import json
import time

import httpx


@contextmanager
def model_session(env):
    with httpx.Client(timeout=15, follow_redirects=False) as client:
        def request(body, started, seconds):
            body = {'model': env['SUPPLY_MODEL'], **body}
            with client.stream('POST', env.get('SUPPLY_MODEL_BASE_URL', 'https://api.deepseek.com').rstrip('/') + '/chat/completions',
                json=body, timeout=min(15, max(1, seconds - (time.monotonic() - started))), headers={'Authorization': 'Bearer ' + (env.get('SUPPLY_MODEL_API_KEY') or env['DEEPSEEK_API_KEY'])}) as response:
                response.raise_for_status()
                raw = bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw) > 100000 or time.monotonic() - started > seconds:
                        raise ValueError('response budget')
            return json.loads(raw)
        yield request
