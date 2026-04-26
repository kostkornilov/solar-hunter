import zipfile
import shutil
import cdsapi
import isodate
import numpy as np
import pandas as pd
import xarray as xr
from datetime import datetime
import logging
import os
from shapely.geometry import shape, Point, Polygon
import cfgrib
import warnings
from math import cos, radians
import isodate  # для разбора P1DT12H

# Игнорируем предупреждения от cfgrib
warnings.filterwarnings("ignore", category=RuntimeWarning)

# Настройка логирования
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s"
)

CDS_API_URL = os.getenv("CDS_API_URL", "https://cds.climate.copernicus.eu/api")
CDS_API_KEY = os.getenv("CDS_API_KEY", "")
DATASET_NAME = "reanalysis-era5-land"
TEMP_DIR = "temp_extracted"
MIN_AREA_SIZE = 0.1  # минимальный размер области в градусах


def remove_nan_values(ds: xr.Dataset, method: str = 'interpolate', max_gap: int = 3) -> xr.Dataset:
    """
    Удаляет или заполняет NaN значения в датасете.
    
    Parameters:
    -----------
    ds : xr.Dataset
        Входной датасет с NaN значениями
    method : str
        Метод обработки NaN:
        - 'drop': удалить все строки/столбцы с NaN
        - 'interpolate': интерполяция по времени и пространству
        - 'fill': заполнение соседними значениями
        - 'zero': заполнение нулями
    max_gap : int
        Максимальный размер пропуска для интерполяции
    
    Returns:
    --------
    xr.Dataset
        Датсет без NaN значений
    """
    if not ds.data_vars:
        return ds
    
    var_name = list(ds.data_vars.keys())[0]
    data_array = ds[var_name]
    
    # Статистика до обработки
    nan_before = data_array.isnull().sum().item()
    total_values = data_array.size
    
    if nan_before == 0:
        logging.info("NaN значений не обнаружено")
        return ds
    
    logging.info(f"Обнаружено NaN значений: {nan_before} ({nan_before/total_values*100:.2f}%)")
    
    if method == 'drop':
        # Удаляем временные срезы, где все значения NaN
        time_mask = data_array.isnull().all(dim=['latitude', 'longitude'])
        ds_clean = ds.isel(time=~time_mask)
        
        # Удаляем пространственные точки, где все значения NaN
        spatial_mask = data_array.isnull().all(dim='time')
        ds_clean = ds_clean.where(~spatial_mask, drop=True)
        
        logging.info(f"Удалено временных срезов: {time_mask.sum().item()}")
        logging.info(f"Удалено пространственных точек: {spatial_mask.sum().item()}")
        
    elif method == 'interpolate':
        # Интерполяция сначала по времени, затем по пространству
        ds_clean = ds.copy()
        
        # Интерполяция по времени
        if 'time' in data_array.dims and len(data_array.time) > 1:
            ds_clean[var_name] = data_array.interpolate_na(
                dim='time', 
                method='linear',
                fill_value="extrapolate",
                limit=max_gap
            )
            logging.info("Выполнена временная интерполяция")
        
        # Интерполяция по пространству
        if 'latitude' in data_array.dims and 'longitude' in data_array.dims:
            ds_clean[var_name] = ds_clean[var_name].interpolate_na(
                dim='longitude', 
                method='linear',
                fill_value="extrapolate"
            ).interpolate_na(
                dim='latitude',
                method='linear',
                fill_value="extrapolate"
            )
            logging.info("Выполнена пространственная интерполяция")
            
    elif method == 'fill':
        # Заполнение соседними значениями
        ds_clean = ds.copy()
        ds_clean[var_name] = data_array.ffill(dim='time').bfill(dim='time')
        logging.info("Выполнено заполнение соседними значениями")
        
    elif method == 'zero':
        # Заполнение нулями
        ds_clean = ds.copy()
        ds_clean[var_name] = data_array.fillna(0)
        logging.info("Выполнено заполнение нулями")
    
    else:
        logging.warning(f"Неизвестный метод {method}, возвращаем исходные данные")
        return ds
    
    # Статистика после обработки
    nan_after = ds_clean[var_name].isnull().sum().item()
    removed_nan = nan_before - nan_after
    logging.info(f"Удалено NaN значений: {removed_nan} ({removed_nan/nan_before*100:.1f}%)")
    logging.info(f"Осталось NaN: {nan_after} ({nan_after/total_values*100:.2f}%)")
    
    return ds_clean


def adjust_area_size(lat_range: tuple, lon_range: tuple) -> tuple:
    """Корректирует размер области до минимального MIN_AREA_SIZE×MIN_AREA_SIZE градусов."""
    lat_diff = lat_range[1] - lat_range[0]
    lon_diff = lon_range[1] - lon_range[0]

    if lat_diff >= MIN_AREA_SIZE and lon_diff >= MIN_AREA_SIZE:
        return lat_range, lon_range

    center_lat = (lat_range[0] + lat_range[1]) / 2
    center_lon = (lon_range[0] + lon_range[1]) / 2

    if lat_diff < MIN_AREA_SIZE:
        new_min_lat = center_lat - MIN_AREA_SIZE / 2
        new_max_lat = center_lat + MIN_AREA_SIZE / 2
    else:
        new_min_lat, new_max_lat = lat_range

    if lon_diff < MIN_AREA_SIZE:
        if abs(center_lat) >= 89.9:  # защита от деления на ноль
            lon_adjustment = MIN_AREA_SIZE
        else:
            lon_adjustment = MIN_AREA_SIZE / cos(radians(center_lat))
        new_min_lon = center_lon - lon_adjustment / 2
        new_max_lon = center_lon + lon_adjustment / 2
    else:
        new_min_lon, new_max_lon = lon_range

    return (new_min_lat, new_max_lat), (new_min_lon, new_max_lon)


def parse_geojson(geojson: dict):
    """Парсинг GeoJSON с поддержкой time = [start, end, step]"""
    try:
        feature = geojson["features"][0]
        geometry = shape(feature["geometry"])
        props = feature.get("properties", {})
    except Exception as e:
        raise ValueError(f"Ошибка в формате GeoJSON: {e}")

    # координаты области
    if isinstance(geometry, Point):
        lat_range = (geometry.y, geometry.y)
        lon_range = (geometry.x, geometry.x)
    elif isinstance(geometry, Polygon):
        b = geometry.bounds  # minx, miny, maxx, maxy
        lat_range = (b[1], b[3])
        lon_range = (b[0], b[2])
    else:
        raise ValueError("GeoJSON должен содержать Point или Polygon.")

    is_rectangle = props.get("is_rectangle", False)

    # обработка time
    time_period = props.get("time", None)
    if time_period is None or not isinstance(time_period, list):
        raise ValueError("В GeoJSON 'properties.time' должен быть список.")

    # --- формат [start, end, step] ---
    if len(time_period) == 3:
        try:
            start = pd.to_datetime(time_period[0])
            end = pd.to_datetime(time_period[1])
            step = isodate.parse_duration(time_period[2])  # PnDTnHnM
            step_hours = int(step.total_seconds() // 3600)
            dates = pd.date_range(start=start, end=end, freq=f"{step_hours}H")
            dates = [d.strftime("%Y-%m-%dT%H:%M:%S") for d in dates]
        except Exception as e:
            raise ValueError(f"Ошибка при обработке времени (start/end/step): {e}")

    # --- формат списка дат ---
    else:
        try:
            dates = [pd.to_datetime(d).strftime("%Y-%m-%dT%H:%M:%S") for d in time_period]
        except Exception:
            raise ValueError("Некорректный формат даты. Используйте YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS.")

    return lat_range, lon_range, dates, is_rectangle


def fetch_process_xarray(geojson: dict, clean_nan: bool = True, nan_method: str = 'interpolate') -> xr.Dataset:
    """Получает 'surface_solar_radiation_downwards' из ERA5-Land для геометрии и периода"""

    lat_range, lon_range, date_list, is_rectangle = parse_geojson(geojson)
    orig_lat_range, orig_lon_range = lat_range, lon_range
    lat_range, lon_range = adjust_area_size(lat_range, lon_range)
    logging.info(
        "CDS area: из GeoJSON lat=%s lon=%s → после adjust_area_size (%s°) lat=%s lon=%s, is_rectangle=%s",
        orig_lat_range,
        orig_lon_range,
        MIN_AREA_SIZE,
        lat_range,
        lon_range,
        is_rectangle,
    )

    try:
        client = cdsapi.Client(url=CDS_API_URL, key=CDS_API_KEY)
    except Exception as e:
        raise Exception(f"Ошибка при инициализации CDS API клиента: {e}")

    def _time_index(values) -> pd.DatetimeIndex:
        # Нормализуем время к naive UTC, чтобы безопасно сравнивать/реиндексировать.
        return pd.DatetimeIndex(pd.to_datetime(values, utc=True)).tz_convert(None)

    def _to_standard_dataset(da: xr.DataArray, source_name: str, cds_source: str) -> xr.Dataset:
        if "valid_time" in da.dims or "valid_time" in da.coords:
            da = da.rename({"valid_time": "time"})
        if "lat" in da.dims or "lat" in da.coords:
            da = da.rename({"lat": "latitude"})
        if "lon" in da.dims or "lon" in da.coords:
            da = da.rename({"lon": "longitude"})

        if "time" not in da.dims:
            raise ValueError("В ARCO-ответе нет измерения 'time'.")

        req_times = _time_index(date_list)
        src_times = _time_index(da["time"].values)
        da = da.assign_coords(time=src_times)
        da = da.reindex(time=req_times, method="nearest", tolerance=pd.Timedelta("1H"))

        center_lat = float((orig_lat_range[0] + orig_lat_range[1]) / 2.0)
        center_lon = float((orig_lon_range[0] + orig_lon_range[1]) / 2.0)

        if "latitude" not in da.dims:
            da = da.expand_dims(latitude=[center_lat])
        if "longitude" not in da.dims:
            da = da.expand_dims(longitude=[center_lon])

        da = da.transpose("time", "latitude", "longitude")
        ds = xr.Dataset({"solar_radiation": da}).sortby("time").sortby("latitude").sortby("longitude")
        ds.attrs = {
            "source": "CDS API",
            "dataset": source_name,
            "variable": "surface_solar_radiation_downwards",
            "units": "J/m^2",
            "processing_date": datetime.now().isoformat(),
            "original_area": f"lat: {orig_lat_range}, lon: {orig_lon_range}, is_rectangle={is_rectangle}",
            "cds_request_area": f"lat: {lat_range}, lon: {lon_range}",
            "cds_source": cds_source,
        }
        return ds

    def _fetch_arco_dataset() -> xr.Dataset | None:
        center_lat = float((orig_lat_range[0] + orig_lat_range[1]) / 2.0)
        center_lon = float((orig_lon_range[0] + orig_lon_range[1]) / 2.0)
        req_times = _time_index(date_list)
        start = req_times.min().strftime("%Y-%m-%dT%H:%M:%S")
        end = req_times.max().strftime("%Y-%m-%dT%H:%M:%S")

        request = {
            "variable": ["surface_solar_radiation_downwards"],
            "location": {"latitude": center_lat, "longitude": center_lon},
            "date": f"{start}/{end}",
            "data_format": "netcdf",
        }

        target_file = f"solar_radiation_timeseries_{safe_filename(start)}_{safe_filename(end)}.zip"
        extract_folder = f"{TEMP_DIR}_arco_{safe_filename(start)}_{safe_filename(end)}"

        def _pick_radiation_var(ds_arco: xr.Dataset) -> str:
            preferred = [
                "surface_solar_radiation_downwards",
                "ssrd",
                "solar_radiation",
            ]
            for name in preferred:
                if name in ds_arco.data_vars:
                    return name

            fallback = next((name for name in ds_arco.data_vars if "radiation" in name.lower()), None)
            if fallback is not None:
                return fallback

            raise ValueError(f"Не удалось найти переменную радиации в ARCO ответе: {list(ds_arco.data_vars)}")

        try:
            logging.info("ARCO time-series запрос: %s -> %s", start, end)
            client.retrieve("reanalysis-era5-land-timeseries", request, target_file)

            ds_arco: xr.Dataset | None = None
            if zipfile.is_zipfile(target_file):
                os.makedirs(extract_folder, exist_ok=True)
                with zipfile.ZipFile(target_file, "r") as z:
                    members = z.namelist()
                    z.extractall(extract_folder)

                nc_files = [m for m in members if m.lower().endswith((".nc", ".nc4", ".netcdf"))]
                csv_files = [m for m in members if m.lower().endswith(".csv")]

                if nc_files:
                    nc_path = os.path.join(extract_folder, nc_files[0])
                    ds_arco = xr.open_dataset(nc_path)
                elif csv_files:
                    csv_path = os.path.join(extract_folder, csv_files[0])
                    df = pd.read_csv(csv_path)
                    time_col = next((c for c in ("time", "date", "valid_time") if c in df.columns), None)
                    if time_col is None:
                        raise ValueError(f"В ARCO CSV не найдена колонка времени: {list(df.columns)}")

                    value_col = next(
                        (
                            c for c in (
                                "surface_solar_radiation_downwards",
                                "ssrd",
                                "solar_radiation",
                            )
                            if c in df.columns
                        ),
                        None,
                    )
                    if value_col is None:
                        value_col = next((c for c in df.columns if "radiation" in c.lower()), None)
                    if value_col is None:
                        raise ValueError(f"В ARCO CSV не найдена колонка радиации: {list(df.columns)}")

                    da_csv = xr.DataArray(
                        pd.to_numeric(df[value_col], errors="coerce").to_numpy(dtype=np.float32),
                        dims=("time",),
                        coords={"time": pd.to_datetime(df[time_col])},
                        name="solar_radiation",
                    )
                    return _to_standard_dataset(
                        da_csv,
                        source_name="reanalysis-era5-land-timeseries",
                        cds_source="arco_timeseries",
                    )
                else:
                    raise ValueError("ARCO архив не содержит .nc или .csv файлов.")
            else:
                ds_arco = xr.open_dataset(target_file)

            if ds_arco is None:
                raise ValueError("Не удалось открыть ARCO результат.")

            var_name = _pick_radiation_var(ds_arco)
            da = ds_arco[var_name].load()
            ds_arco.close()
            return _to_standard_dataset(
                da,
                source_name="reanalysis-era5-land-timeseries",
                cds_source="arco_timeseries",
            )
        except Exception as e:
            logging.warning("ARCO time-series не сработал, включаю fallback legacy: %s", e)
            return None
        finally:
            try:
                if os.path.exists(target_file):
                    os.remove(target_file)
                if os.path.exists(extract_folder):
                    shutil.rmtree(extract_folder)
            except OSError as e:
                logging.warning(f"Не удалось удалить временный ARCO файл: {e}")

    def grib_to_da(grib_path: str, dt: str) -> xr.DataArray:
        try:
            ds_grib = cfgrib.open_dataset(grib_path, decode_timedelta=False)
            if "ssrd" not in ds_grib.variables:
                raise ValueError("В GRIB нет переменной 'ssrd'.")

            data = ds_grib["ssrd"].values
            if data.ndim == 3:
                data = data.mean(axis=0)

            lat_vals = ds_grib["latitude"].values
            lon_vals = ds_grib["longitude"].values

            da = xr.DataArray(
                data,
                dims=("latitude", "longitude"),
                coords={"latitude": lat_vals, "longitude": lon_vals},
                name="solar_radiation",
                attrs={"units": "J/m^2"},
            )

            t = pd.to_datetime(dt)
            da = da.expand_dims(time=[t])

            if da.latitude.ndim == 1 and da.latitude[0] > da.latitude[-1]:
                da = da.sortby("latitude")
            if da.longitude.ndim == 1 and da.longitude[0] > da.longitude[-1]:
                da = da.sortby("longitude")

            return da.transpose("time", "latitude", "longitude")

        except Exception as e:
            logging.error(f"Ошибка при обработке GRIB {grib_path}: {e}")
            raise

    def fetch_datetime(dt: str) -> xr.DataArray | None:
        t = pd.to_datetime(dt)
        year, month, day, hour = t.strftime("%Y %m %d %H").split()

        request = {
            "variable": "surface_solar_radiation_downwards",
            "product_type": "reanalysis",
            "year": year,
            "month": month,
            "day": day,
            "time": [f"{hour}:00"],
            "format": "zip",
            "area": [lat_range[1], lon_range[0], lat_range[0], lon_range[1]],
        }

        target_zip = f"solar_radiation_{safe_filename(dt)}.zip"
        extract_folder = f"{TEMP_DIR}_{dt.replace(':','_')}"
        os.makedirs(extract_folder, exist_ok=True)

        try:
            logging.info(f"Скачивание данных за {dt} …")
            client.retrieve(DATASET_NAME, request, target_zip)

            with zipfile.ZipFile(target_zip, "r") as z:
                members = z.namelist()
                z.extractall(extract_folder)
                grib_file = next(
                    (f for f in members if f.lower().endswith((".grib", ".grb"))), None
                )
                if not grib_file:
                    raise FileNotFoundError("В архиве не найден GRIB-файл.")
            grib_path = os.path.join(extract_folder, grib_file)

            return grib_to_da(grib_path, dt)

        except Exception as e:
            logging.error(f"Ошибка за {dt}: {e}")
            return None
        finally:
            try:
                if os.path.exists(target_zip):
                    os.remove(target_zip)
                if os.path.exists(extract_folder):
                    shutil.rmtree(extract_folder)
            except OSError as e:
                logging.warning(f"Не удалось удалить временные файлы: {e}")

    ds: xr.Dataset | None = None
    lat_span = abs(orig_lat_range[1] - orig_lat_range[0])
    lon_span = abs(orig_lon_range[1] - orig_lon_range[0])
    is_point_like = lat_span <= MIN_AREA_SIZE and lon_span <= MIN_AREA_SIZE

    if is_point_like:
        ds = _fetch_arco_dataset()
    else:
        logging.info(
            "AOI больше %s°, использую legacy-режим area+GRIB.",
            MIN_AREA_SIZE,
        )

    if ds is None:
        slices: list[xr.DataArray] = []
        for dt in date_list:
            da = fetch_datetime(dt)
            if da is not None:
                slices.append(da)

        if not slices:
            logging.warning("Не удалось получить данные ни за одну дату/время.")
            return xr.Dataset()

        try:
            da_all = xr.concat(slices, dim="time")
            ds = _to_standard_dataset(
                da_all,
                source_name=DATASET_NAME,
                cds_source="legacy_grib_loop",
            )
        except Exception as e:
            logging.error(f"Ошибка при формировании xarray.Dataset: {e}")
            return xr.Dataset()

    # Обработка NaN значений
    if clean_nan and ds.data_vars:
        logging.info(f"Применение метода очистки NaN: {nan_method}")
        ds = remove_nan_values(ds, method=nan_method)

    return ds

def safe_filename(date_str: str) -> str:
    """
    Превращает дату-время в безопасное имя файла для Windows.
    """
    return date_str.replace(":", "-").replace("T", "_")


if __name__ == "__main__":
    # Пример использования
    example_geojson = {
      "type": "FeatureCollection",
      "features": [
        {
          "type": "Feature",
          "properties": {
            "time": [
              "2024-10-05T14:00:00",
              "2024-10-08T14:00:00",
              "P1DT12H"
            ],
            "is_rectangle": False
          },
          "geometry": {
            "coordinates": [
              [
                [131.085, 42.84],
                [131.085, 42.93],
                [131.208, 42.93],
                [131.208, 42.84],
                [131.085, 42.84]
              ]
            ],
            "type": "Polygon"
          }
        }
      ]
    }

    try:
        # Теперь можно выбрать метод очистки NaN
        ds_result = fetch_process_xarray(
            example_geojson,
            clean_nan=True,
            nan_method='interpolate'  # 'drop', 'interpolate', 'fill', 'zero'
        )

        if ds_result.data_vars and not ds_result.to_array().isnull().all().item():
            print(ds_result)
            output_file = "solar_radiation_data.nc"
            ds_result.to_netcdf(output_file)
            logging.info(f"Данные сохранены в {output_file}")

            # Проверка результата
            final_nan_count = ds_result.solar_radiation.isnull().sum().item()
            if final_nan_count == 0:
                logging.info("Все NaN значения успешно удалены!")
            else:
                logging.info(f"Осталось NaN значений: {final_nan_count}")

        else:
            logging.warning("Получены пустые данные, файл не сохранен")

    except Exception as e:
        logging.error(f"Ошибка при выполнении: {e}")

