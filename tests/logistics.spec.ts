import { expect, test } from '@playwright/test';

const roleCatalog = { investigator: '调查员', approver: '审核人', data_admin: '数据管理员', system_admin: '系统管理员' };
const grant = (role: string) => ({ role, city: 'Shanghai', regions: role === 'system_admin' ? [] : ['5'] });

test.beforeEach(async ({ page }) => {
  await page.route('**/api/logistics/snapshots/config', r => r.fulfill({ json: { configured: false, region_ids: [] } }));
  await page.route('**/api/logistics/conversations?**', r => r.fulfill({ json: { rows: [] } }));
  await page.route('**/api/logistics/approvals/todos?**', r => r.fulfill({ json: { categories: [], rows: [], total_rows: 0 } }));
});

test('统一空间、区域筛选与撤权清除数据', async ({ page }) => {
  let roles = [grant('data_admin'), grant('investigator')], version = 1;
  await page.route('**/api/auth/session', r => r.fulfill({ json: { user: { name: '测试账号1' } } }));
  await page.route('**/api/logistics/access/me', r => r.fulfill({ json: { account: 'tester', workspaceId: 'shanghai-logistics', enabled: 1, version, roles, roleCatalog, regionCatalog: ['5','6'] } }));
  await page.route('**/api/logistics/data/connection', r => r.fulfill({ json: { batchId: 'batch', batches: [{batch_id:'batch'}], regions:['5'], configured: true } }));
  await page.route('**/api/logistics/data/preview?**', r => r.fulfill({ json: { rows:[{order_id:'TASK-REAL',region_id:'5'}],columns:['order_id','region_id'],total:1,page:1,pageSize:20,regions:['5'] } }));
  await page.goto('/assistant');
  await expect(page.getByRole('heading', { name:'智能助手' })).toBeVisible();
  await expect(page.getByRole('combobox', { name:'运营区域', exact:true })).toBeVisible();
  await expect(page.getByRole('combobox', { name:'工作空间', exact:true })).toHaveCount(0);
  await expect(page.getByText(/normal|supplier_delay|bom_revision|atomic_failure/)).toHaveCount(0);
  await page.getByRole('button', { name:'数据中心' }).click();
  await page.getByRole('link', { name:'数据接入', exact:true }).click();
  await expect(page.getByText('TASK-REAL')).toBeVisible();
  await expect(page.getByRole('combobox',{name:'区域',exact:true}).locator('option')).toHaveCount(2);
  await expect(page.getByRole('button',{name:'保存批次选择'})).toBeDisabled();
  roles=[]; version++;
  await expect(page.getByRole('heading',{name:'需要数据管理员权限'})).toBeVisible({timeout:10000});
  await expect(page.getByText('TASK-REAL')).toHaveCount(0);
});

for (const role of ['system_admin','approver','investigator','pending']) {
  test(`${role} 的角色入口和数据隔离`, async ({ page }, info) => {
    await page.route('**/api/auth/session', r => r.fulfill({ json:{user:{name:'测试用户'}} }));
    await page.route('**/api/logistics/access/me', r => r.fulfill({json:{account:'tester',enabled:1,version:1,roles:role==='pending'?[]:[grant(role)],roleCatalog,regionCatalog:['5','6']}}));
    await page.route('**/api/logistics/access/members', r => r.fulfill({json:[]}));
    await page.route('**/api/logistics/access/audit', r => r.fulfill({json:[]}));
    await page.goto('/assistant');
    await expect(page.getByRole('heading',{name:'智能助手'})).toBeVisible();
    await expect(page.getByRole('link',{name:'系统设置',exact:true})).toHaveCount(role==='system_admin'?1:0);
    await page.getByRole('button',{name:'数据中心'}).click();
    await page.getByRole('link',{name:'数据接入',exact:true}).click();
    await expect(page.getByRole('heading',{name:'需要数据管理员权限'})).toBeVisible();
    await page.setViewportSize({width:390,height:844});
    await page.getByRole('button',{name:'打开导航',exact:true}).click();
    await page.keyboard.press('Escape');
    await expect(page.getByRole('button',{name:'打开导航',exact:true})).toBeFocused();
    expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
    if (role === 'system_admin') await page.screenshot({path:info.outputPath('logistics-mobile.png'),fullPage:true,animations:'disabled'});
  });
}

test('权限服务故障时不展示缓存业务页面', async ({page}) => {
  await page.route('**/api/auth/session',r=>r.fulfill({json:{user:{name:'tester'}}}));
  await page.route('**/api/logistics/access/me',r=>r.fulfill({status:503,json:{message:'权限服务暂不可用'}}));
  await page.goto('/assistant');
  await expect(page.getByRole('alert')).toContainText('权限服务暂不可用');
  await expect(page.getByRole('heading',{name:'智能助手'})).toHaveCount(0);
});
// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});

