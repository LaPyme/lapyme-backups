# La Pyme Backups

This public repository runs off-platform disaster-recovery backups for the La
Pyme production database and Supabase Storage. Backup data and production
configuration never belong in the repository or public workflow logs.

Supabase Point-in-Time Recovery (PITR) is the primary short-window recovery
system. Supabase does not run a separate Daily Backups product while PITR is
enabled. These workflows provide an independent copy, longer database history,
and a portable restore path outside the Supabase control plane.

## Current strategy

| Data | Frequency | Destination | Retention/behavior |
| --- | --- | --- | --- |
| PostgreSQL | Daily at 03:00 Argentina | R2 `db/daily/` | Latest 35 dumps |
| PostgreSQL monthly archive | First successful daily run each UTC month | R2 `db/monthly/` | Latest 12 dumps |
| Supabase Storage | Daily after the database backup | R2 `storage/v1/` | Incremental, content-addressed, non-deleting |
| Database restore check | Monthly | Disposable Supabase Postgres container | Full restore plus structural validation |

The previous `db/4h/` prefix is intentionally not read or deleted by the new
workflows. Remove it only after the new daily backup, monthly archive, and
restore check have all succeeded and an operator has explicitly approved that
cleanup.

## Workflows

- `.github/workflows/main.yml` creates the daily logical PostgreSQL dump,
  validates the custom archive, uploads it to R2, preserves the first successful
  dump of each month, and applies count-based retention.
- `.github/workflows/storage.yml` incrementally backs up the `documents`,
  `imports`, and `public_assets` buckets.
- `.github/workflows/restore-check.yml` downloads the latest daily dump, verifies
  its SHA-256 checksum, restores it into the matching Supabase Postgres image,
  and checks that the application schema is present and non-empty.
- `.github/workflows/ci.yml` runs public, secret-free tests for pull requests and
  changes to `main`.

## Required GitHub environment and secrets

Create a GitHub Actions environment named `production-backups`, restrict it to
the protected `main` branch, and store all production values as environment
secrets. Never commit real endpoints, project references, bucket names, access
keys, database URLs, or example values copied from production.

Database and R2 secrets:

- `BACKUP_DATABASE_URL`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_ENDPOINT`
- `R2_BUCKET`

Additional Supabase Storage secrets:

- `SUPABASE_STORAGE_ENDPOINT`
- `SUPABASE_STORAGE_REGION`
- `SUPABASE_STORAGE_ACCESS_KEY_ID`
- `SUPABASE_STORAGE_SECRET_ACCESS_KEY`

Generate the Supabase Storage credentials from **Storage -> Configuration ->
S3** in the Supabase dashboard. Use the direct Storage endpoint supplied there.
These S3 credentials bypass RLS and have access to every Storage bucket, so use
a dedicated key pair and keep it exclusively in the GitHub environment.

Scope the R2 credentials to this backup bucket only. The workflows need object
list, read, write, and delete access because they compare incremental state,
perform restore checks, and expire database archives.

## Database connection

Use a dedicated backup connection string from Supabase:

- Prefer the Session pooler connection string on port `5432`.
- Do not use the application's transaction pooler connection.
- Do not use the direct connection string if the GitHub runner cannot use IPv6.

The dump uses PostgreSQL's custom format with compression and excludes the
reconstructible `public.iibb_padron_rates` table data. Compression reduces the
R2 object size but happens after database egress. Update both pinned Postgres
image digests deliberately whenever the production Supabase Postgres build is
upgraded.

## Storage backup privacy and recovery model

Storage paths can contain organization identifiers and original filenames. The
incremental backup therefore does not reproduce source paths in R2 object keys.
For every new or changed source object it:

1. downloads the object in a bounded batch without logging its path;
2. hashes both its source path and contents;
3. uploads the bytes under a content-addressed R2 key containing hashes only;
4. checkpoints the original path-to-object mapping in a private, compressed,
   SHA-256-addressed manifest in R2 after every completed batch.

The checkpoints are append-only and make interrupted or timed-out runs resume
from their latest completed batch. Unchanged content is not transferred again.
Source deletions and overwrites do not destroy earlier R2 content. Routine
workflow output contains bucket-level checkpoint percentages and final status
only; archive sizes, object counts, customer paths, and byte totals remain
private. Command failures are represented by a redacted reference rather than
raw tool output.

The Storage archive is append-only and does not currently garbage-collect old
content versions. Add a reviewed retention/erasure process before treating it
as a permanent legal archive.

## R2 bucket locks

Configure bucket locks in the Cloudflare dashboard, not from GitHub Actions.
Keeping the administrative Cloudflare token out of this public repository's
workflows prevents a compromised workflow from weakening its own retention.

Recommended rules:

| Prefix | Minimum lock |
| --- | --- |
| `db/daily/` | 30 days |
| `db/monthly/` | 330 days |
| `storage/v1/` | 365 days |

The database workflow retains slightly longer than each minimum lock, allowing
the oldest object to become deletable before count-based cleanup runs. Bucket
locks take precedence over deletion requests.

## Public repository safety

- Backup workflows run only on schedules or explicit manual dispatches, never
  on pull requests.
- Pull-request CI receives no production secrets.
- Third-party Actions and container images are pinned by immutable digests or
  commit SHAs.
- The rclone binary is version-pinned and SHA-256-verified before execution.
- Derived database and endpoint hostnames are explicitly masked.
- Raw `pg_dump`, `pg_restore`, and rclone errors are suppressed because they can
  contain production identifiers or customer object paths.
- Protect `main` with required pull-request review and do not allow unreviewed
  workflow changes to reach the environment secrets.

## Operational checks

After configuring the new secrets and R2 locks:

1. Run **Database Backup** manually and verify a new object under `db/daily/`.
2. Run **Storage Backup** manually. The first run transfers the existing
   objects; later runs reuse unchanged content without publishing inventory
   metrics.
3. Run **Database Restore Check** manually and require the full restore to pass.
4. Confirm the next scheduled daily runs succeed before considering cleanup of
   legacy `db/4h/` objects.
