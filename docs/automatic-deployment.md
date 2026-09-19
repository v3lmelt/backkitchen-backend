# Backend automatic deployment

The production branch is `master`. GitHub sends signed `push` events to the existing `/hooks/deploy-backend` endpoint. The `webhook` systemd service validates the HMAC-SHA256 signature, event type, repository and branch, then runs `/opt/backkitchen/deploy.sh` as `www-data`, passing the pushed commit SHA.

The versioned script is [deploy/deploy.sh](../deploy/deploy.sh); [webhooks.example.json](../deploy/webhooks.example.json) is a template without production secrets. The live configuration is `/opt/backkitchen/webhooks.json`, outside the Git checkout. Never commit its secret. Merge reviewed changes after CI passes; the push hook itself does not wait for CI.

Each deployment:

1. Acquires a lock, checks the branch, tracked local changes and FFmpeg/FFprobe availability, and fetches `origin/master`.
2. Skips superseded pushes and already deployed revisions. Updates code using a fast-forward merge, preserving unrelated untracked server files.
3. Installs Python requirements and makes a consistent online SQLite backup under `/opt/backkitchen/backups/automatic/`.
4. Applies Alembic migrations, restarts `backkitchen`, checks local health, and records the successful revision in `/opt/backkitchen/deployed-revision`.

The existing sudo policy permits `www-data` to run `systemctl restart backkitchen`; this setup needs no additional sudo grants. Nginx forwards `/hooks/` to the existing webhook listener on port 9000. Updating the versioned deployment script does not automatically replace the live script; install a reviewed copy and preserve its executable permissions.

Create `/opt/backkitchen/backups/automatic` owned by `www-data:www-data` with mode 0700. Keep existing root-owned backups in the parent directory unchanged. The script checks this directory before updating code.

Inspect `journalctl -u webhook -u backkitchen` after a failure. The hook runs asynchronously, so a successful HTTP delivery only means the event was accepted; the deployed revision marker and `/api/health` establish completion. Migrations are not automatically reversed and backups are not automatically deleted. If migration or startup fails, inspect the error and recovery snapshot before restarting or restoring data. The previous API process stays running until the restart step; schema changes that require maintenance downtime need a separately planned deployment.

For a manual deployment, run `sudo -u www-data /opt/backkitchen/deploy.sh`. For recovery, retain the SQLite snapshot and previous Git revision; do not blindly reset or overwrite production data. Changing database engines requires adapting the backup step first.
