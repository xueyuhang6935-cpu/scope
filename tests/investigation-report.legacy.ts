import { test, expect } from '@playwright/test';
import type { Report } from '../src/supply/InvestigationReport';

const base: Report = { mode: 'model', facts: [], hypotheses: [], missing: [], trace: [], modelCalls: 4, tokens: 800, stopReason: '已保存部分调查结果' };
const content = { kind: 'transfer', quantity: '400', covered: '400', remainingShortage: '0', totalCost: '800', currency: 'CNY', availableAt: '2026-10-06T00:00:00Z', validUntil: '2026-10-07T00:00:00Z', feasible: true, reasons: [], sourceEvidence: [], assumptions: [], actions: [] };

test.beforeEach(async ({ page }) => {
  await page.route('**/api/auth/session', route => route.fulfill({ json: { user: { name: '测试计划员' } } }));
  await page.route('**/api/supply/**', route => route.fulfill({ status: 503, json: { message: '未启用' } }));
  await page.route('**/api/supply/access/me', route => route.fulfill({ json: { account: 'test', workspaceId: 'test', workspaces: [{ id: 'test', name: '测试空间' }], roles: [], capabilities: { view: ['A', 'B'] }, roleCatalog: {}, domains: [] } }));
  await page.route('https://fonts.googleapis.com/**', route => route.abort());
  await page.route('https://fonts.gstatic.com/**', route => route.abort());
});

test('逐条结论、引用、截断和未完成提示在桌面与手机可用', async ({ page }, info) => {
  const report: Report = { ...base, status: 'partial', evidence: [
    { id: 'sql:stock', record: { rows: [{ material: 'P001', quantity: '800' }], truncated: true }, provenance: { truncated: true } },
    { id: 'sql:supplier', record: { feedback: '设备故障' }, provenance: { sourceType: 'supplier_statement' } },
  ], modelSummary: { status: 'partial', summary: '兼容摘要', qualification: '模型调查说明，需人工核对。', evidenceIds: ['sql:stock', 'sql:supplier'],
    claims: [
      { statement: '已读取库存记录', kind: 'observed', evidenceIds: ['sql:stock'], limitation: '结果被截断' },
      { statement: '供应商报告设备故障', kind: 'reported', evidenceIds: ['sql:supplier'], limitation: '尚无独立验证' },
      { statement: '晚交可能与供应商报告的事件有关', kind: 'inferred', evidenceIds: ['sql:supplier'], limitation: '不能确认因果关系' },
    ], unresolvedQuestions: ['实际故障是否影响这笔采购'], nextSteps: ['补充设备维修和发运记录'] } };
  await page.route('**/api/supply/plans/PLAN-report', route => route.fulfill({ json: { id: 'PLAN-report', version: 1, content_hash: 'test', status: 'candidate', content, report } }));
  await page.goto('/approvals/PLAN-report');
  const panel = page.getByRole('region', { name: '调查报告' });
  await expect(panel.getByRole('status')).toContainText('调查未完成');
  for (const name of ['数据直接显示的事实', '外部主体陈述', '推断与局限', '未解决问题', '建议补充的证据与下一步']) {
    await expect(panel.getByRole('heading', { name })).toBeVisible();
  }
  await panel.getByText('证据：sql:stock', { exact: true }).first().click();
  await expect(panel.getByText('仅展示部分结果，不能据此推断全部数据。').first()).toBeVisible();
  await expect(panel.locator('pre').first()).toContainText('800');
  await panel.getByText('证据：sql:supplier', { exact: true }).first().click();
  await expect(panel.getByText('含供应商陈述，相关原因尚需独立核实。').first()).toBeVisible();
  await expect(panel.getByText('兼容摘要', { exact: true })).toHaveCount(0);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: info.outputPath('report-desktop.png'), fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBeTruthy();
  await page.screenshot({ path: info.outputPath('report-mobile.png'), fullPage: true, animations: 'disabled' });
});

test('旧报告仍显示摘要、待核实原因与缺失证据提示', async ({ page }) => {
  const report: Report = { ...base, hypotheses: [{ statement: '旧供应商反馈', evidenceIds: ['missing:id'], qualification: '未经独立验证' }],
    modelSummary: { summary: '旧格式调查说明', evidenceIds: ['missing:id'], qualification: '需人工核对' } };
  await page.route('**/api/supply/plans/PLAN-old', route => route.fulfill({ json: { id: 'PLAN-old', version: 1, content_hash: 'test', status: 'candidate', content, report } }));
  await page.goto('/approvals/PLAN-old');
  const panel = page.getByRole('region', { name: '调查报告' });
  await expect(panel.getByText('旧格式调查说明', { exact: true })).toBeVisible();
  await expect(panel.getByRole('heading', { name: '待核实原因' })).toBeVisible();
  await panel.getByText('证据：missing:id', { exact: true }).first().click();
  await expect(panel.getByText('此历史报告未保存可展开的证据内容，请核对原始记录。').first()).toBeVisible();
});
