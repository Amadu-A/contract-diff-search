# app/settings.py
"""Единственная точка чтения конфигурации: baseline, приватный .env, окружение."""

from pathlib import Path
from urllib.parse import quote

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    """Валидирует лимиты, пути и логические контракты общих сервисов."""

    model_config = SettingsConfigDict(
        env_file=(ROOT / ".env.example", ROOT / ".env"),
        env_nested_delimiter="__",
        case_sensitive=False,
        extra="forbid",
    )
    app_host: str = "0.0.0.0"
    app_bind_ip: str = "192.168.55.3"
    app_port: int = Field(8082, ge=1, le=65535)
    data_dir: Path = ROOT / ".data"
    process_temp_dir: Path = ROOT / ".data/tmp"
    max_file_mb: int = Field(100, ge=1, le=100)
    max_pages: int = Field(300, ge=1, le=300)
    max_unpacked_mb: int = Field(300, ge=1, le=1000)
    max_xml_mb: int = Field(20, ge=1, le=100)
    max_jobs: int = Field(20, ge=1, le=100)
    max_session_jobs: int = Field(2, ge=1, le=10)
    retention_seconds: int = Field(3600, ge=60, le=86400)
    job_timeout_seconds: int = Field(3600, ge=30, le=14400)
    process_timeout_seconds: int = Field(180, ge=5, le=600)
    cleanup_interval_seconds: int = Field(15, ge=1, le=60)
    worker_concurrency: int = Field(1, ge=1, le=4)
    native_threads: int = Field(1, ge=1, le=4)
    openblas_num_threads: int = Field(1, ge=1, le=256)
    omp_num_threads: int = Field(1, ge=1, le=256)
    mkl_num_threads: int = Field(1, ge=1, le=256)
    numexpr_num_threads: int = Field(1, ge=1, le=256)
    blis_num_threads: int = Field(1, ge=1, le=256)
    veclib_maximum_threads: int = Field(1, ge=1, le=256)
    omp_thread_limit: int = Field(1, ge=1, le=256)
    worker_health_timeout_seconds: int = Field(5, ge=1, le=15)
    queue_name: str = "contract-diff"
    rabbitmq_host: str = "rabbitmq"
    rabbitmq_container: str = "shared-rabbitmq-1"
    rabbitmq_cli_timeout_seconds: int = Field(30, ge=5, le=120)
    rabbitmq_port: int = Field(5672, ge=1, le=65535)
    rabbitmq_vhost: str = "contract-diff"
    rabbitmq_user: str = "contract-diff"
    rabbitmq_password: SecretStr = SecretStr("")
    shared_vlm_base_url: str = "http://shared-vlm:8000/v1"
    shared_vlm_model: str = "shared-vlm"
    vlm_timeout_seconds: int = Field(90, ge=1, le=180)
    vlm_max_fragments: int = Field(12, ge=1, le=30)
    vlm_batch_size: int = Field(12, ge=1, le=30)
    vlm_concurrency: int = Field(1, ge=1, le=3)
    vlm_retries: int = Field(2, ge=0, le=3)
    vlm_max_tokens: int = Field(3072, ge=512, le=8192)
    vlm_max_retry_tokens: int = Field(6144, ge=1024, le=16384)
    vlm_context_chars: int = Field(3600, ge=500, le=12000)
    vlm_image_pixels: int = Field(2000000, ge=400000, le=4000000)
    vlm_total_seconds: int = Field(2400, ge=5, le=10800)
    vlm_retry_delay_seconds: float = Field(0.5, ge=0, le=10)
    vlm_disable_thinking: bool = True
    vlm_json_schema: bool = True
    vlm_regions_per_request: int = Field(6, ge=1, le=12)
    ocr_dpi: int = Field(240, ge=150, le=400)
    ocr_language: str = "rus+eng"
    ocr_min_confidence: float = Field(0.65, ge=0, le=1)
    tesseract_command: str = "tesseract"
    soffice_command: str = "soffice"
    cookie_secure: bool = False
    log_level: str = "INFO"
    max_page_pixels: int = Field(24000000, ge=1000000, le=50000000)
    max_elements: int = Field(30000, ge=100, le=100000)

    @property
    def broker_url(self) -> str:
        """Собирает AMQP URL с экранированием; его запрещено выводить в логи."""
        return (
            f"amqp://{quote(self.rabbitmq_user, safe='')}:{quote(self.rabbitmq_password.get_secret_value(), safe='')}"
            f"@{self.rabbitmq_host}:{self.rabbitmq_port}/{quote(self.rabbitmq_vhost, safe='')}"
        )
