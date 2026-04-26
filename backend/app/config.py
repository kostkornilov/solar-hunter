from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, Field


class Settings(BaseModel):
    app_name: str = "SolarHunter API"
    app_version: str = "0.1.0"
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"

    model_artifacts_dir: Path = Field(
        default_factory=lambda: Path(
            os.getenv(
                "MODEL_ARTIFACTS_DIR",
                Path(__file__).resolve().parents[1] / "model_serving",
            )
        )
    )

    gee_project: str = Field(default_factory=lambda: os.getenv("GEE_PROJECT", "projectomela"))
    max_gee: int = Field(default_factory=lambda: int(os.getenv("MAX_GEE_CONCURRENCY", "1")))
    max_cds: int = Field(default_factory=lambda: int(os.getenv("MAX_CDS_CONCURRENCY", "1")))
    max_nasa: int = Field(default_factory=lambda: int(os.getenv("MAX_NASA_CONCURRENCY", "4")))
    max_cloud_scene_workers: int = Field(default_factory=lambda: int(os.getenv("MAX_CLOUD_SCENE_WORKERS", "1")))
    retries: int = Field(default_factory=lambda: int(os.getenv("PROVIDER_RETRIES", "2")))
    retry_backoff_sec: float = Field(default_factory=lambda: float(os.getenv("RETRY_BACKOFF_SEC", "2.0")))

    cloud_radius_m: float = Field(default_factory=lambda: float(os.getenv("CLOUD_RADIUS_M", "300")))
    cloud_time_step: str = Field(default_factory=lambda: os.getenv("CLOUD_TIME_STEP", "P30D"))
    embeddings_year: int = Field(default_factory=lambda: int(os.getenv("EMBEDDINGS_YEAR", "2018")))
    download_embeddings: bool = Field(
        default_factory=lambda: os.getenv("DOWNLOAD_EMBEDDINGS", "true").lower() in {"1", "true", "yes"}
    )

    cds_api_url: str = Field(default_factory=lambda: os.getenv("CDS_API_URL", "https://cds.climate.copernicus.eu/api"))
    cds_api_key: str = Field(default_factory=lambda: os.getenv("CDS_API_KEY", ""))
    earthdata_token: str = Field(default_factory=lambda: os.getenv("EARTHDATA_TOKEN", ""))

    @property
    def model_path(self) -> Path:
        return self.model_artifacts_dir / "catboost_capacity_factor.cbm"

    @property
    def feature_columns_path(self) -> Path:
        return self.model_artifacts_dir / "feature_columns.pkl"

    @property
    def index_params_path(self) -> Path:
        return self.model_artifacts_dir / "index_params.json"

