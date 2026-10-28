"""T04 独立复算：仅 Python 标准库，不导入被测快照或指标实现。"""
import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / 'tests/fixtures/logistics-t04.json'


def fixture_records():
    fixture = json.loads(FIXTURE.read_text(encoding='utf-8'))
    rows = []
    for index, task in enumerate(fixture['tasks'], 2):
        end = datetime.strptime('2000-' + task['time_window_end'], '%Y-%m-%d %H:%M:%S')
        rows.append({**task, 'time_window_start': (end - timedelta(hours=1)).strftime('%m-%d %H:%M:%S'),
                     'batch_id':'t04-fixture', 'city':'Shanghai', 'region_id':'5', 'aoi_type':'0',
                     'source_file':'fixtures/logistics-t04.json', 'source_row':index, 'lng':'121.4', 'lat':'31.2',
                     'accept_gps_time':'', 'accept_gps_lng':'', 'accept_gps_lat':'',
                     'pickup_gps_time':'', 'pickup_gps_lng':'', 'pickup_gps_lat':'', 'ds':task['pickup_time'][:5].replace('-', '')})
    return rows


def reference(rows, as_of):
    moment = datetime.strptime(as_of, '%Y-%m-%d %H:%M:%S')
    def date(value):
        return datetime.strptime('2000-' + value, '%Y-%m-%d %H:%M:%S') if value else None
    visible = [r for r in rows if date(r['accept_time']) <= moment]
    completed = [r for r in visible if date(r['pickup_time']) <= moment]
    pending = [r for r in visible if date(r['pickup_time']) > moment]
    tasks = []
    for row in pending:
        start, end = date(row.get('time_window_start')), date(row.get('time_window_end'))
        valid = start is not None and end is not None and start <= end
        delta = (end - moment).total_seconds() if valid else None
        state = 'window_unavailable' if delta is None else 'overdue' if delta < 0 else 'due_soon' if delta <= 1800 else 'normal'
        tasks.append({'order_id':row['order_id'], 'city':row['city'], 'region_id':row['region_id'],
                      'batch_id':row['batch_id'], 'source_row':row['source_row'], 'courier_id':row['courier_id'], 'aoi_id':row['aoi_id'],
                      'risk_state':state, 'current_wait_minutes':(moment-date(row['accept_time'])).total_seconds()/60,
                      'current_overdue_minutes':max(0,-delta/60) if valid else None,
                      'remaining_minutes':delta/60 if valid else None, 'end':end})
    # 独立顺序：分成四个集合后分别排序、拼接，不调用业务优先级函数。
    tie = lambda r: (r['city'],r['region_id'],r['order_id'],r['batch_id'],r['source_row'])
    ordered = []
    for state, score in [('overdue',lambda r:-r['current_overdue_minutes']),
                         ('due_soon',lambda r:r['remaining_minutes']), ('normal',lambda r:r['end']),
                         ('window_unavailable',lambda r:0)]:
        ordered.extend(sorted((r for r in tasks if r['risk_state']==state),key=lambda r:(score(r),*tie(r))))
    missing = sum(r['risk_state']=='window_unavailable' for r in tasks)
    counts = {'visible_tasks':len(visible), 'completed_tasks':len(completed), 'backlog':len(pending),
              'overdue':None if missing else sum(r['risk_state']=='overdue' for r in tasks),
              'due_soon':None if missing else sum(r['risk_state']=='due_soon' for r in tasks),
              'recent_accepted':sum(moment-timedelta(hours=1)<date(r['accept_time'])<=moment for r in visible),
              'recent_completed':sum(moment-timedelta(hours=1)<date(r['pickup_time'])<=moment for r in completed)}
    groups = {}
    for dimension in ('courier_id','aoi_id'):
        groups[dimension] = []
        for value in sorted({r[dimension] for r in tasks}):
            members = [r for r in tasks if r[dimension]==value]
            incomplete = any(r['risk_state']=='window_unavailable' for r in members)
            group = {dimension:value, 'backlog':len(members)}
            for state in ('overdue','due_soon'):
                group[state] = None if incomplete else sum(r['risk_state']==state for r in members)
            groups[dimension].append(group)
    for row in ordered:
        del row['end']
    return {**counts,'missing_window_count':missing,'order':[r['order_id'] for r in ordered], 'tasks':ordered,'groups':groups}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--input',type=Path,help='授权宿主导出的原始测试记录 JSON 数组；默认 A—H 隔离夹具')
    parser.add_argument('--as-of',default='2000-07-02 10:00:00')
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    if args.input:
        result = reference(json.loads(args.input.read_text(encoding='utf-8')), args.as_of)
    else:
        rows = fixture_records()
        expected = json.loads(FIXTURE.read_text(encoding='utf-8'))['expected']
        result = {}
        for moment, answer in expected.items():
            actual = reference(rows,moment)
            assert {k:actual[k] for k in answer} == answer, '独立复算与手算表不一致'
            result[moment] = actual
    text = json.dumps(result,ensure_ascii=False,indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as output:
            output.write(text)
    print(text)


if __name__ == '__main__':
    main()
