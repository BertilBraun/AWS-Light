# Future Architecture Work

This file tracks larger architecture changes that are intentionally not part of
the current dashboard iteration. These need design discussion before
implementation because they change the platform model, not just the UI.

## Storage Service Rework

The current `examples/storage-service` is a demo application with local file
storage. It is useful as a workload, but it does not exercise the AWS-Light
bucket/storage implementation.

Open questions:

- Should a demo workload talk to the platform storage API using injected
  credentials?
- Should storage remain only a built-in platform resource, similar to S3?
- Should we support mounting a bucket into a service container later?
- How should service credentials be scoped for bucket access?

Likely direction:

- Keep platform buckets as the real storage abstraction.
- Rework `storage-service` into a client of the platform storage API, or rename
  it to make clear that it is only a local-state demo.
- Add explicit service permissions for bucket access before allowing workloads
  to read/write platform storage.

## Postgres Database Add-On

Application databases should not reuse the platform Postgres instance. The
platform database stores AWS-Light control-plane state and should remain
internal infrastructure.

Open questions:

- Should databases be declared as a new manifest kind, for example
  `kind: Database`?
- Should a database be represented as a managed service, a platform add-on, or a
  distinct resource type?
- How are credentials created, rotated, and injected into workloads?
- How is database lifecycle handled during service deletion?
- What should backups or persistence look like in this local simulator?

Likely direction:

- Add a `Database` resource for app-owned Postgres instances.
- Provision one container per database initially.
- Store generated credentials as platform secrets.
- Inject connection settings into explicitly bound services.
- Show app databases as first-class nodes in topology.

## Network Management And Internal Communication

Right now managed workload containers share the `aws-light-data` Docker network.
That is simple, but too permissive for a microservice platform. Services from
different developers or unrelated applications should not automatically be able
to communicate.

Open questions:

- Should workloads get isolated per-service networks, per-application networks,
  or explicit allow-list based connectivity?
- Should service-to-service traffic go through the proxy?
- Should the platform support internal DNS names?
- How are policies represented in manifests?
- How do we expose denied/allowed traffic in the dashboard?

Likely direction:

- Add explicit manifest fields for internal dependencies, for example:

  ```yaml
  spec:
    allowEgressTo:
      - cpu-service
      - secret-service
  ```

- Route internal service-to-service traffic through a controlled proxy path
  first, because it centralizes routing, metrics, policy checks, and logging.
- Later, consider per-application networks or DNS if the proxy becomes too
  limiting.
- The dashboard should show allowed internal dependencies and actual traffic
  between services.
