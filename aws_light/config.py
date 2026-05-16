from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    data_directory: Path = Path("data")
    api_port: int = 8000
    proxy_port: int = 8080
    jwt_secret: str = "change-me-in-production"
    jwt_algorithm: str = "HS256"
    jwt_expiry_hours: int = 24
    encryption_key: str = ""
    docker_network: str = "aws-light"
    node_count: int = 10
    node_cpu_capacity: float = 0.5
    node_memory_capacity_mb: int = 512
    autoscaler_interval_seconds: int = 30
    health_check_interval_seconds: int = 10
    replica_port_start: int = 20000
    default_admin_username: str = "admin"
    default_admin_password: str = "admin"

    def ensure_data_directories(self) -> None:
        directories = [
            self.data_directory,
            self.data_directory / "storage",
            self.data_directory / "manifests",
        ]
        for directory in directories:
            directory.mkdir(parents=True, exist_ok=True)


settings = Settings()
