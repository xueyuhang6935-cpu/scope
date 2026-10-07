import type { Tone } from './components';

export const factories = ['all', 'A', 'B'] as const;
export type Factory = typeof factories[number];
export const factoryNames = { all: '全部工厂', A: 'A 工厂', B: 'B 工厂' };
export const risks: { id: string; material: string; name: string; factory: Factory; impact: string; status: string; tone: Tone; priority: string; owner: string; deadline: string }[] = [
  { id: 'EVT-001', material: 'P001', name: '控制器缺料风险', factory: 'A', impact: '未来三天缺口 400 件', status: '待审批', tone: 'warning', priority: '高', owner: '生产计划组', deadline: '10-02 14:00' },
  { id: 'EVT-008', material: 'P008', name: '连接器交付异常', factory: 'A', impact: '交期变更，影响待确认', status: '执行中', tone: 'info', priority: '高', owner: '物流组', deadline: '10-02 16:00' },
  { id: 'EVT-012', material: 'P012', name: '传感器库存单位异常', factory: 'B', impact: '箱 / 件换算缺失，数量尚不可计算', status: '待补充数据', tone: 'warning', priority: '中', owner: '数据管理组', deadline: '待负责人确认' },
];
export const approvals = [{ id: 'APR-001', event: 'EVT-001', factory: 'A', title: 'P001 跨厂调拨方案 v1', status: '待审批', version: 'v1' }];
export const runs = [{ id: 'RUN-008', event: 'EVT-008', factory: 'A', title: 'P008 补货执行跟踪', status: '运行中' }, { id: 'RUN-012', event: 'EVT-012', factory: 'B', title: 'P012 库存标准化检查', status: '等待数据' }];
export const datasets = [
  { id: 'inventory', title: '库存数据', count: '3 条示例记录', status: '待确认', tone: 'warning' as Tone },
  { id: 'purchase', title: '采购订单', count: '尚未接入', status: '未接入', tone: 'warning' as Tone },
  { id: 'bom', title: '物料清单', count: '尚未接入', status: '未接入', tone: 'warning' as Tone },
  { id: 'production', title: '生产计划', count: '尚未接入', status: '未接入', tone: 'warning' as Tone },
];
export function inScope(factory: Factory, item: { factory: string }) { return factory === 'all' || factory === item.factory; }
