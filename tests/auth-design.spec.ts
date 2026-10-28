import { expect, test } from '@playwright/test';

test.beforeEach(async ({ context }) => {
  await context.route('**/api/auth/session', route => route.fulfill({ status: 401, json: { message: '未登录' } }));
});

test('默认显示登录，注册在右侧原位切换', async ({ page }, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.goto('/login-select');
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('.scope-choice-card')).toHaveCount(0);
  await expect(page.locator('.scope-header-switch')).toHaveCount(0);
  await expect(page.getByRole('button', { name: '使用微信登录' })).toHaveCount(0);
  await expect(page.locator('.scope-input-with-icon > svg')).toHaveCount(2);
  await expect(page.locator('.scope-panel')).toHaveCSS('border-top-width', '0px');
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight)).toBe(true);
  expect(await page.locator('.scope-network-story img').evaluate((image: HTMLImageElement) => image.naturalWidth)).toBeGreaterThan(0);
  await page.screenshot({ path: testInfo.outputPath('login-inline-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 720 });
  expect(await page.evaluate(() => document.documentElement.scrollHeight <= innerHeight)).toBe(true);
  await page.setViewportSize({ width: 4096, height: 2368 });
  const wideLayout = await page.evaluate(() => {
    const root = document.querySelector('.scope-auth-network')!;
    const left = document.querySelector('.scope-network-story')!;
    const image = left.querySelector('img')!;
    const form = document.querySelector('.scope-choice-access')!;
    return {
      backgroundWidth: parseFloat(getComputedStyle(root, '::before').width),
      leftWidth: left.getBoundingClientRect().width,
      imageWidth: image.getBoundingClientRect().width,
      formWidth: form.getBoundingClientRect().width,
      overflow: document.documentElement.scrollWidth > innerWidth,
    };
  });
  expect(Math.abs(wideLayout.backgroundWidth - wideLayout.leftWidth)).toBeLessThan(2);
  expect(wideLayout.imageWidth / wideLayout.leftWidth).toBeGreaterThan(0.8);
  expect(wideLayout.formWidth).toBeLessThanOrEqual(970);
  expect(wideLayout.overflow).toBe(false);
  await page.screenshot({ path: testInfo.outputPath('login-wide.png'), fullPage: true });
  await page.setViewportSize({ width: 1024, height: 592 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole('button', { name: '注册账号' }).click();
  await expect(page).toHaveURL(/\/login-select$/);
  await expect(page.getByRole('heading', { name: '创建你的账号' })).toBeVisible();
  await expect(page.locator('.scope-network-story')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('register-inline-desktop.png'), fullPage: true });
  await page.getByRole('button', { name: '返回登录' }).click();
  await expect(page).toHaveURL(/\/login-select$/);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('login-inline-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '注册账号' }).click();
  await expect(page).toHaveURL(/\/login-select$/);
  await expect(page.getByRole('heading', { name: '创建你的账号' })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('register-inline-mobile.png'), fullPage: true });
  await page.getByRole('button', { name: '返回登录' }).click();
  await expect(page).toHaveURL(/\/login-select$/);
  await expect(page.getByRole('heading', { name: '欢迎回来' })).toBeVisible();
  await expect(page.locator('.scope-network-story h1')).toHaveText('看清风险，让每一步决策有依据。');
  await expect(page.locator('.scope-primary')).toHaveCSS('background-color', 'rgb(102, 84, 197)');
  await page.getByLabel('账号', { exact: true }).fill('tester@example.com');
  await page.getByLabel('密码', { exact: true }).fill('password123');
  await page.getByRole('button', { name: '显示密码' }).click();
  await expect(page.locator('#login-password')).toHaveAttribute('type', 'text');
  await page.getByLabel('记住登录账号').check();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

test('账号入口内登录成功进入智能助手', async ({ page, context }) => {
  let signedIn = false;
  await context.route('**/api/auth/session', route => route.fulfill(signedIn ? { json: { user: { name: '测试用户' } } } : { status: 401, json: { message: '未登录' } }));
  await context.route('**/api/auth/login', route => { signedIn = true; return route.fulfill({ json: { user: { name: '测试用户' } } }); });
  await page.goto('/login-select');
  await page.getByLabel('账号', { exact: true }).fill('tester@example.com');
  await page.getByLabel('密码', { exact: true }).fill('password123');
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await expect(page).toHaveURL(/\/assistant$/);
});

test('注册表单限制验证码并要求协议确认', async ({ page }, testInfo) => {
  await page.goto('/register');
  await expect(page.getByRole('button', { name: '使用微信登录' })).toHaveCount(0);
  await expect(page.getByRole('heading', { name: '创建你的账号' })).toBeVisible();
  await expect(page.locator('.scope-auth-intro')).toHaveText('看清风险，让每一步决策有依据。');
  await page.screenshot({ path: testInfo.outputPath('register-desktop.png'), fullPage: true });
  await page.getByLabel('邮箱验证码').fill('a12b34567');
  await expect(page.getByLabel('邮箱验证码')).toHaveValue('123456');
  await page.getByRole('button', { name: '显示密码', exact: true }).click();
  await expect(page.locator('#register-password')).toHaveAttribute('type', 'text');
  await expect(page.getByLabel('我已阅读并同意用户协议与隐私政策')).not.toBeChecked();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.locator('.scope-auth-intro')).toBeHidden();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('register-mobile.png'), fullPage: true });
  await page.goto('/login');
  await expect(page.getByRole('button', { name: '使用微信登录' })).toBeVisible();
});
// 浏览器回归只验证业务流程，外部字体不可用时使用页面原有本地回退。
test.beforeEach(async ({ context }) => {
  await context.route(/^https:\/\/fonts\.(googleapis|gstatic)\.com\//, route => route.abort());
});
