"""
Модуль calculations.py

Здесь реализуются функции для расчета топографических атрибутов на основе DEM.
"""
import numpy as np
from pyproj import CRS, Transformer
from pyproj.aoi import AreaOfInterest
from pyproj.database import query_utm_crs_info
import xdem
import os

try:
    from .utils import combine_xarrays
except ImportError:  # pragma: no cover
    from utils import combine_xarrays
import xarray as xr
from rasterio.transform import from_bounds
import rioxarray as rxr
import rasterio

def get_utm_crs(lat, lon):
    """Получить UTM CRS на основе координат."""
    utm_crs_list = query_utm_crs_info(
        datum_name="WGS 84",
        area_of_interest=AreaOfInterest(
            west_lon_degree=lon,
            south_lat_degree=lat,
            east_lon_degree=lon,
            north_lat_degree=lat,
        ),
    )
    utm_crs = CRS.from_epsg(utm_crs_list[0].code)
    return utm_crs

def crop_to_original_bounds(dataset, original_bounds_wgs84):
    """
    Обрезает Dataset до исходных границ (удаляет буферную зону).
    
    Args:
        dataset: xarray.Dataset с координатами latitude, longitude (в WGS84)
        original_bounds_wgs84: dict с ключами lon1, lat1, lon2, lat2
    
    Returns:
        Обрезанный Dataset
    """
    lon1 = original_bounds_wgs84['lon1']
    lat1 = original_bounds_wgs84['lat1']
    lon2 = original_bounds_wgs84['lon2']
    lat2 = original_bounds_wgs84['lat2']
    
    # Находим индексы ближайших точек
    # longitude
    lon_coords = dataset.coords['longitude'].values
    lon_mask = (lon_coords >= lon1) & (lon_coords <= lon2)
    
    # latitude
    lat_coords = dataset.coords['latitude'].values
    lat_mask = (lat_coords >= lat1) & (lat_coords <= lat2)
    
    # Применяем обрезку
    cropped = dataset.isel(
        longitude=lon_mask,
        latitude=lat_mask
    )
    return cropped

def calculate_terrain_attributes(dem_path, attributes, return_wgs84=True, crop_to_bounds=True,**kwargs):
    """Рассчитать несколько топографических атрибутов с использованием xdem"""
    # Создаем копию attributes, чтобы не изменять оригинальный список
    attributes = attributes.copy()
    
    # Читаем исходные границы ИЗ TIFF ФАЙЛА
    original_bounds = None
    try:
        with rasterio.open(dem_path) as src:
            tags = src.tags()
            if all(k in tags for k in ['original_lon1', 'original_lat1', 'original_lon2', 'original_lat2']):
                original_bounds = {
                    'lon1': float(tags['original_lon1']),
                    'lat1': float(tags['original_lat1']),
                    'lon2': float(tags['original_lon2']),
                    'lat2': float(tags['original_lat2'])
                }
            else:
                print(f"Теги границ НЕ найдены в TIFF.")
    except Exception as e:
        print(f"Не удалось загрузить исходные границы: {e}")
    
    # Создаем DEM объект
    dem = xdem.DEM(dem_path, vcrs="WGS84")
    print('Shape of DEM', dem.shape)
    print('Coordinate system before reprojection', dem.vcrs)
    
    # Если в dem одна точка, делаем репроекцию по координатам этой точки
    if dem.shape == (1, 1):
        print("ПРЕДУПРЕЖДЕНИЕ: ЦМР содержит только одну точку.")
        print("Невозможно рассчитать показатели рельефа (требуются соседние пиксели).")
        print("Возвращается только высота (elevation).\n")
        
        # Получаем координаты точки (уже в WGS84)
        lon = float(dem.bounds.left)
        lat = float(dem.bounds.bottom)
        elevation = float(dem.data[0, 0])
        
        # Создаём простой Dataset
        result = xr.Dataset(
            data_vars={
                'elevation': (['latitude', 'longitude'], [[elevation]])
            },
            coords={
                'longitude': [lon],
                'latitude': [lat]
            }
        )
        
        # Добавляем метаданные
        result.coords['longitude'].attrs = {
            'long_name': 'Longitude',
            'units': 'degrees_east',
            'standard_name': 'longitude',
            'axis': 'X'
        }
        result.coords['latitude'].attrs = {
            'long_name': 'Latitude',
            'units': 'degrees_north',
            'standard_name': 'latitude',
            'axis': 'Y'
        }
        
        result.attrs["resolution_m"] = None
        result.attrs["coordinate_system"] = "EPSG:4326 (WGS84)"
        result.attrs["warning"] = "Single point - terrain attributes cannot be calculated"
        result.attrs["bounds_lon_min"] = float(lon)
        result.attrs["bounds_lon_max"] = float(lon)
        result.attrs["bounds_lat_min"] = float(lat)
        result.attrs["bounds_lat_max"] = float(lat)
        
        print(f"Координаты точки: lat={lat:.6f}°, lon={lon:.6f}°")
        print(f"Высота: {elevation:.2f} м")
        
        return result
    
    else:
        # Получаем координаты центра DEM
        center_lat = (dem.bounds.top + dem.bounds.bottom) / 2
        center_lon = (dem.bounds.left + dem.bounds.right) / 2
        # Получаем UTM CRS на основе координат центра DEM
        target_crs = get_utm_crs(center_lat, center_lon)
        print("Coordinate system after reprojection", target_crs)
    # Перепроекция DEM на целевой CRS(coordinate reference system)
    # ЕСЛИ НЕ СДЕЛАТЬ ПЕРЕПРОЕКЦИЮ, то считаться будет плохо
    # В примере перепроекция сделана на EPSG:32637 (это для подмосковья)
    # CRS выбирается в зависимости от региона
    reprojected_dem = dem.reproject(crs=target_crs, res=30)
    # Считаем все атрибуты
    attribute_arrays = reprojected_dem.get_terrain_attribute(attributes, **kwargs)
    if not isinstance(attribute_arrays, list):
        attribute_arrays = [attribute_arrays]

    # Преобразуем результаты в xarray.DataArray.
    # В зависимости от версии xdem/geoutils, get_terrain_attribute может вернуть
    # либо Raster-подобные объекты с `.to_xarray()`, либо numpy/masked arrays.
    base_da = reprojected_dem.to_xarray()
    attribute_xarrays = []
    for arr in attribute_arrays:
        if hasattr(arr, "to_xarray"):
            attribute_xarrays.append(arr.to_xarray())
        else:
            data = np.ma.filled(arr, np.nan) if np.ma.isMaskedArray(arr) else np.asarray(arr)
            attribute_xarrays.append(
                xr.DataArray(
                    data,
                    coords=base_da.coords,
                    dims=base_da.dims,
                )
            )
    # Добавляем reprojected DEM в список атрибутов (первым)
    attribute_xarrays.insert(0, reprojected_dem.to_xarray())
    attributes.insert(0, "reprojected_dem")
    # Объединим xarray DataArrays в один xarray.Dataset
    combined_xarray = combine_xarrays(attribute_xarrays, attributes)
    combined_xarray.attrs["resolution_m"] = 30
    combined_xarray.attrs["coordinate_system"] = str(target_crs)
    # Сохраняем объединенный xarray на указанный путь
    if return_wgs84:
        # Преобразователь UTM -> WGS84
        transformer = Transformer.from_crs(
            target_crs,      # UTM
            "EPSG:4326",     # WGS84
            always_xy=True
        )
        # Определяем имена координатных измерений
        dims = list(combined_xarray.sizes.keys())
        if len(dims) < 2:
            raise ValueError(f"Dataset должен иметь хотя бы 2 измерения, найдено: {dims}")
        
        # Проверяем, есть ли уже latitude/longitude в координатах
        if 'latitude' in combined_xarray.coords and 'longitude' in combined_xarray.coords:
            # Получаем UTM координаты из текущих latitude/longitude
            lat_dim, lon_dim = dims[0], dims[1]
            y_utm = combined_xarray.coords[lat_dim].values
            x_utm = combined_xarray.coords[lon_dim].values
        else:
            # Ищем x/y координаты
            x_dim = None
            y_dim = None
            for dim in dims:
                dim_lower = str(dim).lower()
                if 'x' in dim_lower or 'lon' in dim_lower or 'east' in dim_lower:
                    x_dim = dim
                elif 'y' in dim_lower or 'lat' in dim_lower or 'north' in dim_lower:
                    y_dim = dim
            
            # Если не нашли, берём первые два
            if x_dim is None or y_dim is None:
                y_dim, x_dim = dims[0], dims[1]
                print(f"Используются измерения по умолчанию: y={y_dim}, x={x_dim}")
            
            y_utm = combined_xarray.coords[y_dim].values
            x_utm = combined_xarray.coords[x_dim].values
        
        # Создаём сетку координат UTM
        x_grid_utm, y_grid_utm = np.meshgrid(x_utm, y_utm)
        
        # Преобразуем в lat/lon (WGS84)
        lon_grid, lat_grid = transformer.transform(x_grid_utm, y_grid_utm)
        
        # Получаем одномерные массивы lat/lon (из центров пикселей)
        # Берём средние значения по строкам/столбцам
        lon_1d = lon_grid[0, :]  # первая строка (все столбцы)
        lat_1d = lat_grid[:, 0]  # первый столбец (все строки)
        
        # ЗАМЕНЯЕМ координаты: удаляем старые UTM, добавляем новые WGS84
        # Сначала переименовываем измерения если нужно
        rename_dict = {}
        if dims[0] != 'latitude':
            rename_dict[dims[0]] = 'latitude'
        if dims[1] != 'longitude':
            rename_dict[dims[1]] = 'longitude'
        
        if rename_dict:
            combined_xarray = combined_xarray.rename(rename_dict)
            print(f"Переименованы измерения: {rename_dict}")
        
        # Удаляем старые координаты (если есть)
        coords_to_drop = []
        for coord in combined_xarray.coords:
            if coord not in ['latitude', 'longitude',]:
                coords_to_drop.append(coord)
        if coords_to_drop:
            combined_xarray = combined_xarray.drop_vars(coords_to_drop)
            print(f"Удалены старые координаты: {coords_to_drop}")
        
        # ЗАМЕНЯЕМ координаты на WGS84
        combined_xarray = combined_xarray.assign_coords({
            'longitude': lon_1d,
            'latitude': lat_1d
        })
        
        # Добавляем метаданные
        combined_xarray.coords['longitude'].attrs = {
            'long_name': 'Longitude',
            'units': 'degrees_east',
            'standard_name': 'longitude',
            'axis': 'X'
        }
        combined_xarray.coords['latitude'].attrs = {
            'long_name': 'Latitude',
            'units': 'degrees_north',
            'standard_name': 'latitude',
            'axis': 'Y'
        }
        
        # Обновляем CRS и transform для WGS84
        wgs84_transform = from_bounds(
            lon_grid.min(), lat_grid.min(),
            lon_grid.max(), lat_grid.max(),
            combined_xarray.sizes['longitude'],
            combined_xarray.sizes['latitude']
        )
        
        combined_xarray.rio.write_crs("EPSG:4326", inplace=True)
        combined_xarray.rio.write_transform(wgs84_transform, inplace=True)
        
        # Сохраняем границы в атрибутах
        combined_xarray.attrs["bounds_lon_min"] = float(lon_grid.min())
        combined_xarray.attrs["bounds_lon_max"] = float(lon_grid.max())
        combined_xarray.attrs["bounds_lat_min"] = float(lat_grid.min())
        combined_xarray.attrs["bounds_lat_max"] = float(lat_grid.max())
        combined_xarray.attrs["coordinate_system"] = "EPSG:4326 (WGS84)"
        combined_xarray.attrs["original_utm_crs"] = str(target_crs)
        # ОБРЕЗКА до исходных границ
        if crop_to_bounds and original_bounds is not None:
            print(f"\nПрименяется обрезка до исходных границ...")
            combined_xarray = crop_to_original_bounds(combined_xarray, original_bounds)
        else:
            if crop_to_bounds and original_bounds is None:
                print("Обрезка запрошена, но исходные границы не найдены. Пропускается.")
        
    
    return combined_xarray


def denoise_dem_wbt(
    dem_path,
    output_path=None,
    filter_size=11,
    norm_diff=15.0,
    num_iter=3,
    method="feature_preserving",
    filterx=11,
    filtery=11,
    threshold=2.0,
    tv_weight=0.15,
    tv_iterations=50,
    nlm_h=10.0,
    nlm_template_window_size=7,
    nlm_search_window_size=21,
    wavelet_name="db2",
    wavelet_level=2,
    wavelet_threshold_mode="soft",
    pm_iterations=20,
    pm_kappa=30.0,
    pm_gamma=0.15,
    bilateral_sigma_dist=0.75,
    bilateral_sigma_int=1.0,
    gaussian_sigma=0.75,
):
    """
    Denoise DEM using WhiteboxTools filters.
    
    IMPORTANT: Automatically reprojects to UTM before denoising for accurate metric calculations,
    then reprojects back to original CRS. This ensures denoising works correctly regardless of
    input coordinate system.
    
    Args:
        dem_path: Path to input DEM GeoTIFF file (any CRS)
        output_path: Path for output denoised DEM (if None, creates temp file in same dir)
        filter_size: Filter size for feature-preserving method (default=11)
        norm_diff: Maximum normal vector difference in degrees (default=15.0)
        num_iter: Number of feature-preserving iterations (default=3)
        method: Denoising method ('feature_preserving', 'adaptive', 'total_variation', 'non_local_means', 'wavelet', 'anisotropic_diffusion', 'bilateral', or 'gaussian')
        filterx: Adaptive filter x-window size (default=11)
        filtery: Adaptive filter y-window size (default=11)
        threshold: Adaptive filter threshold (default=2.0)
        tv_weight: TV denoising weight for total_variation method (default=0.15)
        tv_iterations: Number of TV iterations for total_variation method (default=50)
        nlm_h: Strength for non_local_means denoising (default=10.0)
        nlm_template_window_size: Template patch size for non_local_means (default=7)
        nlm_search_window_size: Search window size for non_local_means (default=21)
        wavelet_name: Wavelet family for wavelet denoising (default='db2')
        wavelet_level: Decomposition level for wavelet denoising (default=2)
        wavelet_threshold_mode: Threshold mode for wavelet denoising ('soft' or 'hard', default='soft')
        pm_iterations: Number of Perona-Malik diffusion iterations (default=20)
        pm_kappa: Perona-Malik conduction parameter controlling edge sensitivity (default=30.0)
        pm_gamma: Perona-Malik time-step parameter, should be <= 0.25 for stability (default=0.15)
        bilateral_sigma_dist: Bilateral spatial sigma for method='bilateral' (default=0.75)
        bilateral_sigma_int: Bilateral intensity sigma for method='bilateral' (default=1.0)
        gaussian_sigma: Gaussian sigma for method='gaussian' (default=0.75)
    
    Returns:
        Path to denoised DEM file (in original CRS)
    
    Example:
        >>> denoised_path = denoise_dem_wbt('srtm.tif', 'srtm_denoised.tif')
        >>> # Use denoised DEM for terrain calculations
        >>> terrain = calculate_terrain_attributes(denoised_path, ['slope', 'curvature'])
    """
    def _denoise_chambolle_tv(image, weight=0.15, iterations=50):
        image = image.astype(np.float64)
        px = np.zeros_like(image)
        py = np.zeros_like(image)
        tau = 0.125
        for _ in range(iterations):
            gradx = np.roll(image, -1, axis=1) - image
            grady = np.roll(image, -1, axis=0) - image
            px_new = px + (tau / weight) * gradx
            py_new = py + (tau / weight) * grady
            norm_new = np.maximum(1.0, np.sqrt(px_new**2 + py_new**2))
            px = px_new / norm_new
            py = py_new / norm_new
            div_p = (px - np.roll(px, 1, axis=1)) + (py - np.roll(py, 1, axis=0))
            image = image + weight * div_p
        return image
    
    def _denoise_wavelet(image, pywt_module, wavelet_name="db2", wavelet_level=2, threshold_mode="soft"):
        wavelet_obj = pywt_module.Wavelet(wavelet_name)
        max_level = pywt_module.dwt_max_level(min(image.shape), wavelet_obj.dec_len)
        level = max(1, min(int(wavelet_level), int(max_level))) if max_level > 0 else 1
        coeffs = pywt_module.wavedec2(image, wavelet=wavelet_obj, level=level)
        cA, detail_coeffs = coeffs[0], coeffs[1:]
        sigma = 0.0
        if detail_coeffs:
            hh = detail_coeffs[-1][2]
            if hh.size:
                sigma = float(np.median(np.abs(hh)) / 0.6745)
        threshold = sigma * np.sqrt(2.0 * np.log(image.size)) if sigma > 0 else 0.0
        denoised_details = [
            tuple(pywt_module.threshold(c, value=threshold, mode=threshold_mode) for c in detail)
            for detail in detail_coeffs
        ]
        denoised = pywt_module.waverec2([cA] + denoised_details, wavelet=wavelet_obj)
        return denoised[: image.shape[0], : image.shape[1]]

    def _anisotropic_diffusion(image, valid_mask, iterations=20, kappa=30.0, gamma=0.15):
        image = image.astype(np.float64, copy=True)
        valid_mask = valid_mask.astype(bool)
        iterations = max(1, int(iterations))
        gamma = float(gamma)
        if gamma <= 0 or gamma > 0.25:
            raise ValueError("pm_gamma must be in (0, 0.25] for anisotropic_diffusion")
        kappa = max(float(kappa), 1e-8)

        for _ in range(iterations):
            delta_n = np.zeros_like(image)
            delta_s = np.zeros_like(image)
            delta_e = np.zeros_like(image)
            delta_w = np.zeros_like(image)

            pair_n = np.zeros_like(valid_mask)
            pair_s = np.zeros_like(valid_mask)
            pair_e = np.zeros_like(valid_mask)
            pair_w = np.zeros_like(valid_mask)

            pair_n[:-1, :] = valid_mask[:-1, :] & valid_mask[1:, :]
            pair_s[1:, :] = valid_mask[1:, :] & valid_mask[:-1, :]
            pair_e[:, :-1] = valid_mask[:, :-1] & valid_mask[:, 1:]
            pair_w[:, 1:] = valid_mask[:, 1:] & valid_mask[:, :-1]

            delta_n[:-1, :] = image[1:, :] - image[:-1, :]
            delta_s[1:, :] = image[:-1, :] - image[1:, :]
            delta_e[:, :-1] = image[:, 1:] - image[:, :-1]
            delta_w[:, 1:] = image[:, :-1] - image[:, 1:]

            delta_n[~pair_n] = 0.0
            delta_s[~pair_s] = 0.0
            delta_e[~pair_e] = 0.0
            delta_w[~pair_w] = 0.0

            c_n = np.exp(-(delta_n / kappa) ** 2)
            c_s = np.exp(-(delta_s / kappa) ** 2)
            c_e = np.exp(-(delta_e / kappa) ** 2)
            c_w = np.exp(-(delta_w / kappa) ** 2)

            image[valid_mask] += gamma * (
                (c_n * delta_n)[valid_mask]
                + (c_s * delta_s)[valid_mask]
                + (c_e * delta_e)[valid_mask]
                + (c_w * delta_w)[valid_mask]
            )
        return image
    
    # Convert all paths to absolute paths with forward slashes
    dem_path = os.path.abspath(dem_path).replace('\\', '/')
    
    # Create output path if not provided
    if output_path is None:
        base_dir = os.path.dirname(dem_path)
        base_name = os.path.splitext(os.path.basename(dem_path))[0]
        output_path = os.path.join(base_dir, f"{base_name}_denoised.tif")
    
    # Convert output path to absolute with forward slashes
    output_path = os.path.abspath(output_path).replace('\\', '/')
    
    # Step 1: Check original CRS and reproject to UTM if needed
    temp_utm_input = None
    temp_utm_output = None
    temp_input = None
    original_crs = None
    original_tags = {}
    
    try:
        with rasterio.open(dem_path) as src:
            original_crs = src.crs
            print(f"  Original DEM CRS: {original_crs}")
            
            # Preserve original_bounds tags to maintain cropping behavior
            tags = src.tags()
            for key in ['original_lon1', 'original_lat1', 'original_lon2', 'original_lat2']:
                if key in tags:
                    original_tags[key] = tags[key]
            if original_tags:
                print(f"  Preserving original_bounds tags: {original_tags}")
            
            # Check if CRS is geographic (lat/lon)
            is_geographic = original_crs.is_geographic if original_crs else True
            
            if is_geographic:
                print(f"  -> Geographic CRS detected, reprojecting to UTM for accurate denoising...")
                
                # Get center point for UTM zone calculation
                bounds = src.bounds
                center_lon = (bounds.left + bounds.right) / 2
                center_lat = (bounds.bottom + bounds.top) / 2
                
                # Get appropriate UTM CRS
                utm_crs = get_utm_crs(center_lat, center_lon)
                print(f"  -> Target UTM CRS: {utm_crs}")
                
                # Create temporary UTM file
                temp_dir = os.path.dirname(output_path)
                temp_base = os.path.splitext(os.path.basename(dem_path))[0]
                temp_utm_input = os.path.join(temp_dir, f"{temp_base}_utm_temp.tif")
                temp_utm_input = os.path.abspath(temp_utm_input).replace('\\', '/')
                
                # Reproject to UTM
                from rasterio.warp import calculate_default_transform, reproject, Resampling
                
                transform, width, height = calculate_default_transform(
                    src.crs, utm_crs, src.width, src.height, *src.bounds
                )
                
                profile = src.profile.copy()
                profile.update({
                    'crs': utm_crs,
                    'transform': transform,
                    'width': width,
                    'height': height,
                    'compress': None,
                    'tiled': False
                })
                
                with rasterio.open(temp_utm_input, 'w', **profile) as dst:
                    reproject(
                        source=rasterio.band(src, 1),
                        destination=rasterio.band(dst, 1),
                        src_transform=src.transform,
                        src_crs=src.crs,
                        dst_transform=transform,
                        dst_crs=utm_crs,
                        resampling=Resampling.bilinear
                    )
                
                print(f"  [OK] Reprojected to UTM: {temp_utm_input}")
                dem_to_denoise = temp_utm_input
                
                # Output will also be in UTM temporarily
                temp_utm_output = os.path.join(temp_dir, f"{temp_base}_utm_denoised_temp.tif")
                temp_utm_output = os.path.abspath(temp_utm_output).replace('\\', '/')
                denoise_output = temp_utm_output
            else:
                print(f"  -> Projected CRS detected, no reprojection needed")
                dem_to_denoise = dem_path
                denoise_output = output_path
        
        # Step 2: Prepare uncompressed version for WhiteboxTools
        with rasterio.open(dem_to_denoise) as src:
            print(f"  Input DEM compression: {src.profile.get('compress', 'none')}")
            print(f"  Re-encoding for WhiteboxTools compatibility...")
            
            # Create temporary file
            temp_dir = os.path.dirname(output_path) if output_path else os.path.dirname(dem_path)
            os.makedirs(temp_dir, exist_ok=True)
            
            temp_base = os.path.splitext(os.path.basename(dem_path))[0]
            temp_input = os.path.join(temp_dir, f"{temp_base}_wbt_temp.tif")
            temp_input = os.path.abspath(temp_input).replace('\\', '/')
            
            # Read data and metadata
            data = src.read()
            profile = src.profile.copy()
            
            # Set to uncompressed (most compatible)
            profile.update(
                compress=None,
                tiled=False
            )
            
            # Write uncompressed version
            with rasterio.open(temp_input, 'w', **profile) as dst:
                dst.write(data)
            
        # Step 2: Prepare uncompressed version for WhiteboxTools
        with rasterio.open(dem_to_denoise) as src:
            print(f"  Input DEM compression: {src.profile.get('compress', 'none')}")
            print(f"  Re-encoding for WhiteboxTools compatibility...")
            
            # Create temporary file
            temp_dir = os.path.dirname(output_path)
            os.makedirs(temp_dir, exist_ok=True)
            
            temp_base = os.path.splitext(os.path.basename(dem_to_denoise))[0]
            temp_input = os.path.join(temp_dir, f"{temp_base}_wbt_temp.tif")
            temp_input = os.path.abspath(temp_input).replace('\\', '/')
            
            # Read data and metadata
            data = src.read()
            profile = src.profile.copy()
            
            # Set to uncompressed (most compatible)
            profile.update(
                compress=None,
                tiled=False
            )
            
            # Write uncompressed version
            with rasterio.open(temp_input, 'w', **profile) as dst:
                dst.write(data)
            
            dem_input_path = temp_input
            print(f"  Temporary uncompressed DEM: {temp_input}")
    
        # Step 3: Run denoising
        print(f"Denoising DEM with WhiteboxTools...")
        print(f"  Input: {dem_input_path}")
        print(f"  Output: {denoise_output}")
        print(f"  Method: {method}")
        
        # Verify input file exists
        if not os.path.exists(dem_input_path):
            raise FileNotFoundError(f"Input DEM file not found: {dem_input_path}")
        
        if method == "total_variation":
            print(f"  TV weight: {tv_weight}, Iterations: {tv_iterations}")
            with rasterio.open(dem_input_path) as src:
                data = src.read(1).astype(np.float64)
                profile = src.profile.copy()
                nodata = src.nodata
                nodata_mask = np.isnan(data)
                if nodata is not None and not np.isnan(nodata):
                    nodata_mask = nodata_mask | (data == nodata)
                valid_data = data[~nodata_mask]
                fill_value = float(np.mean(valid_data)) if valid_data.size else 0.0
                data[nodata_mask] = fill_value
                denoised = _denoise_chambolle_tv(
                    data,
                    weight=tv_weight,
                    iterations=tv_iterations,
                )
                if nodata is None:
                    denoised[nodata_mask] = np.nan
                else:
                    denoised[nodata_mask] = nodata
                profile.update(dtype="float32", count=1)
                with rasterio.open(denoise_output, "w", **profile) as dst:
                    dst.write(denoised.astype(np.float32), 1)
            result = 0
        elif method == "feature_preserving":
            try:
                from whitebox import WhiteboxTools
            except ImportError:
                raise ImportError(
                    "WhiteboxTools not installed. Install with: pip install whitebox"
                )
            wbt = WhiteboxTools()
            wbt.set_verbose_mode(True)  # Enable verbose mode to see errors
            wbt.set_compress_rasters(False)  # Disable compression for maximum compatibility
            print(
                f"  Filter size: {filter_size}, Norm diff: {norm_diff}°, Iterations: {num_iter}"
            )
            result = wbt.feature_preserving_smoothing(
                dem=dem_input_path,
                output=denoise_output,
                filter=filter_size,
                norm_diff=norm_diff,
                num_iter=num_iter,
            )
        elif method == "adaptive":
            try:
                from whitebox import WhiteboxTools
            except ImportError:
                raise ImportError(
                    "WhiteboxTools not installed. Install with: pip install whitebox"
                )
            wbt = WhiteboxTools()
            wbt.set_verbose_mode(True)  # Enable verbose mode to see errors
            wbt.set_compress_rasters(False)  # Disable compression for maximum compatibility
            print(
                f"  Adaptive window: ({filterx}, {filtery}), Threshold: {threshold}"
            )
            result = wbt.adaptive_filter(
                i=dem_input_path,
                output=denoise_output,
                filterx=filterx,
                filtery=filtery,
                threshold=threshold,
            )
        elif method == "non_local_means":
            try:
                import cv2
            except ImportError:
                raise ImportError(
                    "OpenCV not installed. Install with: pip install opencv-python"
                )
            print(
                "  Non-Local Means: "
                f"h={nlm_h}, template={nlm_template_window_size}, search={nlm_search_window_size}"
            )
            with rasterio.open(dem_input_path) as src:
                data = src.read(1).astype(np.float32)
                profile = src.profile.copy()
                nodata = src.nodata
                nodata_mask = np.isnan(data)
                if nodata is not None and not np.isnan(nodata):
                    nodata_mask = nodata_mask | (data == nodata)
                valid_data = data[~nodata_mask]
                fill_value = float(np.mean(valid_data)) if valid_data.size else 0.0
                work_data = data.copy()
                work_data[nodata_mask] = fill_value
                if valid_data.size:
                    valid_min = float(np.min(valid_data))
                    valid_max = float(np.max(valid_data))
                    if valid_max > valid_min:
                        scaled = np.clip(
                            (work_data - valid_min) / (valid_max - valid_min), 0.0, 1.0
                        )
                        image_uint8 = (scaled * 255.0).astype(np.uint8)
                        denoised_uint8 = cv2.fastNlMeansDenoising(
                            image_uint8,
                            None,
                            h=float(nlm_h),
                            templateWindowSize=int(nlm_template_window_size),
                            searchWindowSize=int(nlm_search_window_size),
                        )
                        denoised = (
                            denoised_uint8.astype(np.float32) / 255.0
                        ) * (valid_max - valid_min) + valid_min
                    else:
                        denoised = np.full_like(work_data, valid_min, dtype=np.float32)
                else:
                    denoised = work_data
                if nodata is None:
                    denoised[nodata_mask] = np.nan
                else:
                    denoised[nodata_mask] = nodata
                profile.update(dtype="float32", count=1)
                with rasterio.open(denoise_output, "w", **profile) as dst:
                    dst.write(denoised.astype(np.float32), 1)
            result = 0
        elif method == "wavelet":
            try:
                import pywt
            except ImportError:
                raise ImportError(
                    "PyWavelets is required for method='wavelet'. Install with: pip install PyWavelets"
                )
            print(
                "  Wavelet denoising: "
                f"wavelet={wavelet_name}, level={wavelet_level}, threshold_mode={wavelet_threshold_mode}"
            )
            with rasterio.open(dem_input_path) as src:
                data = src.read(1).astype(np.float64)
                profile = src.profile.copy()
                nodata = src.nodata
                nodata_mask = np.isnan(data)
                if nodata is not None and not np.isnan(nodata):
                    nodata_mask = nodata_mask | (data == nodata)
                valid_data = data[~nodata_mask]
                fill_value = float(np.mean(valid_data)) if valid_data.size else 0.0
                work_data = data.copy()
                work_data[nodata_mask] = fill_value
                denoised = _denoise_wavelet(
                    work_data,
                    pywt_module=pywt,
                    wavelet_name=wavelet_name,
                    wavelet_level=wavelet_level,
                    threshold_mode=wavelet_threshold_mode,
                )
                if nodata is None:
                    denoised[nodata_mask] = np.nan
                else:
                    denoised[nodata_mask] = nodata
                profile.update(dtype="float32", count=1)
                with rasterio.open(denoise_output, "w", **profile) as dst:
                    dst.write(denoised.astype(np.float32), 1)
            result = 0
        elif method == "anisotropic_diffusion":
            print(
                "  Anisotropic diffusion: "
                f"iterations={pm_iterations}, kappa={pm_kappa}, gamma={pm_gamma}"
            )
            with rasterio.open(dem_input_path) as src:
                data = src.read(1).astype(np.float64)
                profile = src.profile.copy()
                nodata = src.nodata
                nodata_mask = np.isnan(data)
                if nodata is not None and not np.isnan(nodata):
                    nodata_mask = nodata_mask | (data == nodata)
                valid_data = data[~nodata_mask]
                fill_value = float(np.mean(valid_data)) if valid_data.size else 0.0
                work_data = data.copy()
                work_data[nodata_mask] = fill_value
                denoised = _anisotropic_diffusion(
                    work_data,
                    valid_mask=~nodata_mask,
                    iterations=pm_iterations,
                    kappa=pm_kappa,
                    gamma=pm_gamma,
                )
                if nodata is None:
                    denoised[nodata_mask] = np.nan
                else:
                    denoised[nodata_mask] = nodata
                profile.update(dtype="float32", count=1)
                with rasterio.open(denoise_output, "w", **profile) as dst:
                    dst.write(denoised.astype(np.float32), 1)
            result = 0
        elif method == "bilateral":
            try:
                from whitebox import WhiteboxTools
            except ImportError:
                raise ImportError(
                    "WhiteboxTools not installed. Install with: pip install whitebox"
                )
            wbt = WhiteboxTools()
            wbt.set_verbose_mode(True)
            wbt.set_compress_rasters(False)
            print(
                "  Bilateral filter: "
                f"sigma_dist={bilateral_sigma_dist}, sigma_int={bilateral_sigma_int}"
            )
            result = wbt.bilateral_filter(
                i=dem_input_path,
                output=denoise_output,
                sigma_dist=bilateral_sigma_dist,
                sigma_int=bilateral_sigma_int,
            )
        elif method == "gaussian":
            try:
                from whitebox import WhiteboxTools
            except ImportError:
                raise ImportError(
                    "WhiteboxTools not installed. Install with: pip install whitebox"
                )
            wbt = WhiteboxTools()
            wbt.set_verbose_mode(True)
            wbt.set_compress_rasters(False)
            print(f"  Gaussian filter: sigma={gaussian_sigma}")
            result = wbt.gaussian_filter(
                i=dem_input_path,
                output=denoise_output,
                sigma=gaussian_sigma,
            )
        else:
            raise ValueError(
                f"Unknown denoise_dem_wbt method: '{method}'. "
                "Valid options: 'feature_preserving', 'adaptive', 'total_variation', 'non_local_means', 'wavelet', 'anisotropic_diffusion', 'bilateral', 'gaussian'"
            )
        
        # Check if operation was successful
        if result != 0:
            raise RuntimeError(
                f"WhiteboxTools denoising method '{method}' failed with return code: {result}"
            )
        
        if not os.path.exists(denoise_output):
            raise FileNotFoundError(f"Output file was not created: {denoise_output}")
        
        print(f"[SUCCESS] Denoised DEM created")
        
        # Step 4: Reproject back to original CRS if needed
        if is_geographic and temp_utm_output:
            print(f"  -> Reprojecting back to original CRS: {original_crs}")
            
            with rasterio.open(temp_utm_output) as src_utm:
                # Calculate transform for original CRS
                from rasterio.warp import calculate_default_transform, reproject, Resampling
                
                # Use original file dimensions to match input exactly
                with rasterio.open(dem_path) as src_orig:
                    orig_bounds = src_orig.bounds
                    orig_width = src_orig.width
                    orig_height = src_orig.height
                    orig_transform = src_orig.transform
                
                # Use original dimensions instead of recalculating
                # This ensures output matches input dimensions exactly
                profile = src_utm.profile.copy()
                profile.update({
                    'crs': original_crs,
                    'transform': orig_transform,  # Use original transform
                    'width': orig_width,          # Use original width
                    'height': orig_height,        # Use original height
                    'compress': 'deflate',
                    'tiled': False
                })
                
                with rasterio.open(output_path, 'w', **profile) as dst:
                    reproject(
                        source=rasterio.band(src_utm, 1),
                        destination=rasterio.band(dst, 1),
                        src_transform=src_utm.transform,
                        src_crs=src_utm.crs,
                        dst_transform=orig_transform,  # Use original transform
                        dst_crs=original_crs,
                        resampling=Resampling.bilinear
                    )
            
            print(f"  [OK] Reprojected back to original CRS (matched original dimensions)")
        
        # Copy original_bounds tags to output file to maintain cropping behavior
        if original_tags:
            with rasterio.open(output_path, 'r+') as dst:
                dst.update_tags(**original_tags)
            print(f"  Copied original_bounds tags to output")
        
        print(f"[SUCCESS] Final denoised DEM saved to: {output_path}")
        
        return output_path
        
    finally:
        # Clean up temporary files
        temp_files = [temp_input, temp_utm_input, temp_utm_output]
        for temp_file in temp_files:
            if temp_file and os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                    print(f"  Cleaned up temporary file: {temp_file}")
                except Exception as e:
                    print(f"  Warning: Could not remove temporary file {temp_file}: {e}")


def denoise_dem_bilateral(dem_path, output_path=None, sigma_dist=0.75, sigma_int=1.0):
    """
    Denoise DEM using WhiteboxTools bilateral filter (edge-preserving smoothing).
    
    Bilateral filter smooths the image while preserving edges by considering both
    spatial distance and intensity (elevation) difference between pixels.
    
    Args:
        dem_path: Path to input DEM GeoTIFF file
        output_path: Path for output denoised DEM (if None, creates temp file in same dir)
        sigma_dist: Standard deviation for spatial distance (default=0.75)
        sigma_int: Standard deviation for intensity/elevation difference (default=1.0)
    
    Returns:
        Path to denoised DEM file
    
    Example:
        >>> denoised_path = denoise_dem_bilateral('srtm.tif', 'srtm_bilateral.tif')
        >>> # Use denoised DEM for terrain calculations
        >>> terrain = calculate_terrain_attributes(denoised_path, ['slope', 'curvature'])
    """
    try:
        from whitebox import WhiteboxTools
    except ImportError:
        raise ImportError(
            "WhiteboxTools not installed. Install with: pip install whitebox"
        )
    
    # Create output path if not provided
    if output_path is None:
        base_dir = os.path.dirname(dem_path)
        base_name = os.path.splitext(os.path.basename(dem_path))[0]
        output_path = os.path.join(base_dir, f"{base_name}_bilateral.tif")
    
    # WhiteboxTools requires uncompressed or LZW/DEFLATE/PACKBITS compression
    # Always re-encode to ensure compatibility
    temp_input = None
    original_tags = {}
    try:
        with rasterio.open(dem_path) as src:
            print(f"  Input DEM compression: {src.profile.get('compress', 'none')}")
            print(f"  Re-encoding to uncompressed for WhiteboxTools compatibility...")
            
            # Preserve original_bounds tags to maintain cropping behavior
            tags = src.tags()
            for key in ['original_lon1', 'original_lat1', 'original_lon2', 'original_lat2']:
                if key in tags:
                    original_tags[key] = tags[key]
            if original_tags:
                print(f"  Preserving original_bounds tags: {original_tags}")
            
            # Create temporary file with uncompressed format
            temp_dir = os.path.dirname(output_path) if output_path else os.path.dirname(dem_path)
            os.makedirs(temp_dir, exist_ok=True)
            
            temp_base = os.path.splitext(os.path.basename(dem_path))[0]
            temp_input = os.path.join(temp_dir, f"{temp_base}_wbt_temp.tif")
            
            # Read data and metadata
            data = src.read()
            profile = src.profile.copy()
            
            # Set to uncompressed (most compatible)
            profile.update(
                compress=None,
                tiled=False
            )
            
            # Write uncompressed version
            with rasterio.open(temp_input, 'w', **profile) as dst:
                dst.write(data)
            
            dem_input_path = temp_input
            print(f"  Temporary uncompressed DEM: {temp_input}")
    
        # Initialize WhiteboxTools
        wbt = WhiteboxTools()
        wbt.set_verbose_mode(True)
        
        print(f"Denoising DEM with Bilateral Filter...")
        print(f"  Input: {dem_input_path}")
        print(f"  Output: {output_path}")
        print(f"  Sigma distance: {sigma_dist}, Sigma intensity: {sigma_int}")
        
        # Run bilateral filter
        result = wbt.bilateral_filter(
            i=dem_input_path,
            output=output_path,
            sigma_dist=sigma_dist,
            sigma_int=sigma_int
        )
        
        # Check if operation was successful
        if result != 0:
            raise RuntimeError(f"WhiteboxTools bilateral_filter failed with return code: {result}")
        
        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Output file was not created: {output_path}")
        
        # Copy original_bounds tags to output file to maintain cropping behavior
        if original_tags:
            with rasterio.open(output_path, 'r+') as dst:
                dst.update_tags(**original_tags)
            print(f"  Copied original_bounds tags to output")
        
        print(f"[SUCCESS] Denoised DEM saved to: {output_path}")
        
        return output_path
        
    finally:
        # Clean up temporary file if it was created
        if temp_input and os.path.exists(temp_input):
            try:
                os.remove(temp_input)
                print(f"  Cleaned up temporary file: {temp_input}")
            except Exception as e:
                print(f"  Warning: Could not remove temporary file {temp_input}: {e}")


def denoise_dem_gaussian(dem_path, output_path=None, sigma=0.75):
    """
    Denoise DEM using WhiteboxTools Gaussian filter (simple smoothing).
    
    Gaussian filter applies a weighted average using a Gaussian kernel.
    Fast and simple, but does not preserve edges as well as other filters.
    
    Args:
        dem_path: Path to input DEM GeoTIFF file
        output_path: Path for output denoised DEM (if None, creates temp file in same dir)
        sigma: Standard deviation of Gaussian kernel (default=0.75)
    
    Returns:
        Path to denoised DEM file
    
    Example:
        >>> denoised_path = denoise_dem_gaussian('srtm.tif', 'srtm_gaussian.tif')
        >>> # Use denoised DEM for terrain calculations
        >>> terrain = calculate_terrain_attributes(denoised_path, ['slope', 'curvature'])
    """
    try:
        from whitebox import WhiteboxTools
    except ImportError:
        raise ImportError(
            "WhiteboxTools not installed. Install with: pip install whitebox"
        )
    
    # Create output path if not provided
    if output_path is None:
        base_dir = os.path.dirname(dem_path)
        base_name = os.path.splitext(os.path.basename(dem_path))[0]
        output_path = os.path.join(base_dir, f"{base_name}_gaussian.tif")
    
    # WhiteboxTools requires uncompressed or LZW/DEFLATE/PACKBITS compression
    # Always re-encode to ensure compatibility
    temp_input = None
    original_tags = {}
    try:
        with rasterio.open(dem_path) as src:
            print(f"  Input DEM compression: {src.profile.get('compress', 'none')}")
            print(f"  Re-encoding to uncompressed for WhiteboxTools compatibility...")
            
            # Preserve original_bounds tags to maintain cropping behavior
            tags = src.tags()
            for key in ['original_lon1', 'original_lat1', 'original_lon2', 'original_lat2']:
                if key in tags:
                    original_tags[key] = tags[key]
            if original_tags:
                print(f"  Preserving original_bounds tags: {original_tags}")
            
            # Create temporary file with uncompressed format
            temp_dir = os.path.dirname(output_path) if output_path else os.path.dirname(dem_path)
            os.makedirs(temp_dir, exist_ok=True)
            
            temp_base = os.path.splitext(os.path.basename(dem_path))[0]
            temp_input = os.path.join(temp_dir, f"{temp_base}_wbt_temp.tif")
            
            # Read data and metadata
            data = src.read()
            profile = src.profile.copy()
            
            # Set to uncompressed (most compatible)
            profile.update(
                compress=None,
                tiled=False
            )
            
            # Write uncompressed version
            with rasterio.open(temp_input, 'w', **profile) as dst:
                dst.write(data)
            
            dem_input_path = temp_input
            print(f"  Temporary uncompressed DEM: {temp_input}")
    
        # Initialize WhiteboxTools
        wbt = WhiteboxTools()
        wbt.set_verbose_mode(True)
        
        print(f"Denoising DEM with Gaussian Filter...")
        print(f"  Input: {dem_input_path}")
        print(f"  Output: {output_path}")
        print(f"  Sigma: {sigma}")
        
        # Run gaussian filter
        result = wbt.gaussian_filter(
            i=dem_input_path,
            output=output_path,
            sigma=sigma
        )
        
        # Check if operation was successful
        if result != 0:
            raise RuntimeError(f"WhiteboxTools gaussian_filter failed with return code: {result}")
        
        if not os.path.exists(output_path):
            raise FileNotFoundError(f"Output file was not created: {output_path}")
        
        # Copy original_bounds tags to output file to maintain cropping behavior
        if original_tags:
            with rasterio.open(output_path, 'r+') as dst:
                dst.update_tags(**original_tags)
            print(f"  Copied original_bounds tags to output")
        
        print(f"[SUCCESS] Denoised DEM saved to: {output_path}")
        
        return output_path
        
    finally:
        # Clean up temporary file if it was created
        if temp_input and os.path.exists(temp_input):
            try:
                os.remove(temp_input)
                print(f"  Cleaned up temporary file: {temp_input}")
            except Exception as e:
                print(f"  Warning: Could not remove temporary file {temp_input}: {e}")


def denoise_dem(dem_path, output_path=None, method='feature_preserving', **kwargs):
    """
    Universal DEM denoising function supporting multiple filtering methods.
    
    Args:
        dem_path: Path to input DEM GeoTIFF file
        output_path: Path for output denoised DEM (if None, auto-generated)
        method: Denoising method to use. Options:
            - 'feature_preserving': Feature-preserving smoothing (default)
                  Preserves terrain features like ridges and valleys
                  Parameters: filter_size=11, norm_diff=15.0, num_iter=3
            - 'bilateral': Bilateral filter (edge-preserving)
                  Preserves sharp boundaries while smoothing
                  Parameters: sigma_dist=0.75, sigma_int=1.0, num_iter=1
            - 'gaussian': Gaussian filter (simple smoothing)
                  Fast, simple smoothing without edge preservation
                  Parameters: sigma=0.75
        **kwargs: Method-specific parameters (see individual functions)
    
    Returns:
        Path to denoised DEM file
    
    Examples:
        >>> # Feature-preserving smoothing (default)
        >>> denoised = denoise_dem('srtm.tif')
        
        >>> # Bilateral filter with custom parameters
        >>> denoised = denoise_dem('srtm.tif', method='bilateral', sigma_dist=1.0)
        
        >>> # Gaussian filter
        >>> denoised = denoise_dem('srtm.tif', method='gaussian', sigma=1.0)
    """
    if method == 'feature_preserving':
        return denoise_dem_wbt(dem_path, output_path, **kwargs)
    elif method == 'bilateral':
        return denoise_dem_bilateral(dem_path, output_path, **kwargs)
    elif method == 'gaussian':
        return denoise_dem_gaussian(dem_path, output_path, **kwargs)
    else:
        raise ValueError(
            f"Unknown denoising method: '{method}'. "
            f"Valid options: 'feature_preserving', 'bilateral', 'gaussian'"
        )
