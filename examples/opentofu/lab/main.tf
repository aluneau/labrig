# A small lab: one Debian cloud image, a private NAT network with fixed
# addresses, and one VM per entry in var.nodes.
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply
#
# Everything shows up live in the VM Manager web UI.

terraform {
  required_providers {
    vmmanager = {
      source  = "local/vmmanager"
      version = "0.1.0"
    }
  }
}

# Authentication: export VMMANAGER_TOKEN=<API token> (web UI: user menu > API tokens, or
# backend/venv/bin/python -m app.cli token create --user $USER --name opentofu), or set token = "...".
provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_cloud_image" "debian" {
  distribution = "debian"
  version      = "13"
  # Cloud images are a shared download cache: keep it for the web UI and other configs
  keep_on_destroy = true
}

resource "vmmanager_network" "lab" {
  name       = "tofu-lab"
  mode       = "nat"
  ip_address = "192.168.200.1"
  prefix     = 24
  dhcp_start = "192.168.200.100"
  dhcp_end   = "192.168.200.199"
  domain     = "lab.internal"

  # Fixed addresses: each VM below gets the MAC its reservation expects
  dhcp_hosts = [
    for name, node in var.nodes : { mac = node.mac, ip = node.ip, name = name }
  ]
}

resource "vmmanager_vm" "node" {
  for_each = var.nodes

  name        = each.key
  description = "Managed by OpenTofu (examples/opentofu/lab)"
  memory      = each.value.memory
  vcpu        = each.value.vcpu
  disk_size   = 10
  running     = each.value.running

  cloud_image_id = vmmanager_cloud_image.debian.id
  network        = vmmanager_network.lab.name
  mac_address    = each.value.mac
  wait_for_ip    = true

  cloud_init = {
    username = var.username
    password = var.password
    ssh_keys = var.ssh_public_key == "" ? [] : [var.ssh_public_key]
    keyboard = var.keyboard
  }
}
