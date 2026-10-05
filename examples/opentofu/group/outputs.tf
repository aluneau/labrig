output "router_ip" {
  value = vmmanager_group.lab.router_ip
}

output "member_ips" {
  value = vmmanager_group.lab.member_ips
}

output "network" {
  value = vmmanager_group.lab.network_name
}

output "wireguard_endpoint" {
  value = vmmanager_group.lab.wireguard ? "udp/${vmmanager_group.lab.wireguard_host_port} on the vmmanager host" : null
}

output "wireguard_configs" {
  description = "Client configs by device (tofu output -json wireguard_configs)"
  value       = { for name, d in vmmanager_wireguard_peer.device : name => d.config }
  sensitive   = true
}

output "wireguard_config_laptop" {
  value     = try(vmmanager_wireguard_peer.device["laptop"].config, null)
  sensitive = true
}
