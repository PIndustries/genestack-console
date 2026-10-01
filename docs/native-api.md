# Native Apple presentation API

`GET /api/v1/environments/{environment_id}/native/topology` is an additive,
read-only presentation contract. It uses the existing authenticated viewer
and tenant-membership dependency. A missing environment returns 404; an
existing environment outside the caller's tenancy returns 403. It does not
add a new authentication system or expose the private hosted account service.

The schema is version 1:

```json
{
  "schema_version": 1,
  "environment_id": "environment UUID",
  "title": "Environment name",
  "generated_at": "2026-09-25T12:00:00Z",
  "is_demo": false,
  "nodes": [],
  "edges": [],
  "warnings": ["No cluster snapshot recorded; live topology is unknown."]
}
```

Nodes contain `id`, `label`, `kind`, `layer`, `status`, nullable `parent_id`
and nullable `detail`. Edges contain `id`, `source`, `target` and `kind`.
Identifiers are stable, kind-prefixed and percent-encoded per identity segment.
Layer names match the browser's semantic layers: `metal`, `overlay`, `k8s`,
`pods`, `nova`, `tenants`, `edge`. A client can lay these out using native
RealityKit entities or SwiftUI Canvas without translating browser coordinates.
Kinds currently emitted are `machine`, `k8s`, `ns`, `pod`, and `helm`.
Relationships are `hosts` (exact matching inventory/Kubernetes hostname),
`contains` (namespace membership), and `schedules` (observed pod node).
No relationship is guessed from similar names. Responses are capped at 2,000
nodes and 4,000 edges with explicit truncation warnings; dangling parents/edges
are removed. Inventory endpoints remain the source for complete resource lists.

Sources are environment-scoped registered hardware, latest stored server
configuration, and latest stored cluster snapshot. This endpoint makes no
provider calls. Hardware inventory/configuration never implies live hardware
health. Kubernetes readiness and pod Running+ready status are reported only
when the successful snapshot is at most five minutes old and not future-dated.
Older or failed observations remain visible with `unknown` health and warnings.
`generated_at` is graph assembly time; the snapshot timestamp is given in the
warnings. Namespace membership and Helm deployment presence do not imply health.

An empty graph means no stored resources were found, not a successful probe.
Network/Nova/tenant/ingress resources are not recorded by this snapshot source;
these layers stay empty and a warning explicitly describes the coverage gap.
The endpoint never synthesizes VMs, networks, ownership or working deployments.
Walkthrough environments use the existing demo predicate and always return
`is_demo: true` plus a sample warning. The native client must preserve that label.

Only identities and normalized state are serialized. Raw configuration,
provider credentials, SSH fields, BMC credentials, kubeconfig contents,
metadata and probe error strings are never included.

## Native operation forms

Existing `GET /api/v1/operations` parameter entries now have nullable `default`
and `enum` fields. Explicit enums cover host basic actions, allowed Ansible
playbooks, Redfish power actions, Tempest actions and hyperconverged platforms.
The Tempest default is `install-run`, matching its handler. All other omitted
values retain server semantics. The existing `secret_params` is authoritative
for secure inputs. Metadata does not replace server authorization or validation.
Job creation remains asynchronous when clients send `run_sync: false`.

## Loki read restoration

The existing Observe router referenced a missing `observe_logs` service and
prevented application import. This implementation reads real Loki log streams
through the environment kubeconfig and Kubernetes service proxy for
`monitoring/loki-gateway:80`, the service in existing Genestack Grafana and
OpenTelemetry configurations. A different installation layout is explicitly
unavailable until supported; there is no public URL or default-cluster fallback.
Queries are bounded to seven days and 2,000 lines. A caller supplies either
LogQL or namespace/pod filters. Loki failures are explicit `ok: false` results;
raw provider errors and query literals are not echoed. Returned log lines use
the existing credential redactor. Staged kubeconfig cleanup runs on success
and failure. Live Loki connectivity has not been verified by unit tests.

## Verification

Run from `genestack-console` with the repository dependencies installed:

```sh
python -m pytest tests/test_native_topology.py tests/test_native_contract_support.py tests/test_observe.py tests/test_tenants.py -q
```

Tests cover actual session resolution, cross-tenant denial, schema, identities,
relationships, secrets excluded, empty/demo/stale/failed/future states, main
router registration, operation metadata, and Loki command construction,
redaction, bounds and cleanup. Loki transport is stubbed only in unit tests;
there is no fake transport in production. The explicit synthetic demonstration
fixture emitted by the endpoint test is `/tmp/genestack-native-topology-demo.json`.

## Atomic native configuration saves

`GET /config` includes `supports_compare_and_swap: true`. Clients must check
this flag before depending on the precondition: older servers can ignore new
JSON fields. `PUT /config` accepts optional `expected_version`, with zero
meaning no configuration exists yet. A current version mismatch returns 409.
The insert uses exactly `expected_version + 1`, and the existing database
unique constraint on `(environment_id, version)` decides concurrent writers.
It never recomputes the next version after checking the precondition. A lost
race rolls the transaction back and returns 409. Existing clients omitting the
field keep their old behavior. Simultaneous writers were tested with file-based
SQLite; no live PostgreSQL was available for a concurrency test.

## Stream protocol 2

Native clients request `GET /api/v1/stream?topics=fleet,jobs,alerts,metrics,env:UUID&protocol=2`
with the existing authorization header or single-use stream ticket. Protocol 1
remains the default for browser compatibility. Every protocol-2 data envelope
adds `sequence` (starts at one per connection) and `epoch` (UUID per connection)
to the existing `topic`/`payload`. A `topic: stream` control payload announces
`type: connected, resync_required: true`. Overflow emits `type: resync,
reason: overflow, resync_required: true`. Changed membership visibility emits
the same control with `reason: scope_changed`. The client must bootstrap its
visible views on connect/reconnect/resync; there is no replay buffer. Sequence
gaps or epoch changes should also cause a bootstrap. Heartbeats remain `: hb`
comments every 15 seconds. Subscriptions are cleaned up on disconnect.

`GET /api/v1/native/capabilities` advertises `stream_protocol: 2`,
`stream_replay: false`, `stream_bootstrap_required: true`, and
`config_compare_and_swap: true` behind existing viewer authentication.

The existing DB relay now also tracks configuration versions, registered
hardware and environment metadata changes, emitting `type: topology` on
`env:UUID` and `fleet`; only the environment ID and a static reason are sent.
Worker-produced metric rows now emit the existing scoped `metrics` payload,
containing the count but no labels or values. Snapshot, alert, job status and
job-log growth relay behavior is retained. Direct environment-topic payloads
are now filtered again after membership visibility refresh, closing the old
connect-time-only authorization gap. The refresh interval remains 60 seconds.

This is streamed invalidation of persisted observations, not direct hardware
telemetry. Defaults are a 60-second cluster collector and a three-second DB
relay (250 ms while jobs are active). External Nova/network mutations that
are not persisted by these collectors do not generate immediate events.
Runtime collectors and relay must be enabled. A client should explicitly show
connection state and observation timestamps instead of labeling stale data live.

## Rollout boundary

A broad baseline regression run returned 1,532 passed, 59 failed and two skipped
before the final CAS/stream changes. Native-focused tests were green, but the
aggregate is not a production-release gate. Overlay testing targets the actual
running source, preserving its newer functionality (including its existing Loki
implementation and `activity` stream topic). Never replace the live source with
this older checkout wholesale. The additive overlay does not require migrations.
