# Single-node OpenShift (agent-based installer) in a lab group created for it: LVM Storage on an
# extra disk, MetalLB in L2 mode with the "hello" demo, and the NMState operator.
#
#   The pull secret is read by the server from pull_secret_path (a path on the vmmanager host);
#   use `content = file("~/pull-secret.json")` instead when tofu runs on another machine.
#   cd opentofu_provider && make install     # once
#   tofu init && tofu apply                  # ~60-90 min (ISO build, install, operators)
#   tofu output -raw kubeconfig > ocp.yaml && oc --kubeconfig ocp.yaml get clusterversion
#   tofu output -raw kubeadmin_password
#
# The console (console_url) resolves through the group router's DNS: enable WireGuard remote access
# on the group (Remote access tab) and connect, or add /etc/hosts entries for the router's uplink IP.
# Needs ~24 GiB of free RAM on the host (SNO default size).

terraform {
  required_providers {
    vmmanager = {
      source  = "local/vmmanager"
      version = "0.1.0"
    }
  }
}

variable "endpoint" {
  type    = string
  default = "http://127.0.0.1:8000"
}

variable "name" {
  type    = string
  default = "tofu-ocp"
}

variable "channel" {
  type    = string
  default = "stable-4.20"
}

variable "pull_secret_path" {
  type    = string
  default = "~/pull-secret.json"
}

variable "operators" {
  type    = list(string)
  default = ["kubernetes-nmstate-operator"] # add one and apply again: installed in place
}

# Authentication: export VMMANAGER_TOKEN=<API token> (web UI: user menu > API tokens, or
# backend/venv/bin/python -m app.cli token create --user $USER --name opentofu), or set token = "...".
provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_openshift_pull_secret" "this" {
  path = var.pull_secret_path
}

# Latest release of the channel at plan time, pinned in the cluster (a newer release later doesn't
# recreate it unless you apply with -refresh and accept the replacement)
data "vmmanager_openshift_release" "this" {
  channel = var.channel
}

resource "vmmanager_cluster" "ocp" {
  name    = var.name
  type    = "openshift"
  version = data.vmmanager_openshift_release.this.version

  openshift = {
    channel      = var.channel
    topology     = "sno"  # compact = 3 schedulable masters, ha = 3 masters + workers
    storage      = "lvms" # odf needs 3 nodes
    operators    = var.operators
    metallb      = true
    metallb_mode = "l2" # bgp: a /27 announced to the group router (switch in place)
    sriov        = false # true: igb NICs + vIOMMU + SR-IOV Network Operator (dev mode)
    # disconnected = true # mirror registry on the group router (+8 GiB RAM), no internet for the nodes
  }

  depends_on = [vmmanager_openshift_pull_secret.this]
}

output "kubeconfig" {
  value     = vmmanager_cluster.ocp.kubeconfig
  sensitive = true
}

output "kubeadmin_password" {
  value     = vmmanager_cluster.ocp.kubeadmin_password
  sensitive = true
}

output "console_url" {
  value = vmmanager_cluster.ocp.console_url
}

output "api_endpoint" {
  value = vmmanager_cluster.ocp.api_endpoint
}
