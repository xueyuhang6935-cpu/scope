import { test, expect } from '@playwright/test';

test('聊天显示实际调查进度、方案比较并在明确确认后提交版本化审批', async ({ page }, testInfo) => {
  const plan = { intent: 'readiness', factory: 'A', material: 'P001', supplier: '', period: '未来三天' };
  const content = { kind: 'transfer', quantity: '400', covered: '400', remainingShortage: '0', totalCost: '800', currency: 'CNY', availableAt: '2026-10-06T00:00:00Z', validUntil: '2026-10-07T00:00:00Z', feasible: true, reasons: [], sourceEvidence: ['stock:1'], assumptions: ['试算不预占库存'], actions: [{ kind: 'transfer', factory: 'A', fromFactory: 'B', material: 'P001', quantity: '400', unit: '件', availableAt: '2026-10-06T00:00:00Z', cost: '800' }] };
  const candidate = { id: 'PLAN-chat', version: 3, content_hash: 'checked-hash', status: 'candidate', content };
  const task = { id: 'TASK-investigate', thread_id: 'CHAT-actions', text: '继续查为什么晚交', status: 'completed', plan, result: { summary: '已发起调查并试算', scope: plan, capturedAt: '2026-10-05T00:00:00Z', ruleVersion: 'test', records: [], missing: [], limitations: [], recommendations: [], workflow: { riskId: 'RISK-chat', runId: 'INV-chat', basedOnTaskId: 'TASK-base' } }, planning: { action: 'investigate' } };
  const thread = { id: 'CHAT-actions', title: '聊天调查测试', factory: 'A', created_at: '2026-10-05T00:00:00Z', tasks: [task] };
  let ready = false;
  let decision: Record<string, unknown> | undefined;
  await page.route('**/api/supply/capabilities', route => route.fulfill({ json: { available: true, intents: {}, parser: 'rules', limitations: [] } }));
  await page.route('**/api/supply/threads?*', route => route.fulfill({ json: [thread] }));
  await page.route('**/api/supply/threads/CHAT-actions', route => route.fulfill({ json: thread }));
  await page.route('**/api/supply/investigations/INV-chat', route => {
    return route.fulfill({ json: { id: 'INV-chat', status: ready ? 'awaiting_approval' : 'running', stage: ready ? 'approval' : 'collect', plans: ready ? [candidate] : [] } });
  });
  await page.route('**/api/supply/plans/PLAN-chat', route => route.fulfill({ json: { ...candidate, canApprove: true, requestedBy: '另一位计划员' } }));
  await page.route('**/api/supply/tasks/TASK-investigate/select-plan', route => route.fulfill({ json: task }));
  await page.route('**/api/supply/plans/PLAN-chat/decision', route => {
    decision = route.request().postDataJSON();
    return route.fulfill({ json: { id: 'APR-chat', decision: 'approved' } });
  });
  await page.goto('/assistant/CHAT-actions?factory=A');
  await expect(page.getByLabel('聊天调查进度')).toContainText('读取场景快照并试算候选方案');
  ready = true;
  await expect(page.getByLabel('聊天调查进度')).toContainText('等待人工审批');
  await expect(page.getByRole('table', { name: '处置方案比较' })).toContainText('800 CNY');
  expect(decision).toBeUndefined();
  await page.getByRole('button', { name: '核对并审批' }).click();
  await expect(page.getByRole('button', { name: '批准方案', exact: true })).toBeDisabled();
  await expect(page.getByText('方案摘要：checked-hash')).toBeVisible();
  await page.getByLabel('审批意见').scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('chat-approval.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel('审批意见').scrollIntoViewIfNeeded();
  await expect(page.getByLabel('审批意见')).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBeTruthy();
  await page.screenshot({ path: testInfo.outputPath('chat-approval-mobile.png'), fullPage: true });
  await page.getByLabel('审批意见').fill('已核对版本、数量和费用');
  expect(decision).toBeUndefined();
  await page.getByRole('button', { name: '批准方案', exact: true }).click();
  await expect.poll(() => decision).toEqual({ version: 3, contentHash: 'checked-hash', decision: 'approved', comment: '已核对版本、数量和费用' });
});

test('聊天多风险选择传递完整对象身份，不显示普通查询确认', async ({ page }) => {
  const plan = { intent: 'readiness', factory: 'all', material: '', supplier: '', period: '未来三天' };
  const task = { id: 'TASK-select', thread_id: 'CHAT-select', text: '继续调查', status: 'awaiting_confirmation', plan, result: null, planning: { action: 'investigate', questions: ['请选择风险'], riskOptions: [{ factory: 'A', material: 'P001', revision: 'R1', shortage: '400' }, { factory: 'B', material: 'P001', revision: 'R2', shortage: '200' }] } };
  const thread = { id: 'CHAT-select', title: '风险选择测试', factory: 'all', tasks: [task] };
  let selected: unknown;
  await page.route('**/api/supply/capabilities', route => route.fulfill({ json: { available: true } }));
  await page.route('**/api/supply/threads?*', route => route.fulfill({ json: [thread] }));
  await page.route('**/api/supply/threads/CHAT-select', route => route.fulfill({ json: thread }));
  await page.route('**/api/supply/tasks/TASK-select/select-risk', route => { selected = route.request().postDataJSON(); return route.fulfill({ json: { ...task, status: 'completed' } }); });
  await page.goto('/assistant/CHAT-select');
  await expect(page.getByLabel('确认分析类型')).toHaveCount(0);
  await page.getByRole('button', { name: 'B 厂 · P001 · R2 · 缺口 200' }).click();
  await expect.poll(() => selected).toEqual({ factory: 'B', material: 'P001', revision: 'R2', shortage: '200' });
});

test('追问文字回答显示来源且不展示查询确认表单', async ({ page }) => {
  const plan = { intent: 'inventory', factory: 'A', material: 'P001', supplier: '', period: '' };
  const thread = { id: 'CHAT-followup', title: '追问测试', factory: 'A', created_at: '2026-10-05T00:00:00Z', tasks: [{ id: 'TASK-answer', thread_id: 'CHAT-followup', text: '该怎么解决', status: 'completed', plan, result: { answer: '建议核对来源批次。\n\n尚未重新查询。', basedOnTaskId: 'TASK-original' }, planning: { execution: 'answer' } }] };
  await page.route('**/api/supply/capabilities', route => route.fulfill({ json: { available: true, intents: {}, parser: 'rules', limitations: [] } }));
  await page.route('**/api/supply/threads?*', route => route.fulfill({ json: [thread] }));
  await page.route('**/api/supply/threads/CHAT-followup', route => route.fulfill({ json: thread }));
  await page.goto('/assistant/CHAT-followup?factory=A');
  await expect(page.getByLabel('真实分析回答')).toContainText('建议核对来源批次。');
  await expect(page.getByRole('link', { name: '查看依据的分析结果' })).toHaveAttribute('href', '#TASK-original');
  await expect(page.getByLabel('确认分析类型')).toHaveCount(0);
});

test.beforeEach(async ({ context }) => {
  await context.route('**/api/supply/**', route => route.fulfill({ status: 503, json: { message: '测试服务未启用' } }));
  await context.route('https://fonts.googleapis.com/**', route => route.abort());
  await context.route('https://fonts.gstatic.com/**', route => route.abort());
  await context.route('**/api/auth/session', route => route.fulfill({ json: { user: { name: '测试计划员' } } }));
});

test('示例填草稿、新建确认、范围恢复与工厂隔离', async ({ page }) => {
  await page.goto('/assistant?factory=A');
  const draft = page.getByLabel('供应链消息草稿');
  await page.getByRole('button', { name: '检查未来三天缺料风险' }).click();
  await expect(draft).toHaveValue('检查未来三天缺料风险');
  await expect(draft).toBeFocused();
  await page.getByRole('button', { name: '当前分析范围', exact: true }).click();
  await page.getByLabel('分析供应商').fill('供应商甲');
  await page.getByLabel('分析时间范围').selectOption('未来三天');
  await page.reload();
  await expect(page.getByLabel('分析供应商')).toHaveValue('供应商甲');
  page.once('dialog', dialog => dialog.dismiss());
  await page.getByRole('button', { name: '新建对话' }).click();
  await expect(draft).toHaveValue('检查未来三天缺料风险');
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: '新建对话' }).click();
  await expect(draft).toHaveValue('');
  await expect(draft).toBeFocused();
  await expect(page.getByLabel('分析供应商')).toHaveValue('');
  await page.reload();
  await expect(draft).toHaveValue('');
  await page.getByRole('link', { name: '查看示例分析' }).click();
  await expect(page.locator('.sp-conversation-link')).toHaveAttribute('aria-current', 'page');
  await expect(page.locator('.sp-conversation-link')).toHaveCSS('background-color', 'rgb(243, 232, 255)');
  await page.getByLabel('工厂范围').selectOption('B');
  await expect(page).toHaveURL(/\/assistant\?factory=B$/);
  await expect(page.getByText('当前工厂暂无对话')).toBeVisible();
  await expect(page.locator('.sp-conversation-link')).toHaveCount(0);
});

test('保存失败明确提示，示例状态和固定依据可读', async ({ page }) => {
  await page.goto('/assistant');
  await page.evaluate(() => { Storage.prototype.setItem = () => { throw new Error('storage unavailable'); }; });
  await page.getByLabel('供应链消息草稿').fill('不能丢失的问题');
  await expect(page.getByText('保存失败，请复制草稿后再离开。')).toBeVisible();
  await expect(page.getByRole('button', { name: '发送', exact: true })).toBeDisabled();
  await page.goto('/assistant/THREAD-001');
  await page.getByRole('button', { name: '当前分析范围', exact: true }).click();
  await expect(page.getByRole('heading', { name: '当次分析范围' })).toBeVisible();
  for (const [state, text] of [['ready', '问题与范围已就绪'], ['running', '没有后台任务运行'], ['failed', '数据查询服务不可用'], ['empty', '不能据此判断没有风险'], ['complete', '方案需审批']]) {
    await page.getByLabel('示例状态预览').selectOption(state);
    await expect(page.getByLabel('助手示例回答')).toContainText(text);
  }
});

test('桌面和手机输入可见，历史范围可折叠', async ({ page }, testInfo) => {
  await page.goto('/assistant/THREAD-001');
  await expect(page.getByLabel('供应链消息草稿')).toBeInViewport();
  await expect(page.getByRole('heading', { name: 'P001 缺料调查', exact: true })).toBeInViewport();
  await page.screenshot({ path: testInfo.outputPath('assistant-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/assistant');
  await expect(page.getByLabel('供应链消息草稿')).toBeInViewport();
  await expect(page.locator('#assistant-history')).not.toBeVisible();
  await page.getByRole('button', { name: '历史对话', exact: true }).click();
  await expect(page.locator('.sp-conversation-link')).toBeVisible();
  await page.getByRole('button', { name: '历史对话', exact: true }).click();
  await page.getByRole('button', { name: '当前分析范围', exact: true }).click();
  await expect(page.getByLabel('分析供应商')).toBeVisible();
  await page.getByRole('button', { name: '当前分析范围', exact: true }).click();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('assistant-mobile.png'), fullPage: true });
});

test('参考布局的欢迎卡片与分析范围弹层', async ({ page }, testInfo) => {
  await page.route('**/api/supply/capabilities', route => route.fulfill({ json: { available: true, intents: {}, parser: 'rules', limitations: [] } }));
  await page.route('**/api/supply/threads?*', route => route.fulfill({ json: [] }));
  await page.goto('/assistant');
  await expect(page.getByRole('heading', { name: '今天想调查什么？' })).toBeVisible();
  await expect(page.locator('.sp-prompt-card')).toHaveCount(3);
  await expect(page.locator('.sp-scope-popover')).not.toBeVisible();
  await page.getByRole('button', { name: '当前分析范围', exact: true }).click();
  await page.getByLabel('分析时间范围', { exact: true }).selectOption('未来三天');
  await page.keyboard.press('Escape');
  await expect(page.locator('.sp-scope-popover')).not.toBeVisible();
  await expect(page.getByRole('button', { name: '当前分析范围', exact: true })).toBeFocused();
  await expect(page.getByLabel('输入区时间范围')).toHaveValue('未来三天');
  await page.getByLabel('输入区时间范围').selectOption('');
  await expect(page.getByText('分析服务已连接 · 缺料计算使用已接入场景')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('welcome-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 390, height: 844 });
  await expect.poll(async () => page.locator('.sp-sidebar').evaluate(el => el.getBoundingClientRect().right)).toBeLessThanOrEqual(0);
  await expect(page.getByLabel('供应链消息草稿')).toBeInViewport();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('welcome-mobile.png'), fullPage: true });
});
