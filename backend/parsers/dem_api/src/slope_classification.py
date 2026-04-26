"""
Модуль для классификации склонов по максимальной кривизне.

Классы:
- 0: Ровная поверхность и пологие склоны (maximum_curvature < 0.1)
- 1: Крутые и очень крутые склоны (maximum_curvature >= 0.1)
"""

import numpy as np
import xarray as xr
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap


def classify_slopes(terrain_dataset, threshold=0.1, variable='maximum_curvature', use_abs=True):
    """
    Классифицирует пиксели на основе заданного атрибута рельефа.
    
    Args:
        terrain_dataset (xr.Dataset): Dataset с атрибутами рельефа
        threshold (float): Порог для разделения классов (по умолчанию 0.1)
        variable (str): Название переменной для классификации (по умолчанию 'maximum_curvature')
        use_abs (bool): Использовать ли модуль значения (по умолчанию True)
    
    Returns:
        xr.DataArray: Массив с классами (0 - низкое значение, 1 - высокое значение)
    
    Raises:
        ValueError: Если переменная отсутствует в dataset
    """
    if variable not in terrain_dataset.data_vars:
        available_vars = list(terrain_dataset.data_vars.keys())
        raise ValueError(f"Dataset должен содержать переменную '{variable}'. Доступные: {available_vars}")
    
    data_var = terrain_dataset[variable]
    
    # Создаём массив классов
    # 0 - низкое значение параметра
    # 1 - высокое значение параметра
    if use_abs:
        condition = np.abs(data_var) < threshold
        desc = f'0: Low (|{variable}| < {threshold}), 1: High (|{variable}| >= {threshold})'
    else:
        condition = data_var < threshold
        desc = f'0: Low ({variable} < {threshold}), 1: High ({variable} >= {threshold})'
    
    slope_classes = xr.where(
        condition,
        0,  # Низкое значение
        1   # Высокое значение
    )
    
    # Добавляем метаданные
    slope_classes.name = 'slope_class'
    slope_classes.attrs = {
        'long_name': f'Classification by {variable}',
        'description': desc,
        'threshold': threshold,
        'variable': variable,
        'use_abs': use_abs,
        'units': 'class'
    }
    
    return slope_classes


def get_class_statistics(slope_classes):
    """
    Вычисляет статистику по классам склонов.
    
    Args:
        slope_classes (xr.DataArray): Массив с классами
    
    Returns:
        dict: Словарь со статистикой
    """
    # Убираем NaN
    valid_data = slope_classes.values[~np.isnan(slope_classes.values)]
    
    if len(valid_data) == 0:
        return {
            'total_pixels': 0,
            'gentle_slopes': 0,
            'steep_slopes': 0,
            'gentle_percentage': 0.0,
            'steep_percentage': 0.0
        }
    
    total = len(valid_data)
    gentle = np.sum(valid_data == 0)
    steep = np.sum(valid_data == 1)
    
    return {
        'total_pixels': int(total),
        'gentle_slopes': int(gentle),
        'steep_slopes': int(steep),
        'gentle_percentage': float(gentle / total * 100),
        'steep_percentage': float(steep / total * 100)
    }


def plot_classification(slope_classes, terrain_dataset=None, figsize=(12, 5)):
    """
    Визуализирует классификацию склонов.
    
    Args:
        slope_classes (xr.DataArray): Массив с классами
        terrain_dataset (xr.Dataset, optional): Исходный dataset для отображения DEM
        figsize (tuple): Размер фигуры
    """
    # Цвета: зелёный - пологий, красный - крутой
    cmap = ListedColormap(['#90EE90', '#FF6B6B'])
    
    if terrain_dataset is not None and 'elevation' in terrain_dataset.data_vars:
        # Два графика: DEM и классификация
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=figsize)
        
        # DEM
        terrain_dataset['elevation'].plot(ax=ax1, cmap='terrain')
        ax1.set_title('Elevation (DEM)')
        ax1.set_xlabel('Longitude')
        ax1.set_ylabel('Latitude')
        
        # Классификация
        im = slope_classes.plot(ax=ax2, cmap=cmap, vmin=0, vmax=1, add_colorbar=False)
        ax2.set_title('Slope Classification')
        ax2.set_xlabel('Longitude')
        ax2.set_ylabel('Latitude')
        
        # Добавляем легенду
        cbar = plt.colorbar(im, ax=ax2, ticks=[0.25, 0.75])
        cbar.ax.set_yticklabels(['Gentle', 'Steep'])
        
    else:
        # Только классификация
        fig, ax = plt.subplots(figsize=(8, 6))
        im = slope_classes.plot(ax=ax, cmap=cmap, vmin=0, vmax=1, add_colorbar=False)
        ax.set_title('Slope Classification')
        ax.set_xlabel('Longitude')
        ax.set_ylabel('Latitude')
        
        # Легенда
        cbar = plt.colorbar(im, ax=ax, ticks=[0.25, 0.75])
        cbar.ax.set_yticklabels(['Gentle', 'Steep'])
    
    plt.tight_layout()
    plt.show()


def add_classification_to_dataset(terrain_dataset, threshold=0.1, variable='maximum_curvature', use_abs=True):
    """
    Добавляет классификацию к существующему Dataset.
    
    Args:
        terrain_dataset (xr.Dataset): Исходный dataset
        threshold (float): Порог для классификации
        variable (str): Название переменной для классификации
        use_abs (bool): Использовать ли модуль значения
    
    Returns:
        xr.Dataset: Dataset с добавленной переменной 'slope_class'
    """
    slope_classes = classify_slopes(terrain_dataset, threshold, variable, use_abs)
    
    # Создаём копию dataset и добавляем классификацию
    result = terrain_dataset.copy()
    result['slope_class'] = slope_classes
    
    return result


def classify_and_report(terrain_dataset, threshold=0.1, variable='maximum_curvature', 
                       use_abs=True, plot=True):
    """
    Комплексная функция: классифицирует, выводит статистику и строит график.
    
    Args:
        terrain_dataset (xr.Dataset): Исходный dataset
        threshold (float): Порог для классификации
        variable (str): Переменная для классификации
        use_abs (bool): Использовать модуль значения
        plot (bool): Строить ли график
    
    Returns:
        tuple: (slope_classes, statistics)
    """
    # Классификация
    slope_classes = classify_slopes(terrain_dataset, threshold, variable, use_abs)
    
    # Статистика
    stats = get_class_statistics(slope_classes)
    
    # Вывод статистики
    print("=" * 50)
    print("КЛАССИФИКАЦИЯ ПО ПАРАМЕТРУ")
    print("=" * 50)
    print(f"Параметр: {variable}")
    print(f"Порог: {threshold}")
    print(f"Использовать модуль: {use_abs}")
    print(f"Всего пикселей: {stats['total_pixels']}")
    print(f"\nНизкие значения (класс 0):")
    print(f"  Количество: {stats['gentle_slopes']} ({stats['gentle_percentage']:.2f}%)")
    print(f"\nВысокие значения (класс 1):")
    print(f"  Количество: {stats['steep_slopes']} ({stats['steep_percentage']:.2f}%)")
    print("=" * 50)
    
    # Визуализация
    if plot:
        plot_classification(slope_classes, terrain_dataset)
    
    return slope_classes, stats


# Добавьте эти импорты в начало файла
from scipy.ndimage import label
from skimage.measure import find_contours
from shapely.geometry import Polygon, MultiPolygon, mapping
from shapely.ops import unary_union
import geopandas as gpd
import json


def cluster_slopes(slope_classes, min_pixels=5):
    """
    Кластеризует смежные пиксели одного класса в отдельные регионы.
    
    Args:
        slope_classes (xr.DataArray): Массив с классами
        min_pixels (int): Минимальный размер кластера (в пикселях)
    
    Returns:
        tuple: (labeled_array, num_clusters) - массив с метками кластеров и их количество
    """
    # Получаем данные
    data = slope_classes.values.copy()
    
    # Создаём маску для каждого класса
    gentle_mask = (data == 0)
    steep_mask = (data == 1)
    
    # Кластеризуем каждый класс отдельно
    # structure определяет связность (8-связность для диагоналей)
    structure = np.ones((3, 3), dtype=int)
    
    gentle_labeled, num_gentle = label(gentle_mask, structure=structure)
    steep_labeled, num_steep = label(steep_mask, structure=structure)
    
    # Объединяем: пологие - положительные номера, крутые - отрицательные
    combined_labeled = np.zeros_like(data)
    combined_labeled[gentle_mask] = gentle_labeled[gentle_mask]
    combined_labeled[steep_mask] = -steep_labeled[steep_mask]
    
    # Удаляем маленькие кластеры
    for cluster_id in np.unique(combined_labeled):
        if cluster_id == 0:  # Пропускаем NaN
            continue
        cluster_size = np.sum(np.abs(combined_labeled) == np.abs(cluster_id))
        if cluster_size < min_pixels:
            combined_labeled[np.abs(combined_labeled) == np.abs(cluster_id)] = 0
    
    # Пересчитываем количество кластеров
    unique_clusters = np.unique(combined_labeled)
    num_clusters = len(unique_clusters[unique_clusters != 0])
    
    print(f"[OK] Найдено кластеров: {num_clusters}")
    print(f"   Пологие склоны: {num_gentle} → {len(unique_clusters[unique_clusters > 0])}")
    print(f"   Крутые склоны: {num_steep} → {len(unique_clusters[unique_clusters < 0])}")
    
    return combined_labeled, num_clusters


def clusters_to_polygons(slope_classes, clustered_array, terrain_dataset, 
                         simplify_tolerance=0.0001):
    """
    Преобразует кластеры в полигоны (GeoJSON).
    
    Args:
        slope_classes (xr.DataArray): Исходная классификация
        clustered_array (np.ndarray): Массив с метками кластеров
        terrain_dataset (xr.Dataset): Dataset с координатами
        simplify_tolerance (float): Упрощение геометрии (в градусах)
    
    Returns:
        gpd.GeoDataFrame: GeoDataFrame с полигонами
    """
    from rasterio.features import shapes
    from affine import Affine
    
    # Получаем координаты
    lons = terrain_dataset.longitude.values
    lats = terrain_dataset.latitude.values
    
    # Создаём affine transform для преобразования пиксельных координат в географические
    transform = Affine.translation(lons[0], lats[0]) * Affine.scale(
        (lons[-1] - lons[0]) / len(lons),
        (lats[-1] - lats[0]) / len(lats)
    )
    
    # Извлекаем полигоны
    polygons = []
    classes = []
    cluster_ids = []
    
    # Для каждого уникального кластера
    unique_clusters = np.unique(clustered_array)
    unique_clusters = unique_clusters[unique_clusters != 0]
    
    for cluster_id in unique_clusters:
        # Создаём маску для текущего кластера
        mask = (np.abs(clustered_array) == np.abs(cluster_id)).astype(np.uint8)
        
        # Определяем класс (0 - gentle, 1 - steep)
        slope_class = 0 if cluster_id > 0 else 1
        
        # Извлекаем контуры
        for geom, value in shapes(mask, transform=transform):
            if value == 1:  # Только заполненные области
                poly = Polygon(geom['coordinates'][0])
                
                # Упрощаем геометрию
                if simplify_tolerance > 0:
                    poly = poly.simplify(simplify_tolerance, preserve_topology=True)
                
                if poly.is_valid and poly.area > 0:
                    polygons.append(poly)
                    classes.append(slope_class)
                    cluster_ids.append(int(cluster_id))
    
    # Создаём GeoDataFrame
    gdf = gpd.GeoDataFrame({
        'cluster_id': cluster_ids,
        'class': classes,
        'class_name': ['Gentle' if c == 0 else 'Steep' for c in classes],
        'area_deg2': [p.area for p in polygons],
        'geometry': polygons
    }, crs='EPSG:4326')
    
    print(f"[OK] Создано {len(gdf)} полигонов")
    print(f"   Площадь пологих склонов: {gdf[gdf['class']==0]['area_deg2'].sum():.6f} deg²")
    print(f"   Площадь крутых склонов: {gdf[gdf['class']==1]['area_deg2'].sum():.6f} deg²")
    
    return gdf


def export_to_geojson(gdf, output_path='slope_clusters.geojson'):
    """
    Экспортирует GeoDataFrame в GeoJSON.
    
    Args:
        gdf (gpd.GeoDataFrame): GeoDataFrame с полигонами
        output_path (str): Путь для сохранения
    """
    gdf.to_file(output_path, driver='GeoJSON')
    print(f"[OK] GeoJSON сохранён: {output_path}")
    
    # Статистика
    print(f"\nСтатистика по файлу:")
    print(f"  Всего полигонов: {len(gdf)}")
    print(f"  Пологих: {len(gdf[gdf['class']==0])}")
    print(f"  Крутых: {len(gdf[gdf['class']==1])}")


def classify_cluster_export(terrain_dataset, threshold=0.1, variable='maximum_curvature', 
                            use_abs=True, min_pixels=10, simplify_tolerance=0.0001, 
                            output_path='slope_clusters.geojson'):
    """
    Полный pipeline: классификация → кластеризация → векторизация → экспорт.
    
    Args:
        terrain_dataset (xr.Dataset): Исходный dataset
        threshold (float): Порог классификации
        variable (str): Название переменной для классификации
        use_abs (bool): Использовать ли модуль значения
        min_pixels (int): Минимальный размер кластера
        simplify_tolerance (float): Упрощение геометрии
        output_path (str): Путь для GeoJSON
    
    Returns:
        gpd.GeoDataFrame: GeoDataFrame с полигонами
    """
    print("=" * 60)
    print(f"ПОЛНЫЙ PIPELINE: КЛАССИФИКАЦИЯ ПО '{variable}'")
    print("=" * 60)
    
    # 1. Классификация
    print("\n1. Классификация...")
    slope_classes = classify_slopes(terrain_dataset, threshold, variable, use_abs)
    stats = get_class_statistics(slope_classes)
    print(f"   Класс 0: {stats['gentle_percentage']:.1f}% | Класс 1: {stats['steep_percentage']:.1f}%")
    
    # 2. Кластеризация
    print("\n2. Кластеризация...")
    clustered_array, num_clusters = cluster_slopes(slope_classes, min_pixels)
    
    # 3. Векторизация
    print("\n3. Векторизация...")
    gdf = clusters_to_polygons(slope_classes, clustered_array, terrain_dataset, simplify_tolerance)
    
    # 4. Экспорт
    print("\n4. Экспорт...")
    export_to_geojson(gdf, output_path)
    
    print("\n" + "=" * 60)
    print("[OK] ГОТОВО!")
    print("=" * 60)
    
    return gdf


def plot_clustered_classification(clustered_array, terrain_dataset, figsize=(14, 7)):
    """
    Визуализирует кластеризованную классификацию.
    
    Args:
        clustered_array (np.ndarray): Массив с метками кластеров
        terrain_dataset (xr.Dataset): Dataset с координатами
        figsize (tuple): Размер фигуры
    """
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=figsize)
    
    # DEM
    if 'elevation' in terrain_dataset.data_vars:
        terrain_dataset['elevation'].plot(ax=ax1, cmap='terrain')
        ax1.set_title('Elevation (DEM)', fontsize=12, fontweight='bold')
    
    # Кластеры
    # Создаём цветовую карту: положительные - зелёные оттенки, отрицательные - красные
    im = ax2.imshow(clustered_array, cmap='RdYlGn', interpolation='nearest')
    ax2.set_title('Clustered Classification', fontsize=12, fontweight='bold')
    ax2.set_xlabel('Longitude')
    ax2.set_ylabel('Latitude')
    plt.colorbar(im, ax=ax2, label='Cluster ID (+ gentle, - steep)')
    
    plt.tight_layout()
    plt.show()


def plot_polygons_overlay(gdf, terrain_dataset, satellite_img=None, figsize=(12, 12)):
    """
    Визуализирует полигоны поверх DEM или спутникового снимка.
    
    Args:
        gdf (gpd.GeoDataFrame): GeoDataFrame с полигонами
        terrain_dataset (xr.Dataset): Dataset с координатами
        satellite_img (PIL.Image, optional): Спутниковый снимок
        figsize (tuple): Размер фигуры
    """
    fig, ax = plt.subplots(figsize=figsize)
    
    extent = [
        float(terrain_dataset.longitude.min()), 
        float(terrain_dataset.longitude.max()),
        float(terrain_dataset.latitude.min()), 
        float(terrain_dataset.latitude.max())
    ]
    
    # Фон: DEM или спутниковый снимок
    if satellite_img is not None:
        ax.imshow(satellite_img, extent=extent, alpha=0.7)
    elif 'elevation' in terrain_dataset.data_vars:
        terrain_dataset['elevation'].plot(ax=ax, cmap='terrain', alpha=0.7, add_colorbar=False)
    
    # Полигоны
    gdf[gdf['class'] == 0].plot(ax=ax, facecolor='green', edgecolor='darkgreen', 
                                 alpha=0.3, linewidth=1, label='Gentle slopes')
    gdf[gdf['class'] == 1].plot(ax=ax, facecolor='red', edgecolor='darkred', 
                                 alpha=0.3, linewidth=1, label='Steep slopes')
    
    ax.set_xlim(extent[0], extent[1])
    ax.set_ylim(extent[2], extent[3])
    ax.set_xlabel('Longitude', fontsize=12)
    ax.set_ylabel('Latitude', fontsize=12)
    ax.set_title('Slope Polygons Overlay', fontsize=14, fontweight='bold')
    ax.legend(loc='upper right', fontsize=12)
    
    plt.tight_layout()
    plt.show()