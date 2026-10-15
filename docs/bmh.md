# Bare-metal hosts (OpenShift SNO on physical servers)

Add physical servers — Dell, Supermicro, HPE… — driven through their BMC (Redfish) and install
**single-node OpenShift (SNO)** on them, next to the VM-based clusters. First step: SNO, standalone on the
host's own routable network (no lab group, router or VPN). The whole flow is driven from this app: it builds
the agent ISO, serves it over HTTP, mounts it via the BMC's virtual media, boots the host and follows the
install over the network.

## What you need

- A server with a **Redfish** BMC (iDRAC 9+/6.x, iLO 5+, Supermicro X11+) reachable from the VM Manager host,
  and its credentials.
- The BMC must be able to reach the VM Manager host on its port (default 8000/tcp, opened by `setup.sh`): it
  fetches the agent ISO from there as virtual media.
- The VM Manager host must reach the node's data network (to follow the install and run `oc`). No VPN is
  needed when both are on routable lab networks.
- `nmstatectl` on the host (`setup.sh` installs `nmstate`): `openshift-install` uses it to validate the node's
  static network configuration.
- An OpenShift **pull secret** (set it in the Create cluster dialog or OpenShift settings).

## Add a host

Bare-metal hosts page → **Add host**: name, BMC address (e.g. `https://10.6.75.161`), username, password
(stored 0600 on the host, never returned by the API), and optionally the boot NIC's MAC. The BMC is probed on
add; the page shows the hardware (CPU/RAM/serial), power state and NICs. Expand a row to pick the **boot NIC**
— the one cabled to the data network the node will use. Power on/off/cycle and refresh inventory from here too.

The REST API is `/api/v1/bare-metal-hosts` (`bmc_service`, Redfish client in `services/bmc.py`).

## Install SNO

From the **Clusters** page → Create cluster → type **OpenShift**, **Runs on: Bare-metal host** → pick the
host and give the node's static network: IP, prefix, gateway, DNS, and optionally NTP (empty = the VM Manager
host's own NTP servers; the installer validates the clock). The same dialog opens pre-filled from the
Bare-metal hosts page's **Install OpenShift (SNO)** action.

The install (`openshift_installer.create_bmh` → `provision_bmh`): build the agent ISO with a static
`networkConfig` (nmstate, the boot NIC matched by MAC) → serve it at `/iso/<token>/agent.iso` → the BMC
inserts it as virtual media, one-time-boots from CD and powers on → the node registers with the on-ISO
Assisted Service (polled directly from the host) → write-to-disk, reboot, bootstrap-in-place → the app
approves CSRs and waits for the cluster operators → eject the media. `oc`/kubeconfig reach the API by the
node's IP with `tls-server-name api.<cluster>.<domain>` (no external DNS needed).

## Reaching api / *.apps (host DNS resolver)

A bare-metal cluster has no lab-group router, so nothing resolves its `api.<cluster>.<domain>` /
`*.apps.<cluster>.<domain>` for the console or other clients. `setup.sh` installs a small resolver on the
vm-manager host (`services/dns_service.py`, the `vm-manager-dns` dnsmasq unit, port 53) that answers those
names → the cluster's node IP and forwards everything else upstream. The host address every lab network can
reach is detected at run time (never hardcoded). Point your laptop's DNS (or one `/etc/hosts` line) at the
vm-manager host and the console at `https://console-openshift-console.apps.<cluster>.<domain>` just works;
new bare-metal nodes are also pointed at it (handy for ACM later). `GET /api/v1/hosts/dns` shows its status
and the zones it serves.

You can also add your own **custom records** to this resolver from the Host page (the "DNS resolver" card),
like managing records on a lab group's router: a name → IPv4 is an A record, a name → another name is an
alias (CNAME); an alias target must be a name this resolver serves itself (another custom record, a
cluster's `api`/`api-int`): dnsmasq can't follow an alias to an upstream name or a wildcard. They are stored in the DB (`host_dns_records`) and rendered into the dnsmasq config alongside
the cluster zones (`host-record=` / `cname=`), applied live. A name that would shadow a cluster zone the
resolver already owns (`api`/`api-int`/`*.apps` of a bare-metal cluster) is rejected. API:
`GET/POST /api/v1/hosts/dns/records` (`{name, ip}` or `{name, cname}`) and
`DELETE /api/v1/hosts/dns/records/{name}`. These resolve for anyone pointing their DNS at the host.

If you don't know the data network, boot the host with DHCP once and read the address from the BMC console
(Dell: `DellLCService.ExportServerScreenShot`), then install with that static address.

## As an ACM spoke (ZTP on real hardware)

A registered bare-metal host can also be provisioned as an **ACM spoke** by an OpenShift hub that has the ACM
add-on: hub page → ACM → *Provision a spoke* → pick a *Bare metal:* host and give the node's static network.
The hub's central infrastructure management (metal3 + assisted service) drives the real BMC's virtual media and
installs SNO through `BareMetalHost` / `InfraEnv` / `NMStateConfig` / `AgentClusterInstall` — true ZTP on a
physical server, next to the emulated-BMC (VM) spokes. See `docs/openshift.md` → *Real bare-metal spokes*.

**GitOps / ZTP from git**: instead of the app applying those CRs directly, tick *Deploy via GitOps* — the spoke's
manifests are committed to an in-lab git server and synced onto the hub by OpenShift GitOps (Argo CD). Install Argo
CD and deploy the Application from the hub's ACM tab → *Deploy from git (GitOps)*. See `docs/openshift.md` →
*GitOps / ZTP (deploy a spoke from git)*.

## Limitations (first step)

- Single node only (no compact/HA yet); no LVMS/ODF storage, SR-IOV, MetalLB or disconnected installs on bare
  metal yet; not in a lab group yet. These work on VM-based clusters (`docs/openshift.md`).
- The node's boot disk is wiped (the installer picks the first eligible disk unless a `root_device` hint is
  given).
