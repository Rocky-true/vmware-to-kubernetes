terraform {
  required_providers {
    alicloud = {
      source  = "aliyun/alicloud"
      version = "~> 1.200"
    }
  }
}

provider "alicloud" {
  region = var.region
}

locals {
  admin_cidr    = trimspace(var.admin_cidr)
  nodeport_cidr = trimspace(var.nodeport_cidr) != "" ? trimspace(var.nodeport_cidr) : local.admin_cidr
  web_cidr      = trimspace(var.web_cidr)
}

data "alicloud_zones" "default" {
  available_resource_creation = "Instance"
}

data "alicloud_images" "ubuntu" {
  name_regex  = "^ubuntu_22_04_x64"
  most_recent = true
  owners      = "system"
}

# ===== 缃戠粶 =====
resource "alicloud_vpc" "lab" {
  vpc_name   = "${var.name_prefix}-vpc"
  cidr_block = "172.16.0.0/16"
}

resource "alicloud_vswitch" "lab" {
  vpc_id       = alicloud_vpc.lab.id
  cidr_block   = "172.16.0.0/24"
  zone_id      = data.alicloud_zones.default.zones[0].id
  vswitch_name = "${var.name_prefix}-vswitch"
}

# ===== 瀹夊叏缁?=====
resource "alicloud_security_group" "lab" {
  security_group_name = "${var.name_prefix}-sg"
  vpc_id              = alicloud_vpc.lab.id
}

# 6443 不对公网开放；kubectl/Kubekey 均通过 SSH 或 VPC 内网访问。

resource "alicloud_security_group_rule" "ssh" {
  count             = local.admin_cidr != "" ? 1 : 0
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "22/22"
  priority          = 1
  security_group_id = alicloud_security_group.lab.id
  cidr_ip           = local.admin_cidr
}

resource "alicloud_security_group_rule" "nodeport" {
  count             = local.nodeport_cidr != "" ? 1 : 0
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "30000/32767"
  priority          = 2
  security_group_id = alicloud_security_group.lab.id
  cidr_ip           = local.nodeport_cidr
}

resource "alicloud_security_group_rule" "http" {
  count             = local.web_cidr != "" ? 1 : 0
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "80/80"
  priority          = 4
  security_group_id = alicloud_security_group.lab.id
  cidr_ip           = local.web_cidr
}

resource "alicloud_security_group_rule" "https" {
  count             = local.web_cidr != "" ? 1 : 0
  type              = "ingress"
  ip_protocol       = "tcp"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "443/443"
  priority          = 5
  security_group_id = alicloud_security_group.lab.id
  cidr_ip           = local.web_cidr
}

resource "alicloud_security_group_rule" "intra" {
  type              = "ingress"
  ip_protocol       = "all"
  nic_type          = "intranet"
  policy            = "accept"
  port_range        = "-1/-1"
  priority          = 3
  security_group_id = alicloud_security_group.lab.id
  cidr_ip           = "172.16.0.0/24"
}

# ===== SSH 瀵嗛挜 =====
resource "alicloud_key_pair" "lab" {
  key_pair_name = var.key_pair_name
  public_key    = file(var.ssh_public_key_path)
}

# ===== 鎺у埗闈細3鍙帮紙鏍囧噯 kubeadm HA锛?====
resource "alicloud_instance" "masters" {
  count = 3

  instance_name              = "${var.name_prefix}-master-${count.index + 1}"
  host_name                  = "${var.name_prefix}-master-${count.index + 1}"
  instance_type              = var.master_instance_type
  security_groups            = [alicloud_security_group.lab.id]
  vswitch_id                 = alicloud_vswitch.lab.id
  private_ip                 = "172.16.0.${11 + count.index}"
  image_id                   = data.alicloud_images.ubuntu.images[0].id
  internet_max_bandwidth_out = var.bandwidth_out
  system_disk_category       = "cloud_essd"
  system_disk_size           = 40
  key_name                   = alicloud_key_pair.lab.key_pair_name
  password                   = var.node_password
  instance_charge_type       = "PostPaid"

  user_data = file("${path.module}/cloud-init/common-init.yaml")
}

# ===== 宸ヤ綔鑺傜偣锛氭暟閲忓彲璋冿紙3=婕旂ず锛?=3涓?浠庯級=====
resource "alicloud_instance" "workers" {
  count = var.worker_count

  instance_name              = "${var.name_prefix}-worker-${count.index + 1}"
  host_name                  = "${var.name_prefix}-worker-${count.index + 1}"
  instance_type              = var.worker_instance_type
  security_groups            = [alicloud_security_group.lab.id]
  vswitch_id                 = alicloud_vswitch.lab.id
  private_ip                 = "172.16.0.${21 + count.index}"
  image_id                   = data.alicloud_images.ubuntu.images[0].id
  internet_max_bandwidth_out = var.bandwidth_out
  system_disk_category       = "cloud_essd"
  system_disk_size           = 40
  key_name                   = alicloud_key_pair.lab.key_pair_name
  password                   = var.node_password
  instance_charge_type       = "PostPaid"

  user_data = file("${path.module}/cloud-init/common-init.yaml")
}

# ===== 内网 SLB：为 API Server 提供真正的高可用入口 =====
resource "alicloud_slb_load_balancer" "api" {
  count = var.enable_ha_slb ? 1 : 0

  load_balancer_name = "${var.name_prefix}-api"
  address_type       = "intranet"
  vswitch_id         = alicloud_vswitch.lab.id
}

resource "alicloud_slb_listener" "api" {
  count = var.enable_ha_slb ? 1 : 0

  load_balancer_id          = alicloud_slb_load_balancer.api[0].id
  protocol                  = "tcp"
  frontend_port             = 6443
  backend_port              = 6443
  bandwidth                 = 100
  scheduler                 = "wrr"
  health_check              = "on"
  health_check_type         = "tcp"
  health_check_connect_port = 6443
  healthy_threshold         = 3
  unhealthy_threshold       = 3
  health_check_interval     = 10
  health_check_timeout      = 5
}

resource "alicloud_slb_backend_server" "api" {
  count = var.enable_ha_slb ? 1 : 0

  load_balancer_id = alicloud_slb_load_balancer.api[0].id
  dynamic "backend_servers" {
    for_each = alicloud_instance.masters[*].id
    content {
      server_id = backend_servers.value
      type      = "ecs"
      weight    = 100
    }
  }
  depends_on = [alicloud_slb_listener.api]
}
