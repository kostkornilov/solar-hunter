#!/usr/bin/env python3
"""
Evaluate DEM Smoothing Filters

This script processes a DEM from a GeoJSON area and evaluates different smoothing filters
by comparing terrain features before and after smoothing.

Usage:
    python evaluate_smoothing_filters.py --geojson <path_to_geojson> [options]

Options:
    --geojson PATH              Path to GeoJSON file (required)
    --features FEAT1,FEAT2,...  Comma-separated list of features (default: reprojected_dem,slope,rugosity,terrain_ruggedness_index,aspect,roughness)
    --filters FILT1,FILT2,...   Comma-separated list of filters (default: gaussian,bilateral,feature_preserving)
    --output-dir PATH           Output directory (default: filter_evaluation_output)

Example:
    python evaluate_smoothing_filters.py --geojson examples/sample_jsons/point.geojson
"""

import argparse
import os
import sys
from pathlib import Path
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt
from matplotlib import colormaps
from scipy.ndimage import label
import rasterio
import warnings

# Setup paths for imports
SCRIPT_DIR = Path(__file__).parent
DEM_API_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(DEM_API_ROOT))

from src.data_download import initialize_gee, download_dem
from src.calculations import calculate_terrain_attributes, denoise_dem, denoise_dem_wbt


SUPPORTED_FILTERS = {
    'feature_preserving': 'feature_preserving',
    'adaptive': 'adaptive',
    'total_variation': 'total_variation',
    'tv': 'total_variation',
    'non_local_means': 'non_local_means',
    'nlm': 'non_local_means',
    'wavelet': 'wavelet',
    'anisotropic_diffusion': 'anisotropic_diffusion',
    'anisotropic': 'anisotropic_diffusion',
    'bilateral': 'bilateral',
    'gaussian': 'gaussian',
}


WRAPPER_FILTERS = {'feature_preserving', 'bilateral', 'gaussian'}


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Evaluate DEM smoothing filters',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__
    )
    
    parser.add_argument(
        '--geojson',
        type=str,
        required=False,
        help='Path to GeoJSON file (required if --dem not provided)'
    )
    
    parser.add_argument(
        '--dem',
        type=str,
        required=False,
        help='Path to existing DEM file (alternative to --geojson)'
    )
    
    parser.add_argument(
        '--features',
        type=str,
        default='reprojected_dem,slope,rugosity,terrain_ruggedness_index,aspect,roughness',
        help='Comma-separated list of features to analyze'
    )
    
    parser.add_argument(
        '--filters',
        type=str,
        default='gaussian,bilateral,feature_preserving',
        help='Comma-separated list of filters to test'
    )
    
    parser.add_argument(
        '--output-dir',
        type=str,
        default='filter_evaluation_output',
        help='Output directory for results'
    )
    
    args = parser.parse_args()
    
    # Validate: either geojson or dem must be provided
    if not args.geojson and not args.dem:
        parser.error("Either --geojson or --dem must be provided")
    
    # Parse comma-separated lists
    args.features = [f.strip() for f in args.features.split(',')]
    args.filters = [f.strip() for f in args.filters.split(',')]
    
    return args


def validate_dem_matches_geojson(dem_path, geojson_path, tolerance=0.001):
    """
    Check if existing DEM matches the GeoJSON coordinates.
    
    Args:
        dem_path: Path to DEM file
        geojson_path: Path to GeoJSON file
        tolerance: Tolerance for coordinate comparison (degrees)
        
    Returns:
        tuple: (matches: bool, message: str)
    """
    import json
    import rasterio
    
    try:
        # Read GeoJSON coordinates
        with open(geojson_path, 'r', encoding='utf-8') as f:
            geojson_data = json.load(f)
        
        feature = geojson_data['features'][0]
        geometry = feature['geometry']
        geom_type = geometry['type']
        
        if geom_type == 'Point':
            geojson_lon, geojson_lat = geometry['coordinates']
            geojson_bounds = {
                'lon1': geojson_lon,
                'lat1': geojson_lat,
                'lon2': geojson_lon,
                'lat2': geojson_lat
            }
        elif geom_type == 'Polygon':
            coords = geometry['coordinates'][0]
            lons = [c[0] for c in coords]
            lats = [c[1] for c in coords]
            geojson_bounds = {
                'lon1': min(lons),
                'lat1': min(lats),
                'lon2': max(lons),
                'lat2': max(lats)
            }
        else:
            return False, f"Unsupported geometry type: {geom_type}"
        
        # Read DEM tags
        with rasterio.open(dem_path) as src:
            tags = src.tags()
            if not all(k in tags for k in ['original_lon1', 'original_lat1', 'original_lon2', 'original_lat2']):
                return False, "DEM missing original_bounds tags"
            
            dem_bounds = {
                'lon1': float(tags['original_lon1']),
                'lat1': float(tags['original_lat1']),
                'lon2': float(tags['original_lon2']),
                'lat2': float(tags['original_lat2'])
            }
        
        # Compare bounds
        lon1_diff = abs(geojson_bounds['lon1'] - dem_bounds['lon1'])
        lat1_diff = abs(geojson_bounds['lat1'] - dem_bounds['lat1'])
        lon2_diff = abs(geojson_bounds['lon2'] - dem_bounds['lon2'])
        lat2_diff = abs(geojson_bounds['lat2'] - dem_bounds['lat2'])
        
        max_diff = max(lon1_diff, lat1_diff, lon2_diff, lat2_diff)
        
        if max_diff > tolerance:
            return False, (f"DEM bounds mismatch (max diff: {max_diff:.6f} degrees):\n"
                          f"  GeoJSON: lon [{geojson_bounds['lon1']:.6f}, {geojson_bounds['lon2']:.6f}], "
                          f"lat [{geojson_bounds['lat1']:.6f}, {geojson_bounds['lat2']:.6f}]\n"
                          f"  DEM tags: lon [{dem_bounds['lon1']:.6f}, {dem_bounds['lon2']:.6f}], "
                          f"lat [{dem_bounds['lat1']:.6f}, {dem_bounds['lat2']:.6f}]")
        
        return True, "DEM matches GeoJSON coordinates"
        
    except Exception as e:
        return False, f"Error validating DEM: {e}"


def load_or_use_dem(geojson_path, dem_path, output_dir):
    """
    Load DEM from GeoJSON area or use existing DEM.
    Automatically re-downloads DEM if cached version doesn't match GeoJSON coordinates.
    
    Args:
        geojson_path: Path to GeoJSON file (or None)
        dem_path: Path to existing DEM (or None)
        output_dir: Directory to store DEM file
        
    Returns:
        Path to DEM file
    """
    if dem_path:
        print("=" * 80)
        print("USING EXISTING DEM")
        print("=" * 80)
        print(f"DEM path: {dem_path}")
        
        if not os.path.exists(dem_path):
            raise FileNotFoundError(f"DEM file not found: {dem_path}")
        
        return dem_path
    else:
        # Setup paths
        dem_dir = os.path.join(output_dir, 'dem_data')
        os.makedirs(dem_dir, exist_ok=True)
        dem_filename = "raw_dem.tif"
        cached_dem_path = os.path.join(dem_dir, dem_filename)
        
        # Check if cached DEM exists and matches GeoJSON
        if os.path.exists(cached_dem_path):
            print("=" * 80)
            print("CHECKING CACHED DEM")
            print("=" * 80)
            print(f"Found cached DEM: {cached_dem_path}")
            
            matches, message = validate_dem_matches_geojson(cached_dem_path, geojson_path)
            print(f"Validation: {message}")
            
            if matches:
                print("[OK] Using cached DEM (coordinates match)")
                return cached_dem_path
            else:
                print("[WARNING] Cached DEM doesn't match GeoJSON - will re-download")
                print(f"Removing old DEM: {cached_dem_path}")
                os.remove(cached_dem_path)
        
        # Load DEM from GeoJSON area
        print("=" * 80)
        print("LOADING DEM FROM GEOJSON")
        print("=" * 80)
        
        # Initialize GEE
        print("Initializing Google Earth Engine...")
        initialize_gee(project='projectomela')
        
        # Download DEM
        print(f"Downloading DEM from GeoJSON: {geojson_path}")
        download_dem(geojson_path, dem_filename, dem_dir)
        
        print(f"[OK] DEM saved to: {cached_dem_path}")
        
        return cached_dem_path


def calculate_features(dem_path, feature_names):
    """
    Calculate terrain features from DEM.
    
    Args:
        dem_path: Path to DEM file
        feature_names: List of feature names to calculate
        
    Returns:
        xarray.Dataset with calculated features
    """
    print(f"\nCalculating {len(feature_names)} features...")
    
    # Remove 'reprojected_dem' from calculation list as it's returned by default
    calc_features = [f for f in feature_names if f != 'reprojected_dem']
    
    # Calculate terrain attributes
    dataset = calculate_terrain_attributes(dem_path, calc_features)
    
    print(f"[OK] Features calculated: {list(dataset.data_vars)}")
    
    return dataset


def apply_filter(dem_path, filter_name, output_path):
    """
    Apply smoothing filter to DEM.
    
    Args:
        filter_name: Name of filter ('gaussian', 'bilateral', 'feature_preserving')
        dem_path: Path to input DEM
        output_path: Path for output smoothed DEM
        
    Returns:
        Path to smoothed DEM file
    """
    print(f"\n  Applying {filter_name} filter...")
    
    normalized_name = filter_name.strip().lower()
    method = SUPPORTED_FILTERS.get(normalized_name)

    if method is None:
        valid_filters = sorted(SUPPORTED_FILTERS.keys())
        raise ValueError(
            f"Unknown filter: {filter_name}. Supported filters: {', '.join(valid_filters)}"
        )

    # denoise_dem wrapper supports only a subset; route other methods via denoise_dem_wbt.
    if method in WRAPPER_FILTERS:
        smoothed_path = denoise_dem(
            dem_path=dem_path,
            output_path=output_path,
            method=method
        )
    else:
        smoothed_path = denoise_dem_wbt(
            dem_path=dem_path,
            output_path=output_path,
            method=method
        )
    
    print(f"  [OK] Smoothed DEM saved: {smoothed_path}")
    
    return smoothed_path


def calculate_morans_i_binary(binary_mask):
    """
    Calculate Moran's I for a binary mask using simple approach.
    
    Args:
        binary_mask: 2D numpy array with binary values
        
    Returns:
        Moran's I value
    """
    # Flatten and remove NaN
    data = binary_mask.flatten()
    valid_mask = ~np.isnan(data)
    data_clean = data[valid_mask].astype(float)
    
    if len(data_clean) < 3:
        return np.nan
    
    # Simple Moran's I calculation
    n = len(data_clean)
    mean_val = np.mean(data_clean)
    
    # Deviation from mean
    deviations = data_clean - mean_val
    
    # Simple spatial weights (assume grid structure)
    # This is a simplified version - proper calculation would use spatial weights matrix
    sum_squared_dev = np.sum(deviations ** 2)
    
    if sum_squared_dev == 0:
        return np.nan
    
    # Autocorrelation (simplified)
    # For binary data, calculate how often adjacent cells have same value
    reshaped = binary_mask.copy()
    autocorr = 0
    count = 0
    
    # Check horizontal neighbors
    for i in range(reshaped.shape[0]):
        for j in range(reshaped.shape[1] - 1):
            if not np.isnan(reshaped[i, j]) and not np.isnan(reshaped[i, j+1]):
                autocorr += (reshaped[i, j] - mean_val) * (reshaped[i, j+1] - mean_val)
                count += 1
    
    # Check vertical neighbors
    for i in range(reshaped.shape[0] - 1):
        for j in range(reshaped.shape[1]):
            if not np.isnan(reshaped[i, j]) and not np.isnan(reshaped[i+1, j]):
                autocorr += (reshaped[i, j] - mean_val) * (reshaped[i+1, j] - mean_val)
                count += 1
    
    if count == 0:
        return np.nan
    
    # Moran's I formula
    morans_i = (n / count) * (autocorr / sum_squared_dev)
    
    return morans_i


def compute_statistics(raw_data, smooth_data, feature_name):
    """
    Compute statistics comparing raw and smoothed data.
    
    Args:
        raw_data: Raw feature data (2D numpy array)
        smooth_data: Smoothed feature data (2D numpy array)
        feature_name: Name of the feature
        
    Returns:
        Dictionary with statistics
    """
    # Remove NaN values for calculations
    raw_valid = raw_data[~np.isnan(raw_data)]
    smooth_valid = smooth_data[~np.isnan(smooth_data)]
    
    if len(raw_valid) == 0 or len(smooth_valid) == 0:
        return {
            'feature_name': feature_name,
            'n_components_raw': np.nan,
            'n_components_smooth': np.nan,
            'n_comp_change_pct': np.nan,
            'morans_i_binary_raw': np.nan,
            'morans_i_binary_smooth': np.nan,
            'morans_i_change_pct': np.nan,
            'peak_retention_pct': np.nan
        }
    
    # Calculate 50% quantile threshold on RAW data
    threshold = np.nanquantile(raw_valid, 0.5)
    
    # Create binary masks
    raw_binary = (raw_data >= threshold).astype(float)
    smooth_binary = (smooth_data >= threshold).astype(float)
    
    # Set NaN values to NaN in binary masks
    raw_binary[np.isnan(raw_data)] = np.nan
    smooth_binary[np.isnan(smooth_data)] = np.nan
    
    # Count connected components (8-connectivity)
    raw_binary_clean = np.nan_to_num(raw_binary, 0).astype(int)
    smooth_binary_clean = np.nan_to_num(smooth_binary, 0).astype(int)
    
    _, n_comp_raw = label(raw_binary_clean, structure=np.ones((3, 3)))
    _, n_comp_smooth = label(smooth_binary_clean, structure=np.ones((3, 3)))
    
    # Calculate component change percentage
    if n_comp_raw > 0:
        n_comp_change = ((n_comp_smooth - n_comp_raw) / n_comp_raw) * 100
    else:
        n_comp_change = np.nan
    
    # Calculate Moran's I on binary masks
    morans_raw = calculate_morans_i_binary(raw_binary)
    morans_smooth = calculate_morans_i_binary(smooth_binary)
    
    # Calculate Moran's I change percentage
    if not np.isnan(morans_raw) and morans_raw != 0:
        morans_change = ((morans_smooth - morans_raw) / abs(morans_raw)) * 100
    else:
        morans_change = np.nan
    
    # Calculate peak retention
    raw_range = np.nanmax(raw_valid) - np.nanmin(raw_valid)
    smooth_range = np.nanmax(smooth_valid) - np.nanmin(smooth_valid)
    
    if raw_range > 0:
        peak_retention = (smooth_range / raw_range) * 100
    else:
        peak_retention = np.nan
    
    return {
        'feature_name': feature_name,
        'n_components_raw': n_comp_raw,
        'n_components_smooth': n_comp_smooth,
        'n_comp_change_pct': n_comp_change,
        'morans_i_binary_raw': morans_raw,
        'morans_i_binary_smooth': morans_smooth,
        'morans_i_change_pct': morans_change,
        'peak_retention_pct': peak_retention
    }


def create_threshold_comparison_plot(raw_data, smooth_data, feature_name, output_path):
    """
    Create 2x3 grid showing binary masks at 25%, 50%, 75% quantiles.
    Top row: RAW, Bottom row: SMOOTHED.
    Quantiles calculated on RAW and applied to both.
    
    Args:
        raw_data: Raw feature data (2D array)
        smooth_data: Smoothed feature data (2D array)
        feature_name: Name of feature
        output_path: Path to save plot
    """
    # Calculate quantiles on RAW data
    raw_valid = raw_data[~np.isnan(raw_data)]
    q25 = np.nanquantile(raw_valid, 0.25)
    q50 = np.nanquantile(raw_valid, 0.50)
    q75 = np.nanquantile(raw_valid, 0.75)
    
    quantiles = [(q25, '25%'), (q50, '50%'), (q75, '75%')]
    
    # Create figure
    fig, axes = plt.subplots(2, 3, figsize=(15, 10))
    fig.suptitle(f'{feature_name}: Threshold Comparison (Raw vs Smoothed)', 
                 fontsize=14, fontweight='bold')
    
    for col, (threshold, label) in enumerate(quantiles):
        # RAW (top row)
        ax_raw = axes[0, col]
        raw_binary = (raw_data >= threshold).astype(float)
        raw_binary[np.isnan(raw_data)] = np.nan
        
        im_raw = ax_raw.imshow(raw_binary, cmap='RdYlBu_r', vmin=0, vmax=1, 
                               interpolation='nearest')
        ax_raw.set_title(f'RAW - {label} (≥{threshold:.3f})')
        ax_raw.axis('off')
        plt.colorbar(im_raw, ax=ax_raw, fraction=0.046, pad=0.04)
        
        # SMOOTHED (bottom row)
        ax_smooth = axes[1, col]
        smooth_binary = (smooth_data >= threshold).astype(float)
        smooth_binary[np.isnan(smooth_data)] = np.nan
        
        im_smooth = ax_smooth.imshow(smooth_binary, cmap='RdYlBu_r', vmin=0, vmax=1,
                                     interpolation='nearest')
        ax_smooth.set_title(f'SMOOTH - {label} (≥{threshold:.3f})')
        ax_smooth.axis('off')
        plt.colorbar(im_smooth, ax=ax_smooth, fraction=0.046, pad=0.04)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    print(f"    [OK] Saved: {output_path}")


def create_raster_comparison_plot(raw_data, smooth_data, feature_name, output_path):
    """
    Create side-by-side comparison of continuous rasters with shared color scale.
    
    Args:
        raw_data: Raw feature data (2D array)
        smooth_data: Smoothed feature data (2D array)
        feature_name: Name of feature
        output_path: Path to save plot
    """
    # Calculate shared vmin/vmax
    vmin = min(np.nanmin(raw_data), np.nanmin(smooth_data))
    vmax = max(np.nanmax(raw_data), np.nanmax(smooth_data))
    
    # Create figure
    fig, axes = plt.subplots(1, 2, figsize=(14, 6))
    fig.suptitle(f'{feature_name}: Raster Comparison', fontsize=14, fontweight='bold')
    
    # RAW
    im1 = axes[0].imshow(raw_data, cmap='terrain', vmin=vmin, vmax=vmax,
                        interpolation='nearest')
    axes[0].set_title('RAW')
    axes[0].axis('off')
    plt.colorbar(im1, ax=axes[0], fraction=0.046, pad=0.04)
    
    # SMOOTHED
    im2 = axes[1].imshow(smooth_data, cmap='terrain', vmin=vmin, vmax=vmax,
                        interpolation='nearest')
    axes[1].set_title('SMOOTHED')
    axes[1].axis('off')
    plt.colorbar(im2, ax=axes[1], fraction=0.046, pad=0.04)
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    print(f"    [OK] Saved: {output_path}")


def create_difference_map(raw_data, smooth_data, feature_name, output_path):
    """
    Create difference map (Raw - Smooth) showing removed noise vs lost signal.
    
    Args:
        raw_data: Raw feature data (2D array)
        smooth_data: Smoothed feature data (2D array)
        feature_name: Name of feature
        output_path: Path to save plot
    """
    # Check if shapes match
    if raw_data.shape != smooth_data.shape:
        print(f"    [WARNING] Shape mismatch detected!")
        print(f"    Raw shape: {raw_data.shape}, Smooth shape: {smooth_data.shape}")
        print(f"    Skipping difference map creation for {feature_name}")
        # Create a placeholder file to indicate skipped
        fig, ax = plt.subplots(figsize=(10, 8))
        ax.text(0.5, 0.5, f'Shape mismatch:\nRaw: {raw_data.shape}\nSmooth: {smooth_data.shape}\nDifference map skipped',
                ha='center', va='center', fontsize=14, color='red')
        ax.axis('off')
        plt.tight_layout()
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        return
    
    # Calculate difference
    difference = raw_data - smooth_data
    
    # Use diverging colormap centered at zero
    abs_max = np.nanmax(np.abs(difference))
    
    # Create figure
    fig, ax = plt.subplots(figsize=(10, 8))
    fig.suptitle(f'{feature_name}: Difference Map (Raw - Smooth)', 
                 fontsize=14, fontweight='bold')
    
    im = ax.imshow(difference, cmap='RdBu_r', vmin=-abs_max, vmax=abs_max,
                   interpolation='nearest')
    ax.set_title('Positive = Raw higher (removed), Negative = Smooth higher (added)')
    ax.axis('off')
    
    cbar = plt.colorbar(im, ax=ax, fraction=0.046, pad=0.04)
    cbar.set_label('Difference (Raw - Smooth)')
    
    plt.tight_layout()
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    plt.close()
    
    print(f"    [OK] Saved: {output_path}")


def process_filter(dem_path, filter_name, feature_names, base_output_dir):
    """
    Process a single filter: apply it, calculate features, generate statistics and plots.
    
    Args:
        dem_path: Path to raw DEM
        filter_name: Name of filter to apply
        feature_names: List of features to analyze
        base_output_dir: Base output directory
        
    Returns:
        DataFrame with statistics
    """
    print("\n" + "=" * 80)
    print(f"PROCESSING FILTER: {filter_name.upper()}")
    print("=" * 80)
    
    # Create filter output directory
    filter_dir = os.path.join(base_output_dir, filter_name)
    os.makedirs(filter_dir, exist_ok=True)
    
    # Apply filter to DEM
    smoothed_dem_path = os.path.join(filter_dir, f'smoothed_dem_{filter_name}.tif')
    smoothed_dem_path = apply_filter(dem_path, filter_name, smoothed_dem_path)
    
    # Calculate features for RAW DEM
    print("\nCalculating RAW features...")
    raw_dataset = calculate_features(dem_path, feature_names)
    
    # Calculate features for SMOOTHED DEM
    print("\nCalculating SMOOTHED features...")
    smooth_dataset = calculate_features(smoothed_dem_path, feature_names)
    
    # Initialize statistics list
    stats_list = []
    
    # Process each feature
    for feature in feature_names:
        print(f"\n  Processing feature: {feature}")
        
        # Create feature directory
        feature_dir = os.path.join(filter_dir, feature)
        os.makedirs(feature_dir, exist_ok=True)
        
        # Get data
        raw_data = raw_dataset[feature].values
        smooth_data = smooth_dataset[feature].values
        
        # Compute statistics
        stats = compute_statistics(raw_data, smooth_data, feature)
        stats_list.append(stats)
        
        # Create plots
        print(f"    Creating plots for {feature}...")
        
        # Threshold comparison
        threshold_path = os.path.join(feature_dir, 'threshold_comparison.png')
        create_threshold_comparison_plot(raw_data, smooth_data, feature, threshold_path)
        
        # Raster comparison
        raster_path = os.path.join(feature_dir, 'raster_comparison.png')
        create_raster_comparison_plot(raw_data, smooth_data, feature, raster_path)
        
        # Difference map
        diff_path = os.path.join(feature_dir, 'difference_map.png')
        create_difference_map(raw_data, smooth_data, feature, diff_path)
    
    # Save statistics to CSV
    stats_df = pd.DataFrame(stats_list)
    csv_path = os.path.join(filter_dir, 'statistics.csv')
    stats_df.to_csv(csv_path, index=False, float_format='%.4f')
    print(f"\n[OK] Statistics saved to: {csv_path}")
    
    return stats_df


def main():
    """Main execution function."""
    # Parse arguments
    args = parse_arguments()
    
    print("\n" + "=" * 80)
    print("DEM SMOOTHING FILTER EVALUATION")
    print("=" * 80)
    if args.dem:
        print(f"\nDEM: {args.dem}")
    else:
        print(f"\nGeoJSON: {args.geojson}")
    print(f"Features: {', '.join(args.features)}")
    print(f"Filters: {', '.join(args.filters)}")
    print(f"Output directory: {args.output_dir}")
    
    # Create output directory
    output_dir = os.path.join(SCRIPT_DIR, args.output_dir)
    os.makedirs(output_dir, exist_ok=True)
    
    # Load or use DEM
    dem_path = load_or_use_dem(args.geojson, args.dem, output_dir)
    
    # Process each filter
    all_stats = {}
    for filter_name in args.filters:
        stats_df = process_filter(dem_path, filter_name, args.features, output_dir)
        all_stats[filter_name] = stats_df
    
    # Create summary
    print("\n" + "=" * 80)
    print("EVALUATION COMPLETE")
    print("=" * 80)
    print(f"\nResults saved to: {output_dir}")
    print("\nSummary:")
    for filter_name in args.filters:
        filter_dir = os.path.join(output_dir, filter_name)
        print(f"  - {filter_name}: {filter_dir}")
        print(f"    - statistics.csv")
        for feature in args.features:
            print(f"    - {feature}/")
            print(f"      - threshold_comparison.png")
            print(f"      - raster_comparison.png")
            print(f"      - difference_map.png")
    
    print("\n[OK] Evaluation completed successfully!")


if __name__ == "__main__":
    # Suppress warnings for cleaner output
    warnings.filterwarnings('ignore')
    
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        print(f"\n\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

