"""T10显式真实能力探测；仅发送合成协议问题，不读取业务数据或保存凭证。"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import httpx
from dotenv import dotenv_values
from backend.investigation_model import model_session
from backend.logistics_model import configured
from project.agents.logistics_model import model_body, model_output
from project.agents.logistics_protocol import ProtocolError, validate_output


def verify(output):
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    if not configured(env):
        raise SystemExit('模型配置不可用；未发送请求。')
    proof = {'task': 'T10', 'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
             'model': env['SUPPLY_MODEL'], 'business_data_sent': False, 'probes': {}}
    expected = {'goal': '协议连通性检查', 'questions': [], 'clarification': '无业务数据，仅测试协议', 'action': 'clarify'}
    for mode in ('tool_call', 'json_object', 'json_schema'):
        started = time.monotonic()
        row = {'attempts': 1, 'supported': None, 'tokens': None}
        try:
            messages = [{'role': 'system', 'content': '只输出JSON协议，内容必须等于：'+json.dumps(expected, ensure_ascii=False)}]
            with model_session(env) as request:
                payload = request(model_body('intent', messages, mode, 256), started, 30)
            validate_output('intent', model_output(payload, 'intent', mode), scope={})
            row.update(supported=True, protocol_valid=True, tokens=payload.get('usage', {}).get('total_tokens'))
        except httpx.HTTPStatusError as exc:
            row.update(supported=False if exc.response.status_code in (400, 422) else None,
                       protocol_valid=False, http_status=exc.response.status_code, error_code='provider_rejected')
        except (ProtocolError, ValueError, KeyError, TypeError):
            row.update(protocol_valid=False, error_code='invalid_protocol_output')
        except Exception:
            row.update(protocol_valid=False, error_code='transport_failed')
        row['elapsed_ms'] = round((time.monotonic()-started)*1000)
        proof['probes'][mode] = row
        print(mode, row['supported'], row.get('error_code', 'ok'))
    preferred = next((mode for mode in ('tool_call', 'json_schema', 'json_object') if proof['probes'][mode]['supported']), 'json_text')
    proof['verified_recommended_mode'] = preferred
    proof['default_mode'] = 'json_text'
    proof['note'] = '探测不修改本机配置；供应商格式不代替后端校验；HTTP拒绝用量未知，不自动重试。'
    path = Path(output); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')
    print('T10模型能力核查已保存：'+str(path))


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', help='显式调用本机配置模型，发送三个无业务数据探测请求')
    parser.add_argument('--output', default='test-results/logistics-t10/model-capabilities.json')
    args = parser.parse_args()
    if not args.live:
        parser.error('必须显式提供--live；默认不调用真实模型。')
    verify(args.output)
