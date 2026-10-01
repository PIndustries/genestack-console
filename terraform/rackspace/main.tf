terraform {
  required_version = ">= 1.3.0"
  required_providers {
    openstack = {
      source  = "terraform-provider-openstack/openstack"
      version = "~> 1.54"
    }
  }
}

provider "openstack" {
  user_name   = var.username
  password    = var.api_key
  tenant_name = var.tenant_name != "" ? var.tenant_name : var.username
  auth_url    = var.auth_url
  region      = var.region
}

resource "openstack_compute_instance_v2" "node" {
  count       = var.node_count
  name        = "${var.name_prefix}-${count.index + 1}"
  flavor_name = var.flavor
  image_name  = var.image
  metadata = {
    role = var.role
  }
}
