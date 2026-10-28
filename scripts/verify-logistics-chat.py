"""显式真实模型与真实只读源验证；账号、会话、草稿仅写隔离 SQLite。"""
import argparse
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from fastapi.testclient import TestClient
from backend.auth import now, password_hash
from backend.common import APIError
from backend.logistics_access import WORKSPACE
from backend.logistics_model import configured
from backend.logistics_repository import source_connection
from backend.main import create_app
from scripts.reference_logistics_metrics import reference


def verify(output):
    env = {**dotenv_values(ROOT/'.env'), **os.environ}
    if not configured(env):
        raise SystemExit('真实聊天模型配置不可用，未发送请求。')
    moment = '2000-07-02 10:00:00'
    with source_connection(env) as db:
        batch = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE verified_at IS NOT NULL ORDER BY imported_at DESC LIMIT 1').fetchone()
        if not batch:
            raise SystemExit('没有已核验的真实源批次。')
        raw = db.execute("SELECT batch_id,source_row,order_id,city,region_id,courier_id,aoi_id,accept_time,pickup_time,time_window_start,time_window_end FROM lade.pickup_tasks WHERE batch_id=%s AND city='Shanghai' AND region_id='5' AND ('2000-'||accept_time)::timestamp<=%s::timestamp", (batch['batch_id'], moment)).fetchall()
    expected = reference(raw, moment)
    proof = {'checked_at': datetime.now(timezone(timedelta(hours=8))).isoformat(),
             'data_kind': '真实只读物流源，隔离业务库；独立复算', 'cases': [], 'independent_metrics': {},
             'source_before': dict(batch), 'model': env['SUPPLY_MODEL']}
    path = Path(output); path.parent.mkdir(parents=True, exist_ok=True)

    def save():
        path.write_text(json.dumps(proof, ensure_ascii=False, indent=2), encoding='utf-8')

    with tempfile.TemporaryDirectory() as directory:
        app = create_app({**env, 'AUTH_DB_PATH': str(Path(directory)/'auth.sqlite'), 'DATA_DB_PATH': str(Path(directory)/'data.sqlite'), 'NODE_ENV': 'test'})
        with TestClient(app):
            account = 'isolated-chat-check'
            app.state.auth.query("INSERT INTO users(account,type,name,password_hash,created_at) VALUES(?,'email','聊天验证',?,?)", (account, password_hash('isolated-check-only'), now()))
            app.state.access.sync()
            app.state.data.db.execute('INSERT INTO logistics_grants VALUES(?,?,?,?,?)', (WORKSPACE, account, 'investigator', 'Shanghai', '["5"]'))
            app.state.data.db.execute('INSERT INTO database_source_connections VALUES(?,?,?)', (WORKSPACE, batch['batch_id'], 'verification'))
            user = app.state.auth.query('SELECT * FROM users WHERE account=?', (account,))
            chat = app.state.logistics_chat
            identifier = chat.create(user, {})['conversation_id']
            steps = [
                ('调查区域5在2000年7月2日10:00的揽收风险', 'investigate'),
                ('把刚才的报告解释得简单一点', 'reply'),
                ('这两条为什么逾期？', 'reply'),
                ('现场反馈有人手不足，这是未经核验的用户陈述', 'fact'),
                ('先核查窗口，暂时不要联系快递员', 'constraint'),
                ('按这个方向生成一个方案，使用窗口核查方式', 'draft'),
                ('先处理第二条，再完善说明', 'draft'),
                ('提交当前方案', 'submit'),
            ]
            for index, (text, action) in enumerate(steps, 1):
                current = chat.read(user, identifier)
                message = chat.send(user, identifier, {'text': text, 'request_id': f'live-{index}', 'context_version': current['version'], 'selection': {'mode': 'deterministic'}})
                started = time.monotonic()
                while time.monotonic()-started < 90:
                    saved = app.state.data.one('SELECT * FROM logistics_messages WHERE message_id=?', (message['message_id'],))
                    if saved['status'] not in ('saved', 'processing', 'waiting'):
                        break
                    time.sleep(.2)
                operation = app.state.data.one('SELECT * FROM logistics_chat_operations WHERE message_id=?', (message['message_id'],))
                candidate = json.loads(operation['action_json']) if operation['action_json'] else None
                replies = app.state.data.all("SELECT text,status FROM logistics_messages WHERE conversation_id=? AND role='assistant' AND seq>? ORDER BY seq", (identifier, message['seq']))
                passed = saved['status'] in ('replied', 'clarification') and candidate and candidate['action'] == action
                proof['cases'].append({'index': index, 'message': text, 'expected_action': action, 'action': candidate,
                    'status': saved['status'], 'reply': replies, 'passed': bool(passed), 'tokens': operation['tokens'],
                    'reserved_tokens': operation['reserved_tokens'], 'error_code': operation['error_code'],
                    'diagnostic': json.loads(operation['diagnostic_json']) if operation['diagnostic_json'] else None})
                save()
                print(f'真实对话 {index}：{saved["status"]}，动作 {candidate["action"] if candidate else "未取得"}', flush=True)
                if not passed:
                    break
                if index == 1:
                    run_id = chat.read(user, identifier)['context']['run_id']
                    report = app.state.logistics_reports.read(user, run_id)
                    for key, metric in report['summary']['metrics'].items():
                        proof['independent_metrics'][key] = {'actual': metric['value'], 'expected': expected[key], 'passed': metric['value'] == expected[key]}
                if index == 5:
                    constraints = chat.read(user, identifier)['context'].get('constraints', [])
                    proof['contact_constraint_active'] = any(c['constraint_key'] == 'contact_courier' and c['active'] for c in constraints)
                if index == 7:
                    drafts = app.state.logistics_collaboration.read(user, run_id)['drafts']
                    before = [t['order_id'] for t in drafts[-2]['content']['tasks']]
                    after = [t['order_id'] for t in drafts[-1]['content']['tasks']]
                    proof['task_order_changed'] = len(before) == 2 and after == list(reversed(before))
            run_id = chat.read(user, identifier)['context'].get('run_id')
            if run_id:
                proof['collaboration'] = app.state.logistics_collaboration.read(user, run_id)
            proof['all_actions_passed'] = len(proof['cases']) == len(steps) and all(c['passed'] for c in proof['cases'])
            proof['business_checks_passed'] = bool(proof.get('contact_constraint_active') and proof.get('task_order_changed') and
                proof['independent_metrics'] and all(v['passed'] for v in proof['independent_metrics'].values()))
            proof['usage'] = app.state.data.all('SELECT stage,tokens,reserved_tokens,elapsed_seconds,error_code,prompt_version,diagnostic_json FROM logistics_chat_operations')
    try:
        with source_connection(env) as db:
            after = db.execute('SELECT batch_id,source_sha256,content_sha256 FROM lade.import_batches WHERE batch_id=%s', (batch['batch_id'],)).fetchone()
        proof['source_after'] = dict(after)
        proof['source_unchanged'] = proof['source_before'] == proof['source_after']
    except APIError:
        proof['source_unchanged'] = None
        proof['source_check_error'] = '复查连接不可用，未宣称源摘要核验通过。'
    save()
    if not proof['all_actions_passed'] or not proof['business_checks_passed']:
        raise SystemExit('真实对话存在未通过项，失败已保存；没有自动重试。')
    print('真实对话动作检查完成；自然语言结论须结合保存证据审核。', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', default='test-results/logistics-chat/live.json')
    args = parser.parse_args()
    if not args.live:
        parser.error('必须显式提供 --live 才会调用真实供应商。')
    verify(args.output)
