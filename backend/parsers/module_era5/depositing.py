import zipfile
import shutil
import cdsapi
import numpy as np
import isodate
import pandas as pd
import xarray as xr
from datetime import datetime
import logging
import os
from shapely.geometry import shape, Point, Polygon
import cfgrib
import warnings
from math import cos, radians

# Игнорируем предупреждения от cfgrib
warnings.filterwarnings("ignore", category=RuntimeWarning)

# Настройка логирования
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

CDS_API_URL = "https://cds.climate.copernicus.eu/api"
CDS_API_KEY = os.getenv("CDS_API_KEY", "e0b64b64-0370-4962-afab-026433ab8dce")
DATASET_NAME = "reanalysis-era5-land"
MIN_AREA_SIZE = 0.1  # градусы (ERA5-Land ~0.1° сетка)


def safe_filename(date_str: str) -> str:
    """Превращает дату-время в безопасное имя файла для Windows."""
    return date_str.replace(":", "-").replace("T", "_")


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
        if abs(center_lat) >= 89.9:
            lon_adjustment = MIN_AREA_SIZE
        else:
            lon_adjustment = MIN_AREA_SIZE / cos(radians(center_lat))
        new_min_lon = center_lon - lon_adjustment / 2
        new_max_lon = center_lon + lon_adjustment / 2
    else:
        new_min_lon, new_max_lon = lon_range

    return (new_min_lat, new_max_lat), (new_min_lon, new_max_lon)


def parse_geojson(geojson: dict):
    """
    Парсит GeoJSON (FeatureCollection с одной Feature).
    Возвращает: lat_range, lon_range, list_of_iso_datetimes, is_rectangle, shapely_geometry
    Поддерживает:
      - properties.time = [...dates or datetimes...]  (list)
      - properties.time = [start, end, step_iso8601] (list of length 3)
    """
    try:
        feature = geojson["features"][0]
        geometry = shape(feature["geometry"])
        props = feature.get("properties", {})
    except Exception as e:
        raise ValueError(f"Ошибка в формате GeoJSON: {e}")

    # координаты области (в GRIB API area = [North, West, South, East])
    if isinstance(geometry, Point):
        lat_range = (geometry.y, geometry.y)
        lon_range = (geometry.x, geometry.x)
    elif isinstance(geometry, Polygon):
        b = geometry.bounds  # (minx, miny, maxx, maxy)
        lat_range = (b[1], b[3])  # (miny, maxy)
        lon_range = (b[0], b[2])  # (minx, maxx)
    else:
        raise ValueError("GeoJSON должен содержать Point или Polygon (Polygon ожидается чаще).")

    is_rectangle = props.get("is_rectangle", False)

    # корректировка минимального размера
    lat_range, lon_range = adjust_area_size(lat_range, lon_range)

    time_period = props.get("time", None)
    if time_period is None or not isinstance(time_period, list):
        raise ValueError("В GeoJSON 'properties.time' должен быть список дат или [start,end,step].")

    # формат [start, end, step]
    if len(time_period) == 3:
        try:
            start = pd.to_datetime(time_period[0])
            end = pd.to_datetime(time_period[1])
            step_iso = time_period[2]  # "P1DT12H"
            
            # Парсим ISO 8601 duration с помощью isodate
            step_duration = isodate.parse_duration(step_iso)
            step_timedelta = pd.Timedelta(step_duration)
            
            # Создаем диапазон дат с указанным шагом
            dates = pd.date_range(start=start, end=end, freq=step_timedelta)
            dates = [d.strftime("%Y-%m-%dT%H:%M:%S") for d in dates]
            
            logging.info(f"Создан диапазон дат: {len(dates)} точек от {dates[0]} до {dates[-1]} с шагом {step_iso}")
            
        except Exception as e:
            raise ValueError(f"Ошибка при обработке time=[start,end,step]: {e}")
    else:
        # список отдельных дат
        try:
            dates = [pd.to_datetime(d).strftime("%Y-%m-%dT%H:%M:%S") for d in time_period]
        except Exception:
            raise ValueError("Некорректный формат даты.")

    return lat_range, lon_range, dates, bool(is_rectangle), geometry

def fetch_process_xarray(geojson: dict) -> xr.Dataset:
    """Основная функция: возвращает xarray.Dataset с переменной 'lai' (time, lat, lon)."""

    lat_range, lon_range, date_list, is_rectangle, geometry = parse_geojson(geojson)
    logging.info(f"Используемая область: lat={lat_range}, lon={lon_range}, is_rectangle={is_rectangle}")

    # инициализация CDS клиента
    try:
        client = cdsapi.Client(url=CDS_API_URL, key=CDS_API_KEY)
    except Exception as e:
        raise RuntimeError(f"Ошибка при инициализации CDS API клиента: {e}")

    # helper: находит в ds_grib имя переменной LAI (hv/lv) среди возможных вариантов
    def find_lai_var_names(ds_grib):
        hv_candidates = ["leaf_area_index_high_vegetation", "lai_hv", "lai_high_vegetation"]
        lv_candidates = ["leaf_area_index_low_vegetation", "lai_lv", "lai_low_vegetation"]
        hv = next((v for v in hv_candidates if v in ds_grib.variables), None)
        lv = next((v for v in lv_candidates if v in ds_grib.variables), None)
        return hv, lv

    def process_grib_file_to_da(grib_path: str, dt_iso: str) -> xr.DataArray:
        """Читает GRIB и возвращает DataArray lai(time, latitude, longitude)."""
        try:
            logging.info(f"Открываю GRIB: {grib_path}")
            ds_grib = cfgrib.open_dataset(grib_path, decode_timedelta=False)

            hv_name, lv_name = find_lai_var_names(ds_grib)
            if hv_name is None or lv_name is None:
                # выводим доступные переменные для дебага
                logging.error(f"Доступные переменные в GRIB: {list(ds_grib.variables.keys())}")
                raise ValueError("Не найдены переменные LAI в GRIB (ищутся hv и lv).")

            # набор значений; ожидание: возможно (time, lat, lon) или (lat, lon)
            hv_vals = ds_grib[hv_name].values
            lv_vals = ds_grib[lv_name].values

            # если есть временная ось (3D), усредним по ней (поскольку пользователь просил дневной LAI)
            if hv_vals.ndim == 3:
                hv_vals = hv_vals.mean(axis=0)
            if lv_vals.ndim == 3:
                lv_vals = lv_vals.mean(axis=0)

            lai_np = (hv_vals + lv_vals) / 2.0

            lat_vals = ds_grib["latitude"].values
            lon_vals = ds_grib["longitude"].values

            lai_da = xr.DataArray(
                lai_np,
                dims=("latitude", "longitude"),
                coords={"latitude": lat_vals, "longitude": lon_vals},
                name="lai",
                attrs={"units": "m2/m2"},
            )

            t = pd.to_datetime(dt_iso)
            lai_da = lai_da.expand_dims(time=[t])

            # упорядочиваем координаты (широта часто в порядке убывания)
            if lai_da.latitude.ndim == 1 and lai_da.latitude[0] > lai_da.latitude[-1]:
                lai_da = lai_da.sortby("latitude")
            if lai_da.longitude.ndim == 1 and lai_da.longitude[0] > lai_da.longitude[-1]:
                lai_da = lai_da.sortby("longitude")

            lai_da = lai_da.transpose("time", "latitude", "longitude")
            return lai_da

        except Exception as e:
            logging.error(f"Ошибка в process_grib_file_to_da: {e}")
            raise

    def fetch_lai_for_datetime(dt_iso: str) -> xr.DataArray | None:
        """
        Запрашивает ZIP для конкретной даты/времени dt_iso (строка ISO с T),
        извлекает GRIB и возвращает DataArray (или None при ошибке).
        """
        # формируем части даты
        t = pd.to_datetime(dt_iso)
        year = t.strftime("%Y")
        month = t.strftime("%m")
        day = t.strftime("%d")
        time_for_request = t.strftime("%H:%M")  # CDS принимает формат HH:MM

        request = {
            "variable": [
                "leaf_area_index_high_vegetation",
                "leaf_area_index_low_vegetation",
            ],
            "product_type": "reanalysis",
            "year": year,
            "month": month,
            "day": day,
            "time": [time_for_request],
            "format": "zip",
            "area": [lat_range[1], lon_range[0], lat_range[0], lon_range[1]],  # North, West, South, East
        }

        safe_dt = safe_filename(dt_iso)
        target_zip = f"carbon_deposition_{safe_dt}.zip"
        extract_folder = f"temp_extracted_{safe_dt}"
        os.makedirs(extract_folder, exist_ok=True)

        try:
            logging.info(f"Скачивание данных за {dt_iso} (time={time_for_request}) ...")
            client.retrieve(DATASET_NAME, request, target_zip)

            with zipfile.ZipFile(target_zip, "r") as z:
                members = z.namelist()
                logging.info(f"Архив содержит: {members}")
                z.extractall(extract_folder)
                grib_file = next((f for f in members if f.lower().endswith((".grib", ".grb"))), None)
                if not grib_file:
                    raise FileNotFoundError("В архиве не найден GRIB-файл.")
            grib_path = os.path.join(extract_folder, grib_file)
            lai_da = process_grib_file_to_da(grib_path, dt_iso)

            # если требуется маска по полигону (не прямоугольник) — применяем её
            if not is_rectangle:
                logging.info("Применяю маску по полигону (is_rectangle=False).")
                # создаём сетку координат
                lat_vals = lai_da.latitude.values
                lon_vals = lai_da.longitude.values
                lon_grid, lat_grid = np.meshgrid(lon_vals, lat_vals)  # shape (lat, lon)

                # проверяем вхождение точек в полигон
                pts = [Point(lon, lat) for lon, lat in zip(lon_grid.ravel(), lat_grid.ravel())]
                contains = np.array([geometry.contains(p) for p in pts], dtype=bool).reshape(lon_grid.shape)

                mask_da = xr.DataArray(contains, coords={"latitude": lat_vals, "longitude": lon_vals}, dims=("latitude", "longitude"))
                lai_da = lai_da.where(mask_da)  # обнуляет (NaN) всё вне полигона

            return lai_da

        except Exception as e:
            logging.error(f"Ошибка за {dt_iso}: {e}")
            return None
        finally:
            # очистка
            for p in (target_zip, extract_folder):
                try:
                    if os.path.exists(p):
                        if os.path.isdir(p):
                            shutil.rmtree(p)
                        else:
                            os.remove(p)
                except Exception as ex:
                    logging.warning(f"Не удалось удалить {p}: {ex}")

    # --- собираем все DataArray по датам/временам ---
    lai_slices: list[xr.DataArray] = []
    for dt_iso in date_list:
        logging.info(f"Обрабатываю {dt_iso}")
        da = fetch_lai_for_datetime(dt_iso)
        if da is not None:
            lai_slices.append(da)
        else:
            logging.warning(f"Нет данных за {dt_iso}")

    if not lai_slices:
        logging.warning("Не удалось получить ни одного среза LAI.")
        return xr.Dataset()

    try:
        lai_all = xr.concat(lai_slices, dim="time")
        ds = xr.Dataset({"lai": lai_all})
        ds = ds.sortby("time").sortby("latitude").sortby("longitude")
        ds.attrs = {
            "source": "CDS API",
            "dataset": DATASET_NAME,
            "variable": ["leaf_area_index_high_vegetation", "leaf_area_index_low_vegetation"],
            "units": "m2/m2",
            "processing_date": datetime.now().isoformat(),
            "original_area": f"lat: {lat_range}, lon: {lon_range}, is_rectangle={is_rectangle}",
        }
        return ds
    except Exception as e:
        logging.error(f"Ошибка при формировании xarray.Dataset: {e}")
        return xr.Dataset()


if __name__ == "__main__":
    # ================== Пример использования ==================
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
            "is_rectangle": True
          },
          "geometry": {
            "coordinates": [[
              [131.085, 42.84],
              [131.085, 42.93],
              [131.208, 42.93],
              [131.208, 42.84],
              [131.085, 42.84]
            ]],
            "type": "Polygon"
          }
        }
      ]
    }
    try:
        ds_result = fetch_process_xarray(example_geojson)
        print(ds_result)

        output_file = "D:/Files/depositing.nc"
        # проверка записи
        out_dir = os.path.dirname(output_file)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir, exist_ok=True)

        ds_result.to_netcdf(output_file)
        logging.info(f"Данные сохранены в {output_file} (размер: {os.path.getsize(output_file)} байт)")

    except Exception as e:
        logging.error(f"Ошибка при выполнении: {e}")


