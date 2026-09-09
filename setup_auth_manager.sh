#!/usr/bin/env bash
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

set -euo pipefail

# ==============================================================================
# Setup Script: Google Cloud Agent Identity Auth Manager (Spotify 3LO)
# ==============================================================================

# Required environment variables
export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-gemini-cyber}"
export GOOGLE_CLOUD_LOCATION="${GOOGLE_CLOUD_LOCATION:-us-central1}"
export SPOTIFY_3LO_AUTH_PROVIDER_ID="${SPOTIFY_3LO_AUTH_PROVIDER_ID:-spotify-3lo-auth}"

echo "========================================================================"
echo "GCP Agent Identity Auth Manager Setup"
echo "Project:   ${GOOGLE_CLOUD_PROJECT}"
echo "Location:  ${GOOGLE_CLOUD_LOCATION}"
echo "Connector: ${SPOTIFY_3LO_AUTH_PROVIDER_ID}"
echo "========================================================================"

# Check if client credentials are provided
if [[ -z "${SPOTIFY_CLIENT_ID:-}" ]] || [[ -z "${SPOTIFY_CLIENT_SECRET:-}" ]]; then
  echo ""
  echo "Please set your Spotify Developer credentials before running this script:"
  echo "  export SPOTIFY_CLIENT_ID='your_spotify_client_id'"
  echo "  export SPOTIFY_CLIENT_SECRET='your_spotify_client_secret'"
  echo ""
  echo "Alternatively, you can pass them interactively below."
  read -r -p "Enter Spotify Client ID: " SPOTIFY_CLIENT_ID
  read -r -s -p "Enter Spotify Client Secret: " SPOTIFY_CLIENT_SECRET
  echo ""
fi

# 1. Enable Required Services
echo ""
echo "[1/4] Enabling required Google Cloud APIs..."
gcloud services enable \
    aiplatform.googleapis.com \
    iamconnectors.googleapis.com \
    --project="${GOOGLE_CLOUD_PROJECT}"

# 2. Create the 3-Legged OAuth Connector
echo ""
echo "[2/4] Creating Agent Identity Connector '${SPOTIFY_3LO_AUTH_PROVIDER_ID}'..."
gcloud alpha agent-identity connectors create "${SPOTIFY_3LO_AUTH_PROVIDER_ID}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_LOCATION}" \
    --three-legged-oauth-client-id="${SPOTIFY_CLIENT_ID}" \
    --three-legged-oauth-client-secret="${SPOTIFY_CLIENT_SECRET}" \
    --three-legged-oauth-authorization-url="https://accounts.spotify.com/authorize" \
    --three-legged-oauth-token-url="https://accounts.spotify.com/api/token" \
    --allowed-scopes="playlist-read-private"

# 3. Retrieve and print Redirect URI
echo ""
echo "[3/4] Connector created. Retrieving Spotify OAuth Redirect URI..."
REDIRECT_URI=$(gcloud alpha agent-identity connectors describe "${SPOTIFY_3LO_AUTH_PROVIDER_ID}" \
    --project="${GOOGLE_CLOUD_PROJECT}" \
    --location="${GOOGLE_CLOUD_LOCATION}" \
    --format="value(connectorTypeParams.threeLeggedOauth.redirectUrl)")

echo "------------------------------------------------------------------------"
echo "IMPORTANT: Ensure this Redirect URI is added in your Spotify App Settings:"
echo "  ${REDIRECT_URI}"
echo "------------------------------------------------------------------------"

# 4. Grant IAM permissions to testing identity
CURRENT_USER=$(gcloud config get-value account 2>/dev/null || echo "")
if [[ -n "${CURRENT_USER}" ]]; then
  echo ""
  echo "[4/4] Granting 'roles/iamconnectors.user' to ${CURRENT_USER}..."
  gcloud alpha agent-identity connectors add-iam-policy-binding "${SPOTIFY_3LO_AUTH_PROVIDER_ID}" \
      --project="${GOOGLE_CLOUD_PROJECT}" \
      --location="${GOOGLE_CLOUD_LOCATION}" \
      --role="roles/iamconnectors.user" \
      --member="user:${CURRENT_USER}"
fi

echo ""
echo "Setup complete! The connector is ready for 3LO authentication."
