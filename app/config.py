from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_data_dir: Path = Path("/data")
    health_interval_seconds: float = 2.0
    topology_interval_seconds: int = 60
    topology_stale_minutes: int = 15
    voyage_timeout_seconds: int = 90
    voyage_probing_rate: int = 50
    voyage_confidence: float = 99.0
    voyage_max_ttl: int = Field(default=32, ge=1, le=64)
    tcp_enabled: bool = True
    tcp_port: int = Field(default=443, ge=1, le=65535)
    tcp_flows: int = Field(default=3, ge=1, le=8)
    tcp_hop_timeout_seconds: float = Field(default=1.0, ge=0.1, le=5.0)
    tcp_sendwait_seconds: float = Field(default=0.05, ge=0.01, le=1.0)
    suspect_after_failures: int = 3
    down_after_failures: int = 5
    degraded_loss_percent: float = 10.0
    degraded_latency_multiplier: float = 2.0

    @property
    def database_url(self) -> str:
        self.app_data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{self.app_data_dir / 'multipathnms.db'}"


settings = Settings()
