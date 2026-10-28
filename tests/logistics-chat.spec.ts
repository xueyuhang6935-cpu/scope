import { test, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';

test('物流聊天：真实隔离后端、调查后追问、约束、草稿排序、跨账号审批与重开', async ({ browser, request }, info) => {
  test.setTimeout(180000);
  const port = await new Promise<number>(resolve => {
    const server = createServer(); server.listen(0, '127.0.0.1', () => {
      const address = server.address(); const value = typeof address === 'object' && address ? address.port : 0;
      server.close(() => resolve(value));
    });
  });
  const code = `import sys,json,threading,uvicorn
from backend.test_logistics_chat import ChatTests
t=ChatTests(); t.setUp()
def generate(messages,evidence_ids,reserve,record):
    reserve(1000)
    record(200,.01,None)
    data=json.loads(messages[1]['content']); text=data['message']; ctx=data['context']
    action='reply'; params={}; reply='已有证据确认逾期状态，现场原因尚未证实。'
    if '调查区域' in text: action='investigate'; params={'region_ids':['5'],'business_as_of':'2000-07-02 10:00:00'}
    elif '不要联系' in text: action='constraint'; params={'note':text,'constraint_key':'contact_courier','active':'yes'}
    elif '生成方案' in text or '修改方案' in text: action='draft'; params={'strategy':'verify_window','note':text,'task_ids':ctx.get('task_ids',[])}
    elif '第二条' in text: action='draft'; params={'strategy':'verify_window','note':text,'task_ids':list(reversed(ctx.get('task_ids',[])))}
    return {'action':action,'params':params,'reply':reply,'evidence_ids':[]}
t.chat.model.generate=generate
server=uvicorn.Server(uvicorn.Config(t.app,host='127.0.0.1',port=int(sys.argv[1]),log_level='error'))
def stop():
    sys.stdin.readline(); server.should_exit=True
threading.Thread(target=stop,daemon=True).start()
try: server.run()
finally: t.tearDown()
`;
  const child = spawn('.venv/Scripts/python.exe', ['-X', 'utf8', '-c', code, String(port)], { stdio: ['pipe', 'pipe', 'pipe'], windowsHide: true });
  let errors = ''; child.stderr.on('data', chunk => { errors += String(chunk); });
  const endpoint = `http://127.0.0.1:${port}`;
  const context = await browser.newContext({ baseURL: 'http://127.0.0.1:3010' });
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
  try {
    await expect.poll(async () => { try { return (await request.get(endpoint+'/api/health', { timeout: 1000 })).status(); } catch { return 0; } }, { timeout: 60000, message: errors }).toBe(200);
    async function login(account: string) {
      expect((await context.request.post(endpoint+'/api/auth/login', { data: { account, password: 'password123' } })).ok()).toBe(true);
    }
    await login('reader');
    await context.route('**/api/**', async route => {
      const url = new URL(route.request().url());
      if (url.searchParams.get('stream')) return route.abort();
      const response = await route.fetch({ url: endpoint+url.pathname+url.search });
      await route.fulfill({ response });
    });
    const page = await context.newPage(); await page.goto('/assistant');
    await expect(page.getByRole('button', { name: '当前调查与待办', exact: true })).toBeHidden();
    expect(await page.locator('.lg-assistant-layout').evaluate(element => getComputedStyle(element).gridTemplateColumns.split(' ').length)).toBe(3);
    async function send(text: string) {
      await page.getByRole('textbox', { name: '调查消息草稿' }).fill(text);
      await page.getByRole('button', { name: '发送', exact: true }).click();
      await expect(page.getByRole('textbox', { name: '调查消息草稿' })).toHaveValue('');
    }
    await send('调查区域5在2000年7月2日10:00的风险');
    await page.waitForURL(/\/assistant\/LCONV-/);
    const chatPath = new URL(page.url()).pathname;
    await expect(page.getByRole('region', { name: '调查报告卡片' })).toBeVisible();
    await send('把刚才的报告解释得简单一点');
    await expect(page.getByText('已有证据确认逾期状态，现场原因尚未证实。', { exact: true })).toBeVisible();
    await send('暂时不要联系快递员');
    await expect(page.getByText('约束已保存', { exact: false })).toBeVisible();
    await send('按当前方向生成方案');
    const cards = page.getByRole('region', { name: '方案草稿卡片' });
    await expect(cards).toHaveCount(1);
    const original = await cards.first().locator('li').allTextContents();
    expect(original.length).toBe(2);
    await send('先处理第二条');
    await expect(cards).toHaveCount(2);
    await expect(cards.last().locator('li').first()).toHaveText(original[1]);
    const before = await context.request.get(endpoint+'/api/logistics/approvals');
    expect((await before.json()).rows).toHaveLength(0);
    await cards.last().getByRole('button', { name: '提交当前草稿审批' }).click();
    await expect(page.getByText('方案 v1 已提交，等待另一账号审核。', { exact: true })).toBeVisible();
    await login('reviewer'); await page.goto('/approvals');
    await page.getByRole('button', { name: '核对建议与证据' }).first().click();
    const panel = page.getByRole('region', { name: '审批详情' });
    await panel.getByText('人工反馈与分析约束', { exact: true }).click();
    await expect(panel).toContainText('暂时不要联系快递员');
    await panel.getByLabel('审核理由').fill('请完善窗口核查说明');
    await panel.getByLabel('已核对提交版本 v1 及相关证据').check();
    await panel.getByRole('button', { name: '驳回当前建议' }).click();
    await expect(panel).toContainText('已驳回');
    await login('reader'); await page.goto(chatPath);
    await expect(page.getByText('请完善窗口核查说明', { exact: false }).first()).toBeVisible();
    await send('按审核意见修改方案，完善窗口核查说明');
    await expect(cards).toHaveCount(3);
    await cards.last().getByRole('button', { name: '提交当前草稿审批' }).click();
    await expect(page.getByText('方案 v2 已提交，等待另一账号审核。', { exact: true })).toBeVisible();
    await login('reviewer'); await page.goto('/approvals');
    await page.getByRole('button', { name: '核对建议与证据' }).first().click();
    await panel.getByLabel('审核理由').fill('范围与证据已核对');
    await panel.getByLabel('已核对提交版本 v2 及相关证据').check();
    await panel.getByRole('button', { name: '批准当前建议' }).click();
    await expect(panel.getByText('已批准，尚未创建跟进单', { exact: false })).toBeVisible();
    await page.screenshot({ path: info.outputPath('approval-desktop.png'), fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 844 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('approval-mobile.png'), fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 1440, height: 1000 });
    await login('reader'); await page.goto(chatPath);
    await expect(page.getByText('已批准，尚未执行', { exact: false })).toBeVisible();
    await page.reload(); await expect(page.getByRole('region', { name: '调查报告卡片' })).toBeVisible();
    await cards.last().scrollIntoViewIfNeeded();
    await page.screenshot({ path: info.outputPath('chat-desktop.png'), fullPage: true, animations: 'disabled' });
    await page.setViewportSize({ width: 390, height: 844 });
    await cards.last().scrollIntoViewIfNeeded();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: info.outputPath('chat-mobile.png'), fullPage: true, animations: 'disabled' });
    await page.getByRole('button', { name: '当前调查与待办', exact: true }).click();
    await expect(page.getByRole('region', { name: '调查处理' })).toBeVisible();
    await page.screenshot({ path: info.outputPath('current-investigation-mobile.png'), fullPage: true, animations: 'disabled' });
  } finally {
    await context.unrouteAll({ behavior: 'ignoreErrors' });
    await context.close(); child.stdin.end('\n');
    await new Promise<void>(resolve => {
      const timer = setTimeout(() => { child.kill(); resolve(); }, 10000);
      child.once('exit', () => { clearTimeout(timer); resolve(); });
    });
  }
});
// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
