# Native live transports

These additions stream actual subprocess/vendor transports. They do not
synthesize video frames or relabel periodic database reads as Kubernetes watches.
No deployment or hardware connection is performed by the tests.

## Kubernetes watch

`GET /api/v1/environments/{id}/native/kubernetes/watch?resource=pods&namespace=openstack`
accepts viewer-scoped console header credentials or a single-use `ticket` query
parameter from `POST /api/v1/auth/ticket`. Hosted cookie clients must mint the
ticket through their hosted API bridge; this stream does not invent a second
cookie authentication mechanism. Reconnect requires a fresh ticket.

Allowed resources: `pods`, `nodes`, `events`, `services`, `deployments`,
`statefulsets`, `daemonsets`, `jobs`. Namespace is optional; omitted means all
namespaces for namespaced kinds. Nodes reject namespace. Secrets, ConfigMaps,
arbitrary resources and user-supplied commands are not allowed.

The source command uses real `kubectl get --watch --output-watch-events
--output=json`. Kubernetes emits existing objects as initial ADDED events and
then incremental changes, documented in the
[official kubectl reference](https://kubernetes.io/docs/reference/kubectl/generated/kubectl_get/).
The client's stream envelope is:

```json
{
  "topic": "env:environment-id",
  "sequence": 2,
  "epoch": "per-connection-UUID",
  "payload": {
    "type": "kubernetes_watch",
    "resource": "pods",
    "change": "MODIFIED",
    "object": {
      "id": "object-UID",
      "name": "nova-api",
      "namespace": "openstack",
      "resource_version": "12345",
      "kind": "Pod",
      "status": "healthy"
    }
  }
}
```

Only identity and normalized Ready-condition state are emitted. There are no
specs, environment variables, annotations, labels or arbitrary event messages.
Other resource kinds report `unknown` health rather than guessing. These
notifications can drive a fresh native inspector read. The existing stored
topology endpoint remains a stored snapshot and is not silently rewritten by
individual watches.

## Pod logs

`GET /api/v1/environments/{id}/native/kubernetes/pods/{namespace}/{pod}/logs?container=name&tail=200`
uses the same viewer authentication. Container is optional; tail is 1–2,000.
The real command is `kubectl logs --follow --timestamps --tail=...`, with no
shell interpolation. Envelopes carry `payload.type: pod_log`, namespace, pod,
nullable container, and `line`. Each line uses the existing credential redactor.
Applications can log arbitrary information; this is pattern-based redaction,
not a guarantee that every application-defined secret can be recognized.

## Stream lifecycle and limits

Both sources use the existing envelope shape (`topic`, `payload`, `sequence`,
`epoch`). Control topic `stream` announces `connected` with source and
`resync_required: true`. EOF emits `complete`; failures emit `error` with a
static reason, never raw stderr. A 15-minute session emits `resync` and closes.
Clients must establish a new source stream rather than assuming replay.
A heartbeat comment is sent every 15 seconds. Environment membership and user
active status are checked on that interval while connected.

There are at most 16 native Kubernetes process streams per API process (or the
configured stream cap if smaller). Watch objects are limited to 1 MiB, log
lines to 64 KiB; malformed/oversize input closes with a generic error. HTTP
backpressure passes through bounded asyncio readers to the kernel pipe; there
is no unbounded fan-out queue. Disconnect terminates/reaps the child and cleans
staged kubeconfig files. No kubeconfig means 409; missing kubectl means 503;
there is never a fallback to the operator's default cluster. A rejected watch
or an unavailable source is not a successful live stream.

## Actual native console descriptors

Operator-scoped `POST /api/v1/environments/{id}/native/consoles/cloud/{server_id}`
returns `kind: rfb`, `frame_encoding: rfb`, `subprotocols: [binary]`, and a
same-origin `websocket_path` under `native/consoles/rfb/{session_id}`.
It uses the existing Nova console token and actual noVNC websockify transport;
the vendor token remains server-side. The native application needs a real RFB
client/decoder. WebSocket message boundaries are not framebuffer boundaries.

Operator-scoped `POST /api/v1/environments/{id}/native/consoles/baremetal/{node_id}`
returns `kind: ilo-dvc`, `frame_encoding: hpe-ilo-dvc`, `channels: [1,2]`, and
`websocket_path` under `native/consoles/ilo/{session_id}`. It requires the
existing shared iLO KVM hub, which authenticates to the BMC server-side. Older
direct-proxy installations return 409 instead of exposing a BMC session key.
For each channel a native client opens a separate authenticated WebSocket.
The existing hub sends byte 80 (AUTHENTICATE); the local client replies with
34 bytes: channel 1 or 2, byte 32, then 32 zero bytes. The hub replies byte 82
(AUTHENTICATED). Subsequent bytes are actual HPE DVC/CMD data; HID input is
forwarded by the existing hub. Each connection requires its own single-use
ticket (or normal console auth header), with operator tenant checks.

Both descriptors explicitly return `decoded_images: false` and
`renderer_required`. The iLO stream is **not JPEG, PNG, MJPEG or RFB**. Apple
ImageIO/Metal cannot decode proprietary DVC directly; a genuine DVC decoder is
still required for native framebuffer display. There is no fake frame endpoint.
BMC session keys, embed URLs and upstream URLs are never returned by the new
descriptor API. Creation uses normal principal resolution, preserving cookie
CSRF protections where the hosted-compatible auth change is installed.

## Existing native terminal contract

The actual path is `WS /api/v1/terminal?environment_id={id}&ticket={ticket}`,
admin-scoped. It already speaks native JSON with no browser assets:

- Client: `{"type":"input","data":"..."}` or `{"type":"resize","cols":80,"rows":24}`.
- Server: `{"type":"output","data":"..."}` or `{"type":"exit","code":0}`.

The output is incremental UTF-8 terminal data including ANSI escape sequences;
a native terminal emulator is needed for terminal behavior. The deployed
version can target the local console host where no deployer host is set; older
source rejects that case. Preserve the deployed handler when applying overlays.
The current terminal output queue is unbounded; this addition does not claim
that older terminal transport has the new watch stream's backpressure limits.
