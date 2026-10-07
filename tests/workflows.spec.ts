import { expect, test } from '@playwright/test';

test.beforeEach(async ({ context }) => {
  await context.route('https://fonts.googleapis.com/**', route => route.abort());
  await context.route('https://fonts.gstatic.com/**', route => route.abort());
  await context.route('**/api/auth/session', route => route.fulfill({ json: { user: { name: '流程测试员' } } }));
  await context.route('**/api/data/**', route => route.fulfill({ json: [] }));
});

test('真实文件入口与模拟导入校验、回退、取消和错误重试', async ({ page }, testInfo) => {
  await page.goto('/data/imports/new?factory=B');
  await expect(page.getByLabel('接入方式')).toHaveValue('file');
  await page.getByRole('button', { name: '预览文件' }).click();
  await expect(page.getByRole('alert')).toContainText('请选择文件');
  await expect(page.getByLabel('数据域')).toBeVisible();
  await expect(page.getByLabel('负责人')).toBeVisible();
  await page.getByLabel('接入方式').selectOption('demo');
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('combobox', { name: '库存数量', exact: true }).selectOption('单位');
  await page.getByRole('button', { name: '下一步' }).click();
  await expect(page.getByRole('alert')).toContainText('不同的来源列');
  await page.getByRole('combobox', { name: '库存数量', exact: true }).selectOption('数量');
  await page.getByRole('button', { name: '下一步' }).click();
  await expect(page.getByText('P012 数量尚不可计算', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: '下一步' }).click();
  await expect(page.getByRole('alert')).toContainText('隔离');
  await page.getByLabel('保留原始记录并隔离 P012').check();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '上一步' }).click();
  await expect(page.getByLabel('保留原始记录并隔离 P012')).toBeChecked();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByLabel('模拟响应').selectOption('error');
  await page.getByRole('button', { name: '提交模拟导入' }).click();
  await page.getByRole('button', { name: '取消', exact: true }).click();
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('button', { name: '提交模拟导入' }).click();
  await page.getByRole('button', { name: '确认模拟提交' }).click();
  await expect(page.getByRole('alert')).toContainText('未生成批次');
  await page.getByLabel('模拟响应').selectOption('ok');
  await page.getByRole('button', { name: '提交模拟导入' }).click();
  await page.screenshot({ path: testInfo.outputPath('import-confirm-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: '确认模拟提交' }).click();
  await expect(page.getByRole('heading', { name: '模拟批次 SIM-001' })).toBeVisible();
  await expect(page.getByText('未上传、未导入', { exact: true })).toBeVisible();
});

test('真实文件五步流程显示后端校验并只提交一次', async ({ page }) => {
  let commits = 0;
  await page.route('**/api/data/imports/preview', route => route.fulfill({ json: {
    draftId: 'draft-001', filename: 'inventory.csv', domain: 'inventory', factory: 'B', columns: ['material', 'quantity', 'unit'],
    sample: [{ material: 'P012', quantity: '20', unit: '箱' }], total: 1, sheets: [], sheet: '',
  } }));
  await page.route('**/api/data/imports/draft-001/check', route => route.fulfill({ json: {
    rows: [{ row: 2, material: 'P012', quantity: '20', unit: '箱', normalizedQuantity: null, status: 'isolated' }],
    issues: [{ row: 2, severity: 'warning', rule: 'unit_conversion', originalValue: '20 箱', reason: '缺少箱/件换算关系。', impact: '尚不可计算。' }],
    blocking: 0, isolated: 1,
  } }));
  await page.route('**/api/data/imports/draft-001/commit', route => { commits++; return route.fulfill({ json: {
    id: 'BATCH-REAL-001', filename: 'inventory.csv', domain: 'inventory', factory: 'B', owner: '张工', sheet: '', total: 1, imported: 0, isolated: 1, created_at: '2026-10-02',
  } }); });
  await page.goto('/data/imports/new?factory=B');
  await expect(page.getByLabel('接入方式')).toHaveValue('file');
  await page.getByLabel('负责人').fill('张工');
  await page.getByLabel('选择文件').setInputFiles({ name: 'inventory.csv', mimeType: 'text/csv', buffer: Buffer.from('material,quantity,unit\nP012,20,箱') });
  await page.getByRole('button', { name: '预览文件' }).click();
  await expect(page.getByRole('heading', { name: '预览与映射' })).toBeVisible();
  await page.getByRole('button', { name: '运行校验' }).click();
  await expect(page.getByText('缺少箱/件换算关系。')).toBeVisible();
  await expect(page.getByRole('button', { name: '下一步' })).toBeDisabled();
  await page.getByLabel('保留原始记录并隔离尚不可计算的记录').check();
  await page.getByRole('button', { name: '下一步' }).click();
  await page.getByRole('button', { name: '提交导入' }).click();
  await page.getByRole('button', { name: '确认提交' }).click();
  await expect(page.getByText('批次 BATCH-REAL-001 已保存')).toBeVisible();
  expect(commits).toBe(1);
});

test('P012 负责人确认、换算验证、失败不保存、复检版本与事件联动', async ({ page }, testInfo) => {
  await page.goto('/data/governance/issues/ISS-012?factory=B');
  await page.getByRole('button', { name: '记录模拟确认任务并继续' }).click();
  await expect(page.getByRole('alert')).toContainText('物料负责人');
  await page.getByLabel('物料负责人', { exact: true }).fill('张工');
  await page.getByLabel('确认任务说明').fill('确认 P012 包装规格');
  await page.getByRole('button', { name: '记录模拟确认任务并继续' }).click();
  await page.getByRole('link', { name: '打开标准与映射' }).click();
  await expect(page.getByLabel('物料负责人', { exact: true })).toHaveValue('张工');
  await page.getByLabel('每箱件数').fill('0');
  await page.getByRole('button', { name: '提交修复与模拟复检' }).click();
  await expect(page.getByRole('alert')).toContainText('大于 0');
  await page.getByLabel('每箱件数').fill('24');
  await page.getByRole('button', { name: '提交修复与模拟复检' }).click();
  await expect(page.getByRole('alert')).toContainText('换算依据');
  await page.getByLabel('换算依据', { exact: true }).fill('物料负责人确认的包装规格单 SPEC-P012');
  await page.getByLabel('物料负责人已确认').check();
  await page.getByLabel('模拟响应').selectOption('error');
  await page.getByRole('button', { name: '提交修复与模拟复检' }).click();
  await page.getByRole('button', { name: '确认模拟提交' }).click();
  await expect(page.getByRole('alert')).toContainText('版本未保存');
  expect(await page.evaluate(() => JSON.parse(sessionStorage.getItem('supply-simulation-v1:流程测试员')!).repair.version)).toBe(1);
  await page.getByLabel('模拟响应').selectOption('ok');
  await page.getByRole('button', { name: '提交修复与模拟复检' }).click();
  await page.getByRole('dialog').press('Escape');
  await expect(page.getByRole('dialog')).not.toBeVisible();
  await page.getByRole('button', { name: '提交修复与模拟复检' }).click();
  await page.getByRole('button', { name: '确认模拟提交' }).click();
  await expect(page.getByText('20 箱 × 24 = 480 件')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('repair-result-desktop.png'), fullPage: true });
  await page.getByRole('link', { name: '查看标准化对照' }).click();
  await expect(page.getByText('480 件（模拟）', { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByText('480 件（模拟）', { exact: true })).toBeVisible();
  await page.goto('/risks/EVT-012?tab=evidence&factory=B');
  await expect(page.getByText(/模拟标准化 v2：20 箱 = 480 件/)).toBeVisible();
  await page.goto('/data/governance/issues/ISS-012?factory=A');
  await expect(page.getByRole('heading', { name: '对象不存在或不在当前工厂范围' })).toBeVisible();
});

test('浏览器记录失败不显示模拟成功', async ({ page }) => {
  await page.goto('/approvals/APR-001');
  await page.getByLabel('意见说明').fill('需要复核库存后执行');
  await page.getByLabel('已核对 APR-001').check();
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new Error('QuotaExceededError'); }; });
  await page.getByRole('button', { name: '提交模拟审批' }).click();
  await page.getByRole('button', { name: '确认模拟提交' }).click();
  await expect(page.getByRole('alert')).toContainText('无法保存');
  await expect(page.getByText(/模拟已批准/)).toHaveCount(0);
});

test('审批模拟绑定方案版本，刷新保留且不执行调拨', async ({ page }) => {
  await page.goto('/approvals/APR-001');
  await page.getByRole('button', { name: '提交模拟审批' }).click();
  await expect(page.getByRole('alert')).toBeVisible();
  await page.getByLabel('审批意见').selectOption('驳回');
  await page.getByLabel('意见说明').fill('成本尚未核算');
  await page.getByLabel('已核对 APR-001').check();
  await page.getByRole('button', { name: '提交模拟审批' }).click();
  await page.getByRole('button', { name: '确认模拟提交' }).click();
  await expect(page.getByText('模拟已驳回 · 真实方案仍待审批')).toBeVisible();
  await page.reload();
  await expect(page.getByText('模拟已驳回 · 真实方案仍待审批')).toBeVisible();
  await expect(page.getByText('未触发调拨执行。')).toBeVisible();
});

test('手机表单和确认弹窗不溢出', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/data/governance?tab=mapping&factory=B');
  await page.getByLabel('物料负责人', { exact: true }).fill('王工');
  await page.getByLabel('确认任务说明').fill('核对包装');
  await page.getByLabel('每箱件数').fill('12');
  await page.getByLabel('换算依据', { exact: true }).fill('规格单');
  await page.getByLabel('物料负责人已确认').check();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mapping-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '提交修复与模拟复检' }).click();
  const bounds = await page.getByRole('dialog').boundingBox();
  expect(bounds!.x).toBeGreaterThanOrEqual(0);
  expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(390);
  await page.screenshot({ path: testInfo.outputPath('repair-confirm-mobile.png'), fullPage: true });
});
