import {expect,test} from '@playwright/test';

test.beforeEach(async({page})=>{
  await page.route('**/api/logistics/knowledge?**',r=>r.fulfill({json:{rows:[],total_rows:0}}));
  await page.route('**/api/logistics/approvals/todos?**',r=>r.fulfill({json:{categories:[],rows:[],total_rows:0}}));
  await page.route('**/api/logistics/snapshots/config',r=>r.fulfill({json:{configured:true,batch_id:'batch',city:'Shanghai',region_ids:['5']}}));
  await page.route('**/api/logistics/runs?**',r=>r.fulfill({json:{rows:[],total_rows:0}}));
  await page.route('**/api/logistics/approvals?**',r=>r.fulfill({json:{rows:[],total_rows:0}}));
  await page.route('**/api/logistics/conversations?**',r=>r.fulfill({json:{rows:[]}}));
  await page.route('**/api/auth/session',r=>r.fulfill({json:{user:{name:'测试账号1'}}}));
  await page.route('**/api/logistics/access/me',r=>r.fulfill({json:{account:'tester',version:1,enabled:1,roles:[{role:'investigator',city:'Shanghai',regions:['5']}],roleCatalog:{investigator:'调查员'},regionCatalog:['5','6']}}));
});

test('恢复原聊天布局、草稿和分析范围，不请求旧业务接口',async({page},info)=>{
  const oldRequests:string[]=[];
  page.on('request',r=>{if(/\/api\/(supply|data)\//.test(r.url()))oldRequests.push(r.url());});
  await page.goto('/assistant');
  await expect(page.getByRole('heading',{name:'今天想调查什么？'})).toBeVisible();
  await expect(page.getByRole('heading',{name:'历史对话'})).toBeVisible();
  await page.getByRole('textbox',{name:'调查消息草稿'}).fill('请检查区域5的揽收风险');
  await page.reload();
  await expect(page.getByRole('textbox',{name:'调查消息草稿'})).toHaveValue('请检查区域5的揽收风险');
  await expect(page.getByRole('button',{name:'发送',exact:true})).toBeEnabled();
  await page.getByRole('button',{name:'分析范围',exact:true}).click();
  await expect(page.getByRole('combobox',{name:'分析区域'}).locator('option')).toHaveCount(2);
  await page.getByRole('button',{name:'关闭范围'}).click();
  await expect(page.getByText(/P001|EVT-001|THREAD-001|模拟环境/)).toHaveCount(0);
  await page.screenshot({path:info.outputPath('assistant-restored.png'),fullPage:true,animations:'disabled'});
  await page.setViewportSize({width:390,height:844});
  await expect(page.getByRole('textbox',{name:'调查消息草稿'})).toBeVisible();
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth)).toBe(true);
  await page.screenshot({path:info.outputPath('assistant-mobile.png'),fullPage:true,animations:'disabled'});
  expect(oldRequests).toEqual([]);
});

test('取消工作台和运行导航，旧入口承接到新页面',async({page})=>{
  await page.goto('/workspace');
  await expect(page).toHaveURL(/assistant$/);
  await expect(page.getByRole('heading',{name:'今天想调查什么？'})).toBeVisible();
  await page.goto('/risks');
  await expect(page).toHaveURL(/metrics$/);
  await expect(page.getByRole('table')).toBeVisible();
  await expect(page.getByRole('region',{name:'风险提醒',exact:true})).toBeVisible();
  await expect(page.getByRole('heading',{name:'需要处理',exact:true})).toHaveCount(0);
  await page.goto('/approvals');
  await page.getByRole('textbox',{name:'搜索记录'}).fill('不存在的记录');
  await expect(page.getByRole('table')).toContainText('暂无业务记录');
  await page.goto('/runs');
  await expect(page).toHaveURL(/knowledge$/);
  await page.getByRole('textbox',{name:'搜索知识库'}).fill('不存在的成果');
  await expect(page.getByRole('heading',{name:'暂无匹配成果'})).toBeVisible();
  await expect(page.getByRole('navigation',{name:'物流导航'})).toHaveCount(0);
  const navigation=page.getByRole('complementary',{name:'物流导航'});
  await expect(navigation.getByRole('link',{name:'工作台',exact:true})).toHaveCount(0);
  await expect(navigation.getByRole('link',{name:'运行记录',exact:true})).toHaveCount(0);
  await expect(navigation.getByRole('link',{name:'知识库',exact:true})).toBeVisible();
  await expect(page.getByRole('combobox',{name:'工作空间'})).toHaveCount(0);
  await expect(page.getByRole('combobox',{name:'工厂范围'})).toHaveCount(0);
});

// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
