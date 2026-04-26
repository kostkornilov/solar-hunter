#!/bin/sh
set -eu

# For serverless environments (e.g. Yandex Cloud), allow passing
# Service Account key JSON via env var instead of mounting a file.
if [ -n "${GEE_SERVICE_ACCOUNT_KEY_JSON:-}" ]; then
  mkdir -p /tmp/secrets
  KEY_FILE_PATH="${GEE_SERVICE_ACCOUNT_KEY_PATH:-/tmp/secrets/gee-sa.json}"
  printf '%s' "${GEE_SERVICE_ACCOUNT_KEY_JSON}" > "${KEY_FILE_PATH}"
  chmod 600 "${KEY_FILE_PATH}"
  export GEE_SERVICE_ACCOUNT_KEY_PATH="${KEY_FILE_PATH}"
fi

exec "$@"
