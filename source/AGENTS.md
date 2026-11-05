# Development and release rules

- This is the isolated development repository. Production code, configuration and durable data are managed separately through the Linux/Docker release manager; see [deployment and rollback](../docs/linux-migration.md).
- Production operations use the verified deployment's `linux-ops` entry point or `source/deploy/linux_ops.py`. Do not use historical Mac `deploy/install.py` or `launchctl` commands, or overwrite running production source as an ordinary development step.
- Each bug needs a reproducible case, a focused commit, relevant regression checks, and acceptance evidence. Run the complete suite before release.
- Development and staging never use production credentials, production data, shared Material Hub writes, or production upload endpoints. Use independent fixtures. Test videos must be copies, never hard links to writable production files.
- Keep durable data schema at version 1 for this phase. Any incompatible data or dependency change must include a separately reviewed migration and tested rollback procedure. Never restore an old database during an ordinary code rollback.
- Publish only a committed, clean revision through the release manager. Record the release ID, Git commit, test result and health checks. Failed health checks restore the previous deployment automatically.
- Git excludes secrets, account databases, original user spreadsheets, videos, feedback JSON and task state. Backups are private and remain outside Git.

## Local dev coordination

- Before starting, stopping, restarting, or reusing local dev, read the [local dev service inventory](../docs/local-dev-services.md). Port 8793 is the standard development default; consult the inventory for separate acceptance instances. Historical ports, PIDs and release labels do not prove current process or code identity. Check current processes and active tasks before acting.
- Keep synthetic delivery fixtures isolated from the main dev data. Record temporary service ownership and stop your own temporary instance after acceptance; do not claim cleanup or restart happened without verifying it.
