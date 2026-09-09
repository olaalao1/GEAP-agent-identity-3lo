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
from google.adk.auth.auth_credential import AuthCredential
from google.adk.auth.auth_tool import AuthConfig
from google.adk.auth.credential_manager import CredentialManager
from google.adk.integrations.agent_identity import GcpAuthProvider
from google.adk.integrations.agent_identity import GcpAuthProviderScheme
from google.adk.tools.authenticated_function_tool import AuthenticatedFunctionTool
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
# Where user is redirected after Spotify authorization to finalize credentials
CONTINUE_URI = os.environ.get(
    "CONTINUE_URI", "http://localhost:8080/commit"
)

MODEL = "gemini-2.5-flash"


# ==============================================================================
# 3. Authenticated Tool Function
# ==============================================================================
async def spotify_get_playlists(credential: AuthCredential) -> str | list[dict[str, Any]]:
    """Fetches the current user's private playlists from Spotify."""
    headers = {}
    if http := credential.http:
        if http.scheme and http.credentials and (token := http.credentials.token):
            headers["Authorization"] = f"{http.scheme.title()} {token}"
        if http.additional_headers:
            headers.update(http.additional_headers)

    if not headers:
        return "Error: No authentication token available."

    async with httpx.AsyncClient() as client:
        response = await client.get(
            "https://api.spotify.com/v1/me/playlists",
            headers=headers,
            params={"limit": 10},
        )

        if response.status_code != 200:
            return f"Error from Spotify API: {response.status_code} - {response.text}"

        data = response.json()
        items = data.get("items", [])

        if not items:
            return "No playlists found for the current user."

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
# 4. Auth Provider Scheme & Tool Registration
# ==============================================================================
# Register GCP Agent Identity provider with CredentialManager
CredentialManager.register_auth_provider(GcpAuthProvider())

spotify_auth_config_3lo = AuthConfig(
    auth_scheme=GcpAuthProviderScheme(
        name=SPOTIFY_3LO_AUTH_PROVIDER,
        scopes=["playlist-read-private"],
        continue_uri=CONTINUE_URI,
    )
)

spotify_get_playlist_tool = AuthenticatedFunctionTool(
    func=spotify_get_playlists,
    auth_config=spotify_auth_config_3lo,
)


# ==============================================================================
# 5. Agent & App Definitions
# ==============================================================================
root_agent = Agent(
    name="spotify_3lo_agent",
    model=MODEL,
    instruction=(
        "You are a helpful Spotify assistant. Use your tools to fetch "
        "the user's private playlists when requested. Keep responses concise, "
        "friendly, and well-structured."
    ),
    tools=[spotify_get_playlist_tool],
)

app = App(
    name="spotify_3lo_app",
    root_agent=root_agent,
)

# Wrapper instance for Gemini Enterprise Agent Platform deployment
adk_app = agent_engines.AdkApp(app=app)
