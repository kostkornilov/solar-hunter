"""
Дополнительные функции для работы с данными
"""

import json
import numpy as np
import rioxarray
import xarray as xr

def dataset_for_netcdf(ds: xr.Dataset, drop_spatial_ref: bool = True) -> xr.Dataset:
    """
    Подготовка Dataset к ds.to_netcdf(...): NetCDF не поддерживает вложенные dict в .attrs.
    При необходимости убирает spatial_ref (из rioxarray), из‑за которого иногда падает запись.
    """
    out = ds.copy()
    if "bounds" in out.attrs and isinstance(out.attrs["bounds"], dict):
        b = dict(out.attrs.pop("bounds"))
        for k, v in b.items():
            if isinstance(v, (int, float, np.floating, np.integer)):
                out.attrs[f"bounds_{k}"] = float(v)
            else:
                out.attrs[f"bounds_{k}"] = str(v)
    for k, v in list(out.attrs.items()):
        if isinstance(v, dict):
            out.attrs.pop(k)
            for sk, sv in v.items():
                key = f"{k}_{sk}"
                if isinstance(sv, (int, float, np.floating, np.integer)):
                    out.attrs[key] = float(sv)
                else:
                    out.attrs[key] = str(sv)
    if drop_spatial_ref:
        names = [
            n
            for n in ("spatial_ref",)
            if n in out.coords or n in out.data_vars
        ]
        if names:
            out = out.drop_vars(names)
    # netCDF4 не поддерживает float16 (см. combine_xarrays → astype float16).
    for name in list(out.data_vars):
        da = out[name]
        if da.dtype == np.float16:
            out[name] = da.astype(np.float32)
    for name in list(out.coords):
        c = out.coords[name]
        if hasattr(c, "dtype") and c.dtype == np.float16:
            out = out.assign_coords({name: c.astype(np.float32)})
    return out


def _polygon_outer_ring_as_array(polygon_coordinates) -> np.ndarray:
    """Одно кольцо полигона: список [lon, lat] или [lat, lon] → (N, 2)."""
    if not polygon_coordinates:
        raise ValueError("Пустой coordinates у Polygon.")
    first = polygon_coordinates[0]
    if isinstance(first, (int, float)):
        raise ValueError("Ожидался Polygon coordinates = [ ring ], ring = [[x,y], ...].")
    if isinstance(first[0], (list, tuple, np.ndarray)):
        ring = first
    else:
        ring = polygon_coordinates
    return np.asarray(ring, dtype=np.float64)


def compare_geojson_polygon_to_source(
    geojson_path: str,
    source_polygon_coords,
    atol: float = 1e-7,
    rtol: float = 0.0,
) -> tuple[bool, str]:
    """
    Проверяет, что внешнее кольцо в geo.json совпадает с исходным ``data``.

    ``source_polygon_coords`` — как в ноутбуке: ``[[[x,y], ...]]`` (shape (1, N, 2))
    или ``[[x,y], ...]`` (одно кольцо). Порядок пар может быть [lon,lat] или [lat,lon]:
    если прямое сравнение не проходит, проверяется вариант с переставленными столбцами.
    """
    with open(geojson_path, encoding="utf-8") as f:
        gj = json.load(f)
    geom = gj["features"][0]["geometry"]
    if geom.get("type") != "Polygon":
        return False, f"ожидался Polygon, получено {geom.get('type')!r}"
    file_ring = _polygon_outer_ring_as_array(geom["coordinates"])

    src = np.asarray(source_polygon_coords, dtype=np.float64)
    if src.ndim == 3:
        if src.shape[0] != 1:
            return False, f"ожидалось одно кольцо (первая размерность 1), shape={src.shape}"
        src = src[0]
    elif src.ndim != 2:
        return False, f"непонятная форма source: {src.shape}"

    if file_ring.shape != src.shape:
        return (
            False,
            f"размеры не совпадают: в файле {file_ring.shape}, в источнике {src.shape}",
        )

    if np.allclose(file_ring, src, rtol=rtol, atol=atol):
        return True, "координаты совпадают с записанным полигоном (то же порядок пар)."

    swapped = src[:, ::-1]
    if np.allclose(file_ring, swapped, rtol=rtol, atol=atol):
        return (
            True,
            "совпадают после смены порядка в паре (файл [lon,lat] vs data [lat,lon] или наоборот).",
        )

    d = float(np.nanmax(np.abs(file_ring - src)))
    d_sw = float(np.nanmax(np.abs(file_ring - swapped)))
    return (
        False,
        f"нет совпадения: max|файл−источник|={d}, max|файл−источник с swap столбцов|={d_sw}",
    )


def create_bounding_box(lat, lon, buffer_degrees):
    """Рассчет координат области."""
    lon1 = lon - buffer_degrees
    lon2 = lon + buffer_degrees
    lat1 = lat - buffer_degrees
    lat2 = lat + buffer_degrees
    return lon1, lat1, lon2, lat2

def tiff_to_xarray(tiff_path):
    """Преобразовать TIFF файл в xarray DataArray."""
    data = rioxarray.open_rasterio(tiff_path, masked=True)
    print(f"Loaded {tiff_path}:")
    print(f"Shape: {data.shape}")
    print(f"CRS: {data.rio.crs}")
    print(f"Nodata: {data.rio.nodata}")
    print(f"Data: {data}")
    return data

def combine_xarrays(xarrays, attributes):
    """Объединить несколько xarray DataArray в один Dataset."""
    combined = xr.concat(xarrays, dim="band")
    combined = combined.assign_coords(band=attributes)
    combined = combined.rename({"y": "latitude", "x": "longitude"})
    combined_dataset = combined.to_dataset(dim="band")
    # высота (DEM) может принимать значения от -10 до 6500 метров (разрешение - 30 м)
    # уклон (slope) может принимать значения от 0 до 90 градусов
    # теневой рельеф (hillshade) может принимать значения от 0 до 255
    # Азимут (aspect) может принимать значения от 0 до 360 градусов
    # макс. отрицательное значение кривизны(curvature)
    # (центр = -10 метров, окружающие пиксели = 6500) = -100*(4*6500-4*(-10))/900 ~= -2893.3
    # макс. положительное значение кривизны(curvature)
    # (центр = 6500 метров, окружающие пиксели = -10) = -100*(4*(-10)-4*6500)/900 ~= 2893.3
    # макс. значение плановой кривизны (Planform curvature)
    # и профильной кривизны (Profile curvature) точно не превывают макс. значения кривизны,
    # т.к. описывают кривизну в определённых направлениях
    # поэтому будем считать, то Planform curvature и
    # Profile curvature тоже могут принимать значения примерно от -3000 до 3000
    # макс. значение максимальной кривизны (Maximum curvature) = 2893.3
    # макс. значение индекса топографического положения (Topographic position index)
    # (если центр = 6500 метров, окружающие пиксели = -10, окно = 3*3 пикселя)
    # = 6500 - -10*8/8 = 6510, min =  -10 - 6500*8/8 = -6510
    # макc. значение индекса пересеченной местности (Terrain ruggedness index)
    # (если центр = 6500 метров, окружающие пиксели = -10, окно = 3*3 пикселя)
    # = sqrt(8*6510**2) ~= 18413.06, min = 0
    # макс. значение шероховатости (Roughness) = 6500 - -10 = 6510, min = 0
    # макс. значение неровность поверхности (Rugosity)
    # при заданных параметрах (rougly) = 1047.65, min = 1
    # Исходя из всех этих рассуждений, делаем вывод,
    # что для хранения всех данных достаточно np.float16
    # (все значения укладываются в диапозон диапазон значений: от -65,504 до 65,504.)
    # Точность 3 знака после запятой считаем достаточной
    combined_dataset = combined_dataset.astype("float16")
    return combined_dataset
