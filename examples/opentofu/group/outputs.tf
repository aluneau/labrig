output "router_ip" {
  value = vmmanager_group.lab.router_ip
}

output "member_ips" {
  value = vmmanager_group.lab.member_ips
}

output "network" {
  value = vmmanager_group.lab.network_name
}
