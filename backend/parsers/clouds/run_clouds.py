from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
import re
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path

import numpy as np
import planetary_computer as pc
import stackstac
import xarray as xr
from pystac_client import Client
from scipy.ndimage import binary_closing, binary_dilation, binary_opening


# Keep rasterio output cleaner.
logging.getLogger("rasterio").setLevel(logging.ERROR)

STAC_PROVIDER_CONFIGS = {
    "pc": {
        "catalog_url": "https://planetarycomputer.microsoft.com/api/stac/v1",
        "collection_name": "hls2-s30",
        "needs_pc_sign": True,
        "supports_bearer_token": False,
    },
    "nasa": {
        "catalog_url": "https://cmr.earthdata.nasa.gov/stac/LPCLOUD",
        "collection_name": "HLSS30.v2.0",
        "needs_pc_sign": False,
        "supports_bearer_token": True,
    },
}


def _get_stac_provider_config(stac_provider: str) -> dict:
    key = str(stac_provider).strip().lower()
    if key not in STAC_PROVIDER_CONFIGS:
        raise ValueError(f"Unsupported stac_provider='{stac_provider}'. Use one of {sorted(STAC_PROVIDER_CONFIGS)}.")
    return STAC_PROVIDER_CONFIGS[key]


def _read_token_from_env_file(env_path: Path, token_name: str) -> str | None:
    if not env_path.exists():
        return None

    for line in env_path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        if key.strip() != token_name:
            continue
        token = value.strip().strip("'").strip('"')
        return token or None

    return None


@lru_cache(maxsize=1)
def _get_earthdata_token() -> str | None:
    token = os.getenv("EARTHDATA_TOKEN")
    if token:
        return token.strip()

    env_path = Path(__file__).resolve().parents[3] / "downloading_train" / ".env"
    return _read_token_from_env_file(env_path, "EARTHDATA_TOKEN")


def _get_stac_auth_headers(stac_provider: str) -> dict | None:
    provider_cfg = _get_stac_provider_config(stac_provider)
    print(f"STAC provider: {stac_provider} | collection: {provider_cfg['collection_name']}")
    if not provider_cfg["supports_bearer_token"]:
        return None

    token = _get_earthdata_token()
    if not token:
        raise ValueError(
            "EARTHDATA_TOKEN is required for stac_provider='nasa'. "
            "Set env var or add it to downloading_train/.env."
        )
    return {"Authorization": f"Bearer {token}"}


def _prepare_items_for_provider(items: list, stac_provider: str) -> list:
    provider_cfg = _get_stac_provider_config(stac_provider)
    if provider_cfg["needs_pc_sign"]:
        return [pc.sign(it) for it in items]
    return items


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _extract_cloud_cover(item) -> float:
    value = (
        item.properties.get("eo:cloud_cover", None)
        or item.properties.get("CLOUD_COVERAGE", None)
        or item.properties.get("cloud_cover", None)
    )
    return float(value) if value is not None else 1e9


def _log_asset_diagnostics_on_failure(
    *,
    item,
    assets: list[str],
    headers: dict | None,
    cause: Exception,
) -> None:
    logging.error("Asset read failed for item=%s: %s", getattr(item, "id", "unknown"), cause)
    if not _env_flag("CLOUD_ASSET_DIAGNOSTIC_PROBE", default=True):
        logging.error("Asset diagnostic probe is disabled by CLOUD_ASSET_DIAGNOSTIC_PROBE=0")
        return
    auth_header = (headers or {}).get("Authorization")
    for asset_name in assets:
        asset = item.assets.get(asset_name)
        if asset is None:
            logging.error("Asset diagnostic: item=%s asset=%s missing in STAC item", getattr(item, "id", "unknown"), asset_name)
            continue

        href = str(getattr(asset, "href", ""))
        if not href:
            logging.error("Asset diagnostic: item=%s asset=%s href is empty", getattr(item, "id", "unknown"), asset_name)
            continue

        req = urllib.request.Request(href, method="GET")
        req.add_header("User-Agent", "SolarHunter-Debug/1.0")
        if auth_header:
            req.add_header("Authorization", auth_header)

        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                status_code = getattr(resp, "status", resp.getcode())
                content_type = resp.headers.get("Content-Type", "")
                first_bytes = resp.read(200)
                logging.error(
                    "Asset diagnostic: url=%s status=%s content_type=%s first_200=%r",
                    href,
                    status_code,
                    content_type,
                    first_bytes,
                )
        except urllib.error.HTTPError as http_err:
            err_content_type = http_err.headers.get("Content-Type", "") if http_err.headers else ""
            err_body = b""
            try:
                err_body = http_err.read(200)
            except Exception:  # noqa: BLE001
                pass
            logging.error(
                "Asset diagnostic: url=%s status=%s content_type=%s first_200=%r",
                href,
                http_err.code,
                err_content_type,
                err_body,
            )
        except Exception as probe_exc:  # noqa: BLE001
            logging.error(
                "Asset diagnostic probe failed: url=%s error=%s",
                href,
                probe_exc,
            )


def _stack_item_with_local_asset_fallback(
    *,
    item,
    assets,
    headers: dict | None,
    stack_kwargs: dict,
    compute_kwargs: dict,
):
    import pystac

    auth_header = (headers or {}).get("Authorization")
    with tempfile.TemporaryDirectory(prefix="solarhunter_hls_local_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        local_item_dict = item.to_dict()

        for asset_name in assets:
            asset = item.assets.get(asset_name)
            if asset is None:
                raise KeyError(f"Asset '{asset_name}' is missing in item '{getattr(item, 'id', 'unknown')}'")

            href = str(getattr(asset, "href", ""))
            if not href:
                raise ValueError(f"Asset '{asset_name}' has empty href in item '{getattr(item, 'id', 'unknown')}'")

            req = urllib.request.Request(href, method="GET")
            req.add_header("User-Agent", "SolarHunter-LocalFallback/1.0")
            if auth_header:
                req.add_header("Authorization", auth_header)

            local_asset_path = tmp_path / f"{asset_name}.tif"
            with urllib.request.urlopen(req, timeout=120) as resp:
                local_asset_path.write_bytes(resp.read())

            local_item_dict["assets"][asset_name]["href"] = str(local_asset_path)

        local_item = pystac.Item.from_dict(local_item_dict)
        local_stack_kwargs = dict(stack_kwargs)
        local_stack_kwargs["items"] = [local_item]
        return stackstac.stack(**local_stack_kwargs).squeeze("time", drop=True).compute(**compute_kwargs)


def _search_hls_items_for_bbox(
    bbox,
    datetime_value,
    *,
    stac_provider: str,
) -> list:
    provider_cfg = _get_stac_provider_config(stac_provider)
    headers = _get_stac_auth_headers(stac_provider)

    client_kwargs = {"headers": headers} if headers else {}
    catalog = Client.open(provider_cfg["catalog_url"], **client_kwargs)
    search = catalog.search(
        collections=[provider_cfg["collection_name"]],
        bbox=bbox,
        datetime=datetime_value,
    )
    items = list(search.items())
    return _prepare_items_for_provider(items, stac_provider)


def _stack_item_with_provider_auth(
    item,
    *,
    assets,
    bbox,
    resolution: int,
    epsg: int,
    chunksize: int,
    stac_provider: str,
):
    stack_kwargs = {
        "items": [item],
        "assets": assets,
        "bounds_latlon": bbox,
        "resolution": resolution,
        "epsg": epsg,
        "chunksize": chunksize,
    }
    compute_kwargs = {}
    if str(stac_provider).strip().lower() == "nasa":
        # Avoid thread-local GDAL auth/context glitches on protected LP DAAC assets.
        compute_kwargs["scheduler"] = "single-threaded"

    headers = _get_stac_auth_headers(stac_provider)
    force_local_fallback = _env_flag("CLOUD_FORCE_LOCAL_ASSET_STACK", default=False)
    try:
        if force_local_fallback and str(stac_provider).strip().lower() == "nasa":
            logging.warning(
                "CLOUD_FORCE_LOCAL_ASSET_STACK=1: forcing local NASA asset fallback for item=%s",
                getattr(item, "id", "unknown"),
            )
            return _stack_item_with_local_asset_fallback(
                item=item,
                assets=list(assets),
                headers=headers,
                stack_kwargs=stack_kwargs,
                compute_kwargs=compute_kwargs,
            )

        if headers:
            import rasterio

            auth_value = headers["Authorization"]
            bearer_token = auth_value.split(" ", 1)[1] if " " in auth_value else auth_value
            gdal_headers = "Authorization: " + auth_value
            # LP DAAC protected assets are more stable with explicit bearer-mode settings.
            with rasterio.Env(
                GDAL_HTTP_HEADERS=gdal_headers,
                GDAL_HTTP_AUTH="BEARER",
                GDAL_HTTP_BEARER=bearer_token,
                CPL_VSIL_CURL_USE_HEAD="NO",
            ):
                return stackstac.stack(**stack_kwargs).squeeze("time", drop=True).compute(**compute_kwargs)

        return stackstac.stack(**stack_kwargs).squeeze("time", drop=True).compute(**compute_kwargs)
    except Exception as exc:  # noqa: BLE001
        if str(stac_provider).strip().lower() == "nasa":
            try:
                logging.warning(
                    "Remote NASA asset stack failed for item=%s; trying local download fallback.",
                    getattr(item, "id", "unknown"),
                )
                return _stack_item_with_local_asset_fallback(
                    item=item,
                    assets=list(assets),
                    headers=headers,
                    stack_kwargs=stack_kwargs,
                    compute_kwargs=compute_kwargs,
                )
            except Exception as fallback_exc:  # noqa: BLE001
                logging.error(
                    "Local NASA asset fallback also failed for item=%s: %s",
                    getattr(item, "id", "unknown"),
                    fallback_exc,
                )
        _log_asset_diagnostics_on_failure(
            item=item,
            assets=list(assets),
            headers=headers,
            cause=exc,
        )
        raise


def find_pc_item_hls_s30(
    lat,
    lon,
    date,
    *,
    days_window=20,
    bbox_deg=0.05,
    max_cloud=100,
    stac_provider="pc",
):
    target_dt = datetime.fromisoformat(date)
    bbox = [lon - bbox_deg, lat - bbox_deg, lon + bbox_deg, lat + bbox_deg]
    time_start = (target_dt - timedelta(days=days_window)).strftime("%Y-%m-%d")
    time_end = (target_dt + timedelta(days=days_window)).strftime("%Y-%m-%d")

    items = _search_hls_items_for_bbox(
        bbox,
        f"{time_start}/{time_end}",
        stac_provider=stac_provider,
    )
    print(f"STAC search (HLS S30): scenes={len(items)}")
    if not items:
        raise ValueError("0 scenes found. Increase days_window/bbox_deg or change date.")

    def time_dist(it):
        item_dt = it.datetime.replace(tzinfo=None)
        return abs((item_dt - target_dt).total_seconds())

    filtered = [it for it in items if _extract_cloud_cover(it) <= max_cloud]
    if not filtered:
        print("No scenes pass max_cloud, using all found scenes.")
        filtered = items

    best = sorted(filtered, key=lambda it: (time_dist(it), _extract_cloud_cover(it)))[0]
    print(
        "Selected:",
        best.id,
        "| date:", best.datetime.date(),
        "| cloud:", best.properties.get("eo:cloud_cover", best.properties.get("CLOUD_COVERAGE")),
    )
    return best, bbox


def find_pc_items_hls_s30_range(
    lat,
    lon,
    date_range,
    *,
    bbox_deg=0.08,
    max_cloud=100,
    stac_provider="pc",
):
    bbox = [lon - bbox_deg, lat - bbox_deg, lon + bbox_deg, lat + bbox_deg]

    items = _search_hls_items_for_bbox(
        bbox,
        date_range,
        stac_provider=stac_provider,
    )
    print(f"STAC search (HLS S30, range): scenes={len(items)}")
    if not items:
        raise ValueError("Scenes not found for date_range.")

    items = [it for it in items if _extract_cloud_cover(it) <= max_cloud]
    items = sorted(items, key=lambda it: it.datetime)
    print(f"After max_cloud={max_cloud}: scenes={len(items)}")
    if not items:
        raise ValueError("No scenes left after max_cloud filter.")
    return items


def get_item_epsg(item):
    epsg = item.properties.get("proj:epsg", None)
    if epsg is None:
        for asset_name in ["Fmask", "B01", "B02", "B03", "B04", "B8A", "B11", "B12"]:
            if asset_name in item.assets:
                epsg = item.assets[asset_name].extra_fields.get("proj:epsg", None)
                if epsg is not None:
                    return int(epsg)

    m = re.search(r"\.T(\d{2})([A-Z])[A-Z]{2}\.", item.id)
    if not m:
        raise ValueError(f"Could not detect EPSG for item: {item.id}")
    zone = int(m.group(1))
    north = m.group(2) >= "N"
    return int(32600 + zone if north else 32700 + zone)


def load_hls_scene_from_item(
    item,
    lat,
    lon,
    *,
    bbox_deg=0.08,
    resolution=30,
    download_reflectance=True,
    stac_provider="pc",
):
    bbox = [lon - bbox_deg, lat - bbox_deg, lon + bbox_deg, lat + bbox_deg]
    epsg = get_item_epsg(item)
    provider_cfg = _get_stac_provider_config(stac_provider)
    print("Using EPSG:", epsg, "| item:", item.id)

    if int(resolution) != 30:
        print("HLS uses 30m; forcing resolution=30.")
        resolution = 30

    assets = (
        ["B01", "B02", "B03", "B04", "B8A", "B11", "B12", "Fmask"]
        if download_reflectance
        else ["Fmask"]
    )
    da = _stack_item_with_provider_auth(
        item,
        assets=assets,
        bbox=bbox,
        resolution=resolution,
        epsg=epsg,
        chunksize=2048,
        stac_provider=stac_provider,
    )

    fmask_da = da.sel(band="Fmask").astype(np.uint8)

    if download_reflectance:
        bands_da = da.sel(band=["B01", "B02", "B03", "B04", "B8A", "B11", "B12"]).astype(np.float32) / 10000.0
        bands_da.attrs.update(
            {
                "source_item_id": item.id,
                "epsg": int(epsg),
                "date_request": str(item.datetime.date()),
                "lat_request": float(lat),
                "lon_request": float(lon),
                "resolution_m": int(resolution),
                "collection": provider_cfg["collection_name"],
            }
        )
    else:
        bands_da = None
        fmask_da.attrs.update(
            {
                "source_item_id": item.id,
                "epsg": int(epsg),
                "date_request": str(item.datetime.date()),
                "lat_request": float(lat),
                "lon_request": float(lon),
                "resolution_m": int(resolution),
                "collection": provider_cfg["collection_name"],
            }
        )

    return bands_da, fmask_da, item


def decode_hls_fmask(fmask_da):
    fm = fmask_da.values.astype(np.uint8)
    cirrus = ((fm >> 0) & 1).astype(bool)
    cloud = ((fm >> 1) & 1).astype(bool)
    adjacent = ((fm >> 2) & 1).astype(bool)
    shadow = ((fm >> 3) & 1).astype(bool)
    snow = ((fm >> 4) & 1).astype(bool)
    water = ((fm >> 5) & 1).astype(bool)
    aerosol = ((fm >> 6) & 0b11).astype(np.uint8)
    return {
        "cirrus": cirrus,
        "cloud": cloud,
        "adjacent": adjacent,
        "shadow": shadow,
        "snow": snow,
        "water": water,
        "aerosol_moderate": aerosol == 2,
        "aerosol_high": aerosol == 3,
    }


def safe_div(a, b, eps=1e-6):
    return a / np.maximum(b, eps)


def get_band(bands_da, band_name):
    return bands_da.sel(band=band_name).values.astype(np.float32)


def build_fmask_qa_mask(fmask_da):
    """Thick cloud / shadow / adjacent / cirrus from HLS Fmask bits only (no spectral haze)."""
    qa = decode_hls_fmask(fmask_da)
    return (qa["cloud"] | qa["cirrus"] | qa["shadow"] | qa["adjacent"]).astype(np.uint8)


def build_hybrid_hls_mask(bands_da, fmask_da, mode="balanced"):
    qa = decode_hls_fmask(fmask_da)
    b01 = get_band(bands_da, "B01")
    b02 = get_band(bands_da, "B02")
    b03 = get_band(bands_da, "B03")
    b04 = get_band(bands_da, "B04")
    b8a = get_band(bands_da, "B8A")
    b11 = get_band(bands_da, "B11")

    ndwi = safe_div(b03 - b8a, b03 + b8a)
    hot = b02 - 0.5 * b04 - 0.08
    vis_mean = (b01 + b02 + b03 + b04) / 4.0
    vis_std = np.std(np.stack([b01, b02, b03, b04], axis=0), axis=0)
    whiteness = safe_div(vis_std, vis_mean + 1e-6)
    blue_nir_ratio = safe_div(b02, b8a + 1e-6)
    coastal_swir_ratio = safe_div(b01, b11 + 1e-6)

    cloud_core = qa["cloud"] | qa["cirrus"]
    shadow_core = qa["shadow"]
    water = qa["water"]
    snow = qa["snow"]

    cloud_spec = (
        (vis_mean > 0.23) &
        (b02 > 0.20) &
        (hot > 0.04) &
        (blue_nir_ratio > 0.90) &
        (whiteness < 0.28)
    )

    haze_spec_base = (
        (
            ((b01 > 0.075) & (b02 > 0.070) & (coastal_swir_ratio > 0.55)) |
            ((hot > -0.01) & (blue_nir_ratio > 0.58) & (b11 < 0.20))
        ) &
        (whiteness < 0.40) &
        (vis_mean > 0.07)
    )

    invalid_for_haze = water | snow | (ndwi > 0.12)
    thick_like = cloud_core | shadow_core | qa["adjacent"] | cloud_spec

    if mode == "strict":
        haze = (haze_spec_base & qa["aerosol_high"]) & (~invalid_for_haze)
    elif mode == "haze_sensitive":
        haze = (haze_spec_base & (qa["aerosol_moderate"] | qa["aerosol_high"])) & (~invalid_for_haze)
    else:
        haze = (
            ((haze_spec_base & qa["aerosol_high"]) | ((haze_spec_base & qa["aerosol_moderate"]) & (hot > 0.0)))
            & (~invalid_for_haze)
        )

    cloud = cloud_core | cloud_spec
    shadow = shadow_core
    haze = haze & (~thick_like)

    cloud = binary_closing(cloud, structure=np.ones((3, 3)))
    cloud = binary_dilation(cloud, structure=np.ones((2, 2)))
    shadow = binary_closing(shadow, structure=np.ones((3, 3)))
    haze = binary_opening(haze, structure=np.ones((2, 2)))

    return (cloud | shadow | haze).astype(np.uint8)


def build_hls_outputs_from_item(
    item,
    lat,
    lon,
    *,
    bbox_deg=0.08,
    resolution=30,
    mode="balanced",
    download_reflectance=True,
    include_reflectance_in_netcdf=True,
    stac_provider="pc",
):
    """Build xarray Dataset for one HLS scene.

    Parameters
    ----------
    download_reflectance
        If False, only the Fmask asset is read from STAC (less I/O). The mask is then
        ``build_fmask_qa_mask`` (no spectral haze / cloud refinement).
    include_reflectance_in_netcdf
        If False, reflectance bands are omitted from the returned Dataset (mask only).
        When ``download_reflectance`` is False, this must be False.
    """
    if not download_reflectance and include_reflectance_in_netcdf:
        raise ValueError("include_reflectance_in_netcdf requires download_reflectance=True.")

    bands_da, fmask_da, item = load_hls_scene_from_item(
        item,
        lat,
        lon,
        bbox_deg=bbox_deg,
        resolution=resolution,
        download_reflectance=download_reflectance,
        stac_provider=stac_provider,
    )
    provider_cfg = _get_stac_provider_config(stac_provider)
    if download_reflectance:
        final_mask = build_hybrid_hls_mask(bands_da, fmask_da, mode=mode)
        mask_kind = "hybrid"
        y_coord = bands_da["y"].values
        x_coord = bands_da["x"].values
        epsg_attr = bands_da.attrs.get("epsg")
    else:
        final_mask = build_fmask_qa_mask(fmask_da)
        mask_kind = "fmask_qa"
        y_coord = fmask_da["y"].values
        x_coord = fmask_da["x"].values
        epsg_attr = fmask_da.attrs.get("epsg")

    data_vars = {}
    if include_reflectance_in_netcdf:
        for name in ("B01", "B02", "B03", "B04", "B8A", "B11", "B12"):
            data_vars[name] = (("y", "x"), bands_da.sel(band=name).values)
    data_vars["cloud_shadow_haze_mask"] = (("y", "x"), final_mask)

    attrs = {
        "source_item_id": item.id,
        "epsg": epsg_attr,
        "date_request": str(item.datetime.date()),
        "lat_request": float(lat),
        "lon_request": float(lon),
        "resolution_m": int(resolution),
        "collection": provider_cfg["collection_name"],
        "mask_mode": mode if mask_kind == "hybrid" else "n/a",
        "mask_kind": mask_kind,
    }
    return xr.Dataset(data_vars=data_vars, coords={"y": y_coord, "x": x_coord}, attrs=attrs)


def build_hls_outputs_time_range(
    lat,
    lon,
    date_range,
    *,
    bbox_deg=0.08,
    max_cloud=100,
    resolution=30,
    mode="balanced",
    download_reflectance=True,
    include_reflectance_in_netcdf=True,
    stac_provider="pc",
):
    provider_cfg = _get_stac_provider_config(stac_provider)
    items = find_pc_items_hls_s30_range(
        lat,
        lon,
        date_range,
        bbox_deg=bbox_deg,
        max_cloud=max_cloud,
        stac_provider=stac_provider,
    )

    ds_list = []
    scene_ids = []
    meta_clouds = []
    for i, item in enumerate(items, start=1):
        print(f"[{i}/{len(items)}] {item.id} | {item.datetime.date()}")
        ds_one = build_hls_outputs_from_item(
            item,
            lat,
            lon,
            bbox_deg=bbox_deg,
            resolution=resolution,
            mode=mode,
            download_reflectance=download_reflectance,
            include_reflectance_in_netcdf=include_reflectance_in_netcdf,
            stac_provider=stac_provider,
        )
        ds_one = ds_one.expand_dims(time=[np.datetime64(item.datetime.date())])
        ds_list.append(ds_one)
        scene_ids.append(item.id)
        meta_cloud = (
            item.properties.get("eo:cloud_cover", None)
            or item.properties.get("CLOUD_COVERAGE", None)
            or item.properties.get("cloud_cover", np.nan)
        )
        meta_clouds.append(float(meta_cloud) if meta_cloud is not None else np.nan)

    if not ds_list:
        raise ValueError("No scenes were collected.")

    ds_time = xr.concat(ds_list, dim="time")
    ds_time = ds_time.assign_coords(
        scene_id=("time", np.array(scene_ids, dtype=object)),
        scene_cloud_cover_meta=("time", np.array(meta_clouds, dtype=np.float32)),
    )
    ds_time.attrs.update(
        {
            "lat_request": float(lat),
            "lon_request": float(lon),
            "date_range": date_range,
            "resolution_m": int(resolution),
            "collection": provider_cfg["collection_name"],
            "mask_mode": mode,
            "download_reflectance": bool(download_reflectance),
            "include_reflectance_in_netcdf": bool(include_reflectance_in_netcdf),
        }
    )
    return ds_time

def run_cloud_statistics_one_function(
    geojson_obj,
    *,
    point_size_km_y=None,
    point_size_km_x=None,
    max_cloud=100,
    resolution=30,
    mode="balanced",
    radius_m=1500.0,
    min_valid_pixels=50,
    ewma_alpha=0.1,
    ewma_nominal_step_days=1.0,
    ewma_use_time_delta=True,
    ewma_return_history=True,
    instant_days_window=20,
    chunksize=2048,
    download_reflectance=True,
    save_to_netcdf=True,
    download_files=True,
    cloud_pct_filename="cloud_percentages.nc",
    cloud_pct_ewma_filename="cloud_percentage_ewma.nc",
    stac_provider="pc",
    cloud_scene_workers=None,
):
    """
    Одна внешняя функция.

    Требует, чтобы в ноутбуке уже были определены:
      - get_item_epsg(item)
      - build_hybrid_hls_mask(bands_da, fmask_da, mode="balanced")

    Вход:
      geojson_obj : dict
        GeoJSON Feature или FeatureCollection с одной Feature.

    Поддерживаемые geometry:
      - Point
      - Polygon (один внешний ring, без дыр)

    properties.time:
      - null
      - "YYYY-MM-DD"
      - "YYYY-MM-DDThh:mm:ss"
      - [start, end, step], где step — ISO 8601 duration

    properties.is_rectangle:
      - null для Point
      - true для прямоугольного Polygon
      - false для любого другого Polygon

    Для Point:
      - обязательно передать point_size_km_y
      - point_size_km_x можно не передавать

    Возвращает:
      {
        "ds_time": ds_time,
        "ds_cloud_pct": ds_cloud_pct,
        "ds_cloud_pct_ewma": ds_cloud_pct_ewma,
        "cloud_pct_filename": cloud_pct_filename,
        "cloud_pct_ewma_filename": cloud_pct_ewma_filename,
      }
    """
    import math
    import re
    from datetime import datetime, timedelta

    import numpy as np
    import xarray as xr
    import rioxarray  # noqa: F401

    from scipy.ndimage import convolve
    from matplotlib.path import Path

    fast_fail = _env_flag("CLOUD_DEBUG_FAST_FAIL", default=False)
    max_items_raw = os.getenv("CLOUD_DEBUG_MAX_ITEMS", "").strip()
    max_items = int(max_items_raw) if max_items_raw else None
    if max_items is not None and max_items <= 0:
        max_items = None
    if cloud_scene_workers is None:
        workers_raw = os.getenv("MAX_CLOUD_SCENE_WORKERS", "1").strip()
        cloud_scene_workers = int(workers_raw) if workers_raw else 1
    cloud_scene_workers = max(1, int(cloud_scene_workers))

    if fast_fail:
        print("Debug fast-fail mode is enabled (CLOUD_DEBUG_FAST_FAIL=1)")
    if max_items is not None:
        print(f"Debug item limit is enabled: first {max_items} scene(s)")
    if cloud_scene_workers > 1:
        print(f"Cloud scenes parallel mode is enabled: workers={cloud_scene_workers}")

    # -------------------------------
    # helpers: serialization-safe attrs
    # -------------------------------
    def _sanitize_value_for_netcdf(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return int(v)
        if isinstance(v, (str, int, float, bytes, np.integer, np.floating)):
            return v
        if isinstance(v, (list, tuple)):
            return [_sanitize_value_for_netcdf(x) for x in v]
        return str(v)

    def _sanitize_attrs_for_netcdf(ds):
        ds = ds.copy()
        ds.attrs = {k: _sanitize_value_for_netcdf(v) for k, v in ds.attrs.items()}
        return ds

    # -------------------------------
    # helpers: geojson
    # -------------------------------
    def _extract_single_feature(obj):
        if not isinstance(obj, dict):
            raise ValueError("GeoJSON должен быть словарем Python.")

        gj_type = obj.get("type")

        if gj_type == "Feature":
            return obj

        if gj_type == "FeatureCollection":
            features = obj.get("features", [])
            if len(features) != 1:
                raise ValueError("Ожидается FeatureCollection ровно с одной Feature.")
            return features[0]

        raise ValueError("Ожидается GeoJSON типа Feature или FeatureCollection.")

    def _validate_point_coords(coords):
        if not isinstance(coords, (list, tuple)) or len(coords) != 2:
            raise ValueError("Point.coordinates должен быть [lon, lat].")

        lon, lat = coords
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("Координаты Point вне диапазона EPSG:4326.")

        return float(lon), float(lat)

    def _validate_polygon_coords(coords):
        if not isinstance(coords, list) or len(coords) != 1:
            raise ValueError("Polygon должен содержать ровно один внешний linear ring без дыр.")

        ring = coords[0]

        if not isinstance(ring, list) or len(ring) < 4:
            raise ValueError("Linear ring должен содержать минимум 4 точки.")

        if ring[0] != ring[-1]:
            raise ValueError("Linear ring должен быть замкнут: первая и последняя точки совпадают.")

        parsed = []
        for pt in ring:
            if not isinstance(pt, (list, tuple)) or len(pt) != 2:
                raise ValueError("Каждая вершина полигона должна иметь вид [lon, lat].")
            lon, lat = pt
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                raise ValueError("Координаты Polygon вне диапазона EPSG:4326.")
            parsed.append((float(lon), float(lat)))

        return parsed

    def _is_axis_aligned_rectangle(ring_lonlat):
        if len(ring_lonlat) != 5:
            return False

        unique_pts = list(dict.fromkeys(ring_lonlat[:-1]))
        if len(unique_pts) != 4:
            return False

        lons = sorted(set([p[0] for p in unique_pts]))
        lats = sorted(set([p[1] for p in unique_pts]))

        if len(lons) != 2 or len(lats) != 2:
            return False

        corners = {
            (lons[0], lats[0]),
            (lons[0], lats[1]),
            (lons[1], lats[0]),
            (lons[1], lats[1]),
        }
        return set(unique_pts) == corners

    def _polygon_bbox_and_center(ring_lonlat):
        lons = [p[0] for p in ring_lonlat]
        lats = [p[1] for p in ring_lonlat]

        lon_min = min(lons)
        lon_max = max(lons)
        lat_min = min(lats)
        lat_max = max(lats)

        lon_center = 0.5 * (lon_min + lon_max)
        lat_center = 0.5 * (lat_min + lat_max)

        return [lon_min, lat_min, lon_max, lat_max], lat_center, lon_center

    def _bbox_from_point_km(lat, lon, size_km_y, size_km_x=None):
        if size_km_x is None:
            size_km_x = size_km_y

        if size_km_y <= 0 or size_km_x <= 0:
            raise ValueError("Размер области в километрах должен быть > 0.")

        half_height_km = size_km_y / 2.0
        half_width_km = size_km_x / 2.0

        km_per_deg_lat = 111.32
        cos_lat = math.cos(math.radians(lat))
        if abs(cos_lat) < 1e-12:
            raise ValueError("Слишком близко к полюсу для перевода долготы в километры.")
        km_per_deg_lon = 111.32 * cos_lat

        dlat = half_height_km / km_per_deg_lat
        dlon = half_width_km / km_per_deg_lon

        return [lon - dlon, lat - dlat, lon + dlon, lat + dlat]

    # -------------------------------
    # helpers: time
    # -------------------------------
    def _is_iso_datetime_no_tz(s):
        if not isinstance(s, str):
            return False

        patterns = [
            r"^\d{4}-\d{2}-\d{2}$",
            r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$",
        ]
        return any(re.fullmatch(p, s) for p in patterns)

    def _parse_iso_datetime_no_tz(s):
        if not _is_iso_datetime_no_tz(s):
            raise ValueError(f"Некорректная дата-время без timezone: {s}")
        if "T" in s:
            return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
        return datetime.strptime(s, "%Y-%m-%d")

    def _validate_iso_duration(step):
        if not isinstance(step, str):
            return False
        pattern = (
            r"^P"
            r"(?:(\d+)Y)?"
            r"(?:(\d+)M)?"
            r"(?:(\d+)D)?"
            r"(?:T"
            r"(?:(\d+)H)?"
            r"(?:(\d+)M)?"
            r"(?:(\d+)S)?"
            r")?$"
        )
        return re.fullmatch(pattern, step) is not None

    def _parse_iso_duration_to_timedelta(step):
        if not _validate_iso_duration(step):
            raise ValueError(f"Некорректный ISO 8601 duration: {step}")

        pattern = (
            r"^P"
            r"(?:(?P<years>\d+)Y)?"
            r"(?:(?P<months>\d+)M)?"
            r"(?:(?P<days>\d+)D)?"
            r"(?:T"
            r"(?:(?P<hours>\d+)H)?"
            r"(?:(?P<minutes>\d+)M)?"
            r"(?:(?P<seconds>\d+)S)?"
            r")?$"
        )
        m = re.fullmatch(pattern, step)
        g = m.groupdict()

        years = int(g["years"]) if g["years"] else 0
        months = int(g["months"]) if g["months"] else 0
        days = int(g["days"]) if g["days"] else 0
        hours = int(g["hours"]) if g["hours"] else 0
        minutes = int(g["minutes"]) if g["minutes"] else 0
        seconds = int(g["seconds"]) if g["seconds"] else 0

        if years != 0 or months != 0:
            raise ValueError("В step не поддерживаются Y и календарные M. Используй D/T/H/M/S.")

        td = timedelta(days=days, hours=hours, minutes=minutes, seconds=seconds)
        if td.total_seconds() <= 0:
            raise ValueError("step должен быть положительным интервалом.")

        return td

    def _parse_time_property(time_value):
        if time_value is None:
            return {
                "mode": "null",
                "instant": None,
                "start": None,
                "end": None,
                "step": None,
            }

        if isinstance(time_value, str):
            dt = _parse_iso_datetime_no_tz(time_value)
            return {
                "mode": "instant",
                "instant": dt,
                "start": None,
                "end": None,
                "step": None,
            }

        if isinstance(time_value, (list, tuple)):
            if len(time_value) != 3:
                raise ValueError("time-массив должен иметь вид [start, end, step].")

            start_s, end_s, step_s = time_value
            start_dt = _parse_iso_datetime_no_tz(start_s)
            end_dt = _parse_iso_datetime_no_tz(end_s)
            step_td = _parse_iso_duration_to_timedelta(step_s)

            if end_dt < start_dt:
                raise ValueError("Конец периода раньше начала периода.")

            return {
                "mode": "range",
                "instant": None,
                "start": start_dt,
                "end": end_dt,
                "step": step_td,
            }

        raise ValueError("properties.time должен быть null, строкой или массивом [start, end, step].")

    # -------------------------------
    # helpers: stac search
    # -------------------------------
    provider_cfg = _get_stac_provider_config(stac_provider)

    def _find_pc_items_hls_s30_bbox(bbox, date_range, max_cloud=100):
        items = _search_hls_items_for_bbox(
            bbox,
            date_range,
            stac_provider=stac_provider,
        )
        print(f"STAC search (HLS S30, bbox range): scenes={len(items)}")

        if not items:
            raise ValueError("Сцены не найдены для заданной области и диапазона дат.")

        items = [it for it in items if _extract_cloud_cover(it) <= max_cloud]
        items = sorted(items, key=lambda it: it.datetime)

        print(f"После фильтра max_cloud={max_cloud}: scenes={len(items)}")

        if not items:
            raise ValueError("После фильтрации по max_cloud сцены не остались.")

        return items

    def _find_pc_item_hls_s30_bbox_nearest(bbox, target_dt, days_window=20, max_cloud=100):
        time_start = (target_dt - timedelta(days=days_window)).strftime("%Y-%m-%d")
        time_end = (target_dt + timedelta(days=days_window)).strftime("%Y-%m-%d")

        items = _search_hls_items_for_bbox(
            bbox,
            f"{time_start}/{time_end}",
            stac_provider=stac_provider,
        )
        print(f"STAC search (HLS S30, bbox nearest): scenes={len(items)}")

        if not items:
            raise ValueError("Не найдено ни одной сцены около заданной даты.")

        def time_dist(it):
            item_dt = it.datetime.replace(tzinfo=None)
            return abs((item_dt - target_dt).total_seconds())

        filtered = [it for it in items if _extract_cloud_cover(it) <= max_cloud]
        if not filtered:
            filtered = items

        best = sorted(filtered, key=lambda it: (time_dist(it), _extract_cloud_cover(it)))[0]

        print(
            "Selected:",
            best.id,
            "| date:", best.datetime.date(),
            "| cloud:", best.properties.get("eo:cloud_cover", best.properties.get("CLOUD_COVERAGE"))
        )
        return best

    # -------------------------------
    # helpers: build ds
    # -------------------------------
    def _build_hls_outputs_from_item_bbox(
        item,
        bbox,
        resolution=30,
        mode="balanced",
        download_reflectance=True,
    ):
        epsg = get_item_epsg(item)
        print("Using EPSG:", epsg, "| item:", item.id)

        if int(resolution) != 30:
            print("Для HLS используется 30 м. Принудительно ставлю resolution=30.")
            resolution = 30

        assets = (
            ["B01", "B02", "B03", "B04", "B8A", "B11", "B12", "Fmask"]
            if download_reflectance
            else ["Fmask"]
        )

        da = _stack_item_with_provider_auth(
            item,
            assets=assets,
            bbox=bbox,
            resolution=resolution,
            epsg=epsg,
            chunksize=chunksize,
            stac_provider=stac_provider,
        )

        fmask_da = da.sel(band="Fmask").astype(np.uint8)
        fmask_da = fmask_da.rename("Fmask").rio.write_crs(epsg)

        if download_reflectance:
            bands_da = (
                da.sel(band=["B01", "B02", "B03", "B04", "B8A", "B11", "B12"]).astype(np.float32) / 10000.0
            )
            bands_da = bands_da.rename("hls_reflectance").rio.write_crs(epsg)
            final_mask = build_hybrid_hls_mask(bands_da, fmask_da, mode=mode)
            y = bands_da["y"].values
            x = bands_da["x"].values
            data_vars = {
                "B01": (("y", "x"), bands_da.sel(band="B01").values),
                "B02": (("y", "x"), bands_da.sel(band="B02").values),
                "B03": (("y", "x"), bands_da.sel(band="B03").values),
                "B04": (("y", "x"), bands_da.sel(band="B04").values),
                "B8A": (("y", "x"), bands_da.sel(band="B8A").values),
                "B11": (("y", "x"), bands_da.sel(band="B11").values),
                "B12": (("y", "x"), bands_da.sel(band="B12").values),
                "cloud_shadow_haze_mask": (("y", "x"), np.asarray(final_mask, dtype=np.uint8)),
            }
            mask_kind = "hybrid"
        else:
            final_mask = build_fmask_qa_mask(fmask_da)
            y = fmask_da["y"].values
            x = fmask_da["x"].values
            data_vars = {
                "cloud_shadow_haze_mask": (("y", "x"), np.asarray(final_mask, dtype=np.uint8)),
            }
            mask_kind = "fmask_qa"

        ds = xr.Dataset(
            data_vars=data_vars,
            coords={"y": y, "x": x},
            attrs={
                "source_item_id": item.id,
                "epsg": int(epsg),
                "resolution_m": int(resolution),
                "collection": provider_cfg["collection_name"],
                "mask_mode": mode if mask_kind == "hybrid" else "n/a",
                "mask_kind": mask_kind,
                "bbox_lon_min": float(bbox[0]),
                "bbox_lat_min": float(bbox[1]),
                "bbox_lon_max": float(bbox[2]),
                "bbox_lat_max": float(bbox[3]),
            },
        )

        return ds

    def _build_hls_outputs_time_from_items_bbox(
        items,
        bbox,
        resolution=30,
        mode="balanced",
        download_reflectance=True,
        scene_workers=1,
    ):
        scene_workers = max(1, int(scene_workers))
        records = []

        def _process_one_item(idx, item):
            print("=" * 80)
            print(f"[{idx + 1}/{len(items)}] {item.id} | {item.datetime.date()}")
            ds_one = _build_hls_outputs_from_item_bbox(
                item,
                bbox,
                resolution=resolution,
                mode=mode,
                download_reflectance=download_reflectance,
            )
            scene_time = np.datetime64(item.datetime.date())
            ds_one = ds_one.expand_dims(time=[scene_time])
            meta_cloud = (
                item.properties.get("eo:cloud_cover", None)
                or item.properties.get("CLOUD_COVERAGE", None)
                or item.properties.get("cloud_cover", np.nan)
            )
            meta_cloud = float(meta_cloud) if meta_cloud is not None else np.nan
            return idx, ds_one, item.id, meta_cloud

        if scene_workers == 1:
            for idx, item in enumerate(items):
                try:
                    records.append(_process_one_item(idx, item))
                except Exception as e:
                    print("Ошибка на сцене:", item.id, "|", e)
                    if fast_fail:
                        raise RuntimeError(f"Fast-fail on scene {item.id}") from e
        else:
            with ThreadPoolExecutor(max_workers=scene_workers) as executor:
                future_to_item = {
                    executor.submit(_process_one_item, idx, item): (idx, item)
                    for idx, item in enumerate(items)
                }
                for future in as_completed(future_to_item):
                    _, item = future_to_item[future]
                    try:
                        records.append(future.result())
                    except Exception as e:
                        print("Ошибка на сцене:", item.id, "|", e)
                        if fast_fail:
                            for pending in future_to_item:
                                if pending is not future:
                                    pending.cancel()
                            raise RuntimeError(f"Fast-fail on scene {item.id}") from e

        if not records:
            raise ValueError("Не удалось собрать ни одной сцены для данного bbox.")

        records.sort(key=lambda rec: rec[0])
        ds_list = [rec[1] for rec in records]
        scene_ids = [rec[2] for rec in records]
        meta_clouds = [rec[3] for rec in records]

        ds_time = xr.concat(ds_list, dim="time")

        ds_time = ds_time.assign_coords(
            scene_id=("time", np.array(scene_ids, dtype=object)),
            scene_cloud_cover_meta=("time", np.array(meta_clouds, dtype=np.float32)),
        )

        return ds_time

    def _select_items_by_time_schedule(items, start_dt, end_dt, step_td):
        if not items:
            return items

        item_times = np.array([np.datetime64(it.datetime.replace(tzinfo=None)) for it in items])
        schedule = []
        cur = start_dt
        while cur <= end_dt:
            schedule.append(np.datetime64(cur))
            cur = cur + step_td

        chosen = []
        for target in schedule:
            idx = int(np.argmin(np.abs(item_times - target)))
            chosen.append(idx)

        chosen = sorted(set(chosen))
        return [items[i] for i in chosen]

    def _apply_time_step_schedule(ds_time, start_dt, end_dt, step_td):
        scene_times = ds_time["time"].values
        if len(scene_times) == 0:
            return ds_time

        schedule = []
        cur = start_dt
        while cur <= end_dt:
            schedule.append(np.datetime64(cur))
            cur = cur + step_td

        chosen = []
        for target in schedule:
            idx = int(np.argmin(np.abs(scene_times - target)))
            chosen.append(idx)

        chosen = sorted(set(chosen))
        return ds_time.isel(time=chosen)

    # -------------------------------
    # helpers: polygon masking
    # -------------------------------
    def _project_lonlat_ring_to_item_crs(ring_lonlat, epsg):
        from pyproj import Transformer

        transformer = Transformer.from_crs("EPSG:4326", f"EPSG:{epsg}", always_xy=True)

        xs = []
        ys = []
        for lon, lat in ring_lonlat:
            x, y = transformer.transform(lon, lat)
            xs.append(x)
            ys.append(y)

        return list(zip(xs, ys))

    def _mask_dataset_by_polygon(ds, ring_lonlat):
        epsg = ds.attrs.get("epsg", None)
        if epsg is None:
            raise ValueError("В ds нет attrs['epsg']; нельзя построить маску полигона.")

        ring_xy = _project_lonlat_ring_to_item_crs(ring_lonlat, epsg)

        x = ds["x"].values
        y = ds["y"].values

        xx, yy = np.meshgrid(x, y)
        pts = np.column_stack([xx.ravel(), yy.ravel()])

        path = Path(ring_xy)
        inside = path.contains_points(pts).reshape(len(y), len(x))

        ds = ds.copy()

        spectral_vars = ["B01", "B02", "B03", "B04", "B8A", "B11", "B12"]
        for v in spectral_vars:
            arr = ds[v].values.astype(np.float32)
            arr[~inside] = np.nan
            ds[v] = (("y", "x"), arr)

        mask_arr = ds["cloud_shadow_haze_mask"].values.astype(np.uint8)
        mask_arr[~inside] = 0
        ds["cloud_shadow_haze_mask"] = (("y", "x"), mask_arr)

        ds.attrs["polygon_mask_applied"] = 1
        return ds

    def _mask_ds_time_by_polygon(ds_time, ring_lonlat):
        ds_list = []

        for t in range(len(ds_time["time"])):
            ds_one = ds_time.isel(time=t).copy()
            ds_one = _mask_dataset_by_polygon(ds_one, ring_lonlat)
            ds_one = ds_one.expand_dims(time=[ds_time["time"].values[t]])
            ds_list.append(ds_one)

        out = xr.concat(ds_list, dim="time")

        if "scene_id" in ds_time.coords:
            out = out.assign_coords(scene_id=("time", ds_time["scene_id"].values))
        if "scene_cloud_cover_meta" in ds_time.coords:
            out = out.assign_coords(scene_cloud_cover_meta=("time", ds_time["scene_cloud_cover_meta"].values))

        out.attrs.update(ds_time.attrs)
        out.attrs["polygon_mask_applied"] = 1
        return out

    # -------------------------------
    # helpers: module 1
    # -------------------------------
    def _build_valid_observation_mask(ds_time):
        valid = (
            np.isfinite(ds_time["B01"].values) &
            np.isfinite(ds_time["B02"].values) &
            np.isfinite(ds_time["B03"].values) &
            np.isfinite(ds_time["B04"].values) &
            np.isfinite(ds_time["B8A"].values) &
            np.isfinite(ds_time["B11"].values) &
            np.isfinite(ds_time["B12"].values)
        )
        return valid.astype(np.uint8)

    def _make_circular_kernel(radius_pixels):
        r = int(radius_pixels)
        yy, xx = np.ogrid[-r:r+1, -r:r+1]
        mask = (xx**2 + yy**2) <= r**2
        return mask.astype(np.float32)

    def _infer_resolution_m(ds_time):
        if "resolution_m" in ds_time.attrs:
            return float(ds_time.attrs["resolution_m"])

        x = ds_time["x"].values
        y = ds_time["y"].values

        dx = float(np.median(np.abs(np.diff(x)))) if len(x) > 1 else np.nan
        dy = float(np.median(np.abs(np.diff(y)))) if len(y) > 1 else np.nan

        vals = [v for v in [dx, dy] if np.isfinite(v) and v > 0]
        if not vals:
            raise ValueError("Не удалось определить resolution_m.")

        return float(np.median(vals))

    def _compute_cloud_percentages_xarray(ds_time, mask_name="cloud_shadow_haze_mask", radius_m=1500.0, min_valid_pixels=50):
        if "time" not in ds_time.dims:
            raise ValueError("В ds_time нет измерения 'time'.")

        if mask_name not in ds_time.data_vars:
            raise ValueError(f"Переменная '{mask_name}' не найдена в ds_time.")

        ds_time = ds_time.sortby("time")

        resolution_m = _infer_resolution_m(ds_time)
        radius_pixels = int(np.ceil(radius_m / resolution_m))
        kernel = _make_circular_kernel(radius_pixels)

        cloud_mask = xr.where(ds_time[mask_name] > 0, 1, 0).astype(np.uint8).values
        valid_mask = _build_valid_observation_mask(ds_time).astype(np.uint8)

        n_time, h, w = cloud_mask.shape

        cloud_count_local = np.zeros((n_time, h, w), dtype=np.float32)
        valid_count_local = np.zeros((n_time, h, w), dtype=np.float32)
        cloud_percentage_local = np.full((n_time, h, w), np.nan, dtype=np.float32)

        for t in range(n_time):
            cur_cloud = cloud_mask[t].astype(np.float32)
            cur_valid = valid_mask[t].astype(np.float32)

            cur_cloud_valid = cur_cloud * cur_valid

            local_cloud = convolve(cur_cloud_valid, kernel, mode="nearest")
            local_valid = convolve(cur_valid, kernel, mode="nearest")

            local_percent = np.divide(
                100.0 * local_cloud,
                np.maximum(local_valid, 1e-6),
                out=np.full((h, w), np.nan, dtype=np.float32),
                where=local_valid >= float(min_valid_pixels)
            )

            cloud_count_local[t] = local_cloud
            valid_count_local[t] = local_valid
            cloud_percentage_local[t] = local_percent.astype(np.float32)

        out = xr.Dataset(
            data_vars={
                "cloud_percentage_local": (("time", "y", "x"), cloud_percentage_local),
                "cloud_count_local": (("time", "y", "x"), cloud_count_local),
                "valid_count_local": (("time", "y", "x"), valid_count_local),
                "valid_observation_mask": (("time", "y", "x"), valid_mask.astype(np.uint8)),
            },
            coords={
                "time": ds_time["time"].values,
                "y": ds_time["y"].values,
                "x": ds_time["x"].values,
            },
            attrs={
                **ds_time.attrs,
                "statistics_type": "cloud_percentage_local_timeseries",
                "radius_m": float(radius_m),
                "radius_pixels": int(radius_pixels),
                "min_valid_pixels": int(min_valid_pixels),
                "percentage_definition": "100 * cloudy_valid_pixels / valid_pixels_in_radius",
            }
        )

        if "scene_id" in ds_time.coords:
            out = out.assign_coords(scene_id=("time", ds_time["scene_id"].values))
        if "scene_cloud_cover_meta" in ds_time.coords:
            out = out.assign_coords(scene_cloud_cover_meta=("time", ds_time["scene_cloud_cover_meta"].values))

        return out

    # -------------------------------
    # helpers: module 2
    # -------------------------------
    def _compute_alpha_eff(delta_days, alpha, nominal_step_days=1.0):
        delta_days = float(delta_days)
        if delta_days <= 0:
            return float(alpha)
        return float(1.0 - (1.0 - float(alpha)) ** (delta_days / float(nominal_step_days)))

    def _compute_ewma_cloud_percentage(
        ds_cloud_pct,
        percentage_var="cloud_percentage_local",
        alpha=0.1,
        nominal_step_days=1.0,
        use_time_delta=True,
        return_history=True,
    ):
        if "time" not in ds_cloud_pct.dims:
            raise ValueError("В ds_cloud_pct нет измерения 'time'.")

        if percentage_var not in ds_cloud_pct.data_vars:
            raise ValueError(f"Переменная '{percentage_var}' не найдена в ds_cloud_pct.")

        ds_cloud_pct = ds_cloud_pct.sortby("time")

        values = ds_cloud_pct[percentage_var].values.astype(np.float32)
        times = ds_cloud_pct["time"].values

        n_time, h, w = values.shape
        if n_time == 0:
            raise ValueError("Временной ряд пустой.")

        ewma = np.full((h, w), np.nan, dtype=np.float32)
        alpha_eff_list = np.full(n_time, np.nan, dtype=np.float32)

        first = values[0]
        first_valid = np.isfinite(first)
        ewma[first_valid] = first[first_valid]

        if return_history:
            history = np.full((n_time, h, w), np.nan, dtype=np.float32)
            history[0] = ewma

        for t in range(1, n_time):
            cur = values[t]
            cur_valid = np.isfinite(cur)

            if use_time_delta:
                delta_days = (times[t] - times[t - 1]) / np.timedelta64(1, "D")
                alpha_eff = _compute_alpha_eff(delta_days, alpha, nominal_step_days)
            else:
                alpha_eff = float(alpha)

            alpha_eff_list[t] = alpha_eff

            upd = np.isfinite(ewma) & cur_valid
            ewma[upd] = (1.0 - alpha_eff) * ewma[upd] + alpha_eff * cur[upd]

            init = (~np.isfinite(ewma)) & cur_valid
            ewma[init] = cur[init]

            if return_history:
                history[t] = ewma

        data_vars = {
            "cloud_percentage_ewma_final": (("y", "x"), ewma.astype(np.float32)),
        }

        if return_history:
            data_vars["cloud_percentage_ewma_history"] = (
                ("time", "y", "x"),
                history.astype(np.float32)
            )

        out = xr.Dataset(
            data_vars=data_vars,
            coords={
                "y": ds_cloud_pct["y"].values,
                "x": ds_cloud_pct["x"].values,
                "time": ds_cloud_pct["time"].values,
                "alpha_eff": ("time", alpha_eff_list),
            },
            attrs={
                **ds_cloud_pct.attrs,
                "statistics_type": "ewma_cloud_percentage",
                "ewma_alpha_base": float(alpha),
                "ewma_nominal_step_days": float(nominal_step_days),
                "ewma_use_time_delta": int(bool(use_time_delta)),
            }
        )

        if "scene_id" in ds_cloud_pct.coords:
            out = out.assign_coords(scene_id=("time", ds_cloud_pct["scene_id"].values))

        return out

    # -------------------------------
    # main logic
    # -------------------------------
    feature = _extract_single_feature(geojson_obj)

    geometry = feature.get("geometry", None)
    properties = feature.get("properties", {})

    if geometry is None:
        raise ValueError("В Feature отсутствует geometry.")

    geom_type = geometry.get("type", None)
    coords = geometry.get("coordinates", None)

    if geom_type not in ("Point", "Polygon"):
        raise ValueError("Поддерживаются только geometry типов Point и Polygon.")

    is_rectangle_prop = properties.get("is_rectangle", None)
    time_prop = properties.get("time", None)

    time_info = _parse_time_property(time_prop)
    polygon_ring = None

    if geom_type == "Point":
        if is_rectangle_prop is not None:
            raise ValueError("Для Point свойство is_rectangle должно быть null.")

        lon, lat = _validate_point_coords(coords)

        if point_size_km_y is None:
            raise ValueError("Для Point нужно задать point_size_km_y.")

        bbox = _bbox_from_point_km(
            lat=lat,
            lon=lon,
            size_km_y=point_size_km_y,
            size_km_x=point_size_km_x,
        )

    else:
        ring = _validate_polygon_coords(coords)
        polygon_ring = ring

        real_is_rectangle = _is_axis_aligned_rectangle(ring)

        if is_rectangle_prop is None:
            raise ValueError("Для Polygon свойство is_rectangle должно быть true или false.")

        if bool(is_rectangle_prop) != bool(real_is_rectangle):
            raise ValueError(
                f"is_rectangle={is_rectangle_prop}, но геометрия "
                f"{'является' if real_is_rectangle else 'не является'} прямоугольником."
            )

        bbox, lat, lon = _polygon_bbox_and_center(ring)

    if time_info["mode"] == "null":
        raise ValueError(
            "Для временных рядов properties.time не может быть null. "
            "Нужна одна дата или [start, end, step]."
        )

    if time_info["mode"] == "instant":
        item = _find_pc_item_hls_s30_bbox_nearest(
            bbox,
            time_info["instant"],
            days_window=instant_days_window,
            max_cloud=max_cloud,
        )

        ds_one = _build_hls_outputs_from_item_bbox(
            item,
            bbox,
            resolution=resolution,
            mode=mode,
        )

        ds_time = ds_one.expand_dims(time=[np.datetime64(item.datetime.date())])
        ds_time = ds_time.assign_coords(scene_id=("time", np.array([item.id], dtype=object)))

    else:
        start_dt = time_info["start"]
        end_dt = time_info["end"]
        step_td = time_info["step"]

        date_range = f"{start_dt.strftime('%Y-%m-%d')}/{end_dt.strftime('%Y-%m-%d')}"

        items = _find_pc_items_hls_s30_bbox(
            bbox,
            date_range,
            max_cloud=max_cloud,
        )
        items = _select_items_by_time_schedule(items, start_dt=start_dt, end_dt=end_dt, step_td=step_td)
        if max_items is not None and len(items) > max_items:
            items = items[:max_items]
        print(f"Items after {step_td} schedule preselect: {len(items)}")

        ds_time = _build_hls_outputs_time_from_items_bbox(
            items,
            bbox,
            resolution=resolution,
            mode=mode,
            download_reflectance=download_reflectance,
            scene_workers=cloud_scene_workers,
        )

    if geom_type == "Polygon" and is_rectangle_prop is False:
        ds_time = _mask_ds_time_by_polygon(ds_time, polygon_ring)

    ds_time.attrs["input_geometry_type"] = str(geom_type)
    ds_time.attrs["input_is_rectangle"] = "null" if is_rectangle_prop is None else int(bool(is_rectangle_prop))
    ds_time.attrs["input_time_mode"] = str(time_info["mode"])
    ds_time.attrs["input_bbox_lon_min"] = float(bbox[0])
    ds_time.attrs["input_bbox_lat_min"] = float(bbox[1])
    ds_time.attrs["input_bbox_lon_max"] = float(bbox[2])
    ds_time.attrs["input_bbox_lat_max"] = float(bbox[3])
    ds_time.attrs["stac_provider"] = str(stac_provider)
    ds_time.attrs["collection"] = provider_cfg["collection_name"]

    ds_cloud_pct = _compute_cloud_percentages_xarray(
        ds_time,
        mask_name="cloud_shadow_haze_mask",
        radius_m=radius_m,
        min_valid_pixels=min_valid_pixels,
    )

    ds_cloud_pct_ewma = _compute_ewma_cloud_percentage(
        ds_cloud_pct,
        percentage_var="cloud_percentage_local",
        alpha=ewma_alpha,
        nominal_step_days=ewma_nominal_step_days,
        use_time_delta=ewma_use_time_delta,
        return_history=ewma_return_history,
    )

    # -------------------------------
    # save + download
    # -------------------------------
    if save_to_netcdf:
        ds_cloud_pct_safe = _sanitize_attrs_for_netcdf(ds_cloud_pct)
        ds_cloud_pct_ewma_safe = _sanitize_attrs_for_netcdf(ds_cloud_pct_ewma)

        ds_cloud_pct_safe.to_netcdf(cloud_pct_filename)
        ds_cloud_pct_ewma_safe.to_netcdf(cloud_pct_ewma_filename)

        if download_files:
            try:
                from google.colab import files
                files.download(cloud_pct_filename)
                files.download(cloud_pct_ewma_filename)
            except Exception as e:
                print("Автоскачивание не выполнено:", e)
                print("Файлы сохранены локально:")
                print(" ", cloud_pct_filename)
                print(" ", cloud_pct_ewma_filename)

    return {
        "ds_time": ds_time,
        "ds_cloud_pct": ds_cloud_pct,
        "ds_cloud_pct_ewma": ds_cloud_pct_ewma,
        "cloud_pct_filename": cloud_pct_filename,
        "cloud_pct_ewma_filename": cloud_pct_ewma_filename,
    }