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

Computed: `id`, `uuid`, `status`, `ip_addresses`, `vnc_port`. Destroy deletes the VM's disks.

### `vmmanager_cluster`
| Argument | | |
|---|---|---|
| `name` | required, replace | DNS label; nodes are `<name>-ctlplane-N` / `<name>-worker-N` |
| `type` | `k3s`, replace | `kubeadm` / `openshift` are rejected as not supported yet |
| `version` | stable channel, replace | k3s release (`v1.33.5+k3s1`); the installed one is read back |
| `ctlplanes` | 1, replace | 3 or 5 = embedded etcd |
| `workers` | 2, **in place** | workers are added, or drained and removed (highest index first) |
| `ctlplane_memory/_vcpu/_disk_size`, `worker_…` | 2048, 2, 20, replace | |
| `cloud_image_id` | Debian 13, else AlmaLinux 9, replace | |
| `domain` | `lab`, replace | API name `api.<name>.<domain>` |
| `network`, `cidr` | new NAT network `vmm-k-<name>` on a free /24, replace | or an existing network (DHCP reservations + DNS records are added to it) |
| `extra_args`, `username`, `password`, `ssh_keys` | optional, replace | extra k3s server flags; node login |
| `running` | `true`, in place | stop (ACPI, workers first) / start (waits for all nodes Ready) |

Computed: `id`, `status`, `api_hostname`, `api_endpoint` (`https://<ip>:6443`, reachable from the host),
`node_ips` (map), `kubeconfig` (sensitive). Create waits until every node is Ready (up to 30 min).
Destroy deletes the nodes, their disks and the cluster's own network. Example: `examples/opentofu/k3s`.

All resources support `tofu import <address> <id>` (ids are the API ids).

## Development

```bash
make build && make test   # go vet
```

Code: `internal/provider/` (`client.go` HTTP client + API payloads, one file per resource).
Example: `examples/opentofu/lab`.
