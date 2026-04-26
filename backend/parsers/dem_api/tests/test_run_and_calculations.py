from __future__ import annotations

from pathlib import Path

import pytest


def _write_geotiff(path: Path, *, width: int, height: int, value: float, tags: dict[str, str] | None = None) -> None:
    rasterio = pytest.importorskip("rasterio")
    import numpy as np

    data = (np.ones((height, width), dtype="float32") * value)

    # 1-degree square, arbitrary but valid
    transform = rasterio.transform.from_origin(-10.0, 10.0, 0.1, 0.1)

    path.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=height,
        width=width,
        count=1,
        dtype="float32",
        crs="EPSG:4326",
        transform=transform,
    ) as dst:
        dst.write(data, 1)
        if tags:
            dst.update_tags(**tags)


def test_run_returns_dataset_and_wires_pipeline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure run.py can be executed without hitting GEE and returns an xr.Dataset."""

    xr = pytest.importorskip("xarray")

    # Import inside test after conftest sys.path adjustment.
    from src import run as run_mod

    calls: dict[str, object] = {}

    def fake_initialize_gee(*, project: str) -> None:
        calls["project"] = project

    def fake_download_dem(geo_json_path: str, filename: str, directory: str, **_kwargs):
        # Create a dummy GeoTIFF where run() expects it.
        out_dir = tmp_path / directory
        out_path = out_dir / filename
        _write_geotiff(
            out_path,
            width=5,
            height=5,
            value=123.0,
            tags={
                "original_lon1": "-10.0",
                "original_lat1": "9.0",
                "original_lon2": "-9.0",
                "original_lat2": "10.0",
            },
        )
        return None

    def fake_calculate_terrain_attributes(dem_path: str, attributes: list[str], **_kwargs):
        calls["dem_path"] = dem_path
        calls["attributes"] = list(attributes)
        return xr.Dataset({"ok": ("points", [1])}).assign_coords(points=[0])

    monkeypatch.setattr(run_mod, "initialize_gee", fake_initialize_gee)
    monkeypatch.setattr(run_mod, "download_dem", fake_download_dem)
    monkeypatch.setattr(run_mod, "calculate_terrain_attributes", fake_calculate_terrain_attributes)

    ds = run_mod.run("dummy.geojson")

    assert isinstance(ds, xr.Dataset)
    assert "ok" in ds.data_vars
    assert calls.get("project") == "projectomela"
    assert isinstance(calls.get("dem_path"), str)
    assert isinstance(calls.get("attributes"), list)


def test_calculate_terrain_attributes_wgs84_and_cropping(tmp_path: Path) -> None:
    """Smoke-test calculations on a small local GeoTIFF (no network)."""

    pytest.importorskip("xdem")
    xr = pytest.importorskip("xarray")

    from src.calculations import calculate_terrain_attributes

    dem_path = tmp_path / "dem.tif"
    _write_geotiff(
        dem_path,
        width=9,
        height=9,
        value=250.0,
        tags={
            "original_lon1": "-10.0",
            "original_lat1": "9.2",
            "original_lon2": "-9.2",
            "original_lat2": "10.0",
        },
    )

    # xdem does not support an empty attribute list; request one supported attribute.
    ds = calculate_terrain_attributes(
        str(dem_path),
        attributes=["slope"],
        return_wgs84=True,
        crop_to_bounds=True,
    )

    assert isinstance(ds, xr.Dataset)
    assert "latitude" in ds.coords
    assert "longitude" in ds.coords
    assert ds.attrs.get("coordinate_system") == "EPSG:4326 (WGS84)"
    assert ds.attrs.get("resolution_m") == 30
    assert "reprojected_dem" in ds.data_vars


def test_calculate_single_point_returns_elevation_only(tmp_path: Path) -> None:
    pytest.importorskip("xdem")
    xr = pytest.importorskip("xarray")

    from src.calculations import calculate_terrain_attributes

    dem_path = tmp_path / "one_pixel.tif"
    _write_geotiff(
        dem_path,
        width=1,
        height=1,
        value=42.0,
        tags={
            "original_lon1": "-10.0",
            "original_lat1": "10.0",
            "original_lon2": "-10.0",
            "original_lat2": "10.0",
        },
    )

    ds = calculate_terrain_attributes(str(dem_path), attributes=["slope"], return_wgs84=True)

    assert isinstance(ds, xr.Dataset)
    assert "elevation" in ds.data_vars
    assert ds.attrs.get("resolution_m") is None
