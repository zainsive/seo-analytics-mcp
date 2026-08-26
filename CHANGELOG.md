# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Renaming a tool or changing an argument breaks every saved prompt a user has, so those
changes are always listed here, and are a minor bump before 1.0 and a major one after.

## [Unreleased]

## [0.1.0] - 2026-08-26

First release.

### Added
- Search Console tools: `gsc_list_sites`, `gsc_search_analytics`, `gsc_compare_periods`,
  `gsc_inspect_url`, `gsc_list_sitemaps`, `gsc_submit_sitemap`.
- GA4 tools: `ga4_list_properties`, `ga4_run_report`, `ga4_landing_pages`.
- IndexNow tools: `indexnow_verify_key`, `indexnow_submit`.
- Composite tools: `page_report` joins Search Console, GA4 and index status for one URL.
- `auth_status` reports which profile is active and what to run when something is wrong.
- Bring-your-own OAuth: a loopback + PKCE flow run once from the terminal against an
  OAuth client in the user's own Google Cloud project. No credential ships in this repo.
- `seo-mcp doctor`, eleven checks with `--json` output for issue reports.
- Named profiles, so several Google accounts can live side by side on one machine.
- Bounded responses: summary plus capped rows, with the remainder spilled to CSV.
- Errors that name the fix, for every 400, 401, 403, 404, 429 and 5xx Google returns.

[Unreleased]: https://github.com/zainsive/seo-analytics-mcp/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/zainsive/seo-analytics-mcp/releases/tag/v0.1.0
