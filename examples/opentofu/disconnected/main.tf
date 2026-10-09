# A disconnected lab (docs/disconnected.md): a group whose router runs a mirror registry
# (registry.<domain>:8443) and blocks the members' internet access (egress), except an allowed
# address (e.g. a proxy on the host network). Apply waits until the registry is installed
# (router restart + ~1.3 GB download + Quay install: 10-30 min).
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply
#   tofu output -raw registry_ca > registry-ca.crt
#
# Switching egress.mode between "open" and "blocked" is applied live on the router.

terraform {
  required_providers {
    vmmanager = {
      source = "local/vmmanager"
    }
  }
}

# Authentication: export VMMANAGER_TOKEN=<API token> (web UI: user menu > API tokens, or
# backend/venv/bin/python -m app.cli token create --user $USER --name opentofu), or set token = "...".
provider "vmmanager" {
  endpoint = var.endpoint
}

variable "endpoint" {
  type    = string
  default = "http://127.0.0.1:8000"
}

variable "name" {
  type    = string
  default = "tofu-dr"
}

variable "cidr" {
  type    = string
  default = "10.42.62.0/24"
}

variable "egress" {
  type    = string
  default = "blocked"
}

resource "vmmanager_group" "lab" {
  name         = var.name
  cidr         = var.cidr
  router_image = "almalinux-9"

  egress = {
    mode  = var.egress
    allow = ["192.168.122.1/32"] # e.g. a proxy on the host's default network
  }

  registry = {
    enabled   = true
    memory_mb = 6144
    vcpus     = 2
    disk_gb   = 100
  }

  member {
    name  = "client"
    image = "debian-13"
  }
}

output "registry" {
  value = "${vmmanager_group.lab.registry.hostname}:${vmmanager_group.lab.registry.port}"
}

output "registry_ca" {
  value = vmmanager_group.lab.registry.ca_pem
}
