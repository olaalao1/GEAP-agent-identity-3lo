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

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.tools import FunctionTool
from google.adk.tools import ToolContext
from google.cloud import iamconnectorcredentials_v1alpha as iam_creds
import httpx
from vertexai import agent_engines

# ==============================================================================
# Configuration & Resource Identifiers
# ==============================================================================
PROJECT_ID = "gemini-cyber"
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
SPOTIFY_3LO_AUTH_PROVIDER_ID = os.environ.get(
    "SPOTIFY_3LO_AUTH_PROVIDER_ID", "spotify-3lo-auth"
)

# 1. Full GCP Auth Manager Connector Resource String
SPOTIFY_3LO_AUTH_PROVIDER = (
    f"projects/{PROJECT_ID}/locations/{LOCATION}/connectors/"
    f"{SPOTIFY_3LO_AUTH_PROVIDER_ID}"
)

# 2. Public Landing Page Return URL (continue_uri in Cloud Storage)
CONTINUE_URI = os.environ.get(
    "CONTINUE_URI",
    "https://storage.googleapis.com/agent-staging-buck/oauth_callback.html",
)

MODEL = "gemini-2.5-flash"

# In-memory store for pending consent nonces per user
_PENDING_NONCES: dict[str, str] = {}


def extract_validation_state(input_str: str) -> str:
    """Extracts the user_id_validation_state from a raw token or full callback URL."""
    s = input_str.strip().strip('"\'`')
    if "user_id_validation_state=" in s:
        match = re.search(r"user_id_validation_state=([A-Za-z0-9_\-=]+)", s)
        if match:
            return match.group(1)
    tokens = re.findall(r"[A-Za-z0-9_\-=]{40,}", s)
    if tokens:
        return tokens[0]
    return s


# ==============================================================================
# 3. Spotify Authenticated Tool Function (Pure Conversational OAuth Flow)
# ==============================================================================
async def spotify_get_playlists(
    tool_context: ToolContext,
    auth_code_or_url: str = "",
) -> str | list[dict[str, Any]]:
    """Fetches the current user's private playlists from Spotify.

    Args:
        auth_code_or_url: Optional authorization code or callback URL obtained from
            the Spotify authorization confirmation page. Leave empty on first call.
    """
    user_id = tool_context.user_id or "default_user_id"
    client = iam_creds.IAMConnectorCredentialsServiceClient(transport="rest")

    # Step A: Query Google Cloud Auth Manager for existing user credentials
    req = iam_creds.RetrieveCredentialsRequest(
        connector=SPOTIFY_3LO_AUTH_PROVIDER,
        user_id=user_id,
        scopes=["playlist-read-private"],
        continue_uri=CONTINUE_URI,
    )
    operation = client.retrieve_credentials(req).operation

    token = None
    if operation.done and operation.response:
        resp = iam_creds.RetrieveCredentialsResponse.deserialize(
            operation.response.value
        )
        if resp.token:
            token = resp.token

    # Step B: If not yet authenticated, check if the user provided an authorization code
    if not token:
        val_state = extract_validation_state(auth_code_or_url) if auth_code_or_url else ""
        is_code_provided = bool(val_state and len(val_state) >= 20)

        if is_code_provided:
            # User supplied the authorization code from the landing page -> Finalize credentials!
            consent_nonce = _PENDING_NONCES.get(user_id)
            if not consent_nonce and operation.metadata:
                meta = iam_creds.RetrieveCredentialsMetadata.deserialize(
                    operation.metadata.value
                )
                if meta.uri_consent_required and meta.uri_consent_required.consent_nonce:
                    consent_nonce = meta.uri_consent_required.consent_nonce

            if not consent_nonce:
                return (
                    "Error: Could not locate an active consent nonce for this session. "
                    "Please ask to view your playlists again to generate a new authorization link."
                )

            finalize_url = (
                f"https://iamconnectorcredentials.googleapis.com/v1alpha/"
                f"{SPOTIFY_3LO_AUTH_PROVIDER}/credentials:finalize"
            )
            payload = {
                "userId": user_id,
                "userIdValidationState": val_state,
                "consentNonce": consent_nonce,
            }

            print(f"DEBUG FINALIZING: url={finalize_url}", flush=True)
            print(f"DEBUG FINALIZING: userId={user_id}, consentNonce={consent_nonce}, val_state_len={len(val_state)}", flush=True)

            async with httpx.AsyncClient() as http_client:
                fin_resp = await http_client.post(
                    finalize_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )

            print(f"DEBUG FINALIZE STATUS: {fin_resp.status_code}", flush=True)
            print(f"DEBUG FINALIZE BODY: {fin_resp.text}", flush=True)

            if fin_resp.status_code != 200:
                return (
                    f"❌ **Spotify Credential Finalization Failed (HTTP {fin_resp.status_code})**\n\n"
                    f"**Details from Google Cloud Auth Manager:**\n"
                    f"```json\n{fin_resp.text}\n```\n\n"
                    f"*Debug parameters used:* `userId`: `{user_id}`, `consentNonce`: `{consent_nonce}`\n\n"
                    "Please check the error details above or ask for your playlists again to receive a fresh authorization link."
                )

            # Step C: Credentials successfully finalized! Retrieve the new access token
            op_final = client.retrieve_credentials(req).operation
            if op_final.done and op_final.response:
                resp = iam_creds.RetrieveCredentialsResponse.deserialize(
                    op_final.response.value
                )
                token = resp.token
            else:
                return (
                    "Authorization was finalized, but access token could not be retrieved. "
                    "Please try asking for your playlists again."
                )

        else:
            # First turn: Return the direct Spotify authorization link with instructions
            if operation.metadata:
                meta = iam_creds.RetrieveCredentialsMetadata.deserialize(
                    operation.metadata.value
                )
                if meta.uri_consent_required and meta.uri_consent_required.authorization_uri:
                    auth_uri = meta.uri_consent_required.authorization_uri
                    _PENDING_NONCES[user_id] = meta.uri_consent_required.consent_nonce

                    return (
                        "🔒 **Spotify Authorization Required**\n\n"
                        "To access your private playlists, please authorize access to your Spotify account:\n\n"
                        f"👉 [**Click here to Authorize Spotify Access**]({auth_uri})\n\n"
                        "**Instructions:**\n"
                        "1. Click the link above to log in and approve Spotify access.\n"
                        "2. You will be redirected to an authorization confirmation page.\n"
                        "3. Click **\"📋 Copy Authorization Code\"** on that page.\n"
                        "4. **Paste the code back into this chat**, and I will fetch your playlists!"
                    )

            return "Error: Unable to retrieve credentials or generate authorization URL from Auth Manager."

    # Step D: Token is available -> Query Spotify Web API for private playlists
    headers = {"Authorization": f"Bearer {token}"}
    async with httpx.AsyncClient() as http_client:
        response = await http_client.get(
            "https://api.spotify.com/v1/me/playlists",
            headers=headers,
            params={"limit": 10},
        )

        if response.status_code != 200:
            return f"Error from Spotify API: {response.status_code} - {response.text}"

        data = response.json()
        items = data.get("items", [])

        if not items:
            return "No playlists found for the current user on Spotify."

        return [
            {
                "name": item.get("name"),
                "public": item.get("public"),
                "total_tracks": item.get("tracks", {}).get("total"),
            }
            for item in items
            if item
        ]


# ==============================================================================
# 4. Agent & App Definitions
# ==============================================================================
spotify_tool = FunctionTool(func=spotify_get_playlists)

root_agent = Agent(
    name="spotify_3lo_agent",
    model=MODEL,
    instruction=(
        "You are a helpful Spotify assistant with access to the user's Spotify account.\n\n"
        "When the user asks for their playlists:\n"
        "1. Always call the spotify_get_playlists tool.\n"
        "2. If the tool returns an authorization link, present the link clearly to the user "
        "as a markdown link without altering the URL, and provide the instructions to copy the "
        "authorization code from the confirmation page and paste it back into the chat.\n"
        "3. When the user provides an authorization code, token, or callback URL in their response, "
        "immediately call spotify_get_playlists with the auth_code_or_url parameter containing "
        "the user's provided code or URL.\n"
        "4. If the tool returns an error message or failure details, display the EXACT error text "
        "returned by the tool verbatim without summarizing or omitting details.\n"
        "5. Once the playlists are retrieved, present the playlist names and track counts clearly."
    ),
    tools=[spotify_tool],
)

app = App(
    name="spotify_3lo_app",
    root_agent=root_agent,
)

# Wrapper instance for Gemini Enterprise Agent Platform deployment
adk_app = agent_engines.AdkApp(app=app)
