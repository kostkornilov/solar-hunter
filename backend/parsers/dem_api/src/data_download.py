"""
Модуль для загрузки данных DEM из Google Earth Engine (GEE) в формате GeoTIFF.
"""

import os
import ee
import geemap
import json
import math
import rioxarray
import numpy as np
import rasterio
from rasterio.merge import merge as rio_merge
from rasterio.transform import from_origin

# Лимит ширины/высоты в пикселях для ee.Image.getDownloadURL (сообщение GEE: ≤ 32768).
# Небольшой запас на округление со стороны сервера.
GEE_GET_DOWNLOAD_MAX_PIXELS = 32600

# Лимит размера ответа getDownloadURL (типичное сообщение: «must be less than 50331648 bytes»).
GEE_GET_DOWNLOAD_MAX_BYTES = 50331648
# Запас: фактический размер может быть больше nx*ny*4 из‑за типов/метаданных ZIP.
GEE_DOWNLOAD_SIZE_SAFETY = 0.88


def _ring_lon_lat_from_geojson(coords):
    """
    Возвращает (lons, lats) для кольца полигона.
    GeoJSON — [lon, lat], но часто приходит [lat, lon]; определяем по диапазону.
    """
    xs = [float(p[0]) for p in coords]
    ys = [float(p[1]) for p in coords]
    if max(abs(y) for y in ys) > 90.0:
        # Вторая компонента не может быть широтой при валидном WGS84 → кольцо в порядке [lat, lon].
        return ys, xs
    return xs, ys


def _estimate_export_pixels_wgs84(min_lon, min_lat, max_lon, max_lat, scale_m):
    """Оценка размера сетки экспорта в EPSG:4326 при заданном scale (м/пиксель)."""
    if max_lon <= min_lon or max_lat <= min_lat:
        raise ValueError("Некорректный прямоугольник: min/max по долготе или широте.")
    mid_lat = (min_lat + max_lat) / 2.0
    m_per_deg_lat = 111_320.0
    m_per_deg_lon = 111_320.0 * math.cos(math.radians(mid_lat))
    width_m = (max_lon - min_lon) * m_per_deg_lon
    height_m = (max_lat - min_lat) * m_per_deg_lat
    nx = max(1, int(math.ceil(width_m / scale_m)))
    ny = max(1, int(math.ceil(height_m / scale_m)))
    return nx, ny


def _gee_max_pixels_one_request(num_bands):
    """
    Макс. число пикселей (nx*ny) на один getDownloadURL при оценке размера ответа.
    Берём float32 на канал (4 байта) и коэффициент ~2 под накладные расходы/ZIP.
    """
    nb = max(1, int(num_bands))
    bytes_per_raster_pixel = 4 * nb * 2
    return max(
        256,
        int(GEE_GET_DOWNLOAD_MAX_BYTES * GEE_DOWNLOAD_SIZE_SAFETY / bytes_per_raster_pixel),
    )


def _tiles_for_gee_download(min_lon, min_lat, max_lon, max_lat, scale_m, max_dim, max_tile_pixels):
    """
    Делит прямоугольник на под-прямоугольники: у каждого тайла
    сторона ≤ max_dim (пикселей) и площадь nx*ny ≤ max_tile_pixels.
    """
    nx_full, ny_full = _estimate_export_pixels_wgs84(
        min_lon, min_lat, max_lon, max_lat, scale_m
    )
    tx = max(1, math.ceil(nx_full / max_dim))
    ty = max(1, math.ceil(ny_full / max_dim))
    for _ in range(512):
        rects = []
        for i in range(tx):
            for j in range(ty):
                lo1 = min_lon + (max_lon - min_lon) * i / tx
                lo2 = min_lon + (max_lon - min_lon) * (i + 1) / tx
                la1 = min_lat + (max_lat - min_lat) * j / ty
                la2 = min_lat + (max_lat - min_lat) * (j + 1) / ty
                rects.append((lo1, la1, lo2, la2))
        max_nx = 0
        max_ny = 0
        max_area = 0
        for bx in rects:
            nnx, nny = _estimate_export_pixels_wgs84(bx[0], bx[1], bx[2], bx[3], scale_m)
            max_nx = max(max_nx, nnx)
            max_ny = max(max_ny, nny)
            max_area = max(max_area, nnx * nny)
        if (
            max_nx <= max_dim
            and max_ny <= max_dim
            and max_area <= max_tile_pixels
        ):
            return rects, (tx, ty)
        if max_nx > max_dim:
            tx += 1
        elif max_ny > max_dim:
            ty += 1
        elif max_area > max_tile_pixels:
            if max_nx >= max_ny:
                tx += 1
            else:
                ty += 1
    raise RuntimeError(
        "Не удалось разбить область на тайлы в пределах лимита GEE getDownloadURL."
    )


def _merge_geotiffs(sources, destination):
    """Склеивает смежные GeoTIFF (одинаковый CRS) в один файл."""
    srcs = [rasterio.open(p) for p in sources]
    try:
        nodata = srcs[0].nodata
        mosaic, out_transform = rio_merge(srcs, nodata=nodata)
        out_meta = srcs[0].meta.copy()
        out_meta.update(
            {
                "driver": "GTiff",
                "height": mosaic.shape[1],
                "width": mosaic.shape[2],
                "transform": out_transform,
                "count": mosaic.shape[0],
                "dtype": mosaic.dtype,
            }
        )
        if nodata is not None:
            out_meta["nodata"] = nodata
        if "compress" not in out_meta:
            out_meta["compress"] = "deflate"
        with rasterio.open(destination, "w", **out_meta) as dst:
            dst.write(mosaic)
    finally:
        for s in srcs:
            s.close()


def _ee_export_image_to_tif(ee_image, out_tif_abs, scale, region_geom, timeout=600):
    """
    Обёртка над geemap.ee_export_image с проверкой результата.
    geemap при ошибке GEE часто только печатает сообщение и не бросает исключение.
    """
    out_tif_abs = os.path.abspath(out_tif_abs)
    zip_abs = out_tif_abs.replace(".tif", ".zip")
    for p in (out_tif_abs, zip_abs):
        if os.path.exists(p):
            os.remove(p)
    geemap.ee_export_image(
        ee_image,
        filename=out_tif_abs,
        scale=scale,
        region=region_geom,
        file_per_band=False,
        timeout=timeout,
    )
    if not os.path.isfile(out_tif_abs):
        raise RuntimeError(
            f"Экспорт GEE не создал файл (проверьте сообщение выше): {out_tif_abs}"
        )


def _export_raster_tiled(
    ee_image,
    out_path,
    scale,
    min_lon,
    min_lat,
    max_lon,
    max_lat,
    max_pixels=GEE_GET_DOWNLOAD_MAX_PIXELS,
    export_timeout=600,
    num_bands=None,
):
    """
    Скачивает ee.Image на всём прямоугольнике одним или несколькими getDownloadURL,
    при необходимости склеивает тайлы через rasterio.merge.
    """
    if num_bands is None:
        num_bands = int(ee.Image(ee_image).bandNames().size().getInfo())
    max_tile_pixels = _gee_max_pixels_one_request(num_bands)
    nx, ny = _estimate_export_pixels_wgs84(min_lon, min_lat, max_lon, max_lat, scale)
    region_full = ee.Geometry.Rectangle([min_lon, min_lat, max_lon, max_lat])
    area_ok = nx * ny <= max_tile_pixels
    if nx <= max_pixels and ny <= max_pixels and area_ok:
        print(
            f"Экспорт одним запросом: ~{nx}×{ny} пикселей, каналов {num_bands} "
            f"(сторона ≤ {max_pixels}, nx×ny ≤ {max_tile_pixels})."
        )
        _ee_export_image_to_tif(
            ee_image, out_path, scale, region_full, timeout=export_timeout
        )
        return

    if not area_ok:
        print(
            f"Лимит размера ответа getDownloadURL (~{GEE_GET_DOWNLOAD_MAX_BYTES} байт): "
            f"оценка nx×ny={nx * ny} при {num_bands} канале(ах) превышает ~{max_tile_pixels} "
            "пикселей на запрос — нужно разбиение."
        )

    rects, (tx, ty) = _tiles_for_gee_download(
        min_lon, min_lat, max_lon, max_lat, scale, max_pixels, max_tile_pixels
    )
    n_tiles = len(rects)
    print(
        f"Область ~{nx}×{ny} пикселей — разбиение на {tx}×{ty} = {n_tiles} тайлов "
        f"(сторона ≤ {max_pixels}, nx×ny ≤ {max_tile_pixels} на тайл)."
    )
    out_dir = os.path.dirname(os.path.abspath(out_path))
    base = os.path.splitext(os.path.basename(out_path))[0]
    tile_paths = []
    try:
        for k, (lo1, la1, lo2, la2) in enumerate(rects):
            sub = ee.Geometry.Rectangle([lo1, la1, lo2, la2])
            tile_name = f"__{base}_tile_{k:04d}.tif"
            tile_abs = os.path.join(out_dir, tile_name)
            print(f"  Тайл {k + 1}/{n_tiles}: [{lo1:.6f}, {la1:.6f}, {lo2:.6f}, {la2:.6f}]")
            _ee_export_image_to_tif(
                ee_image, tile_abs, scale, sub, timeout=export_timeout
            )
            tile_paths.append(tile_abs)
        if os.path.exists(out_path):
            os.remove(out_path)
        _merge_geotiffs(tile_paths, out_path)
    finally:
        for p in tile_paths:
            if os.path.isfile(p):
                os.remove(p)
            z = p.replace(".tif", ".zip")
            if os.path.isfile(z):
                os.remove(z)


def initialize_gee(project: str):
    """Инициализация GEE только через Service Account key file (без fallback)."""
    service_account_email = os.getenv("GEE_SERVICE_ACCOUNT_EMAIL", "").strip()
    service_account_key_path = os.getenv("GEE_SERVICE_ACCOUNT_KEY_PATH", "").strip()

    if not service_account_email or not service_account_key_path:
        raise RuntimeError(
            "Требуется Service Account авторизация: задайте "
            "GEE_SERVICE_ACCOUNT_EMAIL и GEE_SERVICE_ACCOUNT_KEY_PATH."
        )

    key_path = os.path.abspath(
        os.path.expanduser(os.path.expandvars(service_account_key_path))
    )
    if not os.path.isfile(key_path):
        raise RuntimeError(f"GEE key file не найден: {key_path}")

    try:
        credentials = ee.ServiceAccountCredentials(service_account_email, key_path)
        ee.Initialize(credentials=credentials, project=project)
        print("Google Earth Engine успешно инициализирован (Service Account key file).")
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"Ошибка инициализации GEE через Service Account: {e}") from e


def _point_lon_lat_from_geojson(lon_or_lat, lat_or_lon):
    """Point: GeoJSON [lon, lat]; если вторая компонента |y|>90 — считаем [lat, lon]."""
    x, y = float(lon_or_lat), float(lat_or_lon)
    if abs(y) > 90.0:
        return y, x
    return x, y


def download_dem(
    geo_json_path,
    filename,
    directory='example_output',
    buffer_pixels=45,
    dem_source='COPERNICUS',
    download_embeddings=False,
    embeddings_year=2018,
    embeddings_filename=None,
    gee_max_pixels_per_side=GEE_GET_DOWNLOAD_MAX_PIXELS,
    gee_export_timeout=600,
):
    """
    Загрузка DEM (digital elevation model) из GEE и возврат как xarray DataArray.
    
    Args:
        geo_json_path: путь к GeoJSON файлу
        filename: имя выходного файла
        directory: директория для сохранения
        buffer_pixels: количество дополнительных пикселей вокруг области (по умолчанию 15)
        dem_source: источник DEM ('COPERNICUS' или 'SRTM')
        download_embeddings: выгружать ли Google Satellite Embeddings отдельно
        embeddings_year: год annual embeddings (например, 2018)
        embeddings_filename: имя выходного файла embeddings (если None, auto)
        gee_max_pixels_per_side: макс. ширина/высота одного getDownloadURL (≤32768, по умолчанию с запасом)
        gee_export_timeout: таймаут HTTP при скачивании каждого тайла (сек)
    
    Принимает путь к GeoJSON файлу. Из файла извлекаются координаты из первой Feature.
    Если тип геометрии Point, скачивается значение только для точки.
    Если тип геометрии Polygon, загружается расширенная область с буфером.
    """
    try:
        # Чтение GeoJSON файла
        with open(geo_json_path, "r", encoding="utf-8") as f:
            geojson_data = json.load(f)

        # Создание папки, если её нет
        if not os.path.exists(directory):
            os.makedirs(directory)

        feature = geojson_data["features"][0]
        geometry = feature["geometry"]
        geom_type = geometry["type"]
        time = feature["properties"].get("time", "unknown")
        if time is not None:
            print(
                f"В GeoJSON файле указано время: {time}\n"
                "Время не учитывается при загрузке DEM."
            )
        
        # Выбор источника DEM
        if dem_source.upper() == 'COPERNICUS':
            # Copernicus DEM GLO-30 (30m resolution, более точный)
            dem_collection = ee.ImageCollection("COPERNICUS/DEM/GLO30")
            dem = dem_collection.select('DEM').mosaic()  # Мозаика из всех тайлов
            scale = 30  # 30 метров
            band_name = 'DEM'
            print("Используется Copernicus DEM GLO-30 (30m)")
        else:
            # SRTM (по умолчанию)
            dem = ee.Image("USGS/SRTMGL1_003")
            scale = 30
            band_name = 'elevation'
            print("Используется SRTM GLO-30 (30m)")
        
        embeddings_output_path = None

        # Для точки
        if geom_type == "Point":
            lon, lat = _point_lon_lat_from_geojson(*geometry["coordinates"])
            point = ee.Geometry.Point(lon, lat)
            # Получение значения DEM для точки
            sample = dem.sample(region=point, scale=scale).first()

            if sample is None:
                raise ValueError("Не удалось получить значение DEM для точки.")

            dem_value = sample.get(band_name).getInfo()
            print(f"DEM value at point ({lon}, {lat}): {dem_value} m")
            
            # Переводим разрешение (30 м) в градусы (30/111320)
            resolution = scale / 111320
            # Создаем transform так, чтобы центр пикселя был в (lon, lat)
            transform = from_origin(lon - resolution/2, lat + resolution/2, resolution, resolution)
            output_path = os.path.join(directory, filename)

            if os.path.exists(output_path):
                os.remove(output_path)

            with rasterio.open(
                output_path, 'w',
                driver='GTiff',
                height=1, width=1,
                count=1,
                dtype=rasterio.float32,
                crs='EPSG:4326',
                transform=transform,
            ) as dst:
                dst.write(np.array([[dem_value]], dtype=np.float32), 1)
            print(f"Point DEM saved as TIFF at: {output_path}")
            
            if download_embeddings:
                raise ValueError(
                    "download_embeddings=True поддерживается только для Polygon geometry."
                )

            # Сохраняем исходные границы (без буфера)
            original_bounds = {"lon1": lon, "lat1": lat, "lon2": lon, "lat2": lat}
        
        elif geom_type == "Polygon":
            # Берем координаты внешнего кольца (учёт порядка [lat, lon] при ошибочной записи)
            coords = geometry["coordinates"][0]
            lons, lats = _ring_lon_lat_from_geojson(coords)
            lon1, lon2 = min(lons), max(lons)
            lat1, lat2 = min(lats), max(lats)
            
            # Сохраняем исходные границы
            original_bounds = {"lon1": lon1, "lat1": lat1, "lon2": lon2, "lat2": lat2}
            print(f"Исходные границы области: {original_bounds}")
            
            # РАСШИРЯЕМ область на buffer_pixels * 30м (в градусах)
            buffer_degrees = buffer_pixels * scale / 111320  # ~0.00027 градусов на пиксель
            min_lon = lon1 - buffer_degrees
            min_lat = lat1 - buffer_degrees
            max_lon = lon2 + buffer_degrees
            max_lat = lat2 + buffer_degrees
            
            print(
                f"Расширенная область (буфер {buffer_pixels} пикселей): "
                f"[{min_lon:.6f}, {min_lat:.6f}, {max_lon:.6f}, {max_lat:.6f}]"
            )

            est_nx, est_ny = _estimate_export_pixels_wgs84(
                min_lon, min_lat, max_lon, max_lat, scale
            )
            print(
                f"Оценка размера сетки экспорта (~{scale} м/пикс.): {est_nx}×{est_ny} пикселей "
                f"(лимит GEE на сторону: {gee_max_pixels_per_side})"
            )
            
            # Определение РАСШИРЕННОГО региона
            region = ee.Geometry.Rectangle([min_lon, min_lat, max_lon, max_lat])
            
            # Обрезка DEM по расширенному региону
            dem_clipped = dem.clip(region)
            
            output_path = os.path.join(directory, filename)
            if os.path.exists(output_path):
                os.remove(output_path)
            _export_raster_tiled(
                dem_clipped,
                output_path,
                scale,
                min_lon,
                min_lat,
                max_lon,
                max_lat,
                max_pixels=min(gee_max_pixels_per_side, 32768),
                export_timeout=gee_export_timeout,
            )
            print(f"[OK] DEM загружен: {output_path}")

            if download_embeddings:
                year_start = f"{int(embeddings_year)}-01-01"
                year_end = f"{int(embeddings_year) + 1}-01-01"
                dem_proj = dem.projection()
                emb_collection = (
                    ee.ImageCollection("GOOGLE/SATELLITE_EMBEDDING/V1/ANNUAL")
                    .filterDate(year_start, year_end)
                    .filterBounds(region)
                )
                emb_count = emb_collection.size().getInfo()
                if emb_count == 0:
                    raise ValueError(
                        f"Satellite Embeddings не найдены за {embeddings_year} для заданного региона."
                    )

                emb_first = ee.Image(emb_collection.first())
                emb_image = emb_collection.mosaic().setDefaultProjection(
                    emb_first.projection()
                )
                # Apply interpolation in source grid, then block aggregation,
                # then project to DEM grid.
                aligned_emb = emb_image.resample("bilinear").reduceResolution(
                    reducer=ee.Reducer.mean(),
                    maxPixels=1024,
                ).reproject(crs=dem_proj, scale=scale)

                if embeddings_filename is None:
                    base_name = os.path.splitext(filename)[0]
                    emb_name = f"{base_name}_embeddings_{embeddings_year}.tif"
                else:
                    emb_name = embeddings_filename

                embeddings_output_path = os.path.join(directory, os.path.basename(emb_name))
                if os.path.exists(embeddings_output_path):
                    os.remove(embeddings_output_path)
                aligned_clipped = aligned_emb.clip(region)
                _export_raster_tiled(
                    aligned_clipped,
                    embeddings_output_path,
                    scale,
                    min_lon,
                    min_lat,
                    max_lon,
                    max_lat,
                    max_pixels=min(gee_max_pixels_per_side, 32768),
                    export_timeout=gee_export_timeout,
                )
                print(f"[OK] Embeddings загружены: {embeddings_output_path}")
        else:
            raise ValueError(f"Unsupported geometry type: {geom_type}")
        
        # Загрузка DEM как xarray DataArray
        dem_xarray = rioxarray.open_rasterio(output_path)
        
        # Сохраняем исходные границы как атрибут в TIFF через теги
        with rasterio.open(output_path, 'r+') as dst:
            dst.update_tags(
                original_lon1=original_bounds['lon1'],
                original_lat1=original_bounds['lat1'],
                original_lon2=original_bounds['lon2'],
                original_lat2=original_bounds['lat2'],
                dem_source=dem_source
            )
        
        # Перечитываем с обновлёнными тегами
        dem_xarray = rioxarray.open_rasterio(output_path)
        dem_xarray.attrs['original_bounds'] = original_bounds
        dem_xarray.attrs['dem_source'] = dem_source
        
        print(f"[OK] Исходные границы сохранены: {original_bounds}")
        
        if download_embeddings:
            return {
                "dem_xarray": dem_xarray,
                "dem_path": output_path,
                "embeddings_path": embeddings_output_path,
            }

        return dem_xarray
    except Exception as e:
        print(f"[ERROR] Error downloading DEM: {e}")
        return None