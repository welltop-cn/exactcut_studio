# Development and release rules

- This is the isolated development repository. Production data remains in `/Users/admin/Desktop/codex/exact-cut-fbbm`; never develop in that directory or modify an archived release.
- Production operations use `/Users/admin/Desktop/codex/exact-cut-fbbm-ops/bin/ops`. Do not run `deploy/install.py`, `launchctl kickstart`, or overwrite production source as an ordinary development step.
- Each bug needs a reproducible case, a focused commit, relevant regression checks, and acceptance evidence. Run the complete suite before release.
- Development and staging never use production credentials, production data, shared Material Hub writes, or production upload endpoints. Use independent fixtures. Test videos must be copies, never hard links to writable production files.
- Keep durable data schema at version 1 for this phase. Any incompatible data or dependency change must include a separately reviewed migration and tested rollback procedure. Never restore an old database during an ordinary code rollback.
- Publish only a committed, clean revision through the release manager. Record the release ID, Git commit, test result and health checks. Failed health checks restore the previous deployment automatically.
- Git excludes secrets, account databases, original user spreadsheets, videos, feedback JSON and task state. Backups are private and remain outside Git.
