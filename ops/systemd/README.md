# systemd deployment

This unit runs only the host-side Hub worker. Slot count is fixed, but each Hermes container is replaced after one
job so processes and credentials cannot cross the job boundary.

Install the repository and virtual environment under `/opt/hermes-room-runtime`. Put these root-owned files under
`/etc/hermes-room-runtime` with mode `0600`:

- `worker.env`: non-secret runtime paths and limits
- `config.yaml`: the minimal Hermes model configuration
- `auth.json`: optional copied provider authentication state

Keep the Hub worker token outside the repository. A minimal test deployment uses one slot and can set
`HERMES_NETWORK_MODE=bridge`; production must replace this with a restricted model-proxy network. The worker token
must contain only `agent-jobs:work`.

Production uses an internal Docker network plus a dedicated Tinyproxy container connected to that network and a
separate proxy-only egress network. Hermes slots remain attached only to the internal network. Tinyproxy accepts only
clients from the slot subnet, only permits CONNECT on port 443, and defaults to denying every destination except the
checked-in ChatGPT/OpenAI allowlist. Build the pinned proxy image and install the network/proxy units first:

```bash
sudo docker build -t actverse-hermes-egress-proxy:1 -f ops/proxy/Dockerfile ops/proxy
sudo install -o root -g root -m 0755 ops/systemd/ensure-hermes-egress-network.sh \
  /usr/local/libexec/ensure-hermes-egress-network
sudo install -o root -g root -m 0644 \
  ops/systemd/actverse-hermes-egress-network.service \
  ops/systemd/actverse-hermes-egress-proxy.service \
  ops/systemd/hermes-room-worker.service \
  /etc/systemd/system/
sudo install -o root -g root -m 0644 \
  ops/systemd/tinyproxy.conf /etc/tinyproxy/actverse-hermes.conf
sudo install -o root -g root -m 0644 \
  ops/systemd/hermes-egress-allowlist /etc/tinyproxy/actverse-hermes-allowlist
sudo install -o root -g root -m 0600 ops/systemd/provider.env.example \
  /etc/hermes-room-runtime/provider.env
```

Set these values in `worker.env`; startup fails closed if the network is absent, non-internal, missing the label, or
replaced with a different identity while an old slot exists.

```bash
HERMES_NETWORK_MODE=actverse-hermes-egress
HERMES_REQUIRE_RESTRICTED_NETWORK=true
HERMES_SLOTS=5
HERMES_PROVIDER_ENV_FILE=/etc/hermes-room-runtime/provider.env
```

Create the group-private state root used by the host worker and the image's non-root user:

```bash
sudo install -d -o ubuntu -g ubuntu -m 0770 /var/lib/hermes-room-runtime
```

```bash
sudo install -o root -g root -m 0644 \
  ops/systemd/hermes-room-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl disable --now tinyproxy.service tinyproxy || true
sudo systemctl enable --now \
  actverse-hermes-egress-network.service \
  actverse-hermes-egress-proxy.service \
  hermes-room-worker.service
sudo systemctl --no-pager --full status hermes-room-worker
```

Before considering the boundary live, verify that an allowlisted HTTPS request succeeds from a throwaway container
on `actverse-hermes-egress`, while a request to an unrelated domain is rejected by the proxy. The worker also checks
the network's `Internal=true` flag, contract label, and Docker network identity before leasing work.

The service intentionally cannot write the repository, configuration, provider authentication, or the user's home.
Its only writable host path is `/var/lib/hermes-room-runtime`; Docker access remains a privileged host boundary and
must be limited to a dedicated worker host for production.
