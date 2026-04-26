from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import math
import os
import random
import shutil
import tempfile
import time
import uuid
from pathlib import Path
from threading import BoundedSemaphore
from typing import Any, Callable

import xarray as xr

from .config import Settings


class ProviderGate:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.gee = BoundedSemaphore(max(1, settings.max_gee))
        self.cds = BoundedSemaphore(max(1, settings.max_cds))
        self.nasa = BoundedSemaphore(max(1, settings.max_nasa))

    def run_with_retry(
        self,
        semaphore: BoundedSemaphore,
        provider_name: str,
        fn: Callable[..., Any],
        **kwargs,
    ) -> Any:
        retries = self.settings.retries
        backoff = self.settings.retry_backoff_sec
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            semaphore.acquire()
            try:
                return fn(**kwargs)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt >= retries:
                    break
                wait_sec = backoff * (2**attempt) + random.uniform(0.0, backoff * 0.2)
                time.sleep(wait_sec)
            finally:
                semaphore.release()
        raise RuntimeError(f"{provider_name} failed after {retries + 1} attempts: {last_exc}") from last_exc


def set_provider_env(settings: Settings) -> None:
    if settings.cds_api_key:
        os.environ["CDS_API_KEY"] = settings.cds_api_key
    if settings.cds_api_url:
        os.environ["CDS_API_URL"] = settings.cds_api_url
    if settings.earthdata_token:
        os.environ["EARTHDATA_TOKEN"] = settings.earthdata_token


def station_geojson(latitude: float, longitude: float, time_step_iso: str) -> dict[str, Any]:
    half_size_m = 60.0
    lat_deg_per_m = 1.0 / 111320.0
    lon_deg_per_m = 1.0 / (111320.0 * math.cos(math.radians(latitude)))
    lat_buffer = half_size_m * lat_deg_per_m
    lon_buffer = half_size_m * lon_deg_per_m

    min_lat = latitude - lat_buffer
    max_lat = latitude + lat_buffer
    min_lon = longitude - lon_buffer
    max_lon = longitude + lon_buffer
    return {
        "type": "FeatureCollection",
        "features": [
            {
                "type": "Feature",
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [min_lon, min_lat],
                            [max_lon, min_lat],
                            [max_lon, max_lat],
                            [min_lon, max_lat],
                            [min_lon, min_lat],
                        ]
                    ],
                },
                "properties": {
                    "time": ["2018-01-01", "2019-01-01", time_step_iso],
                    "is_rectangle": True,
                },
            }
        ],
    }


def aggr_dataset(ds: xr.Dataset) -> dict[str, float]:
    result: dict[str, float] = {}
    for var in ds.data_vars:
        da = ds[var]
        result[f"{var}_mean"] = float(da.mean().item())
        result[f"{var}_median"] = float(da.median().item())
        result[f"{var}_std"] = float(da.std().item())
        result[f"{var}_min"] = float(da.min().item())
        result[f"{var}_max"] = float(da.max().item())
    return result


def collect_features_for_point(
    *,
    latitude: float,
    longitude: float,
    gate: ProviderGate,
    settings: Settings,
) -> dict[str, float]:
    set_provider_env(settings)

    from parsers.dem_api.src import run as dem_run_module
    from parsers.module_era5 import insolation as era5_module
    from parsers.clouds.run_clouds import run_cloud_statistics_one_function

    sample_geojson = station_geojson(latitude, longitude, settings.cloud_time_step)

    # Override mutable globals in legacy module at runtime from env-backed settings.
    era5_module.CDS_API_URL = settings.cds_api_url
    if settings.cds_api_key:
        era5_module.CDS_API_KEY = settings.cds_api_key
    else:
        raise RuntimeError("CDS_API_KEY is required for live data collection.")
    era5_module.TEMP_DIR = f"solarhunter_tmp_{uuid.uuid4().hex}"

    def _collect_dem_vector() -> dict[str, float]:
        temp_dir = tempfile.mkdtemp(prefix="solarhunter_dem_")
        try:
            dem_ds = gate.run_with_retry(
                gate.gee,
                "gee",
                dem_run_module.run,
                geojson_path=_write_geojson(temp_dir, sample_geojson),
                download_embeddings=settings.download_embeddings,
                embeddings_year=settings.embeddings_year,
                dem_file_name="dem.tif",
                output_directory=temp_dir,
            )
            try:
                return aggr_dataset(dem_ds)
            finally:
                close_method = getattr(dem_ds, "close", None)
                if callable(close_method):
                    close_method()
        finally:
            # Windows can keep TIFF handle locked briefly; tolerate cleanup races.
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _collect_era5_vector() -> dict[str, float]:
        cds_ds = gate.run_with_retry(
            gate.cds,
            "cds",
            era5_module.fetch_process_xarray,
            geojson=sample_geojson,
        )
        try:
            return aggr_dataset(cds_ds)
        finally:
            close_method = getattr(cds_ds, "close", None)
            if callable(close_method):
                close_method()

    def _collect_cloud_vector() -> dict[str, float]:
        cloud_result = gate.run_with_retry(
            gate.nasa,
            "nasa",
            run_cloud_statistics_one_function,
            geojson_obj=sample_geojson,
            radius_m=settings.cloud_radius_m,
            stac_provider="nasa",
            ewma_return_history=False,
            save_to_netcdf=False,
            download_files=False,
            download_reflectance=True,
            cloud_scene_workers=settings.max_cloud_scene_workers,
        )
        cloud_ewma_ds = cloud_result["ds_cloud_pct_ewma"][["cloud_percentage_ewma_final"]].rename(
            {"cloud_percentage_ewma_final": "ewma_perc"}
        )
        try:
            return aggr_dataset(cloud_ewma_ds)
        finally:
            close_method = getattr(cloud_ewma_ds, "close", None)
            if callable(close_method):
                close_method()

    with ThreadPoolExecutor(max_workers=3) as executor:
        dem_future = executor.submit(_collect_dem_vector)
        era5_future = executor.submit(_collect_era5_vector)
        cloud_future = executor.submit(_collect_cloud_vector)

        dem_vector = dem_future.result()
        era5_vector = era5_future.result()
        cloud_vector = cloud_future.result()

    return dem_vector | era5_vector | cloud_vector | {"latitude": latitude, "longitude": longitude}


def _write_geojson(temp_dir: str, geojson_obj: dict[str, Any]) -> str:
    import json

    path = Path(temp_dir) / "aoi.geojson"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(geojson_obj, f, ensure_ascii=False)
    return str(path)

