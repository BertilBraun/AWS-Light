# Real AWS Comparison

AWS-Light is a local learning and demo platform. It borrows ideas from AWS,
ECS/EKS, load balancers, IAM, S3, RDS, and CloudWatch, but it is intentionally a
small single-machine system. This page describes the most important differences
and the areas that would matter if the project were pushed closer to a real
cloud platform.

## Scale And Availability

AWS control planes are distributed systems. They run across many machines and
availability zones, use replicated state, survive partial failures, and operate
at regional scale.

AWS-Light runs on one Docker host. Its control plane, proxy, orchestrator,
Redis, and Postgres all live on the same machine. That is useful for seeing the
whole system, but it means:

- no multi-host scheduling,
- no regional or zonal failure domains,
- no distributed consensus,
- no horizontal control-plane scale,
- no high-availability proxy layer.

The practical scale limit is the laptop or host running Docker. The proxy can be
tuned, but it is still one local process routing to local containers.

## Security And Identity

AWS has deep identity and security layers: IAM policy documents, STS sessions,
resource policies, KMS, CloudTrail, VPC security groups, private endpoints,
service-linked roles, and many kinds of short-lived credentials.

AWS-Light currently has:

- JWT login for users,
- simple role checks for API access,
- encrypted platform secrets,
- generated per-service tokens,
- target-owned internal ingress allow lists,
- Docker network isolation between services.

Missing or simplified areas:

- a real policy language for users, services, buckets, and databases,
- service token rotation and expiry,
- mTLS between platform components and workloads,
- audit-grade event history,
- a dashboard for token lifecycle and service identity,
- a richer developer dashboard for debugging permissions.

A realistic next step is not full AWS IAM. A good intermediate step would be a
small policy model that explains why a request was allowed or denied and exposes
that reasoning in the dashboard.

## Networking

AWS networking is a product in itself: VPCs, subnets, route tables, NAT gateways,
security groups, NACLs, private DNS, load balancers, VPC endpoints, peering, and
transit gateways.

AWS-Light uses Docker bridge networks:

- one platform `internal` network for control-plane components,
- one Docker network per managed service,
- proxy and health-checker attached to service networks,
- database containers attached only to bound service networks.

This is enough to demonstrate the important idea: unrelated services should not
automatically talk to each other, and allowed HTTP calls should go through a
policy-aware proxy. It is not a replacement for VPC networking.

Open networking questions for this project:

- whether to split external and internal proxy instances,
- whether workloads should get stable internal DNS names,
- whether egress should be modeled explicitly,
- how much network policy should live in Docker networks versus platform policy.

## Storage And Databases

S3 and RDS are built for scale, durability, replication, backup, lifecycle
management, encryption, quotas, monitoring, and operational automation.

AWS-Light storage and databases are intentionally out of scale:

- bucket objects live on a local Docker volume owned by the control plane,
- each application database is a local Postgres container,
- database volumes persist locally,
- there is no replication, backup service, lifecycle policy, or managed failover.

The project should not try to become S3 or RDS. The useful part is the platform
contract: declare a bucket or database, bind a service to it, inject only the
needed credentials, and show that relationship in topology.

## Compute And Scheduling

AWS compute services run on large fleets with placement constraints, capacity
providers, health automation, draining, disruption controls, image distribution,
and rollout safety.

AWS-Light has a compact reconciler:

- desired service state lives in Postgres,
- the orchestrator starts Docker containers,
- simulated nodes provide CPU and memory capacity,
- health checker marks replicas routeable,
- autoscaler updates desired replica counts from CPU/RPS signals.

This is enough to teach reconciliation, scheduling, replica health, and
autoscaling. It is not enough for real fleet operation. The largest gap is
multi-host compute scale: once more than one Docker host exists, scheduling,
networking, image distribution, and proxy routing all become distributed-systems
problems.

## Observability

AWS has CloudWatch, CloudTrail, X-Ray, structured metrics, logs, alarms,
dashboards, retention, and query tools.

AWS-Light has:

- dashboard cards,
- platform events,
- proxy metrics,
- autoscaler events,
- health events,
- topology with policy and observed traffic edges.

Useful future improvements:

- durable log aggregation,
- request tracing across proxy and services,
- better denied-request explanations,
- historical autoscaler decisions,
- topology drill-downs for service identity and resource access,
- alerts for unhealthy replicas or policy violations.

## Operations

AWS is managed infrastructure. Users do not rebuild load balancers or manually
remove stale service containers.

AWS-Light is a local developer project. Operations are intentionally hands-on:

- build Docker images locally,
- apply manifests manually,
- rebuild platform components after code changes,
- reset Docker containers and volumes when needed.

That is acceptable for the project goal. The next useful operational polish is
developer convenience rather than enterprise operations: one command to build
all example images, one command to deploy the full demo, and clearer reset
scripts.

## Practical Next Ideas

The most relevant future work:

- a small, understandable policy language,
- token and service-identity visibility in the dashboard,
- a developer-focused dashboard view for debugging manifests and permissions,
- stronger observability around proxy decisions and autoscaler decisions,
- repeatable integration and load tests,
- optional internal/external proxy split once the current proxy model is fully documented.
