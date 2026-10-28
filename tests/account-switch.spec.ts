import {expect,test} from '@playwright/test';

test('头像菜单切换会话、确认草稿、重新加载权限并保留账号设置',async({page})=>{
  let account='admin@scope.test',switches=0;
  await page.route('**/api/auth/session',r=>r.fulfill({json:{user:{name:account,account}}}));
  await page.route('**/api/logistics/conversations?**',r=>r.fulfill({json:{rows:[]}}));
  await page.route('**/api/logistics/approvals/todos?**',r=>r.fulfill({json:{categories:[],rows:[],total_rows:0}}));
  await page.route('**/api/logistics/snapshots/config',r=>r.fulfill({json:{configured:false,region_ids:[]}}));
  await page.route('**/api/logistics/access/me',r=>r.fulfill({json:{account,version:1,enabled:1,roles:[{role:account==='admin@scope.test'?'system_admin':'approver',city:'Shanghai',regions:account==='admin@scope.test'?[]:['5']}],roleCatalog:{system_admin:'系统管理员',approver:'审核人'},regionCatalog:['5']}}));
  await page.route('**/api/logistics/access/test-accounts',r=>r.fulfill({json:{enabled:true,accounts:[{account:'reviewer@scope.test',name:'测试账号2',roles:['审核人']}]}}));
  await page.route('**/api/logistics/access/switch-test-account',r=>{switches++;account=r.request().postDataJSON().account;return r.fulfill({json:{user:{account,name:account}}});});
  await page.goto('/assistant');
  await page.getByRole('textbox',{name:'调查消息草稿'}).fill('保留原账号草稿');
  await page.getByRole('button',{name:'账号菜单'}).click();
  await expect(page.getByRole('region',{name:'账号切换'})).toContainText('系统管理员');
  page.once('dialog',d=>d.dismiss());
  await page.getByRole('button',{name:/切换到测试账号2/}).click();
  expect(switches).toBe(0);
  page.once('dialog',d=>d.accept());
  await page.getByRole('button',{name:/切换到测试账号2/}).click();
  await expect(page).toHaveURL(/\/assistant$/);
  await expect(page.getByRole('link',{name:'系统设置',exact:true})).toHaveCount(0);
  await page.getByRole('button',{name:'账号菜单'}).click();
  await expect(page.getByRole('region',{name:'账号切换'})).toContainText('reviewer@scope.test');
  await expect(page.getByRole('button',{name:/当前：测试账号2/})).toBeDisabled();
  await expect(page.getByRole('button',{name:'管理员登录（需密码）'})).toBeVisible();
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button',{name:'账号菜单'})).toBeFocused();
  await page.getByRole('button',{name:'账号菜单'}).click();
  await page.getByRole('region',{name:'账号切换'}).getByRole('link',{name:'账号设置'}).click();
  await expect(page).toHaveURL(/\/account$/);
  expect(switches).toBe(1);
});

test('非本地环境仅显示账号设置和退出',async({page})=>{
  await page.route('**/api/auth/session',r=>r.fulfill({json:{user:{name:'用户'}}}));
  await page.route('**/api/logistics/access/me',r=>r.fulfill({json:{account:'user',version:1,enabled:1,roles:[],roleCatalog:{},regionCatalog:[]}}));
  await page.route('**/api/logistics/access/test-accounts',r=>r.fulfill({json:{enabled:false,accounts:[]}}));
  await page.goto('/workspace');await page.getByRole('button',{name:'账号菜单'}).click();
  await expect(page.getByRole('region',{name:'账号切换'}).getByRole('link',{name:'账号设置'})).toBeVisible();
  await expect(page.getByRole('button',{name:/切换到测试/})).toHaveCount(0);
});
// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
