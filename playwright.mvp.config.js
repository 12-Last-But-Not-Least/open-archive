import {defineConfig, devices} from '@playwright/test';

export default defineConfig({
  testDir: './tests/e2e',
  workers: 1,
  retries: 0,
  timeout: 60000,
  forbidOnly: Boolean(process.env.CI),
  outputDir: 'test-results/mvp',
  reporter: [['list'], ['html', {outputFolder: 'playwright-report-mvp', open: 'never'}]],
  use: {
    baseURL: 'http://127.0.0.1:8001', actionTimeout: 10000,
    trace: 'retain-on-failure', screenshot: 'only-on-failure',
    serviceWorkers: 'block',
  },
  projects: [
    {name: 'desktop', use: {...devices['Desktop Chrome']}},
    {name: 'mobile', use: {...devices['Pixel 7']}},
  ],
  webServer: {
    command: 'python tests/e2e/server.py',
    url: 'http://127.0.0.1:8001/api/health',
    reuseExistingServer: false,
    timeout: 60000,
  },
});
