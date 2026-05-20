# Future Work

AWS-Light is currently a useful local platform demo, not a production runtime.
The next work should focus on documentation, cleanup, and selective polish rather
than adding large new subsystems immediately.

## Product And Documentation

- Keep README screenshots current.
- Add a small architecture diagram generated from source-controlled text.
- Add a recorded demo script for the combined stack.
- Add a troubleshooting matrix for common proxy and Docker issues.

## Platform Architecture

- Split external and internal proxy deployments.
- Add stronger source identity checks beyond bearer tokens.
- Consider internal DNS or service discovery names for selected use cases.
- Add clearer lifecycle handling for database deletion, secrets, and volumes.
- Add backups or export/import for managed app databases.

## Performance

- Continue measuring proxy changes with repeatable load scripts.
- Keep the raw forwarding path simple unless profiling says otherwise.
- Consider request-body streaming for large uploads.
- Consider tighter Redis batching and lower-cardinality topology metrics.

## Dashboard

- Improve topology layout for larger graphs.
- Add click-through details for denied traffic and failed health checks.
- Show database and bucket usage summaries.
- Add clearer autoscaler decision history.

## Developer Experience

- Add one command to build all example images.
- Add one command to deploy the full demo.
- Add a reset script for local development.
- Add docs checks for links and command snippets.
