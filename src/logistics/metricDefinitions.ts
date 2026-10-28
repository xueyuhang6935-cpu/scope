export type MetricId = 'backlog' | 'overdue' | 'due_soon' | 'recent_accepted' | 'recent_completed';
export type MetricDefinition = { id: MetricId; name: string; category: string; description: string; riskState?: string; tone: string };
export const metricDefinitions: MetricDefinition[] = [
  { id: 'backlog', name: '样本内积压', category: '风险与积压', description: '截至参考时点已接单、尚未观察到完成的样本任务数。', riskState: 'all', tone: 'purple' },
  { id: 'overdue', name: '逾期未完成', category: '风险与积压', description: '待揽收任务中，窗口截止时间早于参考时点的任务数。窗口缺失时完整数量不可计算。', riskState: 'overdue', tone: 'red' },
  { id: 'due_soon', name: '临期未完成', category: '风险与积压', description: '待揽收任务中，窗口截止时间处于参考时点至临期窗口终点（含边界）的任务数。', riskState: 'due_soon', tone: 'orange' },
  { id: 'recent_accepted', name: '近期接单', category: '揽收时效', description: '近期统计窗口内的接单任务数，左边界不包含、参考时点包含。接单表示快递员接单。', tone: 'purple' },
  { id: 'recent_completed', name: '近期完成', category: '揽收时效', description: '近期统计窗口内已经观察到完成的任务数，左边界不包含、参考时点包含。', tone: 'green' },
];
export const defaultMetricIds: MetricId[] = ['backlog', 'overdue', 'due_soon', 'recent_completed'];
export const riskNames: Record<string, string> = { all: '全部待揽收', risk: '全部风险', overdue: '逾期', due_soon: '临期', normal: '普通', window_unavailable: '窗口不可计算' };
export const minutes = (value: number | null) => value == null ? '不可计算' : `${value.toLocaleString('zh-CN', { maximumFractionDigits: 2 })} 分钟`;
