# systemd deployment

This unit runs only the host-side Hub worker. Hermes itself remains inside the fixed Docker slots.

Install the repository and virtual environment under `/opt/hermes-room-runtime`. Put these root-owned files under
`/etc/hermes-room-runtime` with mode `0600`:

- `worker.env`: non-secret runtime paths and limits
- `config.yaml`: the minimal Hermes model configuration
- `auth.json`: optional copied provider authentication state

Keep the Hub worker token outside the repository. A minimal test deployment uses one slot and can set
`HERMES_NETWORK_MODE=bridge`; production must replace this with a restricted model-proxy network. The worker token
must contain only `agent-jobs:work` and the exact Hub read scopes required by the evidence loader.

Create the group-private state root used by the host worker and the image's non-root user:

```bash
sudo install -d -o ubuntu -g ubuntu -m 0770 /var/lib/hermes-room-runtime
```

```bash
sudo install -o root -g root -m 0644 \
  ops/systemd/hermes-room-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hermes-room-worker
sudo systemctl --no-pager --full status hermes-room-worker
```

The service intentionally cannot write the repository, configuration, provider authentication, or the user's home.
Its only writable host path is `/var/lib/hermes-room-runtime`; Docker access remains a privileged host boundary and
must be limited to a dedicated worker host for production.
