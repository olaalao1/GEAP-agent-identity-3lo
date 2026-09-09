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
from typing import Any

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

# 1. Full GCP Auth Manager Connector Resource String
SPOTIFY_3LO_AUTH_PROVIDER = (
    f"projects/{PROJECT_ID}/locations/{LOCATION}/connectors/"
    f"{SPOTIFY_3LO_AUTH_PROVIDER_ID}"
)

# 2. Frontend Return URL (continue_uri)
CONTINUE_URI = os.environ.get(
    "CONTINUE_URI", "http://localhost:8080/commit"
)

MODEL = "gemini-2.5-flash"


# ==============================================================================
# 3. Spotify Authenticated Tool Function (with Conversational OAuth Link)
# ==============================================================================
async def spotify_get_playlists(tool_context: ToolContext) -> str | list[dict[str, Any]]:
    """Fetches the current user's private playlists from Spotify.

    If the user has not yet authorized Spotify access, this tool automatically
    returns a clickable Spotify authorization link directly in the chat.
    """
    user_id = tool_context.user_id or "default_user_id"
    client = iam_creds.IAMConnectorCredentialsServiceClient(transport="rest")

    # Step A: Query Google Cloud Auth Manager for user credentials (SINGLE CALL ONLY)
    req = iam_creds.RetrieveCredentialsRequest(
        connector=SPOTIFY_3LO_AUTH_PROVIDER,
        user_id=user_id,
        scopes=["playlist-read-private"],
        continue_uri=CONTINUE_URI,
    )
    operation = client.retrieve_credentials(req).operation

    token = None
    if operation.done and operation.response:
        resp = iam_creds.RetrieveCredentialsResponse.deserialize(operation.response.value)
        if resp.token:
            token = resp.token

    # Step B: If token is not yet ready, generate the authorization link for the user
    if not token:
        if operation.metadata:
            meta = iam_creds.RetrieveCredentialsMetadata.deserialize(operation.metadata.value)
            if meta.uri_consent_required and meta.uri_consent_required.authorization_uri:
                auth_uri = meta.uri_consent_required.authorization_uri
                consent_nonce = meta.uri_consent_required.consent_nonce

                # Base host from CONTINUE_URI (e.g., http://localhost:8080)
                base_host = CONTINUE_URI.rsplit("/", 1)[0]
                import urllib.parse
                start_auth_url = (
                    f"{base_host}/start-auth?"
                    f"user_id={urllib.parse.quote(user_id)}&"
                    f"consent_nonce={urllib.parse.quote(consent_nonce)}&"
                    f"auth_uri={urllib.parse.quote(auth_uri)}"
                )

                return (
                    "🔒 **Spotify Authorization Required**\n\n"
                    "To access your private playlists, please authorize access to your Spotify account:\n\n"
                    f"👉 [**Click here to Authorize Spotify Access**]({start_auth_url})\n\n"
                    "*(Once you click the link and complete authorization in your browser, "
                    "return to this chat and reply with **'Done'** or **'Fetch my playlists'**)*"
                )

        return "Error: Unable to retrieve credentials or generate authorization URL from Auth Manager."

    # Step C: When token is available, query Spotify Web API
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
        "You are a helpful Spotify assistant. When the user asks for their playlists, "
        "always use the spotify_get_playlists tool. If the tool returns an authorization "
        "link, display it clearly as a clickable markdown link without altering the URL and "
        "prompt the user to complete authorization. Once authorized, format the playlist details cleanly."
    ),
    tools=[spotify_tool],
)

app = App(
    name="spotify_3lo_app",
    root_agent=root_agent,
)

# Wrapper instance for Gemini Enterprise Agent Platform deployment
adk_app = agent_engines.AdkApp(app=app)
