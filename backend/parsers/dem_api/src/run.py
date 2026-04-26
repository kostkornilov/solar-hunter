"""
Файл для запуска парсера.
"""

import os
import sys
import xarray as xr
import rioxarray as rxr
from rasterio.enums import Resampling

try:
    from .data_download import initialize_gee, download_dem
    from .calculations import calculate_terrain_attributes
except ImportError:  # pragma: no cover
    # Fallback for running as a script where package context is unavailable.
    current_dir = os.path.dirname(os.path.abspath(__file__))
    if current_dir not in sys.path:
        sys.path.append(current_dir)
    from data_download import initialize_gee, download_dem
    from calculations import calculate_terrain_attributes

def _merge_embeddings(terrain_dataset: xr.Dataset, embeddings_path: str) -> xr.Dataset:
    """Align embeddings raster to terrain grid and merge into a single dataset."""
    embeddings_da = rxr.open_rasterio(embeddings_path)
    if "band" not in embeddings_da.dims:
        embeddings_da = embeddings_da.expand_dims(dim={"band": [1]})

    terrain_for_match = terrain_dataset.rio.set_spatial_dims(
        x_dim="longitude",
        y_dim="latitude",
        inplace=False,
    )
    aligned = embeddings_da.rio.reproject_match(
        terrain_for_match,
        resampling=Resampling.bilinear,
    )

    emb_vars = {}
    band_size = int(aligned.sizes.get("band", 0))
    latitudes = terrain_dataset.coords["latitude"].values
    longitudes = terrain_dataset.coords["longitude"].values
    for idx in range(band_size):
        band_name = f"emb_A{idx:02d}"
        emb_vars[band_name] = (
            ("latitude", "longitude"),
            aligned.isel(band=idx).values,
        )

    embeddings_ds = xr.Dataset(
        data_vars=emb_vars,
        coords={"latitude": latitudes, "longitude": longitudes},
    )
    return xr.merge([terrain_dataset, embeddings_ds], compat="override")


def run(
    geojson_path,
    download_embeddings=False,
    embeddings_year=2018,
    dem_file_name="copernicus_table.tif",
    output_directory="example_output",
):
    """
    Основная функция для запуска всего процесса.
    
    Принимает путь к GeoJSON файлу и директорию для сохранения выходных данных.
    """
    # Инициализируем GEE
    initialize_gee(project=os.getenv("GEE_PROJECT", "projectomela"))

    # Определим координаты и названия файлов
    directory = output_directory

    # Загрузим DEM из GEE (DEM сохранится в .tiff файл в директории directory)
    download_result = download_dem(
        geojson_path,
        dem_file_name,
        directory,
        download_embeddings=download_embeddings,
        embeddings_year=embeddings_year,
    )
    if download_result is None:
        raise RuntimeError(
            f"download_dem failed for geojson_path={geojson_path}. "
            "See previous [ERROR] log line for details."
        )
    # Путь к DEM файлу
    dem_path = os.path.join(directory, dem_file_name)
    embeddings_path = None
    if isinstance(download_result, dict):
        dem_path = download_result.get("dem_path", dem_path)
        embeddings_path = download_result.get("embeddings_path")
    attributes = [
    'slope', 'hillshade', 'aspect', 'curvature', 'planform_curvature', 'profile_curvature',
    'max_curvature', 'topographic_position_index', 'terrain_ruggedness_index',
    'roughness', 'rugosity']

    # Получаем объект xarray.Dataset
    terrain_dataset = calculate_terrain_attributes(dem_path, attributes)

    if embeddings_path:
        terrain_dataset = _merge_embeddings(terrain_dataset, embeddings_path)

    return terrain_dataset

if __name__ == "__main__":
    # Путь к GeoJSON файлу (устойчиво к запуску из любого cwd)
    json_path = os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "examples", "sample_jsons", "1km.geojson")
    )
    # Запускаем основную функцию
    terrain_dataset_example = run(json_path, download_embeddings=True)
    print(terrain_dataset_example)
