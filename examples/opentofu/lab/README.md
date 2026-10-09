# OpenTofu lab example

Creates a Debian 13 cloud image, a private NAT network `tofu-lab` (192.168.200.0/24) with static DHCP
reservations, and two VMs (`tofu-web` → .10, `tofu-db` → .11) reachable over SSH.

```bash
# once: build and install the provider (see opentofu_provider/README.md)
make -C ../../../opentofu_provider install

cp terraform.tfvars.example terraform.tfvars   # put your SSH public key in it
export VMMANAGER_TOKEN=vmm_…   # API token (web UI: user menu > API tokens); not needed with AUTH_ENABLED=false
tofu init
tofu apply          # ~15 s when the image is already downloaded
ssh admin@192.168.200.10
```

The VMs appear live in the web UI (status, console, IPs). Things to try:

- `running = false` on a node → graceful shutdown in place; start it from the UI and `tofu plan` shows the drift.
- Change a node's `ip` → the reservation is updated live (the VM picks it up when it renews its lease or reboots).
- Add a node to `nodes` → one more VM, nothing else touched.
- `tofu destroy` removes VMs (with their disks) and the network; the cloud image is kept (`keep_on_destroy`).

Changing `memory`, `vcpu`, `disk_size`, the image, network, MAC or `cloud_init` **recreates** the VM (and wipes its disk).
