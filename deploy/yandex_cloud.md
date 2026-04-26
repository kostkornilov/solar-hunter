# SolarHunter: Yandex Cloud deploy runbook

This is an operational guide for redeploying SolarHunter after code changes.
Scope:
- backend: Yandex Serverless Containers
- frontend: Yandex Object Storage static website

Use PowerShell unless specified otherwise.

---

## 0) One-time setup (do once per environment)

### 0.1 Required tools
- `yc`
- `docker`
- `aws` (for S3-compatible Object Storage commands)

Check:
```powershell
yc version
docker --version
aws --version
```

### 0.2 Login and target folder
```powershell
yc init
yc config list
```

Ensure the target `folder-id` is correct before any deploy command.

### 0.3 Core resources and names
Recommended stable names:
- registry: `solarhunter-registry`
- backend repository: `solarhunter-backend`
- backend container: `solarhunter-backend`
- runtime service account: `solarhunter-runtime-sa`
- Lockbox secret: `solarhunter-backend-secrets`
- frontend bucket: one unique name per environment (e.g. `solarhunter-frontend-prod-...`)

---

## 1) Backend redeploy (when backend code changes)

### 1.1 Export common variables
```powershell
$REGISTRY_NAME = "solarhunter-registry"
$REPO_NAME = "solarhunter-backend"
$CONTAINER_NAME = "solarhunter-backend"
$RUNTIME_SA_NAME = "solarhunter-runtime-sa"
$LOCKBOX_SECRET_NAME = "solarhunter-backend-secrets"
$TAG = Get-Date -Format "yyyyMMdd-HHmmss"

$REGISTRY_ID = (yc container registry get --name $REGISTRY_NAME --format json | ConvertFrom-Json).id
$SA_ID = (yc iam service-account get --name $RUNTIME_SA_NAME --format json | ConvertFrom-Json).id
$SECRET_ID = (yc lockbox secret get --name $LOCKBOX_SECRET_NAME --format json | ConvertFrom-Json).id

$IMAGE = "cr.yandex/${REGISTRY_ID}/${REPO_NAME}:${TAG}"
```

PowerShell nuance: always use `${VAR}` near `:` in strings.

### 1.2 Build and push image
```powershell
yc container registry configure-docker
docker build -t $IMAGE ./backend
docker push $IMAGE
yc container image list --registry-id $REGISTRY_ID
```

### 1.3 Deploy new Serverless Container revision
$SECRET_VERSION_ID = 'e6qni8s9ocui9dfg5tuk'
```powershell
yc serverless container revision deploy `
>>   --container-name $CONTAINER_NAME `
>>   --image $IMAGE `
>>   --cores 1 `
>>   --memory 2GB `
>>   --concurrency 2 `
>>   --execution-timeout 600s `
>>   --service-account-id $SA_ID `
>>   --environment LOG_LEVEL=INFO `
>>   --environment MODEL_ARTIFACTS_DIR=/app/model_serving `
>>   --environment GEE_PROJECT=projectomela `
>>   --environment MAX_GEE_CONCURRENCY=1 `
>>   --environment MAX_CDS_CONCURRENCY=1 `
>>   --environment MAX_NASA_CONCURRENCY=2 `
>>   --environment MAX_CLOUD_SCENE_WORKERS=4 `
>>   --environment PROVIDER_RETRIES=2 `
>>   --environment RETRY_BACKOFF_SEC=2.0 `
>>   --environment CLOUD_RADIUS_M=300 `
>>   --environment CLOUD_TIME_STEP=P30D `
>>   --environment DOWNLOAD_EMBEDDINGS=true `
>>   --environment EMBEDDINGS_YEAR=2025 `
>>   --environment CDS_API_URL=https://cds.climate.copernicus.eu/api `
>>   --secret environment-variable=CDS_API_KEY,id=$SECRET_ID,version-id=$SECRET_VERSION_ID,key=CDS_API_KEY `
>>   --secret environment-variable=EARTHDATA_TOKEN,id=$SECRET_ID,version-id=$SECRET_VERSION_ID,key=EARTHDATA_TOKEN `
>>   --secret environment-variable=GEE_SERVICE_ACCOUNT_EMAIL,id=$SECRET_ID,version-id=$SECRET_VERSION_ID,key=GEE_SERVICE_ACCOUNT_EMAIL `
>>   --secret environment-variable=GEE_SERVICE_ACCOUNT_KEY_JSON,id=$SECRET_ID,version-id=$SECRET_VERSION_ID,key=GEE_SERVICE_ACCOUNT_KEY_JSON
```

Why `GEE_SERVICE_ACCOUNT_KEY_JSON` works:
- `backend/entrypoint.sh` writes env JSON to a temp file and exports `GEE_SERVICE_ACCOUNT_KEY_PATH`.

### 1.4 Make sure container is publicly invokable
```powershell
yc serverless container allow-unauthenticated-invoke --name $CONTAINER_NAME
```

### 1.5 Smoke test backend
```powershell
$BACKEND_URL = (yc serverless container get --name $CONTAINER_NAME --format json | ConvertFrom-Json).url
$BASE = $BACKEND_URL.TrimEnd('/')

Invoke-WebRequest "$BASE/health"
Invoke-WebRequest "$BASE/openapi.json"

$body = @{
  lat    = 55.7558
  lon    = 37.6176
  P      = 120
  tariff = 8.5
} | ConvertTo-Json

Invoke-RestMethod -Method Post -Uri "$BASE/v1/evaluate" -ContentType "application/json" -Body $body
```

If `/health` returns 404, check double slash (`//health`) and use `$BASE = $BACKEND_URL.TrimEnd('/')`.

---

## 2) Frontend redeploy (when frontend code changes)

### 2.1 Update API URL
Set `frontend/config.js`:
```js
window.SOLARHUNTER_API_BASE_URL = "https://<backend-container-url-without-trailing-slash>";
```

### 2.2 Upload static files
```powershell
$BUCKET = "<your-frontend-bucket>"

aws s3 sync ".\frontend" "s3://$BUCKET" `
  --exclude "README.md" `
  --endpoint-url https://storage.yandexcloud.net

aws s3 ls "s3://$BUCKET" --endpoint-url https://storage.yandexcloud.net
```

### 2.3 Ensure website hosting is enabled
Create website config JSON without BOM (important on Windows):
```powershell
$json = '{"IndexDocument":{"Suffix":"index.html"},"ErrorDocument":{"Key":"index.html"}}'
$path = Join-Path $PWD "website.json"
$utf8NoBom = New-Object System.Text.UTF8Encoding($false)
[System.IO.File]::WriteAllText($path, $json, $utf8NoBom)

aws s3api put-bucket-website `
  --bucket $BUCKET `
  --website-configuration file://$path `
  --endpoint-url https://storage.yandexcloud.net
```

Verify:
```powershell
aws s3api get-bucket-website --bucket $BUCKET --endpoint-url https://storage.yandexcloud.net
```

### 2.4 Open frontend URL
- `http://<bucket>.website.yandexcloud.net`
- if needed, also test `https://<bucket>.website.yandexcloud.net`

---

## 3) Secret management (backend)

Do not keep runtime secrets in git or Docker image.
Store these keys in Lockbox secret `solarhunter-backend-secrets`:
- `CDS_API_KEY`
- `EARTHDATA_TOKEN`
- `GEE_SERVICE_ACCOUNT_EMAIL`
- `GEE_SERVICE_ACCOUNT_KEY_JSON` (full GEE service account JSON content as a single string)

Grant runtime service account access:
```powershell
yc lockbox secret add-access-binding `
  --id $SECRET_ID `
  --service-account-id $SA_ID `
  --role lockbox.payloadViewer
```

---

## 4) Logs and diagnostics

### 4.1 Read logs in CLI
```powershell
yc logging group list
yc logging read --group-name=default --since=1h --limit=200
yc logging read --group-name=default --follow
```

### 4.2 Common expected warnings
- `joblib ... serial mode`: usually not correctness-critical; mainly performance signal.
- `FutureWarning` from pandas: non-blocking for runtime.

---

## 5) Performance notes

- `concurrency=1` means one request per instance at a time.
- Slow calls are usually dominated by external providers (GEE/CDS/NASA) and cold starts.
- If latency is too high:
  - increase CPU/memory (`--cores`, `--memory`);
  - consider `concurrency=2` carefully;
  - keep one warm instance (provisioned/min instances via supported config path).

---

## 6) Rollback

List revisions:
```powershell
yc serverless container revision list --container-name $CONTAINER_NAME
```

Rollback to known-good revision:
```powershell
yc serverless containers rollback --name $CONTAINER_NAME --revision-id <revision-id>
```

Retest:
```powershell
Invoke-WebRequest "$BASE/health"
```

---

## 7) Minimal release checklist

- Backend image pushed.
- New backend revision deployed with Lockbox secrets.
- `/health` and `/v1/evaluate` return success.
- `frontend/config.js` points to current backend URL.
- Frontend synced to Object Storage bucket.
- Website URL opens and real browser flow works.

