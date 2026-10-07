import { defineConfig } from '@playwright/test';

process.env.NO_PROXY = [process.env.NO_PROXY, '127.0.0.1', 'localhost'].filter(Boolean).join(',');

export default defineConfig({
  testDir: './tests',
  fullyParallel: false,
  timeout: 60000,
  use: { baseURL: 'http://127.0.0.1:3010', browserName: 'chromium', viewport: { width: 1440, height: 1000 } },
  webServer: {
    command: 'node node_modules/vite/bin/vite.js --host 127.0.0.1 --port 3010 --strictPort',
    url: 'http://127.0.0.1:3010',
    reuseExistingServer: true,
    timeout: 120000,
  },
});
