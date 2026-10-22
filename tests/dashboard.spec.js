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

test('outages API + card: mock grid outage is listed and summarized', async ({ page, request }) => {
  const body = await (await request.get('/outages?range=24h')).json();
  expect(body.grid_tracking).toBe(true);
  const grid = body.outages.filter((o) => o.kind === 'grid');
  expect(grid.length).toBeGreaterThanOrEqual(1);
  expect(grid[0].duration_s).toBe(240);
  expect(body.summary.grid.count).toBe(grid.length);

  await expect(page.locator('#og_gridCount')).toHaveText(String(body.summary.grid.count));
  await expect(page.locator('#og_pvsCount')).toHaveText(String(body.summary.pvs.count));
  await expect(page.locator('#outageList li[data-kind="grid"]').first()).toContainText('4m');
});

test('chart legend lists outage kinds present in the window', async ({ page }) => {
  // 24H is the default range; the mock seeds a grid outage 3h ago.
  await page.waitForFunction(() => typeof powerChart !== 'undefined' && powerChart
    && powerChart.data.datasets.some((d) => d._outageKind));
  const labels = await page.evaluate(() => powerChart.data.datasets.filter((d) => d._outageKind).map((d) => d.label));
  expect(labels).toContain('Power outage');
});

test('SunVault badge: on-grid chip normally, red on backup or UPS outage', async ({ page, request }) => {
  const sv = await (await request.get('/sunvault')).json();
  expect(sv.midstate).toBe(2);
  expect(sv.kind).toBe('on_grid');
  await expect(page.locator('#gridStatus')).toHaveClass(/ok/);
  await expect(page.locator('#gridStatus')).toContainText('SunVault on grid');
  // Backup (any value but 0/2) with no grid import.
  await page.evaluate(() => {
    lastSunvault = { midstate: 1, kind: 'backup', since: Date.now() / 1000 - 120 };
    liveSnapshot.gridKw = 0; renderGridStatus();
  });
  await expect(page.locator('#gridStatus')).toHaveClass(/outage/);
  await expect(page.locator('#gridStatus')).toContainText('SunVault on backup power');
  // UPS lost power but SunVault still says on grid.
  await page.evaluate(() => {
    lastSunvault = { midstate: 2, kind: 'on_grid', since: null };
    lastOutages = [{ kind: 'grid', ongoing: true }]; renderGridStatus();
  });
  await expect(page.locator('#gridStatus')).toContainText('SunVault still says it is on grid');
});

test('hovering an outage band shows its window', async ({ page }) => {
  await page.waitForFunction(() => typeof powerChart !== 'undefined' && powerChart
    && powerChart.data.datasets.some((d) => d._outageKind));
  const pt = await page.evaluate(() => ({ a: powerChart.chartArea, n: powerChart.data.labels.length }));
  expect(pt.n).toBeGreaterThan(1);
  // Sweep across the chart until the tip appears.
  const box = await page.locator('#powerChart').boundingBox();
  let shown = false;
  for (let px = pt.a.left; px <= pt.a.right && !shown; px += 2) {
    await page.mouse.move(box.x + px, box.y + (pt.a.top + pt.a.bottom) / 2);
    shown = (await page.locator('.outage-tip').isVisible().catch(() => false))
      && (await page.locator('.outage-tip').innerText()).includes('Power outage');
  }
  expect(shown).toBe(true);
  await expect(page.locator('.outage-tip')).toContainText('Power outage');
  await expect(page.locator('.outage-tip')).toContainText('4m');
  await page.mouse.move(0, 0);
  await expect(page.locator('.outage-tip')).toBeHidden();
});

test('battery charge source label: solar vs grid', async ({ page }) => {
  await page.waitForFunction(() => typeof liveSnapshot !== 'undefined' && liveSnapshot);
  const label = (prod, cons, bat) => page.evaluate(([p, c, b]) => {
    Object.assign(liveSnapshot, { prodKw: p, consKw: c, batteryKw: b }); renderBattery();
    return document.getElementById('bat_flowSub').textContent;
  }, [prod, cons, bat]);
  expect(await label(5, 1, -2)).toBe('storing solar');
  expect(await label(0.5, 1.2, -0.8)).toBe('from grid');
  expect(await label(1.5, 1.0, -0.8)).toBe('from solar + grid');
});
