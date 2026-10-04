output "vms" {
  description = "Name, address and status of each VM"
  value = {
    for name, vm in vmmanager_vm.node : name => {
      ip     = try(vm.ip_addresses[0], null)
      status = vm.status
      ssh    = "ssh ${var.username}@${try(vm.ip_addresses[0], var.nodes[name].ip)}"
    }
  }
}

output "network" {
  value = {
    bridge = vmmanager_network.lab.bridge
    subnet = "${vmmanager_network.lab.ip_address}/${vmmanager_network.lab.prefix}"
  }
}
