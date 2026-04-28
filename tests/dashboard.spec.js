// @ts-check
const { test, expect } = require('@playwright/test');

// All tests run against the mock-mode dashboard brought up by
// `docker-compose.dev.yml`. Fixtures seed 5 days of synthetic history,
// so 7D and 30D should be enabled and 90D / 1Y should be disabled.

test.beforeEach(async ({ page }) => {
  // Surface JS errors as test failures rather than hidden console noise.
  page.on('pageerror', (err) => { throw err; });
  await page.goto('/');
  // Wait for the range tabs to be wired up — refreshAvailableRanges()
  // runs after fetch('/health'), so we wait for any tab to be enabled.
  await page.waitForSelector('#chartTabs .chart-tab:not([disabled])', { timeout: 15000 });
});

test('dashboard loads without console errors', async ({ page }) => {
  await expect(page.locator('h1, .brand, header')).toBeVisible();
  await expect(page).toHaveTitle(/solar|pvs/i);
});

test('LIVE view hides POWER/ENERGY toggle', async ({ page }) => {
  await page.locator('#chartTabs .chart-tab[data-range="live"]').click();
  await expect(page.locator('#chartModes')).toBeHidden();
  await expect(page.locator('#powerFlow')).toBeVisible();
});

test('non-LIVE views show POWER/ENERGY toggle', async ({ page }) => {
  // Start on LIVE so the toggle is hidden, then flip to 24H.
  await page.locator('#chartTabs .chart-tab[data-range="live"]').click();
  await expect(page.locator('#chartModes')).toBeHidden();
  await page.locator('#chartTabs .chart-tab[data-range="24h"]').click();
  await expect(page.locator('#chartModes')).toBeVisible();
});

test('range gating: 7D and 30D enabled, 90D and 1Y disabled with ~5d of data', async ({ page }) => {
  await expect(page.locator('#chartTabs .chart-tab[data-range="7d"]')).toBeEnabled();
  await expect(page.locator('#chartTabs .chart-tab[data-range="30d"]')).toBeEnabled();
  await expect(page.locator('#chartTabs .chart-tab[data-range="90d"]')).toBeDisabled();
  await expect(page.locator('#chartTabs .chart-tab[data-range="1y"]')).toBeDisabled();
});

test('clicking 30D fetches history and renders chart', async ({ page }) => {
  const [resp] = await Promise.all([
    page.waitForResponse((r) => r.url().includes('/history?range=30d') && r.ok()),
    page.locator('#chartTabs .chart-tab[data-range="30d"]').click(),
  ]);
  const body = await resp.json();
  expect(body.readings.length).toBeGreaterThan(0);
  expect(body.period_totals).not.toBeNull();
  await expect(page.locator('#powerChart')).toBeVisible();
});
