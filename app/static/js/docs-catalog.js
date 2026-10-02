/* Genestack Console API — catalog of what exists and what it does.
   Keep in sync with genestack.dev/public/docs.js */
(function () {
  const host = (location.hostname || "").replace(/^www\./, "");
  const onSite = host === "genestack.dev";
  const BASE = onSite ? "http://127.0.0.1:8080" : location.origin || "http://127.0.0.1:8080";
  const GROUPS = [
    {
      id: "start",
      title: "Start",
      blurb: "Health, then a token. Everything else is Bearer or an API key from config.",
      routes: [
        { m: "GET", p: "/health", d: "Is the Console up, which version, dry-run or live." },
        { m: "GET", p: "/health/ready", d: "Readiness for the load balancer." },
        {
          m: "POST",
          p: "/api/v1/oauth/token",
          d: "OAuth 2 token endpoint. grant_type is password, authorization_code, or refresh_token. A second login is a second session. Refresh rotates one login.",
          body: "grant_type=password&username=you&password=…",
        },
        {
          m: "GET",
          p: "/api/v1/oauth/authorize",
          d: "OAuth 2 authorization code with PKCE S256. The browser returns to redirect_uri with a one-time code.",
        },
        {
          m: "POST",
          p: "/api/v1/oauth/revoke",
          d: "Drop the session for one access token or refresh token. The other login stays.",
          body: "token=…",
        },
        {
          m: "GET",
          p: "/.well-known/oauth-authorization-server",
          d: "OAuth 2 discovery: token, authorize, revoke, and introspect URLs.",
        },
        {
          m: "POST",
          p: "/api/v1/auth/login",
          d: "Same session as the token endpoint, for a client that already posts JSON here.",
          body: '{"username":"you","password":"…"}',
        },
        { m: "GET", p: "/api/v1/auth/whoami", d: "Who you are, role, and which tenants you belong to." },
        { m: "POST", p: "/api/v1/auth/logout", d: "Drop the session. No-op for static API keys." },
        { m: "POST", p: "/api/v1/auth/ticket", d: "One-shot ticket for browser streams that cannot send headers." },
      ],
    },
    {
      id: "tenants",
      title: "Tenants",
      blurb: "One Console, many tenants. Members and roles live here. Environments belong to a tenant.",
      routes: [
        { m: "GET", p: "/api/v1/tenants", d: "List tenants you can see." },
        { m: "POST", p: "/api/v1/tenants", d: "Create a tenant (platform admin).", body: '{"name":"acme"}' },
        { m: "GET", p: "/api/v1/tenants/{id}", d: "Tenant detail." },
        { m: "PATCH", p: "/api/v1/tenants/{id}", d: "Rename or describe a tenant." },
        { m: "DELETE", p: "/api/v1/tenants/{id}", d: "Remove a tenant." },
        { m: "GET", p: "/api/v1/tenants/{id}/members", d: "Who is in this tenant, and their role." },
        { m: "POST", p: "/api/v1/tenants/{id}/members", d: "Add a member.", body: '{"username":"alice","role":"operator"}' },
        { m: "DELETE", p: "/api/v1/tenants/{id}/members/{user}", d: "Remove a member." },
        { m: "GET", p: "/api/v1/users", d: "List local users (platform admin)." },
        { m: "POST", p: "/api/v1/users", d: "Create a local user." },
      ],
    },
    {
      id: "environments",
      title: "Environments",
      blurb: "Each environment is a Genestack cloud: metal, Talos, Kubernetes, OpenStack. One Console runs as many as you need.",
      routes: [
        { m: "GET", p: "/api/v1/environments", d: "List environments in your tenant scope." },
        { m: "POST", p: "/api/v1/environments", d: "Create an environment.", body: '{"name":"prod","tenant_id":"$TENANT"}' },
        { m: "GET", p: "/api/v1/environments/{id}", d: "Detail for one environment." },
        { m: "PATCH", p: "/api/v1/environments/{id}", d: "Update name, description, tenant." },
        { m: "DELETE", p: "/api/v1/environments/{id}", d: "Delete the environment record." },
        { m: "GET", p: "/api/v1/environments/{id}/inventory", d: "Machines assigned to this environment." },
        { m: "GET", p: "/api/v1/environments/{id}/workflow", d: "Guided setup state: connect → fabric → deploy → operate." },
        { m: "GET", p: "/api/v1/environments/{id}/access/kubeconfig", d: "Download kubeconfig." },
        { m: "GET", p: "/api/v1/environments/{id}/access/talosconfig", d: "Download talosconfig." },
        { m: "GET", p: "/api/v1/fleet", d: "Fleet-wide summary across environments." },
      ],
    },
    {
      id: "reach",
      title: "Reach",
      blurb: "How this deploy host reaches an environment. WireGuard is served here. Tailscale and Cloudflare Tunnel are joined here. Secrets are write-only.",
      routes: [
        { m: "GET", p: "/api/v1/reach", d: "WireGuard, Tailscale, and Cloudflare status. No keys." },
        {
          m: "PUT",
          p: "/api/v1/reach/{kind}",
          d: "Platform admin. kind is wireguard, tailscale, or cloudflare. A secret is stored and not returned.",
          body: '{"enabled":true,"endpoint":"203.0.113.10:51820"}',
        },
        { m: "POST", p: "/api/v1/reach/{kind}/apply", d: "Write the config and apply it. A missing program is reported. Nothing starts at boot." },
        { m: "POST", p: "/api/v1/reach/{kind}/stop", d: "Stop WireGuard or the cloudflared process this console started. Tailscale is left up." },
        { m: "GET", p: "/api/v1/environments/{id}/reach", d: "Paths saved for this environment. No private keys." },
        {
          m: "POST",
          p: "/api/v1/environments/{id}/reach/{kind}",
          d: "Attach a path. A WireGuard peer returns its client config once.",
          body: '{"name":"default","address":"node.tailnet.ts.net"}',
        },
        { m: "DELETE", p: "/api/v1/environments/{id}/reach/{kind}/{name}", d: "Remove that path. Another environment's peer stays." },
      ],
    },
    {
      id: "database",
      title: "Database",
      blurb: "Where this console stores its own data. SQLite is the default. Postgres is the other supported database. The password is not returned.",
      routes: [
        { m: "GET", p: "/api/v1/database", d: "Engine kind, sqlite or postgresql, and the URL with the password replaced. Platform admin." },
        {
          m: "POST",
          p: "/api/v1/database/move",
          d: "Copy every table onto an empty target, create the schema, and write database_url. Restart the console after. Platform admin. The password is not returned.",
          body: '{"target_url":"postgresql+psycopg://console@127.0.0.1:5432/console"}',
        },
      ],
    },
    {
      id: "traces",
      title: "Traces",
      blurb: "Spans of this console process and of modules people add. In memory. A restart clears them. Admin only. No tokens. On main. Not in the v2026.10.03 binary.",
      routes: [
        { m: "GET", p: "/api/v1/traces", d: "Recent spans, newest last. limit defaults to 50 and caps at 200." },
        {
          m: "POST",
          p: "/api/v1/traces",
          d: "Append one span. status is ok or error. name is at most 128 characters.",
          body: '{"name":"hello.say","duration_ms":1.5,"status":"ok"}',
        },
      ],
    },
    {
      id: "hardware",
      title: "Hardware",
      blurb: "Inventory and provision. Terraform bare metal (Rackspace, AWS, Azure, GCP), OVH via API, PXE, SSH, BMC/Redfish.",
      routes: [
        { m: "GET", p: "/api/v1/environments/{id}/baremetal", d: "BMC-registered nodes: power, PXE, provision." },
        { m: "GET", p: "/api/v1/environments/{id}/discovery", d: "PXE sightings and Redfish BMC finds." },
        { m: "POST", p: "/api/v1/environments/{id}/discovery/claim", d: "Claim a discovered PXE host into inventory." },
        { m: "POST", p: "/api/v1/environments/{id}/discovery/bmc-creds", d: "Attach Redfish credentials to a found BMC." },
        { m: "GET", p: "/api/v1/environments/{id}/pxe", d: "PXE sidecar status." },
        { m: "GET", p: "/api/v1/environments/{id}/servers", d: "Servers bound to the environment." },
        {
          m: "POST",
          p: "/api/v1/environments/{id}/jobs",
          d: "baremetal.bmc_scan, hardware.terraform.plan, hardware.terraform.apply, power, pxe_boot, provision.",
          body: '{"operation":"hardware.terraform.plan","params":{"account_id":"$ACCOUNT"}}',
        },
      ],
    },
    {
      id: "talos",
      title: "Talos",
      blurb: "The OS for the plane. API, not SSH. Health, etcd, machineconfig, disks, logs, resources, events, containers, services, reboot, shutdown, reset, upgrade.",
      routes: [
        { m: "GET", p: "/api/v1/environments/{id}/platform", d: "Machines, versions, Ready, Talos reachability." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/health", d: "talosctl health --wait=false." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/etcd", d: "etcd members and status." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/machineconfig", d: "Live machineconfig YAML." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/disks", d: "Disks and discovered volumes." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/dmesg", d: "Kernel logs from that node." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/logs", d: "Service logs (query service=kubelet|containerd|machined|…)." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/resources", d: "Memory, CPU, runtime, network — degrades per command." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/events", d: "Talos events (--since if given)." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/containers", d: "CRI / system containers." },
        { m: "GET", p: "/api/v1/environments/{id}/platform/nodes/{name}/services", d: "apid, machined, kubelet, …" },
        { m: "POST", p: "/api/v1/environments/{id}/platform/nodes/{name}/reboot", d: "Enqueue platform.talos.reboot (audited job + env lock)." },
        { m: "POST", p: "/api/v1/environments/{id}/platform/nodes/{name}/shutdown", d: "Enqueue platform.talos.shutdown (audited job + env lock)." },
        {
          m: "POST",
          p: "/api/v1/environments/{id}/platform/nodes/{name}/reset",
          d: "Enqueue platform.talos.reset (audited job + env lock). graceful/reboot/wipe flags.",
          body: '{"graceful":true,"reboot":true,"wipe":true}',
        },
        {
          m: "POST",
          p: "/api/v1/environments/{id}/platform/nodes/{name}/apply-config",
          d: "Enqueue platform.talos.apply_config (audited job + env lock).",
          body: '{"yaml":"machine: {}","mode":"auto"}',
        },
        { m: "POST", p: "/api/v1/environments/{id}/platform/nodes/{name}/service/{id}/{action}", d: "start | stop | restart a Talos service." },
        { m: "POST", p: "/api/v1/environments/{id}/platform/nodes/{name}/upgrade", d: "Enqueue platform.talos.upgrade (audited job + env lock)." },
        { m: "GET", p: "/api/v1/environments/{id}/cluster", d: "Live cluster snapshot." },
      ],
    },
    {
      id: "k8s",
      title: "Kubernetes",
      blurb: "The plane Genestack already runs on. Day-2 control of workloads, networking, storage, helm, and nodes — from this Console.",
      routes: [
        { m: "GET", p: "/api/v1/environments/{id}/k8s/workloads", d: "Deployments, StatefulSets, DaemonSets, pods." },
        {
          m: "POST",
          p: "/api/v1/environments/{id}/k8s/workloads/{kind}/{ns}/{name}/scale",
          d: "Scale a Deployment or StatefulSet.",
          body: '{"replicas":3}',
        },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/workloads/{kind}/{ns}/{name}/restart", d: "Rolling restart." },
        { m: "DELETE", p: "/api/v1/environments/{id}/k8s/workloads/{kind}/{ns}/{name}", d: "Delete a Deployment, StatefulSet, or DaemonSet." },
        { m: "DELETE", p: "/api/v1/environments/{id}/k8s/pods/{ns}/{name}", d: "Delete a pod." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/events", d: "Cluster or namespace events." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/namespaces", d: "Namespaces and phases." },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/namespaces", d: "Create a namespace.", body: '{"name":"apps"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/k8s/namespaces/{ns}", d: "Delete a namespace." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/services", d: "Services: type, ClusterIP, ports." },
        { m: "DELETE", p: "/api/v1/environments/{id}/k8s/services/{ns}/{name}", d: "Delete a Service." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/ingresses", d: "Ingress hosts, class, addresses." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/persistentvolumeclaims", d: "PVCs plus PVs and StorageClasses." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/persistentvolumes", d: "PersistentVolumes." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/storageclasses", d: "StorageClasses." },
        { m: "DELETE", p: "/api/v1/environments/{id}/k8s/persistentvolumeclaims/{ns}/{name}", d: "Delete a PVC." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/configmaps", d: "ConfigMaps (key names, not values)." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/secrets", d: "Secret names and types only. Never values." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/jobs", d: "batch/v1 Jobs." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/helm", d: "Helm releases: name, namespace, revision, status, chart." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/helm/{ns}/{name}", d: "Helm status without values or manifests." },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/helm/{ns}/{name}/history", d: "Helm revision history." },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/helm/{ns}/{name}/rollback", d: "Roll back a release.", body: '{"revision":2}' },
        {
          m: "POST",
          p: "/api/v1/environments/{id}/k8s/helm/{ns}/{name}/upgrade",
          d: "Upgrade an existing release (--reuse-values).",
          body: '{"chart":"nova"}',
        },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/nodes", d: "Nodes: labels, taints, unschedulable, conditions, capacity." },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/nodes/{name}/cordon", d: "Cordon a node." },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/nodes/{name}/uncordon", d: "Uncordon a node." },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/nodes/{name}/drain", d: "Enqueue k8s.node.drain (audited job + env lock)." },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/nodes/{name}/taint", d: "Add a taint.", body: '{"key":"dedicated","value":"gpu","effect":"NoSchedule"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/k8s/nodes/{name}/taint", d: "Remove a taint.", body: '{"key":"dedicated","effect":"NoSchedule"}' },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/nodes/{name}/label", d: "Set a node label.", body: '{"key":"role","value":"worker"}' },
        { m: "POST", p: "/api/v1/environments/{id}/k8s/apply", d: "Enqueue k8s.apply (audited job + env lock; honors dry_run).", body: '{"yaml":"apiVersion: v1\\nkind: ConfigMap\\nmetadata:\\n  name: demo\\n"}' },
        { m: "GET", p: "/api/v1/environments/{id}/k8s/describe", d: "Describe an object. Secret data is stripped." },
      ],
    },
    {
      id: "cloud",
      title: "OpenStack",
      blurb: "The cloud APIs as they land on that cluster. Instances, volumes, networks, identity, consoles.",
      routes: [
        { m: "GET", p: "/api/v1/environments/{id}/cloud", d: "Cloud overview: instances, images, volumes, nets, identity." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/servers", d: "Create an instance." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/servers/{id}/{action}", d: "Reboot, start, stop, pause, shelve, confirm-resize, …" },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/servers/{id}", d: "Delete an instance." },
        { m: "GET", p: "/api/v1/environments/{id}/cloud/servers/{id}/console", d: "Serial / VNC console URL." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/servers/{id}/console/session", d: "Mint an in-console noVNC session." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/servers/{id}/resize", d: "Resize an instance.", body: '{"flavor":"m1.small"}' },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/servers/{id}/rebuild", d: "Rebuild an instance from an image.", body: '{"image":"$IMAGE"}' },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/servers/{id}/snapshot", d: "Snapshot an instance to a Glance image.", body: '{"name":"snap-1"}' },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/servers/{id}/security-groups", d: "Add a security group to an instance.", body: '{"name":"default"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/servers/{id}/security-groups/{name}", d: "Remove a security group from an instance." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/images", d: "Create a Glance image (metadata + optional web-download URL).", body: '{"name":"cirros","url":"https://example/cirros.img"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/images/{id}", d: "Delete an image." },
        { m: "PATCH", p: "/api/v1/environments/{id}/cloud/images/{id}", d: "Rename or change image visibility.", body: '{"visibility":"public"}' },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/flavors", d: "Create a flavor.", body: '{"name":"m1.tiny","vcpus":1,"ram":512,"disk":1}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/flavors/{id}", d: "Delete a flavor." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/keypairs", d: "Generate or import a key pair.", body: '{"name":"laptop"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/keypairs/{name}", d: "Delete a key pair." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/security-groups", d: "Create a security group.", body: '{"name":"web"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/security-groups/{id}", d: "Delete a security group." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/security-groups/{id}/rules", d: "Add a security-group rule." },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/security-groups/{id}/rules/{id}", d: "Delete a security-group rule." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/volumes", d: "Create a volume." },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/volumes/{id}", d: "Delete a volume." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/volumes/attach", d: "Attach a volume." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/volumes/detach", d: "Detach a volume." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/volumes/{id}/extend", d: "Extend a volume.", body: '{"size":20}' },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/volumes/{id}/snapshot", d: "Snapshot a volume.", body: '{"name":"data-1-snap"}' },
        { m: "GET", p: "/api/v1/environments/{id}/cloud/volume-snapshots", d: "List volume snapshots." },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/volume-snapshots/{id}", d: "Delete a volume snapshot." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/networks", d: "Create a network." },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/networks/{id}", d: "Delete a network." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/subnets", d: "Create a subnet.", body: '{"network":"$NET","cidr":"10.0.0.0/24"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/subnets/{id}", d: "Delete a subnet." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/routers", d: "Create a router." },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/routers/{id}", d: "Delete a router." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/routers/{id}/interfaces", d: "Add a router interface." },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/routers/{id}/interfaces/{subnet_id}", d: "Remove a router interface." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/floating-ips", d: "Allocate a floating IP." },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/floating-ips/{id}", d: "Release a floating IP." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/floating-ips/associate", d: "Associate a floating IP." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/floating-ips/disassociate", d: "Disassociate a floating IP.", body: '{"address":"203.0.113.10"}' },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/projects", d: "Create a Keystone project.", body: '{"name":"demo","enabled":true}' },
        { m: "PATCH", p: "/api/v1/environments/{id}/cloud/projects/{id}", d: "Update a project.", body: '{"enabled":false}' },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/users", d: "Create a Keystone user.", body: '{"name":"alice","password":"…"}' },
        { m: "PATCH", p: "/api/v1/environments/{id}/cloud/users/{id}", d: "Enable/disable a user or set a password.", body: '{"enabled":true}' },
        { m: "PUT", p: "/api/v1/environments/{id}/cloud/quotas", d: "Set quotas." },
        { m: "GET", p: "/api/v1/environments/{id}/cloud/load-balancers", d: "List Octavia load balancers (available:false if not in catalog)." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/load-balancers", d: "Create a load balancer.", body: '{"name":"lb-1","vip_subnet_id":"$SUBNET"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/load-balancers/{id}", d: "Delete a load balancer." },
        { m: "GET", p: "/api/v1/environments/{id}/cloud/dns-zones", d: "List Designate DNS zones (available:false if not in catalog)." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/dns-zones", d: "Create a DNS zone.", body: '{"name":"example.com.","email":"hostmaster@example.com"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/dns-zones/{id}", d: "Delete a DNS zone." },
        { m: "GET", p: "/api/v1/environments/{id}/cloud/secrets", d: "List Barbican secrets (available:false if not in catalog)." },
        { m: "POST", p: "/api/v1/environments/{id}/cloud/secrets", d: "Store a secret.", body: '{"name":"api-token","payload":"…"}' },
        { m: "DELETE", p: "/api/v1/environments/{id}/cloud/secrets/{id}", d: "Delete a secret." },
      ],
    },
    {
      id: "observe",
      title: "Observe",
      blurb: "Talos, Kubernetes, OpenStack, jobs, alerts, and metrics in this Console. Live tiles plus stored series — not another product.",
      routes: [
        {
          m: "GET",
          p: "/api/v1/environments/{id}/observe",
          d: "Live tiles plus downsampled series for one environment (viewer). Query hours=1..168 (default 24).",
        },
        {
          m: "GET",
          p: "/api/v1/fleet/observe",
          d: "Per-environment live tiles and optional series rollup across tenants you can see (viewer).",
        },
        {
          m: "GET",
          p: "/api/v1/environments/{id}/metrics/names",
          d: "Metric names with samples in the retention window. Empty list when none.",
        },
        {
          m: "GET",
          p: "/api/v1/environments/{id}/metrics/series",
          d: "Downsampled series for one metric. Query name, hours, bucket_minutes.",
        },
      ],
    },
    {
      id: "jobs",
      title: "Jobs",
      blurb: "Mutations are jobs. Create, poll, retry, cancel. The catalog lists every operation the Console can run.",
      routes: [
        { m: "GET", p: "/api/v1/operations", d: "Catalog: verify, helm, Talos upgrade, BMC scan, …" },
        { m: "POST", p: "/api/v1/environments/{id}/jobs", d: "Start an operation on an environment.", body: '{"operation":"verify"}' },
        { m: "GET", p: "/api/v1/jobs", d: "List jobs." },
        { m: "GET", p: "/api/v1/jobs/{id}", d: "Status, logs, result." },
        { m: "POST", p: "/api/v1/jobs/{id}/retry", d: "Retry a failed job." },
        { m: "POST", p: "/api/v1/jobs/{id}/cancel", d: "Cancel a running job." },
        { m: "GET", p: "/api/v1/audit", d: "Who did what." },
        { m: "GET", p: "/api/v1/stream", d: "SSE: live job and cluster events." },
      ],
    },
  ];

  const root = document.getElementById("docs-root");
  const nav = document.getElementById("docs-nav");
  const search = document.getElementById("docs-search");
  if (!root || !nav) return;

  const TOTAL = GROUPS.reduce(function (n, g) {
    return n + g.routes.length;
  }, 0);

  function esc(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function fillPath(group, path) {
    return path
      .replace("/environments/{id}", "/environments/$ENV")
      .replace("/tenants/{id}", "/tenants/$TENANT")
      .replace("/jobs/{id}", "/jobs/$JOB")
      .replace("/cloud/servers/{id}", "/cloud/servers/$SERVER")
      .replace("/cloud/keypairs/{name}", "/cloud/keypairs/mykey")
      .replace("/security-groups/{name}", "/security-groups/default")
      .replace("{subnet_id}", "$SUBNET")
      .replace("{image_id}", "$IMAGE")
      .replace("{volume_id}", "$VOLUME")
      .replace("{snapshot_id}", "$SNAP")
      .replace("{network_id}", "$NET")
      .replace("{router_id}", "$ROUTER")
      .replace("{project_id}", "$PROJECT")
      .replace("{user_id}", "$USER")
      .replace("{zone_id}", "$ZONE")
      .replace("{secret_id}", "$SECRET")
      .replace("{lb_id}", "$LB")
      .replace("{fip_id}", "$FIP")
      .replace("{flavor_id}", "$FLAVOR")
      .replace("{sg_id}", "$SG")
      .replace("{environment_id}", "$ENV")
      .replace("{tenant_id}", "$TENANT")
      .replace("{user}", "alice")
      .replace("{name}", "control-a")
      .replace("{kind}", "deployments")
      .replace("{ns}", "openstack")
      .replace("{namespace}", "openstack")
      .replace("{action}", "reboot")
      .replace("{server_id}", "$SERVER")
      .replace("{id}", group.id === "tenants" ? "$TENANT" : group.id === "jobs" ? "$JOB" : "$ENV")
      .replace(/\{[^}]+\}/g, "x");
  }

  function publicPath(route) {
    return (
      route.p === "/health" ||
      route.p.indexOf("/health/") === 0 ||
      route.p === "/api/v1/auth/login"
    );
  }

  function curlFor(group, route) {
    const url = BASE + fillPath(group, route.p);
    const lines = ["curl -s"];
    if (route.m !== "GET") lines[0] += " -X " + route.m;
    if (!publicPath(route)) lines.push('  -H "Authorization: Bearer $TOKEN"');
    if (route.body) {
      lines.push('  -H "Content-Type: application/json"');
      lines.push("  -d '" + route.body + "'");
    } else if (route.m !== "GET" && route.m !== "DELETE") {
      lines.push('  -H "Content-Type: application/json"');
    }
    lines.push("  " + url);
    return lines[0] + (lines.length > 1 ? " \\\n" + lines.slice(1).join(" \\\n") : "");
  }

  function matches(g, r, q) {
    if (!q) return true;
    return (r.p + " " + r.d + " " + r.m + " " + g.title).toLowerCase().indexOf(q) !== -1;
  }

  function render(filter) {
    const q = (filter || "").trim().toLowerCase();
    const shown = GROUPS.map(function (g) {
      const routes = g.routes.filter(function (r) {
        return matches(g, r, q);
      });
      return { g: g, routes: routes };
    }).filter(function (row) {
      return row.routes.length > 0;
    });

    nav.innerHTML = GROUPS.map(function (g) {
      const n = q
        ? (shown.filter(function (s) {
            return s.g.id === g.id;
          })[0] || { routes: [] }).routes.length
        : g.routes.length;
      const off = q && n === 0 ? " muted" : "";
      return (
        '<a href="#' +
        g.id +
        '" data-nav="' +
        g.id +
        '" class="' +
        off +
        '"><span>' +
        esc(g.title) +
        "</span><b>" +
        n +
        "</b></a>"
      );
    }).join("");

    if (!shown.length) {
      root.innerHTML =
        '<p class="docs-empty">Nothing matches <strong>' +
        esc(filter) +
        "</strong>. Try a path, a verb, or an area — tenants, hardware, Talos.</p>";
      return;
    }

    root.innerHTML = shown
      .map(function (row) {
        const g = row.g;
        return (
          '<section class="docs-group" id="' +
          g.id +
          '">' +
          "<header><h2>" +
          esc(g.title) +
          "</h2><p>" +
          esc(g.blurb) +
          "</p></header>" +
          row.routes
            .map(function (r, i) {
              const id = g.id + "-" + i;
              const curl = curlFor(g, r);
              return (
                '<article class="ep" id="' +
                id +
                '">' +
                '<button type="button" class="ep-head" data-open="' +
                id +
                '" aria-expanded="false">' +
                '<span class="verb ' +
                r.m.toLowerCase() +
                '">' +
                r.m +
                "</span>" +
                '<code class="ep-path">' +
                esc(r.p) +
                "</code>" +
                '<span class="ep-does">' +
                esc(r.d) +
                "</span>" +
                "</button>" +
                '<div class="ep-body" hidden>' +
                "<pre>" +
                esc(curl) +
                "</pre>" +
                '<button type="button" class="ep-copy" data-copy="' +
                id +
                '">Copy curl</button>' +
                "</div>" +
                "</article>"
              );
            })
            .join("") +
          "</section>"
        );
      })
      .join("");

    markNav(location.hash.replace(/^#/, "") || (shown[0] && shown[0].g.id));
  }

  function closeAll() {
    root.querySelectorAll(".ep-body").forEach(function (b) {
      b.hidden = true;
    });
    root.querySelectorAll(".ep").forEach(function (a) {
      a.classList.remove("on");
    });
    root.querySelectorAll(".ep-head").forEach(function (b) {
      b.setAttribute("aria-expanded", "false");
    });
  }

  function markNav(id) {
    nav.querySelectorAll("a").forEach(function (a) {
      a.classList.toggle("on", a.dataset.nav === id);
    });
  }

  root.addEventListener("click", function (e) {
    const copy = e.target.closest("[data-copy]");
    if (copy) {
      const art = document.getElementById(copy.dataset.copy);
      const pre = art && art.querySelector("pre");
      if (!pre) return;
      const text = pre.textContent || "";
      const done = function () {
        copy.textContent = "Copied";
        setTimeout(function () {
          copy.textContent = "Copy curl";
        }, 1400);
      };
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(done).catch(done);
      } else {
        done();
      }
      return;
    }
    const btn = e.target.closest("[data-open]");
    if (!btn) return;
    const art = btn.closest(".ep");
    const body = art && art.querySelector(".ep-body");
    if (!body) return;
    const wasClosed = body.hidden;
    closeAll();
    if (wasClosed) {
      body.hidden = false;
      art.classList.add("on");
      btn.setAttribute("aria-expanded", "true");
    }
  });

  if (search) {
    search.addEventListener("input", function () {
      render(search.value);
    });
  }

  document.addEventListener("keydown", function (e) {
    if (e.key === "/" && document.activeElement !== search) {
      e.preventDefault();
      if (search) search.focus();
    }
    if (e.key === "Escape" && document.activeElement === search) {
      search.blur();
    }
  });

  nav.addEventListener("click", function (e) {
    const a = e.target.closest("a[data-nav]");
    if (!a) return;
    markNav(a.dataset.nav);
  });

  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(
      function (entries) {
        const vis = entries
          .filter(function (en) {
            return en.isIntersecting;
          })
          .sort(function (a, b) {
            return a.boundingClientRect.top - b.boundingClientRect.top;
          })[0];
        if (vis && vis.target.id) markNav(vis.target.id);
      },
      { rootMargin: "-20% 0px -65% 0px", threshold: 0 }
    );
    const watch = function () {
      root.querySelectorAll(".docs-group").forEach(function (sec) {
        io.observe(sec);
      });
    };
    const orig = render;
    render = function (filter) {
      orig(filter);
      io.disconnect();
      watch();
    };
  }

  const heroLead = document.querySelector(".docs-hero .lead");
  if (heroLead) {
    const note = document.createElement("p");
    note.className = "docs-count";
    note.textContent = TOTAL + " endpoints · " + GROUPS.length + " areas";
    heroLead.after(note);
  }

  const baseEl = document.getElementById("docs-base");
  if (baseEl) baseEl.textContent = BASE;

  const also = document.getElementById("docs-also");
  if (also) {
    also.innerHTML = onSite
      ? "A running Console serves this same catalog at <code>/docs</code>. Live OpenAPI (try it against your box) is <code>/swagger</code>."
      : 'Live OpenAPI for this Console: <a href="/swagger">/swagger</a>. Public catalog: <a href="https://genestack.dev/docs">genestack.dev/docs</a>.';
  }

  function openFromHash() {
    const id = location.hash.replace(/^#/, "");
    if (!id) return;
    const el = document.getElementById(id);
    if (!el) return;
    if (el.classList.contains("ep")) {
      const btn = el.querySelector("[data-open]");
      const body = el.querySelector(".ep-body");
      if (btn && body && body.hidden) btn.click();
      el.scrollIntoView({ block: "center" });
      return;
    }
    el.scrollIntoView();
    markNav(id);
  }

  const params = new URLSearchParams(location.search);
  if (search && params.get("q")) {
    search.value = params.get("q");
    render(search.value);
  } else {
    render("");
  }
  openFromHash();
  addEventListener("hashchange", openFromHash);
})();
