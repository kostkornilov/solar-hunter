"""
Simple test script for WhiteboxTools DEM denoising (ASCII-only for Windows).
"""
import sys
from pathlib import Path

# Set UTF-8 encoding for console output
import io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

import os
import numpy as np
import rasterio

# Ensure local dem_api package imports work
DEM_API_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(DEM_API_ROOT))

from src.calculations import (
    denoise_dem_wbt, 
    denoise_dem_bilateral, 
    denoise_dem_gaussian,
    denoise_dem,
    calculate_terrain_attributes
)

# Test configuration
TEST_DEM = DEM_API_ROOT / 'examples' / 'example_output' / 'srtm.tif'
OUTPUT_DIR = Path(__file__).parent / 'test_output'

OUTPUT_DIR.mkdir(exist_ok=True)

print("=" * 70)
print("WHITEBOX TOOLS DEM DENOISING TESTS")
print("=" * 70)
print(f"\nTest DEM: {TEST_DEM}")
print(f"Exists: {TEST_DEM.exists()}")
print(f"Output directory: {OUTPUT_DIR}\n")

if not TEST_DEM.exists():
    print(f"[ERROR] Test DEM not found at {TEST_DEM}")
    sys.exit(1)


def test_basic_functionality():
    """Test 1: Basic functionality"""
    print("\n" + "=" * 70)
    print("TEST 1: Basic Functionality")
    print("=" * 70)
    
    output_path = OUTPUT_DIR / 'test_basic.tif'
    
    try:
        print("\nRunning denoise_dem_wbt()...")
        result = denoise_dem_wbt(str(TEST_DEM), str(output_path))
        
        assert Path(result).exists(), "Output file not created"
        assert output_path.stat().st_size > 0, "Output file is empty"
        
        print("[PASSED] Function executed successfully")
        print(f"   Output: {result}")
        print(f"   Size: {output_path.stat().st_size / 1024:.1f} KB")
        return True
        
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback
        traceback.print_exc()
        return False


def test_noise_reduction():
    """Test 2: Check noise reduction quality"""
    print("\n" + "=" * 70)
    print("TEST 2: Noise Reduction Quality")
    print("=" * 70)
    
    output_path = OUTPUT_DIR / 'test_quality.tif'
    
    try:
        denoise_dem_wbt(str(TEST_DEM), str(output_path), filter_size=11, norm_diff=15.0, num_iter=3)
        
        # Read original data
        with rasterio.open(TEST_DEM) as src:
            original = src.read(1).astype(float)
            original[original == src.nodata] = np.nan
        
        # Read denoised data
        with rasterio.open(output_path) as src:
            denoised = src.read(1).astype(float)
            denoised[denoised == src.nodata] = np.nan
        
        # Calculate statistics
        orig_mean = np.nanmean(original)
        orig_std = np.nanstd(original)
        orig_min = np.nanmin(original)
        orig_max = np.nanmax(original)
        
        den_mean = np.nanmean(denoised)
        den_std = np.nanstd(denoised)
        den_min = np.nanmin(denoised)
        den_max = np.nanmax(denoised)
        
        # Calculate gradient magnitude
        grad_y_orig, grad_x_orig = np.gradient(original)
        grad_mag_orig = np.sqrt(grad_x_orig**2 + grad_y_orig**2)
        grad_std_orig = np.nanstd(grad_mag_orig)
        
        grad_y_den, grad_x_den = np.gradient(denoised)
        grad_mag_den = np.sqrt(grad_x_den**2 + grad_y_den**2)
        grad_std_den = np.nanstd(grad_mag_den)
        
        print(f"\nOriginal DEM Statistics:")
        print(f"   Mean: {orig_mean:.2f} m")
        print(f"   Std Dev: {orig_std:.2f} m")
        print(f"   Range: [{orig_min:.2f}, {orig_max:.2f}] m")
        print(f"   Gradient Std: {grad_std_orig:.4f}")
        
        print(f"\nDenoised DEM Statistics:")
        print(f"   Mean: {den_mean:.2f} m")
        print(f"   Std Dev: {den_std:.2f} m")
        print(f"   Range: [{den_min:.2f}, {den_max:.2f}] m")
        print(f"   Gradient Std: {grad_std_den:.4f}")
        
        print(f"\nChanges:")
        print(f"   Mean difference: {abs(den_mean - orig_mean):.2f} m ({abs(den_mean - orig_mean)/orig_mean*100:.2f}%)")
        print(f"   Std Dev change: {den_std - orig_std:.2f} m ({(den_std - orig_std)/orig_std*100:.1f}%)")
        print(f"   Gradient Std change: {grad_std_den - grad_std_orig:.4f} ({(grad_std_den - grad_std_orig)/grad_std_orig*100:.1f}%)")
        
        # Check that range is preserved (within 5%)
        range_preserved = (den_min >= orig_min - abs(orig_min) * 0.05 and 
                          den_max <= orig_max + abs(orig_max) * 0.05)
        
        print(f"\nQuality Checks:")
        print(f"   Value range preserved: {'[PASS]' if range_preserved else '[FAIL]'}")
        print(f"   Gradient smoothed: {'[PASS]' if grad_std_den < grad_std_orig else '[UNEXPECTED]'}")
        
        print("\n[PASSED] Quality metrics calculated")
        return True
        
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback
        traceback.print_exc()
        return False


def test_parameter_variations():
    """Test 3: Test different parameters"""
    print("\n" + "=" * 70)
    print("TEST 3: Parameter Variations")
    print("=" * 70)
    
    params = [
        (5, 10.0, 1, "small_filter_conservative"),
        (11, 15.0, 3, "default_parameters"),
        (21, 25.0, 5, "large_filter_aggressive"),
    ]
    
    results = []
    
    for filter_size, norm_diff, num_iter, name in params:
        output_path = OUTPUT_DIR / f'test_params_{name}.tif'
        
        try:
            print(f"\n  Testing: {name}")
            print(f"    filter_size={filter_size}, norm_diff={norm_diff}, num_iter={num_iter}")
            
            result = denoise_dem_wbt(
                str(TEST_DEM), 
                str(output_path),
                filter_size=filter_size,
                norm_diff=norm_diff,
                num_iter=num_iter
            )
            
            assert Path(result).exists()
            
            with rasterio.open(output_path) as src:
                data = src.read(1).astype(float)
                data[data == src.nodata] = np.nan
                std = np.nanstd(data)
            
            print(f"    [SUCCESS] Std Dev: {std:.2f}")
            results.append((name, True, std))
            
        except Exception as e:
            print(f"    [FAILED] {e}")
            results.append((name, False, None))
    
    # Summary
    print(f"\nParameter Test Summary:")
    for name, success, std in results:
        status = "[PASS]" if success else "[FAIL]"
        std_str = f"(std={std:.2f})" if std else ""
        print(f"   {status} {name} {std_str}")
    
    all_passed = all(r[1] for r in results)
    if all_passed:
        print("\n[PASSED] All parameter combinations work")
    else:
        print("\n[PARTIAL] Some parameter combinations failed")
    
    return all_passed


def test_integration_with_terrain():
    """Test 4: Integration with calculate_terrain_attributes"""
    print("\n" + "=" * 70)
    print("TEST 4: Integration with Terrain Calculation")
    print("=" * 70)
    
    output_path = OUTPUT_DIR / 'test_integration.tif'
    
    try:
        # Denoise
        print("\n  Step 1: Denoising DEM...")
        denoised_path = denoise_dem_wbt(str(TEST_DEM), str(output_path))
        print(f"    [SUCCESS] Denoised: {denoised_path}")
        
        # Calculate terrain from denoised
        print("\n  Step 2: Calculating terrain attributes...")
        terrain = calculate_terrain_attributes(
            denoised_path,
            attributes=['slope', 'curvature']
        )
        print(f"    [SUCCESS] Terrain calculated")
        
        # Check output
        print(f"\n  Step 3: Validating output...")
        assert terrain is not None, "Terrain is None"
        assert 'slope' in terrain.data_vars, "slope not in output"
        assert 'curvature' in terrain.data_vars, "curvature not in output"
        assert 'latitude' in terrain.coords, "latitude not in coords"
        assert 'longitude' in terrain.coords, "longitude not in coords"
        
        print(f"    Data variables: {list(terrain.data_vars)}")
        print(f"    Coordinates: {list(terrain.coords)}")
        print(f"    Shape: {terrain.slope.shape}")
        
        # Calculate terrain from original for comparison
        print("\n  Step 4: Comparing with original DEM terrain...")
        terrain_orig = calculate_terrain_attributes(str(TEST_DEM), attributes=['slope'])
        
        slope_orig_std = float(terrain_orig['slope'].std())
        slope_den_std = float(terrain['slope'].std())
        
        print(f"    Slope std (original DEM): {slope_orig_std:.4f}")
        print(f"    Slope std (denoised DEM): {slope_den_std:.4f}")
        print(f"    Difference: {slope_den_std - slope_orig_std:.4f} ({(slope_den_std - slope_orig_std)/slope_orig_std*100:.1f}%)")
        
        print("\n[PASSED] Integration test successful")
        return True
        
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback
        traceback.print_exc()
        return False


def test_bilateral_basic_functionality():
    """Test 5: Bilateral filter basic functionality"""
    print("\n" + "=" * 70)
    print("TEST 5: Bilateral Filter - Basic Functionality")
    print("=" * 70)
    
    output_path = OUTPUT_DIR / 'test_bilateral.tif'
    
    try:
        print("\nRunning denoise_dem_bilateral()...")
        result = denoise_dem_bilateral(str(TEST_DEM), str(output_path))
        
        assert Path(result).exists(), "Output file not created"
        assert output_path.stat().st_size > 0, "Output file is empty"
        
        print("[PASSED] Bilateral filter executed successfully")
        print(f"   Output: {result}")
        print(f"   Size: {output_path.stat().st_size / 1024:.1f} KB")
        return True
        
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback
        traceback.print_exc()
        return False


def test_bilateral_parameter_variations():
    """Test 6: Bilateral filter parameter variations"""
    print("\n" + "=" * 70)
    print("TEST 6: Bilateral Filter - Parameter Variations")
    print("=" * 70)
    
    params = [
        (0.5, 0.5, "conservative"),
        (0.75, 1.0, "default"),
        (1.5, 2.0, "aggressive"),
    ]
    
    results = []
    
    for sigma_dist, sigma_int, name in params:
        output_path = OUTPUT_DIR / f'test_bilateral_{name}.tif'
        
        try:
            print(f"\n  Testing: {name}")
            print(f"    sigma_dist={sigma_dist}, sigma_int={sigma_int}")
            
            result = denoise_dem_bilateral(
                str(TEST_DEM), 
                str(output_path),
                sigma_dist=sigma_dist,
                sigma_int=sigma_int
            )
            
            assert Path(result).exists()
            
            with rasterio.open(output_path) as src:
                data = src.read(1).astype(float)
                data[data == src.nodata] = np.nan
                std = np.nanstd(data)
            
            print(f"    [SUCCESS] Std Dev: {std:.2f}")
            results.append((name, True, std))
            
        except Exception as e:
            print(f"    [FAILED] {e}")
            results.append((name, False, None))
    
    # Summary
    print(f"\nParameter Test Summary:")
    for name, success, std in results:
        status = "[PASS]" if success else "[FAIL]"
        std_str = f"(std={std:.2f})" if std else ""
        print(f"   {status} {name} {std_str}")
    
    all_passed = all(r[1] for r in results)
    if all_passed:
        print("\n[PASSED] All bilateral parameter combinations work")
    else:
        print("\n[PARTIAL] Some bilateral parameter combinations failed")
    
    return all_passed


def test_gaussian_basic_functionality():
    """Test 7: Gaussian filter basic functionality"""
    print("\n" + "=" * 70)
    print("TEST 7: Gaussian Filter - Basic Functionality")
    print("=" * 70)
    
    output_path = OUTPUT_DIR / 'test_gaussian.tif'
    
    try:
        print("\nRunning denoise_dem_gaussian()...")
        result = denoise_dem_gaussian(str(TEST_DEM), str(output_path))
        
        assert Path(result).exists(), "Output file not created"
        assert output_path.stat().st_size > 0, "Output file is empty"
        
        print("[PASSED] Gaussian filter executed successfully")
        print(f"   Output: {result}")
        print(f"   Size: {output_path.stat().st_size / 1024:.1f} KB")
        return True
        
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback
        traceback.print_exc()
        return False


def test_gaussian_parameter_variations():
    """Test 8: Gaussian filter parameter variations"""
    print("\n" + "=" * 70)
    print("TEST 8: Gaussian Filter - Parameter Variations")
    print("=" * 70)
    
    params = [
        (0.5, "small_sigma"),
        (0.75, "default_sigma"),
        (1.5, "large_sigma"),
    ]
    
    results = []
    
    for sigma, name in params:
        output_path = OUTPUT_DIR / f'test_gaussian_{name}.tif'
        
        try:
            print(f"\n  Testing: {name}")
            print(f"    sigma={sigma}")
            
            result = denoise_dem_gaussian(
                str(TEST_DEM), 
                str(output_path),
                sigma=sigma
            )
            
            assert Path(result).exists()
            
            with rasterio.open(output_path) as src:
                data = src.read(1).astype(float)
                data[data == src.nodata] = np.nan
                std = np.nanstd(data)
            
            print(f"    [SUCCESS] Std Dev: {std:.2f}")
            results.append((name, True, std))
            
        except Exception as e:
            print(f"    [FAILED] {e}")
            results.append((name, False, None))
    
    # Summary
    print(f"\nParameter Test Summary:")
    for name, success, std in results:
        status = "[PASS]" if success else "[FAIL]"
        std_str = f"(std={std:.2f})" if std else ""
        print(f"   {status} {name} {std_str}")
    
    all_passed = all(r[1] for r in results)
    if all_passed:
        print("\n[PASSED] All gaussian parameter combinations work")
    else:
        print("\n[PARTIAL] Some gaussian parameter combinations failed")
    
    return all_passed


def test_universal_wrapper():
    """Test 9: Universal denoise_dem() wrapper function"""
    print("\n" + "=" * 70)
    print("TEST 9: Universal denoise_dem() Wrapper")
    print("=" * 70)
    
    methods = [
        ('feature_preserving', {'filter_size': 5, 'num_iter': 1}),
        ('bilateral', {'sigma_dist': 0.75}),
        ('gaussian', {'sigma': 0.75}),
    ]
    
    results = []
    
    for method, params in methods:
        output_path = OUTPUT_DIR / f'test_wrapper_{method}.tif'
        
        try:
            print(f"\n  Testing method: {method}")
            print(f"    Parameters: {params}")
            
            result = denoise_dem(
                str(TEST_DEM),
                str(output_path),
                method=method,
                **params
            )
            
            assert Path(result).exists()
            print(f"    [SUCCESS] File created: {Path(result).name}")
            results.append((method, True))
            
        except Exception as e:
            print(f"    [FAILED] {e}")
            results.append((method, False))
    
    # Summary
    print(f"\nWrapper Test Summary:")
    for method, success in results:
        status = "[PASS]" if success else "[FAIL]"
        print(f"   {status} {method}")
    
    all_passed = all(r[1] for r in results)
    if all_passed:
        print("\n[PASSED] Universal wrapper works for all methods")
    else:
        print("\n[PARTIAL] Some methods failed")
    
    return all_passed


def test_compare_filters():
    """Test 10: Compare all three filters"""
    print("\n" + "=" * 70)
    print("TEST 10: Filter Comparison")
    print("=" * 70)
    
    filters = [
        ('Feature-Preserving', lambda: denoise_dem_wbt(str(TEST_DEM), str(OUTPUT_DIR / 'compare_fp.tif'), filter_size=11, num_iter=1)),
        ('Bilateral', lambda: denoise_dem_bilateral(str(TEST_DEM), str(OUTPUT_DIR / 'compare_bilateral.tif'))),
        ('Gaussian', lambda: denoise_dem_gaussian(str(TEST_DEM), str(OUTPUT_DIR / 'compare_gaussian.tif'))),
    ]
    
    try:
        # Read original
        with rasterio.open(TEST_DEM) as src:
            original = src.read(1).astype(float)
            original[original == src.nodata] = np.nan
        
        orig_std = np.nanstd(original)
        grad_y_orig, grad_x_orig = np.gradient(original)
        grad_mag_orig = np.sqrt(grad_x_orig**2 + grad_y_orig**2)
        grad_std_orig = np.nanstd(grad_mag_orig)
        
        print(f"\nOriginal DEM:")
        print(f"  Std Dev: {orig_std:.2f}")
        print(f"  Gradient Std: {grad_std_orig:.4f}")
        
        results = []
        
        for name, filter_func in filters:
            print(f"\n  Testing {name} filter...")
            
            try:
                result_path = filter_func()
                
                with rasterio.open(result_path) as src:
                    filtered = src.read(1).astype(float)
                    filtered[filtered == src.nodata] = np.nan
                
                filt_std = np.nanstd(filtered)
                grad_y_filt, grad_x_filt = np.gradient(filtered)
                grad_mag_filt = np.sqrt(grad_x_filt**2 + grad_y_filt**2)
                grad_std_filt = np.nanstd(grad_mag_filt)
                
                std_reduction = (1 - filt_std/orig_std) * 100
                grad_reduction = (1 - grad_std_filt/grad_std_orig) * 100
                
                print(f"    Std Dev: {filt_std:.2f} (reduced by {std_reduction:.1f}%)")
                print(f"    Gradient Std: {grad_std_filt:.4f} (reduced by {grad_reduction:.1f}%)")
                
                results.append((name, True, filt_std, grad_std_filt, std_reduction, grad_reduction))
                
            except Exception as e:
                print(f"    [FAILED] {e}")
                results.append((name, False, None, None, None, None))
        
        # Summary table
        print(f"\n{'Filter':<20} {'Std Dev':<10} {'Gradient':<12} {'Reduction':<12}")
        print("-" * 60)
        for name, success, std, grad, std_red, grad_red in results:
            if success:
                print(f"{name:<20} {std:<10.2f} {grad:<12.4f} {std_red:>6.1f}% / {grad_red:>4.1f}%")
            else:
                print(f"{name:<20} [FAILED]")
        
        all_passed = all(r[1] for r in results)
        if all_passed:
            print("\n[PASSED] All filters compared successfully")
        else:
            print("\n[PARTIAL] Some filters failed")
        
        return all_passed
        
    except Exception as e:
        print(f"[FAILED] {e}")
        import traceback
        traceback.print_exc()
        return False


def main():
    """Run all tests"""
    print("\nStarting test suite...\n")
    
    tests = [
        test_basic_functionality,
        test_noise_reduction,
        test_parameter_variations,
        test_integration_with_terrain,
        test_bilateral_basic_functionality,
        test_bilateral_parameter_variations,
        test_gaussian_basic_functionality,
        test_gaussian_parameter_variations,
        test_universal_wrapper,
        test_compare_filters,
    ]
    
    results = []
    for test_func in tests:
        try:
            passed = test_func()
            results.append((test_func.__name__, passed))
        except Exception as e:
            print(f"\n[CRITICAL ERROR] in {test_func.__name__}: {e}")
            results.append((test_func.__name__, False))
    
    # Final summary
    print("\n" + "=" * 70)
    print("FINAL TEST SUMMARY")
    print("=" * 70)
    
    for test_name, passed in results:
        status = "[PASSED]" if passed else "[FAILED]"
        print(f"{status}: {test_name}")
    
    passed_count = sum(1 for _, p in results if p)
    total_count = len(results)
    
    print(f"\nTotal: {passed_count}/{total_count} tests passed")
    
    if passed_count == total_count:
        print("\n[SUCCESS] ALL TESTS PASSED!")
        return 0
    else:
        print(f"\n[WARNING] {total_count - passed_count} test(s) failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
