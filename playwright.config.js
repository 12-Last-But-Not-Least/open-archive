import {defineConfig, devices} from '@playwright/test';
import path from 'node:path';

export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: true,
  forbidOnly: Boolean(process.env.CI),
  retries: process.env.CI ? 1 : 0,
  workers: 2,
  globalTimeout: 10 * 60 * 1000,
  reporter: [['list'], ['html', {open: 'never'}]],
  use: {
    baseURL: 'http://127.0.0.1:8000',
    locale: 'en-US', timezoneId: 'UTC', colorScheme: 'light',
    reducedMotion: 'reduce', serviceWorkers: 'block',
    trace: 'retain-on-failure', screenshot: 'only-on-failure',
  },
  projects: [
    {name: 'desktop', use: {...devices['Desktop Chrome'], viewport: {width: 1440, height: 1000}}},
    {name: 'mobile', use: {...devices['Pixel 7']}},
  ],
  webServer: {
    command: 'python scripts/dev.py',
    cwd: process.env.PLAYWRIGHT_APP_DIR || '.',
    url: 'http://127.0.0.1:8000/api/health',
    reuseExistingServer: false,
    timeout: 120000,
    env: {OPEN_ARCHIVE_STATE_DIR: path.resolve('.local/playwright')},
  },
});
