variable "region" {
  description = "阿里云地域"
  default     = "cn-guangzhou"
}

variable "master_instance_type" {
  description = "控制面规格（2C4G）"
  default     = "ecs.e-c1m2.large"
}

variable "worker_instance_type" {
  description = "工作节点规格（2C4G）"
  default     = "ecs.e-c1m2.large"
}

variable "worker_count" {
  description = "工作节点数量（演示3台；生产3主8从改8）"
  default     = 3
}

variable "node_password" {
  description = "节点root密码（Kubekey SSH用，必须显式提供）"
  type        = string
  sensitive   = true
}

variable "admin_cidr" {
  description = "允许 SSH/NodePort 的来源 CIDR（建议你的公网 IP/32）；留空则不创建 SSH/NodePort 规则"
  type        = string
  default     = ""
}

variable "nodeport_cidr" {
  description = "允许 NodePort 的来源 CIDR；留空时复用 admin_cidr"
  type        = string
  default     = ""
}

variable "web_cidr" {
  description = "允许 80/443 的来源 CIDR；留空则不创建 HTTP/HTTPS 规则"
  type        = string
  default     = "0.0.0.0/0"
}

variable "enable_ha_slb" {
  description = "创建内网 SLB 作为 3 master API 入口；关闭则回退到 master1 单点"
  type        = bool
  default     = true
}

variable "ssh_public_key_path" {
  description = "SSH公钥路径"
  default     = "keys/id_ed25519.pub"
}

variable "name_prefix" {
  description = "资源名称前缀（VPC/ECS/安全组）"
  default     = "course-k8s"
}

variable "key_pair_name" {
  description = "阿里云SSH密钥对名称（多环境部署时避免冲突）"
  default     = "vmware-to-kubernetes-2026"
}

variable "bandwidth_out" {
  description = "ECS公网出带宽（Mbps）"
  default     = 5
}
