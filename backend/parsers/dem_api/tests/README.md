# DEM API Tests

This directory contains tests for the dem_api parser, including WhiteboxTools DEM denoising functionality.

## Test Files

- **test_denoise_wbt.py** - Comprehensive tests for WhiteboxTools denoising filter
  - Tests basic functionality
  - Tests noise reduction quality
  - Tests parameter variations
  - Tests integration with terrain calculation
  
- **TEST_REPORT.md** - Detailed test report with results and analysis

- **test_output/** - Generated test output files (denoised DEMs)

## Running Tests

### Run Denoising Tests

**Option 1: Direct Python script**

From the project root:
```bash
python src/parsers/dem_api/tests/test_denoise_wbt.py
```

Or from the dem_api directory:
```bash
cd src/parsers/dem_api
python tests/test_denoise_wbt.py
```

**Option 2: Using pytest**

From the project root:
```bash
pytest src/parsers/dem_api/
```

Or from the dem_api directory:
```bash
cd src/parsers/dem_api
pytest
```

To see verbose output:
```bash
pytest -v
```

To run only denoising tests:
```bash
pytest tests/test_denoise_wbt.py
```

### Expected Output

```
======================================================================
WHITEBOX TOOLS DEM DENOISING TESTS
======================================================================

TEST 1: Basic Functionality
[PASSED] Function executed successfully

TEST 2: Noise Reduction Quality
[PASSED] Quality metrics calculated

TEST 3: Parameter Variations
[PASSED] All parameter combinations work

TEST 4: Integration with Terrain Calculation
[PASSED] Integration test successful

======================================================================
FINAL TEST SUMMARY
======================================================================
[PASSED]: test_basic_functionality
[PASSED]: test_noise_reduction
[PASSED]: test_parameter_variations
[PASSED]: test_integration_with_terrain

Total: 4/4 tests passed

[SUCCESS] ALL TESTS PASSED!
```

## Requirements

- `whitebox` package installed: `pip install whitebox`
- Test DEM file at: `examples/example_output/srtm.tif`

## Test Coverage

The test suite validates:

✅ WhiteboxTools integration  
✅ GeoTIFF compression compatibility  
✅ Parameter handling (filter_size, norm_diff, num_iter)  
✅ Output file creation and format  
✅ Metadata preservation (CRS, bounds)  
✅ Noise reduction quality  
✅ Integration with calculate_terrain_attributes()  

## Test Output Files

Test runs generate denoised DEM files in `test_output/`:
- Various parameter combinations tested
- File size: ~2.3 MB each (uncompressed GeoTIFF)
- Can be inspected or used for visual comparison

## Documentation

See also:
- `../DENOISING.md` - User guide for denoising functionality
- `TEST_REPORT.md` - Detailed test report with metrics
