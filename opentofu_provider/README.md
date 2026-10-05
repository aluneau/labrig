# terraform-provider-vmmanager

OpenTofu (and Terraform) provider for the VM Manager API. It talks to the backend over HTTP, so
everything it creates is visible live in the web UI.

## Install (local)

```bash
make install    # builds and copies into ~/.terraform.d/plugins/registry.opentofu.org/local/vmmanager/0.1.0/<os_arch>/
```

```hcl
terraform {
  required_providers {
    vmmanager = { source = "local/vmmanager", version = "0.1.0" }
  }
}

provider "vmmanager" {
  endpoint = "http://127.0.0.1:8000" # or $VMMANAGER_ENDPOINT
}
```

## Resources

### `vmmanager_cloud_image`
| Argument | | |
|---|---|---|
| `distribution`, `version` | required, replace | ubuntu 26.04/24.04/22.04, debian 13/12, almalinux 10/9, rocky 9, centos 10-stream/9-stream, or anything with `url` |
| `url` | optional, replace | custom qcow2 URL |
| `keep_on_destroy` | optional (false) | only forget it on destroy |

Computed: `id`, `path`, `size`. An already downloaded image is adopted. Downloads wait up to 60 min.

### `vmmanager_network`
| Argument | | |
|---|---|---|
| `name` | required, replace | |
| `mode` | `nat` | `nat`, `route`, `open`, `isolated` |
| `forward_dev`, `domain` | optional | |
| `ip_address`, `prefix` | optional | host address and prefix length |
| `dhcp_enabled` | `true` | |
| `dhcp_start`, `dhcp_end` | optional | server picks the whole subnet if unset |
| `autostart` | `true` | |
| `dhcp_hosts` | optional set of `{mac, ip, name}` | static reservations, applied live |

Computed: `id`, `bridge`, `active`. Settings changes restart the network.

### `vmmanager_vm`
| Argument | | |
|---|---|---|
| `name` | required, replace | also the guest hostname |
| `memory` (MiB), `vcpu`, `disk_size` (GiB) | 2048, 2, 20, replace | |
| `cloud_image_id` / `iso_path` | optional, replace | boot source (neither = empty disk) |
| `network`, `mac_address` | `default`, generated, replace | |
| `cloud_init` | optional, replace | `{username, password, ssh_keys, keyboard, user_data}` |
| `description`, `autostart` | updated in place | |
| `running` | `true`, in place | graceful shutdown (forced after 2 min) / start; drift is detected |
| `wait_for_ip` | `false` | wait up to 5 min for a DHCP address |
| `boot_order` | computed, in place | list of `hd`, `cdrom`, `network`; applies at the next start |
| `cdrom` | computed, in place | ISO volume path in the CD-ROM drive, `""` = empty (live when running) |

Computed: `id`, `uuid`, `status`, `ip_addresses`, `vnc_port`. Destroy deletes the VM's disks.

### `vmmanager_disk`
| Argument | | |
|---|---|---|
| `vm_id` | required, replace | |
| `size_gb` | required, in place | grow only (live when running); shrinking is an error at plan time |
| `pool`, `bus` (`virtio`/`sata`), `format` (`qcow2`/`raw`) | optional, replace | default pool, virtio, qcow2 |

Computed: `id` (`<vm_id>/<target>`), `target` (vdb, …), `path`. Hot-plugged when the VM runs. Destroy
detaches it and deletes the volume.

### `vmmanager_group`
A lab group: isolated network `vmm-g-<name>`, router VM `<name>-rtr` (DHCP, DNS, NAT), members `<name>-<member>`.
| Argument | | |
|---|---|---|
| `name`, `cidr` | required, replace | router = first address of `cidr` |
| `uplink` | `default`, replace | libvirt network for the router's internet access |
| `router_image`, `router_memory` | first ready EL image, 512, replace | e.g. `almalinux-9`; 512 MiB is enough (1 GiB swap file for the first-boot dnf run) |
| `cloud_init` | optional, replace | `{username, password, ssh_keys, keyboard}` for router and members |
| `domain` | `<name>.lab`, in place | |
| `dns_forwarders` | uplink's DNS, in place | |
| `member` blocks | in place | `name`, `image` (`debian-13`), `memory` (1024), `vcpu` (1), `disk_size` (10), `role`, `ip` (assigned if unset). Added/removed live; changing image/size recreates that member |
| `dns_record` blocks | in place, live | `name` (relative to `domain`, `*.x` wildcards), `a` or `cname` |
| `running` | `true`, in place | start (router first) / stop (router last) |

Computed: `id`, `network_name`, `router_ip`, `router_vm_id`, `member_ips`, `member_macs`, `member_vm_ids`
(maps keyed by member name). Creation waits for the router's first boot (~1 min). Destroy deletes the
group's VMs, disks and network.

### `vmmanager_cluster`
| Argument | | |
|---|---|---|
| `name` | required, replace | DNS label; nodes are `<name>-ctlplane-N` / `<name>-worker-N` |
| `type` | `k3s`, replace | `k3s` (standalone network, no router) or `kubeadm` (in a lab group with a router); `openshift` is rejected as not supported yet |
| `version` | stable channel / pinned minor, replace | k3s release (`v1.33.5+k3s1`); kubeadm: `v1.37` or `v1.37.1`; the installed one is read back |
| `ctlplanes` | 1, replace | 3 or 5 = embedded (k3s) / stacked (kubeadm) etcd |
| `workers` | 2, **in place** | workers are added, or drained and removed (highest index first) |
| `ctlplane_memory/_vcpu/_disk_size`, `worker_…` | 2048, 2, 20, replace | |
| `cloud_image_id` | Debian 13, else AlmaLinux 9, replace | |
| `domain` | `lab`, replace | API name `api.<name>.<domain>` |
| `network`, `cidr` | new NAT network `vmm-k-<name>` on a free /24, replace | k3s; or an existing network (DHCP reservations + DNS records are added to it). kubeadm: computed (the group's network); `cidr` = subnet of an auto-created group |
| `group_id` | new group `<name>`, replace | kubeadm: an existing `vmmanager_group` id; its other members and records are kept when the cluster is destroyed. Default: a group created for the cluster, deleted with it |
| `router_memory` | group default (512), replace | kubeadm with an auto-created group |
| `extra_args`, `username`, `password`, `ssh_keys` | optional, replace | extra k3s server flags; node login |
| `running` | `true`, in place | stop (ACPI, workers first) / start (waits for all nodes Ready) |

Computed: `id`, `status`, `api_hostname`, `api_endpoint` (k3s `https://<ctlplane-0>:6443`, kubeadm
`https://<router uplink ip>:<port>` = haproxy in front of every control plane; reachable from the host),
`node_ips` (map), `kubeconfig` (sensitive). Create waits until every node is Ready (up to 30 min).
Destroy deletes the nodes, their disks and the cluster's own network or auto-created group (an existing
group only loses the cluster's nodes, records and load balancer). Examples: `examples/opentofu/k3s`,
`examples/opentofu/kubeadm`.

All resources support `tofu import <address> <id>` (ids are the API ids).

## Development

```bash
make build && make test   # go vet
```

Code: `internal/provider/` (`client.go` HTTP client + API payloads, one file per resource).
Examples: `examples/opentofu/lab`, `examples/opentofu/devices`, `examples/opentofu/group`.

To try a provider build without touching the installed one, use a CLI config with
`provider_installation { dev_overrides { "local/vmmanager" = "<this dir>" } direct {} }` and
`TF_CLI_CONFIG_FILE=<that file> tofu plan` (no `tofu init` needed).
