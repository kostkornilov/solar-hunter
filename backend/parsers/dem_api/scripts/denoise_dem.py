"""
Скрипт для загрузки DEM и применения сглаживания с помощью denoise_dem_wbt.

Использование:
    python denoise_dem.py <geojson_path> [--filter-type TYPE] [--output-dir DIR]
    
Пример:
    python denoise_dem.py sample.geojson --filter-type feature_preserving --output-dir ./output
"""

import sys
import argparse
from pathlib import Path
import numpy as np
import rasterio

# Добавляем путь к модулям dem_api
DEM_API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEM_API_ROOT))

from src.data_download import download_dem, initialize_gee
from src.calculations import denoise_dem_wbt, denoise_dem_bilateral, denoise_dem_gaussian


def denoise_dem_with_filter(dem_path, output_path, filter_type='feature_preserving', **filter_params):
    """
    Применение сглаживания к DEM с выбранным типом фильтра.
    
    Args:
        dem_path: путь к входному DEM
        output_path: путь для сохранения результата
        filter_type: тип фильтра ('feature_preserving', 'bilateral', 'gaussian')
        **filter_params: дополнительные параметры фильтра
        
    Returns:
        путь к сглаженному DEM
    """
    if filter_type == 'feature_preserving':
        # Параметры по умолчанию для feature-preserving фильтра
        params = {
            'filter_size': filter_params.get('filter_size', 11),
            'norm_diff': filter_params.get('norm_diff', 15.0),
            'num_iter': filter_params.get('num_iter', 3),
        }
        return denoise_dem_wbt(dem_path, output_path, **params)
    
    elif filter_type == 'bilateral':
        params = {
            'sigma_dist': filter_params.get('sigma_dist', 0.75),
            'sigma_int': filter_params.get('sigma_int', 1.0),
        }
        return denoise_dem_bilateral(dem_path, output_path, **params)
    
    elif filter_type == 'gaussian':
        params = {
            'sigma': filter_params.get('sigma', 0.75),
        }
        return denoise_dem_gaussian(dem_path, output_path, **params)
    
    else:
        raise ValueError(f"Неизвестный тип фильтра: {filter_type}. "
                        f"Допустимые значения: feature_preserving, bilateral, gaussian")


def main():
    parser = argparse.ArgumentParser(
        description='Загрузка DEM и применение сглаживания',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Примеры использования:
  # По умолчанию (feature_preserving фильтр)
  python denoise_dem.py sample.geojson
  
  # С указанием типа фильтра
  python denoise_dem.py sample.geojson --filter-type bilateral
  
  # С указанием директории для результатов
  python denoise_dem.py sample.geojson --output-dir ./results
  
  # С указанием GEE проекта и источника DEM
  python denoise_dem.py sample.geojson --gee-project my-project --dem-source SRTM
        """
    )
    
    parser.add_argument('geojson', type=str, help='Путь к GeoJSON файлу с координатами области')
    parser.add_argument('--filter-type', type=str, default='feature_preserving',
                       choices=['feature_preserving', 'bilateral', 'gaussian'],
                       help='Тип фильтра для сглаживания (по умолчанию: feature_preserving)')
    parser.add_argument('--output-dir', type=str, default='./output',
                       help='Директория для сохранения результатов (по умолчанию: ./output)')
    parser.add_argument('--gee-project', type=str, default=None,
                       help='ID проекта Google Earth Engine (если не указан, используется настройка по умолчанию)')
    parser.add_argument('--dem-source', type=str, default='COPERNICUS',
                       choices=['COPERNICUS', 'SRTM'],
                       help='Источник DEM (по умолчанию: COPERNICUS)')
    parser.add_argument('--buffer-pixels', type=int, default=45,
                       help='Количество пикселей буфера вокруг области (по умолчанию: 45)')
    
    # Параметры для feature_preserving фильтра
    parser.add_argument('--filter-size', type=int, default=11,
                       help='Размер фильтра для feature_preserving (по умолчанию: 11)')
    parser.add_argument('--norm-diff', type=float, default=15.0,
                       help='Максимальная разница нормалей для feature_preserving (по умолчанию: 15.0)')
    parser.add_argument('--num-iter', type=int, default=3,
                       help='Количество итераций для feature_preserving (по умолчанию: 3)')
    
    # Параметры для bilateral фильтра
    parser.add_argument('--sigma-dist', type=float, default=0.75,
                       help='Sigma для пространственной компоненты bilateral (по умолчанию: 0.75)')
    parser.add_argument('--sigma-int', type=float, default=1.0,
                       help='Sigma для интенсивности bilateral (по умолчанию: 1.0)')
    
    # Параметры для gaussian фильтра
    parser.add_argument('--sigma', type=float, default=0.75,
                       help='Sigma для gaussian фильтра (по умолчанию: 0.75)')
    
    args = parser.parse_args()
    
    # Проверка существования GeoJSON файла
    geojson_path = Path(args.geojson)
    if not geojson_path.exists():
        print(f"ОШИБКА: Файл {geojson_path} не найден")
        sys.exit(1)
    
    # Создание директории для результатов
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    
    print("=" * 80)
    print("СКРИПТ ЗАГРУЗКИ И СГЛАЖИВАНИЯ DEM")
    print("=" * 80)
    print(f"\nВходной GeoJSON: {geojson_path}")
    print(f"Тип фильтра: {args.filter_type}")
    print(f"Директория результатов: {output_dir}")
    print(f"Источник DEM: {args.dem_source}")
    
    # Шаг 1: Инициализация GEE
    print("\n" + "-" * 80)
    print("ШАГ 1: Инициализация Google Earth Engine")
    print("-" * 80)
    
    if args.gee_project:
        initialize_gee(args.gee_project)
    else:
        try:
            import ee
            ee.Initialize()
            print("Google Earth Engine успешно инициализирован (проект по умолчанию)")
        except Exception as e:
            print(f"ПРЕДУПРЕЖДЕНИЕ: Не удалось инициализировать GEE: {e}")
            print("Попробуйте указать --gee-project или выполните 'earthengine authenticate'")
    
    # Шаг 2: Загрузка DEM
    print("\n" + "-" * 80)
    print("ШАГ 2: Загрузка DEM из Google Earth Engine")
    print("-" * 80)
    
    original_dem_path = output_dir / f"dem_original_{args.dem_source.lower()}.tif"
    
    try:
        download_dem(
            geo_json_path=str(geojson_path),
            filename=original_dem_path.name,
            directory=str(output_dir),
            buffer_pixels=args.buffer_pixels,
            dem_source=args.dem_source
        )
        print(f"\n✓ DEM успешно загружен: {original_dem_path}")
    except Exception as e:
        print(f"\nОШИБКА при загрузке DEM: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    
    # Применение сглаживания
    print("\n" + "-" * 80)
    print(f"ШАГ 4: Применение фильтра '{args.filter_type}'")
    print("-" * 80)
    
    denoised_dem_path = output_dir / f"dem_denoised_{args.filter_type}.tif"
    
    # Собираем параметры фильтра
    filter_params = {}
    if args.filter_type == 'feature_preserving':
        filter_params = {
            'filter_size': args.filter_size,
            'norm_diff': args.norm_diff,
            'num_iter': args.num_iter,
        }
        print(f"\nПараметры фильтра:")
        print(f"  filter_size: {filter_params['filter_size']}")
        print(f"  norm_diff: {filter_params['norm_diff']}")
        print(f"  num_iter: {filter_params['num_iter']}")
    elif args.filter_type == 'bilateral':
        filter_params = {
            'sigma_dist': args.sigma_dist,
            'sigma_int': args.sigma_int,
        }
        print(f"\nПараметры фильтра:")
        print(f"  sigma_dist: {filter_params['sigma_dist']}")
        print(f"  sigma_int: {filter_params['sigma_int']}")
    elif args.filter_type == 'gaussian':
        filter_params = {
            'sigma': args.sigma,
        }
        print(f"\nПараметры фильтра:")
        print(f"  sigma: {filter_params['sigma']}")
    
    try:
        result_path = denoise_dem_with_filter(
            str(original_dem_path),
            str(denoised_dem_path),
            args.filter_type,
            **filter_params
        )
        print(f"\n✓ Сглаживание выполнено: {result_path}")
    except Exception as e:
        print(f"\nОШИБКА при применении фильтра: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
