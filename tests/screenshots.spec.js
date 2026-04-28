// @ts-check
// Captures receipts for the two UI changes (LIVE hides POWER/ENERGY,
// range-gating loosened) plus a default 24H view. Run with:
//   npx playwright test tests/screenshots.spec.js
// PNGs land in screenshots/ in the repo root (gitignored).
const { test } = require('@playwright/test');
const path = require('path');

const OUT_DIR = path.join(__dirname, '..', 'screenshots');

test.describe.configure({ mode: 'serial' });

test('01 default 24H view', async ({ page }) => {
  await page.goto('/');
  await page.waitForSelector('#chartTabs .chart-tab:not([disabled])', { timeout: 15000 });
  // Wait for the chart canvas to render
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(OUT_DIR, '01_default_24h.png'), fullPage: true });
});

test('02 LIVE view hides POWER/ENERGY toggle', async ({ page }) => {
  await page.goto('/');
  await page.waitForSelector('#chartTabs .chart-tab:not([disabled])', { timeout: 15000 });
  await page.locator('#chartTabs .chart-tab[data-range="live"]').click();
  await page.waitForTimeout(500);
  await page.screenshot({ path: path.join(OUT_DIR, '02_live_view_no_toggle.png'), fullPage: true });
});

test('03 30D view (range gating) shows 90D and 1Y disabled', async ({ page }) => {
  await page.goto('/');
  await page.waitForSelector('#chartTabs .chart-tab:not([disabled])', { timeout: 15000 });
  await page.locator('#chartTabs .chart-tab[data-range="30d"]').click();
  await page.waitForTimeout(800);
  await page.screenshot({ path: path.join(OUT_DIR, '03_30d_with_gating.png'), fullPage: true });
});

test('04 chart-controls strip closeup (range tabs)', async ({ page }) => {
  await page.goto('/');
  await page.waitForSelector('#chartTabs .chart-tab:not([disabled])', { timeout: 15000 });
  await page.locator('.chart-controls').first().screenshot({
    path: path.join(OUT_DIR, '04_range_tabs_closeup.png'),
  });
});
