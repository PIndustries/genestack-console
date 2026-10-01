output "hosts" {
  value = [
    for n in google_compute_instance.node : {
      hostname = n.name
      ip       = n.network_interface[0].network_ip
      role     = var.role
    }
  ]
}
