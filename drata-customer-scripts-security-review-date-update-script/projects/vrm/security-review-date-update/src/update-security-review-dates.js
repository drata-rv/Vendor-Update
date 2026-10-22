/**
 * Bulk-update the "last review date" of Vendor Security Reviews from a CSV.
 *
 * In Drata a vendor's "Last Review Date" is surfaced from the security review's
 * `updatedAt` timestamp. The public API v2 Update Vendor Security Review endpoint
 * accepts an optional `updatedAt` field (allowed even on COMPLETED reviews) so a
 * historical review date can be corrected/backfilled.
 *
 * CSV format (no header required, but a header row is auto-detected and skipped):
 *   column 1: security review id   (integer, required)
 *   column 2: date                 (required — `YYYY-MM-DD`, `M/D/YYYY`, or full ISO)
 *   column 3: vendor id            (optional — skips the id->vendor lookup when present)
 *
 * The PUT endpoint is keyed by BOTH vendorId and securityReviewId, but the CSV
 * only carries the review id, so this script resolves each review's vendorId via
 * GET /vendor-security-reviews (across vendors) unless column 3 is supplied.
 *
 * Usage:
 *   DRATA_API_TOKEN=... node update-security-review-dates.js --csv ./dates.csv            # dry run (default)
 *   DRATA_API_TOKEN=... node update-security-review-dates.js --csv ./dates.csv --apply    # actually write
 *
 * Options:
 *   --csv <path>       Path to the input CSV (required).
 *   --apply            Perform the writes. Without it the script only prints the plan.
 *   --base-url <url>   Override the API base (default https://public-api.drata.com/public).
 *   -v, --verbose      Verbose logging.
 *   -h, --help         Show this help.
 *
 * Env:
 *   DRATA_API_TOKEN    Drata public API key. Needs permissions:
 *                        - Vendors: Read           (VENDORS_GET)  — list/get reviews
 *                        - Security Review: Update  (VENDORS_PUT_SECURITY_REVIEW)
 *   DRATA_API_BASE_URL Optional alternative to --base-url.
 */

const fs = require('fs');
const path = require('path');
const axios = require('axios');
const { parse } = require('csv-parse/sync');

try {
    require('dotenv').config();
} catch (_) {
    // dotenv is optional; env vars can be provided directly.
}

// ---- args ----------------------------------------------------------------
const argv = process.argv.slice(2);
function argValue(name) {
    const i = argv.indexOf(name);
    return i !== -1 && i + 1 < argv.length ? argv[i + 1] : undefined;
}
if (argv.includes('-h') || argv.includes('--help')) {
    console.log(fs.readFileSync(__filename, 'utf8').split('*/')[0].replace(/^\/\*\*?/, ''));
    process.exit(0);
}

const CSV_PATH = argValue('--csv');
const APPLY = argv.includes('--apply');
const VERBOSE = argv.includes('-v') || argv.includes('--verbose');
const BASE_URL = (
    argValue('--base-url') ||
    process.env.DRATA_API_BASE_URL ||
    'https://public-api.drata.com/public'
).replace(/\/+$/, '');
const TOKEN = process.env.DRATA_API_TOKEN;
const V2 = `${BASE_URL}/v2`;

const LOG_PATH = path.resolve(process.cwd(), 'security-review-date-update.log');
const logLines = [];
function log(msg, level = 'INFO') {
    const line = `[${new Date().toISOString()}] [${level}] ${msg}`;
    logLines.push(line);
    if (level === 'DEBUG' && !VERBOSE) return;
    console.log(line);
}

// ---- date normalization --------------------------------------------------
/**
 * Normalize a CSV date cell to an ISO string the API accepts.
 * Date-only values are pinned to 12:00:00 UTC to avoid a timezone-driven
 * date rollover (matches the manual backfill convention). Full date-times are
 * passed through unchanged. Returns null when the value cannot be parsed.
 */
function normalizeDate(raw) {
    const value = String(raw || '').trim();
    if (!value) return null;

    // Already a date-time (has a time component) — trust it if parseable.
    if (/[T ]\d{1,2}:/.test(value)) {
        const d = new Date(value);
        return isNaN(d.getTime()) ? null : d.toISOString();
    }

    let year, month, day;
    let m = value.match(/^(\d{4})-(\d{1,2})-(\d{1,2})$/); // YYYY-MM-DD
    if (m) {
        [, year, month, day] = m;
    } else {
        m = value.match(/^(\d{1,2})\/(\d{1,2})\/(\d{4})$/); // M/D/YYYY
        if (m) [, month, day, year] = m;
    }
    if (!year) return null;

    const y = Number(year);
    const mo = Number(month);
    const d = Number(day);
    if (mo < 1 || mo > 12 || d < 1 || d > 31) return null;

    const iso = `${y.toString().padStart(4, '0')}-${mo
        .toString()
        .padStart(2, '0')}-${d.toString().padStart(2, '0')}T12:00:00.000Z`;
    // Round-trip guards against impossible dates like 2025-02-31.
    const parsed = new Date(iso);
    if (isNaN(parsed.getTime()) || parsed.getUTCMonth() + 1 !== mo || parsed.getUTCDate() !== d) {
        return null;
    }
    return iso;
}

// ---- http ----------------------------------------------------------------
const http = axios.create({
    baseURL: V2,
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    // We inspect status codes ourselves rather than throwing on 4xx.
    validateStatus: () => true,
});

const sleep = ms => new Promise(r => setTimeout(r, ms));

/** Single request with one retry on 429 (rate limit). */
async function request(config) {
    for (let attempt = 0; attempt < 2; attempt++) {
        const res = await http.request(config);
        if (res.status !== 429) return res;
        const retryAfter = Number(res.headers['retry-after']) || 2;
        log(`429 rate-limited on ${config.method} ${config.url}; retrying in ${retryAfter}s`, 'DEBUG');
        await sleep(retryAfter * 1000);
    }
    return http.request(config);
}

/** Page a cursor-paginated list endpoint, handing each page's `data` array to `onRows`. */
async function pageAll(url, onRows, baseParams = {}) {
    let cursor;
    let page = 0;
    do {
        const params = { size: 100, ...baseParams };
        if (cursor) params.cursor = cursor;
        const res = await request({ method: 'get', url, params });
        if (res.status !== 200) {
            const err = new Error(`GET ${url} failed (${res.status}): ${JSON.stringify(res.data)}`);
            err.status = res.status;
            throw err;
        }
        onRows(res.data?.data ?? []);
        cursor = res.data?.pagination?.cursor ?? null;
        page += 1;
        log(`  ${url} page ${page}: ${(res.data?.data ?? []).length} row(s)`, 'DEBUG');
    } while (cursor);
}

/**
 * Build a securityReviewId -> vendorId map. Only called when at least one CSV row
 * lacks an explicit vendorId.
 *
 * Prefers the cross-vendor list `GET /vendor-security-reviews` (a single pass). If
 * that endpoint is unavailable (404), falls back to enumerating vendors via
 * `GET /vendors` and paging each `GET /vendors/{vendorId}/security-reviews` — both
 * vendor-scoped endpoints — so a missing cross-vendor route does not abort the run.
 */
async function buildReviewVendorMap() {
    const map = new Map();
    try {
        await pageAll('/vendor-security-reviews', rows => {
            for (const r of rows) map.set(Number(r.id), Number(r.vendorId));
        });
        return map;
    } catch (e) {
        if (e.status !== 404) throw e;
        log('/vendor-security-reviews unavailable (404); falling back to per-vendor scan.', 'WARN');
    }

    const vendorIds = [];
    await pageAll('/vendors', rows => {
        for (const v of rows) vendorIds.push(Number(v.id));
    });
    log(`Scanning security reviews across ${vendorIds.length} vendor(s)...`, 'DEBUG');
    for (const vendorId of vendorIds) {
        await pageAll(`/vendors/${vendorId}/security-reviews`, rows => {
            for (const r of rows) map.set(Number(r.id), vendorId);
        });
    }
    return map;
}

async function getReview(vendorId, reviewId) {
    const res = await request({
        method: 'get',
        url: `/vendors/${vendorId}/security-reviews/${reviewId}`,
    });
    return res;
}

async function putUpdatedAt(vendorId, reviewId, updatedAt) {
    return request({
        method: 'put',
        url: `/vendors/${vendorId}/security-reviews/${reviewId}`,
        data: { updatedAt },
    });
}

// ---- csv ------------------------------------------------------------------
function loadRows(csvPath) {
    const raw = fs.readFileSync(csvPath, 'utf8');
    const records = parse(raw, { skip_empty_lines: true, trim: true, relax_column_count: true });
    const rows = [];
    records.forEach((cols, idx) => {
        const reviewIdRaw = cols[0];
        // Auto-skip a header row (first cell not a positive integer).
        if (idx === 0 && !/^\d+$/.test(String(reviewIdRaw || '').trim())) {
            log(`Skipping header row: ${JSON.stringify(cols)}`, 'DEBUG');
            return;
        }
        rows.push({
            lineNo: idx + 1,
            reviewId: Number(String(reviewIdRaw).trim()),
            dateRaw: cols[1],
            vendorId: cols[2] ? Number(String(cols[2]).trim()) : undefined,
        });
    });
    return rows;
}

// ---- main -----------------------------------------------------------------
async function main() {
    if (!CSV_PATH) {
        console.error('Error: --csv <path> is required. Use --help for usage.');
        process.exit(2);
    }
    if (!TOKEN) {
        console.error('Error: DRATA_API_TOKEN env var is required.');
        process.exit(2);
    }
    if (!fs.existsSync(CSV_PATH)) {
        console.error(`Error: CSV not found at ${CSV_PATH}`);
        process.exit(2);
    }

    log(`Base URL: ${V2}`);
    log(`Mode: ${APPLY ? 'APPLY (writing changes)' : 'DRY RUN (no writes) — pass --apply to write'}`);

    const rows = loadRows(CSV_PATH);
    if (rows.length === 0) {
        log('No data rows found in CSV.', 'WARN');
        return finish({ total: 0, updated: 0, skipped: 0, failed: 0 });
    }
    log(`Loaded ${rows.length} row(s) from ${CSV_PATH}`);

    // Validate dates up front so a bad cell is reported before any writes.
    for (const row of rows) {
        row.updatedAt = normalizeDate(row.dateRaw);
    }

    const needsLookup = rows.some(r => r.updatedAt && !Number.isInteger(r.vendorId));
    let reviewVendorMap = new Map();
    if (needsLookup) {
        log('Resolving vendor ids from security review ids (scanning /vendor-security-reviews)...');
        reviewVendorMap = await buildReviewVendorMap();
        log(`Resolved a map of ${reviewVendorMap.size} security review(s).`);
    }

    const summary = { total: rows.length, updated: 0, skipped: 0, failed: 0 };

    for (const row of rows) {
        const tag = `review ${row.reviewId} (csv line ${row.lineNo})`;

        if (!Number.isInteger(row.reviewId) || row.reviewId <= 0) {
            log(`SKIP ${tag}: invalid security review id`, 'ERROR');
            summary.skipped += 1;
            continue;
        }
        if (!row.updatedAt) {
            log(`SKIP ${tag}: could not parse date "${row.dateRaw}"`, 'ERROR');
            summary.skipped += 1;
            continue;
        }

        const vendorId = Number.isInteger(row.vendorId)
            ? row.vendorId
            : reviewVendorMap.get(row.reviewId);
        if (!Number.isInteger(vendorId)) {
            log(
                `SKIP ${tag}: no vendor found for this review id (not visible to this API key, ` +
                    `deleted, or wrong id). Add a vendor id as CSV column 3 to override.`,
                'ERROR',
            );
            summary.skipped += 1;
            continue;
        }

        // Capture the current value for the before/after record.
        const before = await getReview(vendorId, row.reviewId);
        if (before.status !== 200) {
            log(
                `SKIP ${tag}: GET returned ${before.status}: ${JSON.stringify(before.data)}`,
                'ERROR',
            );
            summary.skipped += 1;
            continue;
        }
        const currentUpdatedAt = before.data?.updatedAt;
        const reviewStatus = before.data?.status;

        if (!APPLY) {
            log(
                `DRY RUN ${tag} [vendor ${vendorId}, status ${reviewStatus}]: ` +
                    `${currentUpdatedAt} -> ${row.updatedAt}`,
            );
            continue;
        }

        const res = await putUpdatedAt(vendorId, row.reviewId, row.updatedAt);
        if (res.status === 200) {
            const persisted = res.data?.updatedAt;
            const ok =
                persisted && new Date(persisted).toISOString() === new Date(row.updatedAt).toISOString();
            if (ok) {
                log(
                    `UPDATED ${tag} [vendor ${vendorId}, status ${reviewStatus}]: ` +
                        `${currentUpdatedAt} -> ${persisted}`,
                );
                summary.updated += 1;
            } else {
                log(
                    `WARN ${tag}: PUT returned 200 but updatedAt is ${persisted}, expected ${row.updatedAt}`,
                    'WARN',
                );
                summary.failed += 1;
            }
        } else {
            log(`FAILED ${tag}: PUT ${res.status}: ${JSON.stringify(res.data)}`, 'ERROR');
            summary.failed += 1;
        }
        // Gentle pace to stay under any rate limit.
        await sleep(150);
    }

    finish(summary);
}

function finish(summary) {
    const report = [
        '',
        '=== SECURITY REVIEW DATE UPDATE SUMMARY ===',
        `Mode:        ${APPLY ? 'APPLY' : 'DRY RUN'}`,
        `Rows:        ${summary.total}`,
        `Updated:     ${summary.updated}`,
        `Skipped:     ${summary.skipped}`,
        `Failed:      ${summary.failed}`,
        '===========================================',
    ].join('\n');
    console.log(report);
    try {
        fs.writeFileSync(LOG_PATH, `${logLines.join('\n')}\n${report}\n`, 'utf8');
        console.log(`Log written to ${LOG_PATH}`);
    } catch (e) {
        console.error(`Could not write log file: ${e.message}`);
    }
    if (summary.failed > 0) process.exitCode = 1;
}

main().catch(err => {
    log(`Fatal: ${err.message}`, 'ERROR');
    finish({ total: 0, updated: 0, skipped: 0, failed: 1 });
    process.exit(1);
});
