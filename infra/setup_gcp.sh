#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# PigOps Sentinel — one-shot Google Cloud project bootstrap.
#
# Creates (or reuses) a DEDICATED project, links billing, enables the APIs,
# creates the Firestore database, the two Cloud Run service accounts with
# least-privilege roles, a low-spend budget alert, and the Firestore indexes
# from firestore.indexes.json. Everything is idempotent: re-running is safe.
#
# Usage:
#   PROJECT_ID=pigops-sentinel BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX \
#   BUDGET_USD=10 ./infra/setup_gcp.sh
#
# Requires: gcloud (authenticated: `gcloud auth login` and
#           `gcloud auth application-default login`), curl.
# ---------------------------------------------------------------------------
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-pigops-sentinel}"
PROJECT_NAME="${PROJECT_NAME:-PigOps Sentinel}"
REGION="${REGION:-europe-west1}"           # Cloud Run + Firestore + Scheduler
BILLING_ACCOUNT="${BILLING_ACCOUNT:?set BILLING_ACCOUNT=XXXXXX-XXXXXX-XXXXXX}"
BUDGET_USD="${BUDGET_USD:-10}"
AGENT_SA="sentinel-agent"
CONSOLE_SA="sentinel-console"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# --- 1. project ------------------------------------------------------------
if gcloud projects describe "$PROJECT_ID" >/dev/null 2>&1; then
  log "Project $PROJECT_ID already exists — reusing"
else
  log "Creating project $PROJECT_ID"
  gcloud projects create "$PROJECT_ID" --name="$PROJECT_NAME" --labels=purpose=hackathon
fi
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"

# --- 2. gcloud configuration (keeps your other projects' config untouched) --
log "Activating a dedicated gcloud configuration 'sentinel'"
gcloud config configurations create sentinel --activate 2>/dev/null || gcloud config configurations activate sentinel
gcloud config set project "$PROJECT_ID" >/dev/null
gcloud config set compute/region "$REGION" >/dev/null
gcloud config set run/region "$REGION" >/dev/null
gcloud config set ai/region "$REGION" >/dev/null

# --- 3. billing --------------------------------------------------------------
log "Linking billing account $BILLING_ACCOUNT"
gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT" >/dev/null

# --- 4. APIs -----------------------------------------------------------------
log "Enabling APIs"
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  firestore.googleapis.com \
  cloudscheduler.googleapis.com \
  billingbudgets.googleapis.com \
  cloudbuild.googleapis.com \
  artifactregistry.googleapis.com

# --- 5. Firestore (native mode, same region as Cloud Run) --------------------
if gcloud firestore databases describe --database='(default)' >/dev/null 2>&1; then
  log "Firestore (default) database already exists"
else
  log "Creating Firestore native database in $REGION"
  gcloud firestore databases create --location="$REGION" --type=firestore-native
fi

# --- 6. service accounts (NOT the default compute SA) -----------------------
for SA in "$AGENT_SA" "$CONSOLE_SA"; do
  if ! gcloud iam service-accounts describe "$SA@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1; then
    log "Creating service account $SA"
    gcloud iam service-accounts create "$SA" --display-name="PigOps Sentinel $SA (Cloud Run)"
  fi
done
log "Granting roles"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None --format=none \
  --member="serviceAccount:$AGENT_SA@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/aiplatform.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None --format=none \
  --member="serviceAccount:$AGENT_SA@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/datastore.user"
gcloud projects add-iam-policy-binding "$PROJECT_ID" --condition=None --format=none \
  --member="serviceAccount:$CONSOLE_SA@$PROJECT_ID.iam.gserviceaccount.com" --role="roles/datastore.viewer"

# --- 7. budget alert (spend guard — alerts go to billing-account admins) ----
if gcloud billing budgets list --billing-account="$BILLING_ACCOUNT" --format='value(displayName)' \
     | grep -q "^$PROJECT_ID (hackathon)"; then
  log "Budget for $PROJECT_ID already exists"
else
  log "Creating $BUDGET_USD USD budget with alerts at 25/50/90/100% (+100% forecast)"
  gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" \
    --display-name="$PROJECT_ID (hackathon) - low spend guard" \
    --budget-amount="${BUDGET_USD}USD" \
    --filter-projects="projects/$PROJECT_NUMBER" \
    --threshold-rule=percent=0.25 --threshold-rule=percent=0.5 \
    --threshold-rule=percent=0.9  --threshold-rule=percent=1.0 \
    --threshold-rule=percent=1.0,basis=forecasted-spend
fi

# --- 8. Firestore indexes (mirrors firestore.indexes.json) -------------------
log "Creating composite indexes (async, Firestore builds them in the background)"
ci() { gcloud firestore indexes composite create --async "$@" 2>&1 | grep -v "already exists" || true; }
ci --collection-group=modositasok --query-scope=COLLECTION_GROUP \
   --field-config=field-path=telepId,order=ascending --field-config=field-path=datum,order=ascending
ci --collection-group=modositasok --query-scope=COLLECTION_GROUP \
   --field-config=field-path=teremId,order=ascending --field-config=field-path=datum,order=ascending
ci --collection-group=elhullasok --query-scope=COLLECTION \
   --field-config=field-path=teremId,order=ascending --field-config=field-path=datum,order=ascending
ci --collection-group=elhullasok --query-scope=COLLECTION \
   --field-config=field-path=hizlaldaId,order=ascending --field-config=field-path=datum,order=ascending
ci --collection-group=feladatok --query-scope=COLLECTION \
   --field-config=field-path=done,order=ascending --field-config=field-path=deadline,order=ascending
ci --collection-group=feladatok --query-scope=COLLECTION \
   --field-config=field-path=done,order=ascending --field-config=field-path=createdAt,order=descending

# Single-field override: `modositasok.datum` must be indexed at COLLECTION_GROUP
# scope, otherwise collectionGroup('modositasok').where('datum', ...) fails with
# FAILED_PRECONDITION. gcloud cannot set queryScope on field overrides, so we
# call the Firestore Admin API directly.
log "Setting collection-group field override on modositasok.datum"
TOKEN="$(gcloud auth application-default print-access-token)"
curl -sS -X PATCH \
  "https://firestore.googleapis.com/v1/projects/$PROJECT_ID/databases/(default)/collectionGroups/modositasok/fields/datum?updateMask=indexConfig" \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"indexConfig":{"indexes":[
        {"queryScope":"COLLECTION","fields":[{"fieldPath":"datum","order":"ASCENDING"}]},
        {"queryScope":"COLLECTION","fields":[{"fieldPath":"datum","order":"DESCENDING"}]},
        {"queryScope":"COLLECTION_GROUP","fields":[{"fieldPath":"datum","order":"ASCENDING"}]},
        {"queryScope":"COLLECTION_GROUP","fields":[{"fieldPath":"datum","order":"DESCENDING"}]}]}}' \
  | head -c 300; echo

log "Done. Verify:"
echo "  gcloud config list"
echo "  gcloud firestore indexes composite list"
echo "  gcloud firestore indexes fields list --collection-group=modositasok"
