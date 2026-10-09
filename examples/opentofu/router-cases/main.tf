# Router primitives for customer cases (docs/router-cases.md) in one small lab:
#  - split DNS: corp.example is forwarded to the member dns1 (its own dnsmasq), the rest to public resolvers
#  - proxy-only egress: no direct internet, squid on the router (basic auth); members get the proxy environment
#  - narrow hop: the router's interfaces are 1400 bytes, ICMP "fragmentation needed" dropped (PMTUD black hole),
#    TCP MSS clamping as the fix (flip clamp_mss and apply: live)
# Needs ready cloud images (an EL one for the router, Debian 13 for the members).
#
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply

terraform {
  required_providers {
    vmmanager = {
      source = "local/vmmanager"
    }
  }
}

variable "endpoint" {
  type    = string
  default = "http://127.0.0.1:8000"
}

variable "name" {
  type    = string
  default = "tofu-cases"
}

variable "password" {
  type      = string
  sensitive = true
  default   = "changeme"
}

provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_group" "cases" {
  name           = var.name
  cidr           = "10.42.243.0/24"
  dns_forwarders = ["1.1.1.1", "9.9.9.9"]
  mtu            = 1500

  cloud_init = {
    username = "admin"
    password = var.password
  }

  dns_zone {
    domain  = "corp.example"
    servers = ["dns1"]
  }

  egress = {
    mode = "proxy"
    proxy = {
      username      = "proxyuser"
      password      = "proxypass"
      allow_domains = [] # e.g. [".debian.org", ".redhat.com", "quay.io"]
    }
  }

  path = {
    mtu              = 1400
    drop_frag_needed = true
    clamp_mss        = false
  }

  member {
    name   = "client"
    image  = "debian-13"
    memory = 768
  }

  # The customer's internal DNS server
  member {
    name      = "dns1"
    image     = "debian-13"
    memory    = 512
    ip        = "10.42.243.53"
    user_data = <<-EOT
      #cloud-config
      users:
        - default
        - {name: admin, plain_text_passwd: "${var.password}", lock_passwd: false, sudo: "ALL=(ALL) NOPASSWD:ALL", shell: /bin/bash}
      # raw user_data gets no proxy environment from the app: apt needs the proxy to install packages
      apt:
        proxy: http://proxyuser:proxypass@10.42.243.1:3128
      package_update: true
      packages: [qemu-guest-agent, dnsmasq]
      write_files:
        - path: /etc/dnsmasq.d/internal.conf
          content: |
            bind-dynamic
            no-resolv
            local=/corp.example/
            host-record=app.corp.example,10.42.243.80
      runcmd:
        - systemctl enable --now qemu-guest-agent
        - systemctl restart dnsmasq
    EOT
  }
}

output "router_ip" {
  value = vmmanager_group.cases.router_ip
}

output "proxy_url" {
  value = "http://${vmmanager_group.cases.router_ip}:3128"
}
