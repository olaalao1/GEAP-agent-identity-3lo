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

"""Deployment script for ADK Agent to Gemini Enterprise Agent Platform."""

import os
import subprocess
import sys
import vertexai
from vertexai import types
from agent import adk_app

PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "gemini-cyber")
LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1")
STAGING_BUCKET = os.environ.get("STAGING_BUCKET", "gs://agent-staging-buck")
CONNECTOR_ID = os.environ.get("SPOTIFY_3LO_AUTH_PROVIDER_ID", "spotify-3lo-auth")
DISPLAY_NAME = os.environ.get("AGENT_DISPLAY_NAME", "spotify-3lo-agent")

print(f"Initializing Vertex AI SDK for project: {PROJECT_ID}, location: {LOCATION}...")
vertexai.init(
    project=PROJECT_ID,
    location=LOCATION,
    staging_bucket=STAGING_BUCKET,
)

client = vertexai.Client(
    project=PROJECT_ID,
    location=LOCATION,
    http_options=dict(api_version="v1beta1"),
)

# Note: google-cloud-aiplatform uses [agent_engines] extra; omit [adk] extra here
# as it conflicts with google-adk>=2.0.
requirements = [
    "google-cloud-aiplatform[agent_engines]==1.153.1",
    "google-adk[agent-identity]==2.5.0",
    "google-cloud-iamconnectorcredentials==0.1.1",
    "httpx==0.28.1",
    "cloudpickle",
    "pydantic",
]

deploy_config = {
    "display_name": DISPLAY_NAME,
    "description": "ADK Agent with Spotify 3LO Authentication via GCP Auth Manager",
    "identity_type": types.IdentityType.AGENT_IDENTITY,
    "staging_bucket": STAGING_BUCKET,
    "requirements": requirements,
    "extra_packages": ["agent.py"],
}

print(f"Deploying agent '{DISPLAY_NAME}' with AGENT_IDENTITY to Agent Engine...")
print("This may take 1-3 minutes to package and stage in Vertex AI...")

engine = client.agent_engines.create(
    agent=adk_app,
    config=deploy_config,
)

engine_resource_name = engine.api_resource.name
engine_id = engine_resource_name.split("/")[-1]

print("\n" + "=" * 70)
print(f"SUCCESS: Agent deployed successfully!")
print(f"Resource Name: {engine_resource_name}")
print(f"Engine ID:     {engine_id}")
print("=" * 70)

# Check and authorize agent identity on connector
try:
    print("\nRetrieving Project Number and Organization ID for Agent SPIFFE Identity...")
    project_number = subprocess.check_output(
        ["gcloud", "projects", "describe", PROJECT_ID, "--format=value(projectNumber)"],
        text=True,
    ).strip()
    org_id = subprocess.check_output(
        ["gcloud", "projects", "get-ancestors", PROJECT_ID, "--format=value(id)"],
        text=True,
    ).strip().splitlines()[-1]

    spiffe_member = (
        f"principal://agents.global.org-{org_id}.system.id.goog/resources/aiplatform/"
        f"projects/{project_number}/locations/{LOCATION}/reasoningEngines/{engine_id}"
    )

    print(f"Binding 'roles/iamconnectors.user' on connector '{CONNECTOR_ID}' for:")
    print(f"  {spiffe_member}")

    cmd = [
        "gcloud", "alpha", "agent-identity", "connectors", "add-iam-policy-binding",
        CONNECTOR_ID,
        f"--project={PROJECT_ID}",
        f"--location={LOCATION}",
        "--role=roles/iamconnectors.user",
        f"--member={spiffe_member}",
    ]
    subprocess.run(cmd, check=True)
    print("Connector IAM binding granted successfully!")
except Exception as e:
    print(f"Warning: Failed to automatically apply IAM binding: {e}")
    print("You can apply it manually using the gcloud command shown above.")

print("\nDeployment complete.")
