"""验证当前供应商的实际报告协议输出；每种模式两次，不自动重试或降级。"""
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
from project.tests.test_logistics_protocol import SCOPE, evidence, report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    path = Path(args.output)
    if path.exists():
        raise FileExistsError('不覆盖已有核验结果')
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    if not configured(env):
        raise ValueError('模型配置不可用')
    expected = report()
    result = {'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
              'model': env['SUPPLY_MODEL'], 'business_data_sent': False, 'probes': []}
    for mode in ('tool_call', 'json_schema'):
        for repeat in range(1, 3):
            row = {'mode': mode, 'repeat': repeat, 'protocol_valid': False}
            started = time.monotonic()
            try:
                messages = [{'role': 'system', 'content': '提交报告JSON，内容严格等于：'+json.dumps(expected, ensure_ascii=False)}]
                with model_session(env) as request:
                    response = request(model_body('report', messages, mode, 1024), started, 30)
                value = model_output(response, 'report', mode)
                validate_output('report', value, scope=SCOPE, evidence=evidence())
                row.update(protocol_valid=value == expected, tokens=response.get('usage', {}).get('total_tokens'), value=value)
            except httpx.HTTPStatusError as exc:
                row.update(error='provider_rejected', http_status=exc.response.status_code, tokens=None)
            except ProtocolError as exc:
                row.update(error=exc.code, path=exc.path)
            except Exception:
                row.update(error='transport_failed')
            row['elapsed_ms'] = round((time.monotonic()-started)*1000)
            result['probes'].append(row)
            print(mode, repeat, row['protocol_valid'], row.get('error', 'ok'), flush=True)
    result['recommended_mode'] = next((mode for mode in ('tool_call', 'json_schema')
        if all(r['protocol_valid'] for r in result['probes'] if r['mode'] == mode)), None)
    result['limitation'] = '小规模真实协议探测，不能证明供应商强制执行schema或保证调查语义正确；后端校验仍保留。'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')


if __name__ == '__main__':
    main()
