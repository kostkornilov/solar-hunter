from setuptools import setup, find_packages

setup(
    name='dem_features_extractor',
    version='0.1.0',
    packages=find_packages(),
    install_requires=[
        'earthengine-api',
        'geemap',
        'matplotlib',
        'numpy',
        'pyproj',
        'rasterio',
        'rioxarray',
        'xarray',
        'xdem',
        # Used by slope classification utilities
        'scipy',
        'scikit-image',
        'shapely',
        'geopandas',
    ],
    description='API for downloading DEM models from GEE and calculating various indicators.',
    author='Kostya Kornilov',
    author_email='k.kornilov1015@gmail.com',
    url='https://github.com/kostkornilov/dem_api',
)
