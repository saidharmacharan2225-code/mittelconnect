output "sites" {
  description = "Per-site connection details and the next commands to run."
  value = {
    for name, server in hcloud_server.site : name => {
      public_ipv4      = server.ipv4_address
      public_ipv6      = server.ipv6_address
      private_ip       = local.site_private_ip[name]
      ssh              = "deploy@${server.ipv4_address}"
      wireguard_peer   = "${server.ipv4_address}:${var.wireguard_port}"
      wireguard_pubkey = "ssh deploy@${server.ipv4_address} cat /etc/wireguard/server.pub"
      first_deploy     = "scripts/deploy.sh deploy@${server.ipv4_address} --init ${name}"
      metrics_target   = "${local.site_private_ip[name]}:9464"
    }
  }
}

output "private_network_id" {
  description = "Attach the Prometheus server to this network to scrape every site's mcmon."
  value       = hcloud_network.main.id
}
