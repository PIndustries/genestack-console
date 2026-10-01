output "hosts" {
  value = [
    for n in openstack_compute_instance_v2.node : {
      hostname = n.name
      ip       = n.access_ip_v4
      role     = var.role
    }
  ]
}
