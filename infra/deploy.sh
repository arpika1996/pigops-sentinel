#!/usr/bin/env bash
# Deploy PigOps Sentinel to Cloud Run and wire up Cloud Scheduler. Idempotent.
#
#   ./infra/deploy.sh                 # build once, deploy both services, (re)create the scheduler job
#   SKIP_BUILD=1 ./infra/deploy.sh    # redeploy the last built image (config/IAM changes only)
#
# What it creates in project $PROJECT_ID (default pigops-sentinel), region $REGION:
#   * Cloud Run service  sentinel-agent    — POST /run, NOT public (run.invoker for the scheduler SA only),
#                                            runs as sentinel-agent@ (aiplatform.user + datastore.user),
#                                            concurrency 1, max 1 instance, 15 min request timeout
#   * Cloud Run service  sentinel-console  — public URL, runs as sentinel-console@ (datastore.viewer)
#   * Service account    sentinel-scheduler@ with roles/run.invoker on sentinel-agent
#   * Cloud Scheduler    sentinel-tick     — every SCAN_INTERVAL_MINUTES, OIDC to sentinel-agent /run
#
# Prerequisites: infra/setup_gcp.sh has run (APIs, Firestore, the two service accounts), gcloud is
# authenticated and pointed at the project. The image is built by Cloud Build from the Dockerfile
# (`gcloud run deploy --source .`) into the auto-created Artifact Registry repo cloud-run-source-deploy.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-pigops-sentinel}"
REGION="${REGION:-europe-west1}"
AGENT_SERVICE="${AGENT_SERVICE:-sentinel-agent}"
CONSOLE_SERVICE="${CONSOLE_SERVICE:-sentinel-console}"
AGENT_SA="sentinel-agent@$PROJECT_ID.iam.gserviceaccount.com"
CONSOLE_SA="sentinel-console@$PROJECT_ID.iam.gserviceaccount.com"
SCHED_SA_NAME="sentinel-scheduler"
SCHED_SA="$SCHED_SA_NAME@$PROJECT_ID.iam.gserviceaccount.com"
JOB="${JOB:-sentinel-tick}"
SCAN_INTERVAL_MINUTES="${SCAN_INTERVAL_MINUTES:-15}"
DEMO_FARM_ID="${DEMO_FARM_ID:-demo-farm}"
GEMINI_MODEL="${GEMINI_MODEL:-gemini-3.5-flash}"

# Everything the agent needs at runtime — no secrets, no keys: ADC = the service account.
AGENT_ENV="GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,GOOGLE_CLOUD_LOCATION=global,\
GOOGLE_GENAI_USE_ENTERPRISE=TRUE,GOOGLE_GENAI_USE_VERTEXAI=TRUE,GEMINI_MODEL=$GEMINI_MODEL,\
DEMO_FARM_ID=$DEMO_FARM_ID,SCAN_INTERVAL_MINUTES=$SCAN_INTERVAL_MINUTES,PYTHONUNBUFFERED=1"
CONSOLE_ENV="GOOGLE_CLOUD_PROJECT=$PROJECT_ID,GCP_REGION=$REGION,DEMO_FARM_ID=$DEMO_FARM_ID,\
SCAN_INTERVAL_MINUTES=$SCAN_INTERVAL_MINUTES,GEMINI_MODEL=$GEMINI_MODEL,PYTHONUNBUFFERED=1"

cd "$(dirname "$0")/.."
log() { printf '\n\033[1;36m▶ %s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudscheduler.googleapis.com >/dev/null

# --- 1. build + deploy the agent (the build happens here, once) --------------
if [[ "${SKIP_BUILD:-0}" == "1" ]]; then
  IMAGE="$(gcloud run services describe "$AGENT_SERVICE" --region "$REGION" --format='value(spec.template.spec.containers[0].image)')"
  log "Skipping build; redeploying $AGENT_SERVICE from $IMAGE"
  gcloud run deploy "$AGENT_SERVICE" --image "$IMAGE" --region "$REGION" --quiet \
    --service-account "$AGENT_SA" --no-allow-unauthenticated \
    --command python --args=-m,sentinel.service \
    --concurrency 1 --max-instances 1 --min-instances 0 --timeout 900 --memory 1Gi --cpu 1 \
    --set-env-vars "$AGENT_ENV"
else
  log "Building the image with Cloud Build and deploying $AGENT_SERVICE"
  gcloud run deploy "$AGENT_SERVICE" --source . --region "$REGION" --quiet \
    --service-account "$AGENT_SA" --no-allow-unauthenticated \
    --command python --args=-m,sentinel.service \
    --concurrency 1 --max-instances 1 --min-instances 0 --timeout 900 --memory 1Gi --cpu 1 \
    --set-env-vars "$AGENT_ENV"
  IMAGE="$(gcloud run services describe "$AGENT_SERVICE" --region "$REGION" --format='value(spec.template.spec.containers[0].image)')"
fi
AGENT_URL="$(gcloud run services describe "$AGENT_SERVICE" --region "$REGION" --format='value(status.url)')"
log "$AGENT_SERVICE → $AGENT_URL (image $IMAGE)"

# --- 2. the console, same image, public -----------------------------------
log "Deploying $CONSOLE_SERVICE (public, read-only)"
# Cost shape (the console is public, so this is a spend limit as much as a config):
#   * max-instances 1  — a hard ceiling on the burn rate whatever the traffic
#   * min-instances 0  — an unvisited console costs nothing
#   * concurrency 80   — one instance serves a crowd; visitors share the same cached reads
#   * timeout 300      — the page polls (sub-second requests); nothing needs to hold a request open
#   * CPU throttling left at the default (allocated only during requests)
#   * startup CPU boost left ON — the service scales to zero, so a visitor's first
#     request is a cold start; a few seconds of boosted CPU is worth the good impression
gcloud run deploy "$CONSOLE_SERVICE" --image "$IMAGE" --region "$REGION" --quiet \
  --service-account "$CONSOLE_SA" --allow-unauthenticated \
  --command python --args=-m,sentinel.console \
  --concurrency 80 --max-instances 1 --min-instances 0 --timeout 300 --memory 512Mi --cpu 1 \
  --set-env-vars "$CONSOLE_ENV"
CONSOLE_URL="$(gcloud run services describe "$CONSOLE_SERVICE" --region "$REGION" --format='value(status.url)')"
log "$CONSOLE_SERVICE → $CONSOLE_URL"

# --- 3. scheduler identity + permission -------------------------------------
if ! gcloud iam service-accounts describe "$SCHED_SA" >/dev/null 2>&1; then
  log "Creating service account $SCHED_SA_NAME"
  gcloud iam service-accounts create "$SCHED_SA_NAME" --display-name="PigOps Sentinel scheduler (invokes sentinel-agent)"
fi
gcloud run services add-iam-policy-binding "$AGENT_SERVICE" --region "$REGION" --quiet \
  --member="serviceAccount:$SCHED_SA" --role="roles/run.invoker" >/dev/null

# --- 4. the tick ------------------------------------------------------------
SCHEDULE="*/$SCAN_INTERVAL_MINUTES * * * *"
JOB_ARGS=(--location "$REGION" --schedule "$SCHEDULE" --time-zone "Europe/Budapest"
  --uri "$AGENT_URL/run?trigger=scheduler" --http-method POST
  --oidc-service-account-email "$SCHED_SA" --oidc-token-audience "$AGENT_URL"
  --attempt-deadline 1200s --max-retry-attempts 0
  --description "PigOps Sentinel: scan → investigate → decide → act → follow up")
if gcloud scheduler jobs describe "$JOB" --location "$REGION" >/dev/null 2>&1; then
  log "Updating scheduler job $JOB ($SCHEDULE)"
  gcloud scheduler jobs update http "$JOB" "${JOB_ARGS[@]}" >/dev/null
else
  log "Creating scheduler job $JOB ($SCHEDULE)"
  gcloud scheduler jobs create http "$JOB" "${JOB_ARGS[@]}" >/dev/null
fi

log "Done."
echo "  agent   : $AGENT_URL   (private — POST /run with an OIDC token; the scheduler does this)"
echo "  console : $CONSOLE_URL"
echo "  tick    : gcloud scheduler jobs run $JOB --location $REGION     # trigger a run right now"
echo "  pause   : gcloud scheduler jobs pause $JOB --location $REGION   # stop the 15-min cadence"
