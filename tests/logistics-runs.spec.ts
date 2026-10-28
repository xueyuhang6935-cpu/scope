import { test, expect } from '@playwright/test';
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';

test('旧运行入口承接到知识库，部分结果、证据及权限错误', async ({ page }, info) => {
  await page.route('**/api/auth/session', r => r.fulfill({ json: { user: { name: '测试账号1' } } }));
  await page.route('**/api/logistics/access/me', r => r.fulfill({ json: { account: 'reader', version: 1, enabled: 1,
    roles: [{ role: 'investigator', city: 'Shanghai', regions: ['5'] }], roleCatalog: {}, regionCatalog: ['5'] } }));
  const entry = { knowledge_id: 'LKNOW-LRUN-browser', run_id: 'LRUN-browser', title: '保存的风险调查', result_kind: 'model', result_status: 'partial',
    archived_at: '2026-10-08T10:00:00Z', abstract: '原因未确认', scope: { snapshot_id: 'SNP-fixed', batch_id: 'batch', city: 'Shanghai', region_ids: ['5'], business_as_of: '2000-07-02 10:00:00' },
    limitations: ['历史样本'], unresolved_questions: ['待核查 AOI 原因'], stop_reason: 'no_progress',
    report: null, model_report: { value: { status: 'partial', claims: [{ statement: '观察到 627 条积压', kind: 'computed', evidence_ids: ['LEV-real'] }], limitations: ['历史样本'], unresolved_questions: ['原因未确认'] } },
    evidence_ids: ['LEV-real'], processing: { phase: 'investigation', proposals: [], followup: null, review: null } };
  let fail = false;
  const writes: string[] = [];
  page.on('request', r => { if (r.method() === 'POST') writes.push(new URL(r.url()).pathname); });
  await page.route('**/api/logistics/knowledge?**', r => r.fulfill({ json: { rows: [entry], total_rows: 1 } }));
  await page.route('**/api/logistics/knowledge/LKNOW-LRUN-browser', r => fail
    ? r.fulfill({ status: 403, json: { message: '当前成果访问权限已失效' } }) : r.fulfill({ json: entry }));
  await page.route('**/api/logistics/runs/LRUN-browser/destination', r => r.fulfill({ json: { path: '/knowledge/LKNOW-LRUN-browser' } }));
  await page.route('**/api/logistics/runs/LRUN-browser/evidence/LEV-real', r => r.fulfill({ json: { record: { data: { metrics: { backlog: { name: '样本内积压', value: 627 } } }, rows: [], limitations: ['历史样本'] } } }));
  await page.goto('/runs');
  await expect(page).toHaveURL(/knowledge$/);
  await expect(page.getByRole('article')).toContainText('部分结果');
  await page.goto('/runs/LRUN-browser');
  await expect(page).toHaveURL(/knowledge\/LKNOW-LRUN-browser$/);
  await expect(page.getByRole('heading', { name: '调查尚未完整完成' })).toBeVisible();
  await expect(page.getByText('停止原因：连续两次没有新的有效观察')).toBeVisible();
  await expect(page.getByText('待核查 AOI 原因').first()).toBeVisible();
  await page.locator('details summary').first().click();
  await expect(page.locator('details').first()).toContainText('样本内积压：627');
  await expect(page.getByRole('link', { name: '下载成果' })).toHaveAttribute('href', /knowledge\/LKNOW-LRUN-browser\/download$/);
  await expect(page.getByRole('heading', { name: '持久化事件' })).toHaveCount(0);
  await page.reload();
  await expect(page.getByRole('heading', { name: '成果与来源' })).toBeVisible();
  await page.screenshot({ path: info.outputPath('knowledge-desktop.png'), fullPage: true, animations: 'disabled' });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: info.outputPath('knowledge-mobile.png'), fullPage: true, animations: 'disabled' });
  expect(writes).toEqual([]);
  fail = true; await page.reload();
  await expect(page.getByRole('alert')).toContainText('权限已失效');
  await expect(page.getByRole('heading', { name: '成果与来源' })).toHaveCount(0);
  await expect(page.getByText('观察到 627 条积压')).toHaveCount(0);
});

test('G2：真实后端接口创建，关闭页面后重开保存的报告与证据',async({browser,request},info)=>{
 test.setTimeout(90000);
 const port=await new Promise<number>(resolve=>{const server=createServer();server.listen(0,'127.0.0.1',()=>{const address=server.address();const value=typeof address==='object'&&address?address.port:0;server.close(()=>resolve(value));});});
 const code=`import sys,threading,uvicorn
from backend.test_logistics_events import EventsTests
t=EventsTests(); t.setUp()
server=uvicorn.Server(uvicorn.Config(t.app,host='127.0.0.1',port=int(sys.argv[1]),log_level='error'))
def stop():
    sys.stdin.readline()
    server.should_exit=True
threading.Thread(target=stop,daemon=True).start()
try: server.run()
finally: t.tearDown()
`;
 const child=spawn('.venv/Scripts/python.exe',['-X','utf8','-c',code,String(port)],{stdio:['pipe','pipe','pipe'],windowsHide:true});
 const endpoint=`http://127.0.0.1:${port}`;
 let errors='';child.stderr.on('data',chunk=>{errors+=String(chunk);});
 const context=await browser.newContext({baseURL:'http://127.0.0.1:3010'});
 await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
 try {
  await expect.poll(async()=>{try{return (await request.get(endpoint+'/api/health',{timeout:1000})).status();}catch{return 0;}},{timeout:30000,message:errors}).toBe(200);
  const login=await request.post(endpoint+'/api/auth/login',{data:{account:'reader',password:'password123'}});
  expect(login.ok()).toBe(true);
  const cookies=(await request.storageState()).cookies;
  await context.addCookies(cookies);
  // 转发浏览器请求到真实隔离后端；SSE由独立HTTP专项验收，页面此处使用真实持久化轮询。
  await context.route('**/api/**',async route=>{
   const url=new URL(route.request().url());
   if(url.searchParams.get('stream'))return route.abort();
   const response=await route.fetch({url:endpoint+url.pathname+url.search});
   await route.fulfill({response});
  });
  const page=await context.newPage();await page.goto('/metrics');
  const snapshotReady=page.waitForResponse(response=>new URL(response.url()).pathname==='/api/logistics/snapshots'&&response.request().method()==='POST');
  await page.getByRole('button',{name:'创建回放快照',exact:true}).click();
  await expect(page.getByText('当前结果：',{exact:false})).toBeVisible();
  const snapshot=await snapshotReady;
  expect(snapshot.ok()).toBe(true);
  const {snapshot_id}=await snapshot.json();
  const created=await request.post(endpoint+'/api/logistics/runs',{data:{snapshot_id,idempotency_key:'g2-browser-api'}});
  expect(created.ok()).toBe(true);
  const {run_id}=await created.json();
  const path=`/runs/${run_id}`;
  await page.close();
  // 浏览器无页面、无订阅时由后端执行；完成后新页面读取保存结果。
  await expect.poll(async()=>{const response=await request.get(endpoint+'/api/logistics'+path);return (await response.json()).status;},{timeout:15000}).toBe('completed');
  const reopened=await context.newPage();await reopened.goto(path);
  await expect(reopened).toHaveURL(new RegExp('/knowledge/LKNOW-'+run_id+'$'));
  await expect(reopened.getByRole('region',{name:'调查报告卡片'})).toBeVisible();
  await reopened.getByRole('region',{name:'调查报告卡片'}).locator('details summary').first().click();
  await expect(reopened.getByRole('region',{name:'调查报告卡片'}).locator('details').first()).toContainText('任务编号');
  const download = await context.request.get(endpoint+'/api/logistics/knowledge/LKNOW-'+run_id+'/download');
  expect(download.ok()).toBe(true);
  expect(await download.text()).toContain('2000-07-02 10:00:00');
  const before = await (await request.get(endpoint+'/api/logistics/runs')).json();
  await reopened.getByRole('button',{name:'引用到新对话',exact:true}).click();
  await expect(reopened).toHaveURL(/\/assistant\/LCONV-/);
  await expect(reopened.getByRole('region',{name:'历史成果引用'})).toContainText('原参考时点 2000-07-02 10:00:00');
  const after = await (await request.get(endpoint+'/api/logistics/runs')).json();
  expect(after.total_rows).toBe(before.total_rows);
  await reopened.evaluate(()=>{(document.activeElement as HTMLElement)?.blur();window.scrollTo(0,0);});
  await reopened.screenshot({path:info.outputPath('g2-real-reopened.png'),fullPage:true,animations:'disabled'});
 } finally {
  await context.close();child.stdin.end('\n');
  await new Promise<void>(resolve=>{const timer=setTimeout(()=>{child.kill();resolve();},10000);child.once('exit',()=>{clearTimeout(timer);resolve();});});
 }
});
// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
