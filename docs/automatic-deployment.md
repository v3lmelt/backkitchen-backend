# Backend automatic deployment

The production branch is `master`. GitHub sends signed `push` events to the existing `/hooks/deploy-backend` endpoint. The `webhook` systemd service validates the HMAC-SHA256 signature, event type, repository and branch, then runs `/opt/backkitchen/deploy.sh` as `www-data`, passing the pushed commit SHA.

The versioned script is [deploy/deploy.sh](../deploy/deploy.sh); [webhooks.example.json](../deploy/webhooks.example.json) is a template without production secrets. The live configuration is `/opt/backkitchen/webhooks.json`, outside the Git checkout. Never commit its secret. Merge reviewed changes after CI passes; the push hook itself does not wait for CI.

Each deployment:

1. Acquires a lock, checks the branch, tracked local changes and FFmpeg/FFprobe availability, and fetches `origin/master`.
2. Skips superseded pushes and already deployed revisions. Updates code using a fast-forward merge, preserving unrelated untracked server files.
3. Installs Python requirements, stops the API, and makes a consistent SQLite backup under `/opt/backkitchen/backups/`.
4. Applies Alembic migrations, starts `backkitchen`, checks local health, and records the successful revision in `/opt/backkitchen/deployed-revision`.

The existing sudo policy grants `www-data` only the required `systemctl start`, `stop` and `restart backkitchen` commands. The webhook listener binds to localhost behind Nginx. Updating the versioned deployment script does not automatically replace the live script; install a reviewed copy with root ownership and mode 0755.

Inspect `journalctl -u webhook -u backkitchen` after a failure. The hook runs asynchronously, so a successful HTTP delivery only means the event was accepted; the deployed revision marker and `/api/health` establish completion. Migrations are not automatically reversed and backups are not automatically deleted. If migration or startup fails, inspect the error and recovery snapshot before restarting or restoring data. The service may remain stopped after a migration failure.

For a manual deployment, run `sudo -u www-data /opt/backkitchen/deploy.sh`. For recovery, retain the SQLite snapshot and previous Git revision; do not blindly reset or overwrite production data. Changing database engines requires adapting the backup step first.
