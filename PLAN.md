# AWS Light — Executive Summary

AWS Light is a fully local, educational reimplementation of the most important Amazon Web Services concepts. It runs entirely on a single machine while simulating a multi-node cloud environment.

## Goal

Provide a platform where a developer can:

* Deploy a Docker image
* Receive a stable URL
* Automatically scale replicas based on load
* Perform zero-downtime deployments
* Store and retrieve objects (S3-like)
* Manage secrets securely
* Authenticate users with role-based access control
* Define infrastructure declaratively
* Observe everything in a live dashboard

## Core Components

### Compute Orchestrator

Manages containerized services, scheduling replicas across simulated nodes with CPU and memory constraints.

### Custom Reverse Proxy

Routes requests to healthy replicas, performs load balancing, and exposes stable hostnames (e.g. `api.localhost`).

### Autoscaler

Adjusts replica counts using real CPU and request metrics.

### IAM

Provides authentication and authorization using JWT tokens and roles (`admin`, `developer`, `viewer`).

### Rolling Deployment Controller

Performs zero-downtime updates by gradually replacing old replicas.

### Infrastructure as Code

Applies YAML specifications declaratively, similar to Terraform or AWS CloudFormation.

### Secrets Manager

Stores sensitive values and injects them into services securely.

### Object Storage

S3-compatible API for buckets, objects, and presigned URLs.

### Dashboard

Real-time visualization of services, nodes, metrics, scaling events, and failures.

## Local Simulation

On an 8-core machine, AWS Light can simulate ~10 worker nodes, each limited to 0.5 CPU and 512 MB RAM. This makes scheduling and autoscaling behavior visible under realistic resource constraints.

## Example Workflow

```bash
aws-light apply sentiment-api.yaml
k6 run load.js
aws-light status sentiment-api
```

This deploys a service, generates load using [k6](https://k6.io/), and displays scaling activity in real time.

## Primary Learning Outcomes

AWS Light teaches the core principles behind AWS and Kubernetes:

* Control plane vs data plane
* Desired state reconciliation
* Scheduling and bin packing
* Reverse proxy design
* Autoscaling algorithms
* Authentication and RBAC
* Declarative infrastructure
* Secrets management
* Object storage
* Observability and operational dashboards

## Deliverable

A compact but feature-rich local cloud platform that reproduces the essential behaviors of AWS, while remaining small enough to build and understand end-to-end.
