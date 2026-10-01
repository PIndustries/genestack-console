output "hosts" {
  value = [
    for n in aws_instance.node : {
      hostname = n.tags.Name
      ip       = n.private_ip
      role     = var.role
    }
  ]
}
