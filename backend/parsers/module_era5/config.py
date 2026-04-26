
import os

MONGO_CONNECT_STRING = "mongodb://localhost:27017/"
DATABASE_NAME = "admin_data"
COLLECTION_NAME = "admin_table"
CDS_API_URL = os.getenv("CDS_API_URL", "https://cds.climate.copernicus.eu/api")
CDS_API_KEY = os.getenv("CDS_API_KEY", "")
DATASET_NAME = "reanalysis-era5-land"
