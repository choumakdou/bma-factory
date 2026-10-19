#!/usr/bin/env bash
# bma-factory demo — one-shot deploy to Cloud Run.
#
# Prereqs:
#   - You are authenticated: `gcloud auth login` (or use Cloud Shell which is
#     pre-authenticated as the project owner).
#   - The `bma-factory` project exists and billing is linked.
#   - The service account `service-ac1@<project>.iam.gserviceaccount.com`
#     has been granted Secret Manager Secret Accessor + Storage Object Admin.
#   - The secret `engine-sa-key` exists in Secret Manager and contains the
#     service-account JSON.
#   - The Drive folders `/ORO/`, `/Outputs/`, `/Templates/` either exist or
#     the service account will create them on first engine boot.
#
# Run from a checkout of this repo:
#   bash deploy.sh
#
# To see the URL after deploy: it's printed at the end.

set -euo pipefail

PROJECT="${PROJECT:-bma-factory}"
SERVICE="${SERVICE:-bma-factory-demo}"
REGION="${REGION:-asia-east2}"
SA="service-ac1@${PROJECT}.iam.gserviceaccount.com"
SECRET_NAME="${SECRET_NAME:-engine-sa-key}"
IMAGE="gcr.io/${PROJECT}/${SERVICE}"

echo "==> Project: ${PROJECT}"
echo "==> Service: ${SERVICE}"
echo "==> Region:  ${REGION}"
echo "==> Runtime SA: ${SA}"
echo "==> Mounting secret: ${SECRET_NAME}:latest as GOOGLE_APPLICATION_CREDENTIALS"
echo

gcloud config set project "${PROJECT}"

echo "==> Building container image (Cloud Build)"
gcloud builds submit --tag "${IMAGE}" --quiet

echo
echo "==> Deploying to Cloud Run"
gcloud run deploy "${SERVICE}" \
  --image "${IMAGE}" \
  --region "${REGION}" \
  --platform managed \
  --service-account "${SA}" \
  --set-secrets "GOOGLE_APPLICATION_CREDENTIALS=${SECRET_NAME}:latest" \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 0 \
  --max-instances 1 \
  --concurrency 80 \
  --timeout 60

URL=$(gcloud run services describe "${SERVICE}" --region "${REGION}" --format 'value(status.url)')

echo
echo "============================================================"
echo "  Deployed: ${SERVICE}"
echo "  URL:      ${URL}"
echo "  Case:     ${URL}/case/bel-air-m2"
echo "============================================================"
