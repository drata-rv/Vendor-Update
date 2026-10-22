# Security Review Date Update

Bulk-update the **Last Review Date** of Vendor Security Reviews from a CSV, via the
Drata public API v2.

In Drata, a vendor's "Last Review Date" is surfaced from the security review's
`updatedAt` timestamp. The public API v2 *Update Vendor Security Review* endpoint
accepts an optional `updatedAt` field — allowed even on **COMPLETED** reviews — so a
historical review date can be corrected or backfilled without direct database access.

## Input CSV

No header is required (a header row is auto-detected and skipped).

| column | meaning | required | example |
|-------|---------|----------|---------|
| 1 | security review id | yes | `82` |
| 2 | date | yes | `2025-09-17`, `9/17/2025`, or full ISO |
| 3 | vendor id | optional | `35` |

- Date-only values are pinned to **12:00:00 UTC** to avoid a timezone-driven date
  rollover. Full date-times (`...T..:..`) are passed through as given.
- The update endpoint is keyed by **both** vendor id and security review id, but the
  CSV only carries the review id. The script resolves each review's vendor id by
  scanning `GET /vendor-security-reviews` (and, if that cross-vendor route is
  unavailable, falls back to `GET /vendors` + per-vendor `GET /vendors/{id}/security-reviews`).
  Supplying column 3 skips the lookup entirely (faster for large tenants).

See `dates.example.csv`.

## Setup

```sh
cd projects/vrm/security-review-date-update/src
npm install
```

Create a `.env` (or export the vars):

```sh
DRATA_API_TOKEN=your_public_api_key
# Optional — defaults to https://public-api.drata.com/public
# DRATA_API_BASE_URL=https://public-api.drata.com/public
```

The API key needs these permissions:
- **Vendors: Read** (`VENDORS_GET`) — list and read reviews
- **Security Review: Update** (`VENDORS_PUT_SECURITY_REVIEW`) — set the date

## Run

Dry run first (default — prints the planned `before -> after` for each row, writes nothing):

```sh
node update-security-review-dates.js --csv ./dates.csv
```

Apply the changes:

```sh
node update-security-review-dates.js --csv ./dates.csv --apply
```

Options: `--csv <path>` (required), `--apply`, `--base-url <url>`, `-v/--verbose`,
`-h/--help`.

A run log is written to `security-review-date-update.log` in the working directory,
and the console prints a summary (rows / updated / skipped / failed). Exit code is
non-zero if any row failed.

## Caveats

- **Not durable.** `updatedAt` is re-stamped whenever the review is edited afterwards
  (e.g. uploading evidence, changing the reviewer/deadline). This script corrects the
  date; it does not prevent a later edit from moving it. Re-run as needed.
- An invalid date cell, an unknown review id, or a review the API key can't see is
  reported and skipped — it does not abort the run.
