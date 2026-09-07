// @ts-check
const { test, expect } = require('@playwright/test');

// All tests run against the mock-mode dashboard brought up by
// `docker-compose.dev.yml`. History is seeded from whichever fixture is
// present (auto-refreshed live capture, ~30d, or the committed ~5d
// baseline), so tests that depend on the span derive it from /health
// rather than assuming a fixed number of days.

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

// Mirrors RANGE_SECS + refreshAvailableRanges() in solar_dashboard.html:
// a tab is enabled once history covers >= 10% of its window.
const RANGE_SECS = { '7d': 7 * 86400, '30d': 30 * 86400, '90d': 90 * 86400, '1y': 365 * 86400 };

test('range gating: tabs enabled iff history covers >=10% of their window', async ({ page, request }) => {
  const health = await (await request.get('/health')).json();
  expect(health.history_earliest).toBeTruthy();
  const availableSecs = Date.now() / 1000 - new Date(health.history_earliest).getTime() / 1000;

  const expected = {};
  for (const [r, secs] of Object.entries(RANGE_SECS)) expected[r] = secs <= availableSecs * 10;
  for (const [r, enabled] of Object.entries(expected)) {
    const tab = page.locator(`#chartTabs .chart-tab[data-range="${r}"]`);
    if (enabled) await expect(tab, `${r} should be enabled`).toBeEnabled();
    else await expect(tab, `${r} should be disabled`).toBeDisabled();
  }

  // Guard the fixture itself: both branches must be exercised. 7D needs
  // >=0.7d of data, 1Y needs >=36.5d — any fixture between those spans works.
  expect(expected['7d'], 'fixture too short: 7D disabled').toBe(true);
  expect(expected['1y'], 'fixture too long: 1Y enabled, gating never exercised').toBe(false);
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
