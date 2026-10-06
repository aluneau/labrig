# Disconnected labs: egress switch and mirror registry on the group router

Many customer sites have no direct internet access: clusters install from a **mirror registry**, and
machines only reach the outside through a proxy, if at all. A lab group reproduces that with two router
features, both in the group's **Registry & egress** tab (and the spec, API, OpenTofu):

```
           internet
              │  (the router keeps its own access: it is the "bastion")
     ┌────────┴─────────┐
     │  router          │  registry.<domain>:8443  = Quay (mirror-registry) on the router
     │  egress: blocked │  oc-mirror v2 / skopeo run here, data on an extra disk
     └────────┬─────────┘
              │ lab network: members / cluster nodes pull from registry.<domain>, nothing else leaves
```

## Egress switch (`router.egress`)

`mode: blocked` makes the router refuse every packet the group network sends towards anything that is not
the lab: the internet, the host, other libvirt networks. Connections fail at once (ICMP "administratively
prohibited", no 2-minute timeouts) and running downloads stop. It is applied live (nftables on the router),
no reboot, and switches back the same way.

Still working when blocked:

- the lab network itself, the router's **DNS** (internet names still resolve: the router forwards them, as
  a site DNS would), **NTP**, the **load balancers** (haproxy), the **mirror registry**;
- **WireGuard** devices (laptops) and **BGP**-announced addresses;
- the host reaching the router's uplink address (kubeconfig, registry from the host);
- `allow`: CIDRs / addresses that stay reachable, e.g. a customer proxy running on the host network.

Recipes:

| Customer case | Spec |
|---|---|
| Fully disconnected site | `egress: {mode: blocked}` + registry |
| Only a proxy goes out | `egress: {mode: blocked, allow: ["192.168.122.10/32"]}` + a proxy VM on the `default` network (squid), cluster `proxy:` settings pointing at it |
| Internal mirror + some direct access (e.g. a Git server) | `allow` its address |
| "It worked until the firewall change" | install open, then switch to blocked day-2 and watch what breaks |

API: the normal spec PUT (`PUT /api/v1/groups/{id}` with `router.egress`); a spec without the `egress` /
`registry` blocks keeps the stored ones (older clients, OpenTofu without them). OpenTofu:
`egress = { mode = "blocked", allow = ["192.168.122.10/32"] }` on `vmmanager_group`.

## Mirror registry (`router.registry`)

```yaml
router:
  registry: {enabled: true, port: 8443, disk_gb: 250, memory_mb: 8192, vcpus: 4}
```

- The router runs Red Hat's **mirror-registry** (Quay + Redis in podman, SQLite storage) at
  **`registry.<domain>:<port>`** (DNS record served by the router). Data, podman's storage, oc-mirror's
  cache and workspaces live on an **extra thin qcow2 disk** of `disk_gb` (serial `vmm-registry`, xfs, mounted
  at `/var/lib/vmm-registry`), deleted with the group like the router's other disks.
- Only routers with the registry get more resources: `memory_mb` / `vcpus` replace `router.memory` / `vcpu`
  (512 MiB / 1 vCPU otherwise). Enabling it on an existing group hot-plugs the disk, sets RAM / vCPUs in the
  saved config and the **setup task restarts the router once** (clean shutdown + start: ~1 min without
  DHCP / DNS / internet for the lab). A new group with the registry is created with them.
- The setup (task "Set up mirror registry"): formats the disk, installs podman / skopeo, downloads
  `mirror-registry` (~1.3 GB, mirror.openshift.com) and `oc-mirror` (stable, ~130 MB) **on the router**, makes a
  CA + server certificate (SANs: `registry.<domain>`, the router LAN IP, its pinned uplink IP), installs Quay.
  Typical: 10–30 min, mostly the download. Re-running it is safe (each step is skipped when done).
- The **CA** is read back into the spec (`router.registry.ca_pem`, `GET /groups/{id}/registry/ca.crt`, Download
  button). Credentials (user `vmm` + generated password) are kept by the app in
  `DATA_DIR/groups/<group>/registry-auth.json` (0600), never in the spec, DB or API responses except
  `GET /groups/{id}/registry/credentials` (on demand, for `podman login`). Repositories are created
  **public on push**: lab machines pull without credentials; pushing / deleting needs them.
- From the host the registry is `<uplink_ip>:<port>` (the router's pinned uplink address). Quay's
  `SERVER_HOSTNAME` (the token realm clients are sent to) is that address, so the auth flow works from the
  host, WireGuard devices and the lab alike (lab → router uplink address is local to the router: never
  blocked by the egress switch).
- Disabling stops Quay and keeps the disk and its content (and the RAM until the router restarts).

### Mirroring (oc-mirror v2)

Registry tab → *Mirror content* (or `POST /groups/{id}/registry/mirror`):

```json
{"openshift_version": "4.19.10",
 "operators": [{"packages": [{"name": "lvms-operator"}, {"name": "metallb-operator", "channel": "stable"}]}],
 "additional_images": ["registry.access.redhat.com/ubi9/ubi-minimal:latest"]}
```

- One ImageSetConfiguration per request (`mirror.platform.channels` with `minVersion = maxVersion`, `type:
  ocp`, no graph image; operators from `registry.redhat.io/redhat/redhat-operator-index:v<minor>` unless a
  `catalog` is given), run on the router as `oc-mirror --v2 … docker://registry.<domain>:<port>` (mirror to
  mirror) in a **detached systemd unit** (`vmm-mirror-<id>`): a guest-agent timeout or an app restart doesn't
  stop it; the app polls its log (progress = images copied / total). One run at a time per router.
- The **OpenShift pull secret** (set in the Create cluster dialog) is merged with the registry credentials
  into `/run/vmm-mirror-<id>/auth.json` (tmpfs, 0600) for the run only and deleted when oc-mirror exits. It is
  never logged nor returned.
- **Idempotent**: the same request again only reads the results back (seconds); a new request copies only
  what is missing (oc-mirror checks the registry, blobs are cached on the registry disk).
- Results (used by disconnected OpenShift installs): `imageDigestSources` for install-config, the
  cluster resources oc-mirror generates (IDMS, ITMS, CatalogSource / ClusterCatalog, signature config maps)
  and the CatalogSource name per catalog.
- Sizes / times (desktop, ~20 MB/s line): a release ~15–20 GB, 30–60 min; LVMS / MetalLB a few GB each; a
  release + 3 operators: plan for 100 GB of disk (thin: only what is used takes space).

### Your own images

Registry tab → *Your own images*, to test e.g. a patched build in a disconnected cluster:

- **Copy from a registry**: `POST /groups/{id}/registry/images {"source": "quay.io/me/app:1.0", "dest_repo":
  "me/app", "dest_tag": "1.0", "username": …, "password": …}` → `skopeo copy --all` on the router (all
  architectures). The source login is for this copy only (never stored); without it the OpenShift pull
  secret's credentials are used (registry.redhat.io …). Destination default: the source path without its
  registry host.
- **Upload an archive** from the browser: `podman save -o app.tar app:1.0`, `docker save`, or an OCI archive.
  The app spools it to `DATA_DIR/groups/<group>/uploads/` (disk, not memory), pushes it from the host to
  `<uplink_ip>:<port>` with the registry API (no tool needed on the host), then deletes the file.
  API: `POST /groups/{id}/registry/upload?filename=app.tar&repo=me/app&tag=1.0` with the archive as the body.
- **podman push from your machine** (the tab shows the commands with your values):

  ```bash
  # on the host: the router's uplink address; on a WireGuard laptop: registry.<domain>:8443
  sudo mkdir -p /etc/containers/certs.d/192.168.122.26:8443
  curl -s http://127.0.0.1:8000/api/v1/groups/<id>/registry/ca.crt | sudo tee /etc/containers/certs.d/192.168.122.26:8443/ca.crt
  podman login 192.168.122.26:8443 -u vmm -p '<password from "Show credentials">'
  podman tag localhost/app:1.0 192.168.122.26:8443/me/app:1.0 && podman push 192.168.122.26:8443/me/app:1.0
  ```

  `--tls-verify=false` on login / push is the quick alternative to installing the CA. Lab machines then pull
  `registry.<domain>:8443/me/app:1.0` (the name inside the lab).
- The **image list** (registry API) shows repositories and tags with a copy-the-pull-reference button and a
  delete button (deleting oc-mirror content asks first: clusters may need it).

**In a cluster**: once its trust bundle has the CA and its pull secret the registry auth (disconnected
OpenShift installs do both; or pull anonymously), use the image by its registry name in a pod:
`image: registry.<domain>:8443/me/app:1.0`. Pulls **by tag** work for your own images; the IDMS that
oc-mirror generates only redirects **digest** references of mirrored sources (`quay.io/…@sha256:…`), and the
ITMS only the tag references of mirrored additional images.

## API

| | |
|---|---|
| `GET /groups/{id}/registry` | state (`disabled`, `not-installed`, `installing`, `restart-required`, `ready`, `stopped`, `error`), URLs, CA, disk usage, mirror runs |
| `POST /groups/{id}/registry/setup` | (re)run the setup (task) |
| `POST /groups/{id}/registry/mirror` | mirror a `MirrorRequest` (task) |
| `GET /groups/{id}/registry/ca.crt` | the CA (PEM) |
| `GET /groups/{id}/registry/credentials` | user / password (on demand) |
| `GET / POST / DELETE /groups/{id}/registry/images` | list / copy (task) / delete `?ref=repo:tag` |
| `POST /groups/{id}/registry/upload` | image archive as the body (task) |

`registry_service.ensure_mirrored(db, group, MirrorRequest, task)` is what the OpenShift installer calls.

## Troubleshooting

- **Setup fails**: the error is in the task and in the tab; details on the router in
  `/var/log/vmm-registry-setup.log` (root), phase in `/var/lib/vmm-router/registry/phase`. *Retry setup*
  re-runs it (completed steps are skipped). The download comes from mirror.openshift.com through the
  router's uplink: check that the router has internet (`curl -I https://mirror.openshift.com` from its console).
- **restart-required**: the router runs with less RAM than the registry needs; *Set up now* restarts it.
- **Quay**: `systemctl status quay-pod quay-app quay-redis` on the router; storage under
  `/var/lib/vmm-registry/quay-storage`, config `/var/lib/vmm-registry/quay-install/quay-config/config.yaml`.
- **Mirror run failed**: the error (oc-mirror's last lines + `mirroring_errors_*`) is on the run's row; the
  full log is `/var/lib/vmm-registry/runs/<id>/log`. Re-run the same request: what was copied stays.
- **x509 / unknown authority** from a client: install the CA (`/etc/containers/certs.d/<host:port>/ca.crt`
  for podman / CRI-O, or the system trust store), or use the name the certificate covers
  (`registry.<domain>`, router LAN IP, uplink IP).
- **Egress**: `nft list table ip vmm_group` on the router shows the rule and its counter
  (`vmm egress blocked`).
