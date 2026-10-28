"""确定性物流报告：只接收已授权证据结果，不访问数据源或模型。"""
import html


def build_report(run_id, summary, tasks, quality):
    results = (summary, tasks, quality)
    if any(r['status'] == 'error' or r['data'] is None for r in results):
        raise ValueError('报告所需工具结果不可用。')
    if any(any(r[key] != summary[key] for key in ('run_id', 'snapshot_id', 'fingerprint', 'business_as_of', 'metric_version')) for r in results):
        raise ValueError('报告证据范围不一致。')
    if summary['run_id'] != run_id:
        raise ValueError('报告调查不一致。')
    focus = [{key: row[key] for key in ('order_id', 'courier_id', 'aoi_id', 'risk_state',
              'current_wait_minutes', 'current_overdue_minutes', 'remaining_minutes', 'evidence_id')} for row in tasks['rows']]
    follow_up = []
    if summary['data']['metrics']['overdue'].get('value') or summary['data']['metrics']['overdue'].get('known_lower_bound'):
        follow_up.append('人工核查逾期未完成任务的当前现场情况，原因尚未确认。')
    if summary['data']['metrics']['due_soon'].get('value') or summary['data']['metrics']['due_soon'].get('known_lower_bound'):
        follow_up.append('人工关注临期任务，不把优先级清单当作真实派单。')
    if summary['data']['missing_window_count']:
        follow_up.append('补充缺失窗口；当前逾期/临期完整数量不可计算。')
    if quality['status'] == 'unavailable':
        follow_up.append('核查当前可见字段质量；无可用GPS不证明人员没有到场。')
    follow_up.append('后续业务时点使用新快照单独复核，不据历史完成推断建议效果。')
    return {'report_version': 'logistics-report-v1', 'run_id': run_id,
            'scope': {key: summary[key] for key in ('snapshot_id', 'fingerprint', 'visible_content_hash',
                                                  'business_as_of', 'city', 'region_ids', 'metric_version',
                                                  'horizon_minutes', 'recent_minutes')},
            'status': summary['status'], 'source_type': 'deterministic_calculation',
            'summary': summary['data'], 'summary_evidence_id': summary['result_evidence_id'],
            'focus_tasks': focus, 'task_list': {key: tasks[key] for key in ('total_rows', 'returned_rows', 'truncated')},
            'tasks_evidence_id': tasks['result_evidence_id'], 'quality': quality['data'],
            'quality_status': quality['status'], 'quality_evidence_id': quality['result_evidence_id'],
            'limitations': list(dict.fromkeys(item for r in results for item in r['limitations'])),
            'follow_up': follow_up}


def text(value):
    """转义源标识中的Markdown/HTML，下载不注入链接、标签或表格结构。"""
    value = html.escape(str(value), quote=True).replace('\r', ' ').replace('\n', ' ')
    return ''.join('\\'+char if char in '\\`*_{}[]()#+-.!|' else char for char in value)


def number(value):
    return '不可计算' if value is None else format(value, '.6g') if isinstance(value, float) else str(value)


def render_markdown(report):
    scope = report['scope']
    lines = ['# 上海物流揽收时效风险基础报告', '',
             f"- 调查：{text(report['run_id'])}",
             f"- 范围：{text(scope['city'])} / 区域 {text(', '.join(scope['region_ids']))}",
             f"- 历史回放参考时间：{text(scope['business_as_of'])}（2000为参考轴，真实年份未知）",
             f"- 快照：{text(scope['snapshot_id'])}", f"- 指标口径：{text(scope['metric_version'])}",
             '- 来源：程序确定性计算；原因未确认，未执行派单。', '', '## 指标与证据', '',
             '| 指标 | 数量 | 状态 | 已知下界 |', '| --- | ---: | --- | ---: |']
    for metric in report['summary']['metrics'].values():
        lines.append(f"| {text(metric['name'])} | {number(metric['value'])} | {text(metric['status'])} | {number(metric.get('known_lower_bound')) if 'known_lower_bound' in metric else '—'} |")
    lines += ['', f"汇总证据：{text(report['summary_evidence_id'])}", '', '## 重点待揽收任务', '',
              f"全部 {report['task_list']['total_rows']} 条，展示 {report['task_list']['returned_rows']} 条；后续明细{'有截断' if report['task_list']['truncated'] else '无截断'}。",
              '', '| 任务 | 快递员 | AOI | 风险 | 等待分钟 | 逾期分钟 | 剩余分钟 | 证据 |',
              '| --- | --- | --- | --- | ---: | ---: | ---: | --- |']
    for row in report['focus_tasks']:
        lines.append('| ' + ' | '.join(text(row[k]) for k in ('order_id', 'courier_id', 'aoi_id', 'risk_state'))
                     + ' | ' + ' | '.join(number(row[k]) for k in ('current_wait_minutes', 'current_overdue_minutes', 'remaining_minutes'))
                     + ' | ' + text(row['evidence_id']) + ' |')
    lines += ['', f"任务清单证据：{text(report['tasks_evidence_id'])}", '', '## 当前可见字段质量与缺失', '',
              f"未完成窗口缺失：{report['summary']['missing_window_count']} 条。", '',
              '| 字段 | 可用 | 不可用 | 适用分母 | 不适用 |', '| --- | ---: | ---: | ---: | ---: |']
    for key, quality in report['quality']['quality'].items():
        lines.append('| '+text(key)+' | '+' | '.join(str(quality[k]) for k in ('available', 'unavailable', 'denominator', 'not_applicable'))+' |')
    lines += ['', f"质量证据：{text(report['quality_evidence_id'])}", '', '## 限制', '']
    lines += ['- '+text(item) for item in report['limitations']]
    lines += ['', '## 待跟进事项', ''] + ['- '+text(item) for item in report['follow_up']]
    return '\n'.join(lines)+'\n'


def render_html(report):
    # 无脚本页面直接展示同一结构化报告；工作台/运行页面接线留给T09。
    safe = lambda value: html.escape(str(value), quote=True)
    def table(headers, rows):
        table_class = 'table wide' if len(headers) > 6 else 'table'
        return '<div class="'+table_class+'"><table><thead><tr>'+''.join('<th>'+safe(h)+'</th>' for h in headers)+'</tr></thead><tbody>'+''.join(
            '<tr>'+''.join('<td>'+safe(v)+'</td>' for v in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    def items(values):
        return '<ul>'+''.join('<li>'+safe(value)+'</li>' for value in values)+'</ul>'
    scope = report['scope']
    body = '<h1>上海物流揽收时效风险基础报告</h1>'+items([
        '调查：'+report['run_id'], '范围：'+scope['city']+' / 区域 '+', '.join(scope['region_ids']),
        '历史回放参考时间：'+scope['business_as_of']+'（2000为参考轴，真实年份未知）',
        '快照：'+scope['snapshot_id'], '指标口径：'+scope['metric_version'],
        '程序确定性计算；原因未确认，未执行派单。'])
    body += '<h2>指标与证据</h2>'+table(['指标', '数量', '状态', '已知下界'], [
        [m['name'], number(m['value']), m['status'], number(m['known_lower_bound']) if 'known_lower_bound' in m else '—']
        for m in report['summary']['metrics'].values()])+'<p>汇总证据：'+safe(report['summary_evidence_id'])+'</p>'
    counts = report['task_list']
    body += '<h2>重点待揽收任务</h2><p>'+safe(f"全部{counts['total_rows']}条，展示{counts['returned_rows']}条；后续明细{'有截断' if counts['truncated'] else '无截断'}。")+'</p>'
    body += table(['任务', '快递员', 'AOI', '风险', '等待分钟', '逾期分钟', '剩余分钟', '证据'], [
        [*[row[k] for k in ('order_id', 'courier_id', 'aoi_id', 'risk_state')],
         *[number(row[k]) for k in ('current_wait_minutes', 'current_overdue_minutes', 'remaining_minutes')], row['evidence_id']]
        for row in report['focus_tasks']])+'<p>任务清单证据：'+safe(report['tasks_evidence_id'])+'</p>'
    body += '<h2>当前可见字段质量与缺失</h2><p>未完成窗口缺失：'+safe(report['summary']['missing_window_count'])+'条。</p>'
    body += table(['字段', '可用', '不可用', '适用分母', '不适用'], [
        [key, *[q[k] for k in ('available', 'unavailable', 'denominator', 'not_applicable')]]
        for key, q in report['quality']['quality'].items()])+'<p>质量证据：'+safe(report['quality_evidence_id'])+'</p>'
    body += '<h2>限制</h2>'+items(report['limitations'])+'<h2>待跟进事项</h2>'+items(report['follow_up'])
    return '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>物流调查基础报告</title><style>body{font:16px/1.7 system-ui;margin:24px auto;padding:0 20px;max-width:1100px;color:#172b4d;background:#fafbfc}h1{font-size:26px}h2{margin-top:32px}.table{overflow-x:auto}.wide table{min-width:1000px}.wide th,.wide td{white-space:nowrap}table{width:100%;border-collapse:collapse;background:white}th,td{border:1px solid #dce2e9;padding:8px;text-align:left;overflow-wrap:anywhere}th{background:#eef2f7}li,p{overflow-wrap:anywhere}</style><body>'+body+'</body></html>'
