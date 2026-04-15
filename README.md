# La Pyme Backups

This repository runs an off-platform logical backup workflow for the La Pyme
database.

It complements, but does not replace, Supabase's built-in daily backups.

## Current strategy

- Frequency: every 4 hours
- Type: logical PostgreSQL dump (`pg_dump --format=custom`)
- Storage: Cloudflare R2
- Retention: last 42 dumps (7 days at a 4-hour cadence)

## Why this exists

Supabase daily backups are useful for project-level disaster recovery, but this
repository adds:

- a downloadable backup you control outside Supabase
- a better recovery point objective than 24 hours
- a simpler restore path into another Postgres environment

## Required GitHub Actions secrets

Configure these in the GitHub repository settings under
`Settings -> Secrets and variables -> Actions`.

- `BACKUP_DATABASE_URL`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_ENDPOINT`
- `R2_BUCKET`

## `BACKUP_DATABASE_URL`

Use a dedicated backup connection string from Supabase:

- Prefer the `Session pooler` connection string on port `5432`
- Do not use the app's transaction pooler connection
- Do not use the direct connection string if the runner cannot use IPv6

This repository intentionally uses a separate backup URL so the backup workflow
does not inherit app runtime connection assumptions.

## Notes

- This workflow backs up the database only
- Supabase Storage objects are not included in these dumps
- Backup contents must never be written to the repository itself
