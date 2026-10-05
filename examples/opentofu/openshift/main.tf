# Single-node OpenShift (agent-based installer) in a lab group created for it: LVM Storage on an
# extra disk, MetalLB in L2 mode with the "hello" demo, and the NMState operator.
#
#   The pull secret must be stored on the server first (Create cluster dialog, or
#   curl -X PUT $ENDPOINT/api/v1/openshift/pull-secret -H 'Content-Type: application/json' \
#        -d '{"path": "~/pull-secret.json"}')
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

variable "version_ocp" {
  type    = string
  default = null # latest of the channel
}

provider "vmmanager" {
  endpoint = var.endpoint
}

resource "vmmanager_cluster" "ocp" {
  name    = var.name
  type    = "openshift"
  version = var.version_ocp

  openshift = {
    channel   = "stable-4.20"
    topology  = "sno" # compact = 3 schedulable masters, ha = 3 masters + workers
    storage   = "lvms" # odf needs 3 nodes
    operators = ["kubernetes-nmstate-operator"]
    metallb   = true
    sriov     = false # true: igb NICs + vIOMMU + SR-IOV Network Operator (dev mode)
  }
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
