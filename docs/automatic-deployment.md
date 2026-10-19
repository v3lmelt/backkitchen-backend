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

## Protected audio uploads

The application serves only uploaded images under `/uploads/`. Source audio, master deliveries and audio attachments use their existing authenticated API endpoints. Before releasing this change to an existing server, replace the Nginx `/uploads/` filesystem alias with the application proxy used by `server-init.sh`:

```nginx
location /uploads/ {
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
}
```

Apply the reviewed configuration with `nginx -t` followed by `systemctl reload nginx`. Code deployment alone leaves an existing Nginx alias in place. Verify that an anonymous request for a known `/uploads/<audio-filename>` returns 404, an uploaded image still returns 200, and an authorized master API request supports playback and download. Purge any previously cached `/uploads/` audio responses from external proxies or CDNs.

R2 audio download endpoints issue signed GET URLs valid for five minutes, with private, no-store response caching. Disable both custom-domain public access and the public `r2.dev` URL for the audio bucket, and purge previously cached audio on its former public domain. Existing public object URLs remain a bypass while bucket public access is enabled. Keep the bucket CORS policy compatible with the frontend origin and direct PUT/GET requests; signed PUT uploads send `Cache-Control: private, no-store`. The server retains access through its existing R2 credentials.

Ordinary album members can retrieve only the latest file delivery in the current cycle of a completed track. Production participants retain draft and historical access. Reopening a track blocks new ordinary-member requests; already downloaded files and already issued signed URLs remain usable until their normal expiry.
