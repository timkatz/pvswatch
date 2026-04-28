# Contributing to PVS Watch

Thanks for your interest! This is a small project with a focused scope, but
contributions — bug reports, firmware-compatibility data points, code, and
docs — are all welcome.

## Reporting issues

Use the issue templates:

- **[Bug report](.github/ISSUE_TEMPLATE/bug_report.yml)** — something isn't working as documented
- **[Firmware compatibility report](.github/ISSUE_TEMPLATE/firmware_compatibility.yml)** — your PVS firmware works (or doesn't); helps build the README compatibility matrix from real data
- **[Feature request](.github/ISSUE_TEMPLATE/feature_request.yml)** — suggest an improvement

The firmware version field on the bug template is **required** because most
"it doesn't work" reports come down to a PVS firmware that has locked down
the local API. Including it up front saves a back-and-forth.

## Submitting code changes

1. Fork the repo and create a topic branch from `main`
2. Make your change with a focused commit (or a small series of focused commits)
3. Verify locally:
   - `python -m py_compile proxy.py` — basic syntax check
   - `docker compose build` — make sure the container still builds
   - `./pvswatch.sh test` — Playwright e2e suite against a mock-mode
     container (no real PVS gateway needed). Requires Docker + Node.js.
4. Open a PR against `main`. Describe **what** changed and **why**, and
   reference the issue you're solving.

CI on the PR will:

- Run `py_compile` and an import smoke test on `proxy.py`
- Build the container image (multi-arch) without pushing

## Project structure

See the **Architecture** section in [README.md](README.md). The two files
you'll spend most of your time in:

- `proxy.py` — Flask app, PVS auth + cache, SQLite history, JSON API
- `solar_dashboard.html` — the entire single-file UI (HTML + CSS + JS + Chart.js)

## Releasing

Releases use **CalVer** (calendar versioning): `YYYY.MM.DD` for the first
release of a day, `YYYY.MM.DD.N` for subsequent releases that same day
(e.g., `2026.04.25.1`, `2026.04.25.2`).

Pushing a CalVer tag triggers `.github/workflows/docker-publish.yml`,
which builds and pushes the multi-arch container image to
`ghcr.io/<owner>/pvswatch`, tagged with the full version, the date
roll-up, the year, and `latest` (e.g. `2026.04.25.3`, `2026.04.25`,
`2026`, `latest`).

## Code of conduct

Be kind, assume good faith, and stay focused on the technical issue.
