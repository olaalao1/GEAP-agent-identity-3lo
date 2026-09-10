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
import urllib.parse

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
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-cyber")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
SPOTIFY_3LO_AUTH_PROVIDER_ID = os.environ.get(
    "SPOTIFY_3LO_AUTH_PROVIDER_ID", "spotify-3lo-auth"
)

# 1. GCP Auth Manager Connector Resource String
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


async def extract_validation_state(input_str: str) -> str:
    """Extracts the user_id_validation_state from a raw token, callback URL, or redirect."""
    s = input_str.strip().strip('"\'`')

    # Case 1: User pasted the Spotify authorization link by mistake
    if "accounts.spotify.com/authorize" in s:
        return "ERROR_SPOTIFY_AUTH_URL"

    # Case 2: User pasted the Auth Manager callback URL directly
    if "iamconnectorcredentials.googleapis.com" in s and "oauthcallback" in s:
        try:
            async with httpx.AsyncClient() as client:
                r = await client.get(s, follow_redirects=False)
                loc = r.headers.get("location")
                if loc and "user_id_validation_state=" in loc:
                    m = re.search(r"user_id_validation_state=([^&\s]+)", loc)
                    if m:
                        s = m.group(1)
        except Exception as e:
            print(f"Warning following callback URL: {e}", flush=True)

    # Case 3: User pasted full GCS landing page URL
    if "user_id_validation_state=" in s:
        m = re.search(r"user_id_validation_state=([^&\s]+)", s)
        if m:
            s = m.group(1)

    # Case 4: Strip all internal whitespace / line breaks from copying
    s = "".join(s.split())

    # Case 5: Strip quotes or key: value prefixes
    if ":" in s:
        s = s.split(":")[-1]

    # Decode URL-encoding if present
    s = urllib.parse.unquote(s)

    # Ensure required base64 padding
    missing_padding = len(s) % 4
    if missing_padding:
        s += "=" * (4 - missing_padding)

    return s


# ==============================================================================
# 3. Spotify Authenticated Tool Function (Conversational OAuth Flow)
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

    val_state = await extract_validation_state(auth_code_or_url) if auth_code_or_url else ""

    print(f"DEBUG INPUT auth_code_or_url: {repr(auth_code_or_url)[:100]}", flush=True)
    print(f"DEBUG EXTRACTED val_state: {repr(val_state)[:100]}", flush=True)
    print(f"DEBUG val_state len: {len(val_state)}", flush=True)

    # If the user pasted the Spotify login link instead of the code:
    if val_state == "ERROR_SPOTIFY_AUTH_URL":
        return (
            "⚠️ **Spotify Login Link Detected**\n\n"
            "You pasted the Spotify login link (`accounts.spotify.com/authorize...`) instead of the authorization code.\n\n"
            "**Next Step:**\n"
            "1. Click that link in your browser to log into Spotify and approve access.\n"
            "2. You will be redirected to the green confirmation page (`oauth_callback.html`).\n"
            "3. Click **\"📋 Copy Authorization Code\"** on that page.\n"
            "4. **Paste that code back into this chat**."
        )

    is_code_provided = bool(val_state and len(val_state) >= 20)

    # Step B: If the user provided an authorization code -> Finalize credentials!
    if is_code_provided:
        # Retrieve consent_nonce from persistent session state first, then in-memory fallback
        consent_nonce = None
        try:
            consent_nonce = tool_context.state.get("consent_nonce")
            saved_user_id = tool_context.state.get("user_id")
            if saved_user_id:
                user_id = saved_user_id
        except Exception as e:
            print(f"Warning reading tool_context.state: {e}", flush=True)

        if not consent_nonce:
            consent_nonce = _PENDING_NONCES.get(user_id) or _PENDING_NONCES.get("latest")

        print(f"DEBUG FINALIZE: user_id={user_id}, consent_nonce={consent_nonce}", flush=True)

        if not consent_nonce:
            return (
                "❌ **Session Nonce Not Found**\n\n"
                "Could not locate an active consent nonce for this session. "
                "Please make sure you paste the code into the same chat session where you "
                "requested your playlists, or ask for your playlists again to generate a fresh link."
            )

        payload = {
            "userId": user_id,
            "userIdValidationState": val_state,
            "consentNonce": consent_nonce,
        }

        # Try both PROJECT_ID and 'gemini-cyber' in case of container env mismatch
        endpoints = [
            f"https://iamconnectorcredentials.googleapis.com/v1alpha/{SPOTIFY_3LO_AUTH_PROVIDER}/credentials:finalize",
            f"https://iamconnectorcredentials.googleapis.com/v1alpha/projects/gemini-cyber/locations/{LOCATION}/connectors/{SPOTIFY_3LO_AUTH_PROVIDER_ID}/credentials:finalize",
            f"https://iamconnectorcredentials.googleapis.com/v1alpha/projects/1092333466205/locations/{LOCATION}/connectors/{SPOTIFY_3LO_AUTH_PROVIDER_ID}/credentials:finalize",
        ]

        fin_resp = None
        for fin_url in endpoints:
            print(f"DEBUG CALLING FINALIZE: {fin_url}", flush=True)
            async with httpx.AsyncClient() as http_client:
                fin_resp = await http_client.post(
                    fin_url,
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
            print(f"DEBUG FINALIZE STATUS: {fin_resp.status_code}", flush=True)
            print(f"DEBUG FINALIZE BODY: {fin_resp.text}", flush=True)
            if fin_resp.status_code == 200:
                break

        if not fin_resp or fin_resp.status_code != 200:
            return (
                f"❌ **Spotify Credential Finalization Failed (HTTP {fin_resp.status_code if fin_resp else 'Unknown'})**\n\n"
                f"**Details from Google Cloud Auth Manager:**\n"
                f"```json\n{fin_resp.text if fin_resp else 'No response'}\n```\n\n"
                f"*Debug parameters used:* `userId`: `{user_id}`, `consentNonce`: `{consent_nonce}`, `tokenLen`: `{len(val_state)}`\n\n"
                "Please check the error details above or ask for your playlists again to receive a fresh authorization link."
            )

        # Step C: Credentials finalized! Retrieve the new access token
        req = iam_creds.RetrieveCredentialsRequest(
            connector=SPOTIFY_3LO_AUTH_PROVIDER,
            user_id=user_id,
            scopes=["playlist-read-private"],
            continue_uri=CONTINUE_URI,
        )
        op_final = client.retrieve_credentials(req).operation
        token = None
        if op_final.done and op_final.response:
            resp = iam_creds.RetrieveCredentialsResponse.deserialize(
                op_final.response.value
            )
            token = resp.token

        if not token:
            return (
                "Authorization was finalized, but the access token could not be retrieved. "
                "Please try asking for your playlists again."
            )

    else:
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

        if not token:
            # First turn: Return the direct Spotify authorization link with instructions
            if operation.metadata:
                meta = iam_creds.RetrieveCredentialsMetadata.deserialize(
                    operation.metadata.value
                )
                if meta.uri_consent_required and meta.uri_consent_required.authorization_uri:
                    auth_uri = meta.uri_consent_required.authorization_uri
                    consent_nonce = meta.uri_consent_required.consent_nonce

                    _PENDING_NONCES[user_id] = consent_nonce
                    _PENDING_NONCES["latest"] = consent_nonce

                    try:
                        tool_context.state["user_id"] = user_id
                        tool_context.state["consent_nonce"] = consent_nonce
                        print(f"DEBUG SAVED STATE: user_id={user_id}, consent_nonce={consent_nonce}", flush=True)
                    except Exception as e:
                        print(f"Warning saving tool_context.state: {e}", flush=True)

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
