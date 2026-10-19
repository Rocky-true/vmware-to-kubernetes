output "master_public_ips" {
  value = alicloud_instance.masters[*].public_ip
}

output "worker_public_ips" {
  value = alicloud_instance.workers[*].public_ip
}

output "master1_public_ip" {
  value = alicloud_instance.masters[0].public_ip
}

output "master_private_ips" {
  value = alicloud_instance.masters[*].private_ip
}

output "worker_private_ips" {
  value = alicloud_instance.workers[*].private_ip
}

output "api_endpoint" {
  description = "Kubernetes API 入口；启用 HA 时为 SLB 内网地址，否则为 master1 私网地址"
  value       = var.enable_ha_slb ? one(alicloud_slb_load_balancer.api[*].address) : alicloud_instance.masters[0].private_ip
}
