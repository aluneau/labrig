# A dual stack lab group (docs/ipv6.md): the group network gets an IPv6 /64 next to its IPv4 subnet. The router
# sends router advertisements and serves DHCPv6: every member gets <prefix>::<host number of its IPv4 address>
# (10.42.90.10 -> <prefix>::10) and an AAAA record. IPv6 is lab-internal (the uplink is IPv4 only).
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply
#   tofu output member_ip6s
#
# ipv6.egress = "drop" makes IPv6 towards anything outside the lab hang instead of failing at once: with the
# "app" record below (A = web1, AAAA = an address outside the lab), clients that prefer AAAA time out
# (template ipv6-dual-stack). Switching ipv6.enabled / egress is applied live.

terraform {
  required_providers {
    vmmanager = {
      source = "local/vmmanager"
    }
  }
}

provider "vmmanager" {
  endpoint = var.endpoint
}

variable "endpoint" {
  type    = string
  default = "http://localhost:8000"
}

variable "name" {
  type    = string
  default = "tofu-v6"
}

variable "cidr" {
  type    = string
  default = "10.42.90.0/24"
}

variable "egress" {
  description = "IPv6 from the lab to outside it: reject (fails at once) or drop (hangs)"
  type        = string
  default     = "reject"
}

resource "vmmanager_group" "lab" {
  name         = var.name
  cidr         = var.cidr
  router_image = "almalinux-9"

  ipv6 = {
    enabled = true
    egress  = var.egress
  }

  cloud_init = {
    username = "admin"
    password = "admin"
  }

  member {
    name   = "web1"
    image  = "debian-13"
    memory = 768
    ip     = cidrhost(var.cidr, 10)
  }

  member {
    name   = "web2"
    image  = "debian-13"
    memory = 768
  }

  dns_record {
    name = "app"
    a    = cidrhost(var.cidr, 10)
    aaaa = "fd00:bad::80"
  }
}

output "ipv6_prefix" {
  value = vmmanager_group.lab.ipv6.prefix
}

output "router_ip6" {
  value = vmmanager_group.lab.router_ip6
}

output "member_ip6s" {
  value = vmmanager_group.lab.member_ip6s
}
