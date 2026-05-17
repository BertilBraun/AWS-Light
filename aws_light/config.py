from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # ── Infrastructure ──────────────────────────────────────────────────────
    database_url: str = "postgresql://awslight:awslight@localhost:5432/awslight"
    redis_url: str = "redis://localhost:6379"

    # ── Storage ─────────────────────────────────────────────────────────────
    data_directory: Path = Path("data")

    # ── API / network ────────────────────────────────────────────────────────
    api_port: int = 8000
    proxy_port: int = 8080
    docker_network: str = "aws-light-data"

    # When true the proxy validates the JWT before forwarding.
    proxy_require_auth: bool = False

    # ── Auth ─────────────────────────────────────────────────────────────────
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24
    encryption_key: str = ""
    default_admin_username: str = "admin"
    default_admin_password: str = "admin"

    # ── Nodes ─────────────────────────────────────────────────────────────────
    node_count: int = 10
    node_cpu_capacity: float = 0.5
    node_memory_capacity_mb: int = 512
    scheduler_policy: str = "binpack"

    # ── Orchestrator ──────────────────────────────────────────────────────────
    reconcile_interval_seconds: int = 5
    rollout_poll_interval_seconds: int = 2
    # How many times to retry docker inspect for a container IP (200 ms apart).
    container_ip_poll_retries: int = 10

    # ── CPU stats (orchestrator → Redis) ─────────────────────────────────────
    cpu_stats_interval_seconds: int = 30

    # ── Health checker ────────────────────────────────────────────────────────
    health_check_interval_seconds: int = 10
    # Consecutive failures before a replica is marked unhealthy.
    health_check_failure_threshold: int = 3
    # Consecutive successes before a replica is marked healthy again.
    health_check_success_threshold: int = 1
    health_check_connect_timeout: float = 2.0
    health_check_read_timeout: float = 5.0

    # ── Autoscaler ────────────────────────────────────────────────────────────
    autoscaler_interval_seconds: int = 30
    autoscaler_cpu_scale_up_threshold: float = 70.0
    autoscaler_rps_scale_up_threshold: float = 100.0
    autoscaler_cpu_scale_down_threshold: float = 20.0
    autoscaler_rps_scale_down_threshold: float = 10.0
    # How many consecutive scale-down evaluations before the replica count drops.
    autoscaler_scale_down_consecutive_checks: int = 3

    def ensure_data_directories(self) -> None:
        for directory in [
            self.data_directory,
            self.data_directory / "storage",
            self.data_directory / "manifests",
        ]:
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
