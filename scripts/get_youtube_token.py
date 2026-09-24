"""Run this once on your own computer to get a YouTube refresh token.

1. Google Cloud Console → create a project → enable "YouTube Data API v3".
2. OAuth consent screen → External → add your Google account as a test user
   (or publish the app so the token does not expire after 7 days).
3. Credentials → Create OAuth client ID → "Desktop app" → download the JSON
   as client_secret.json next to this script.
4. pip install google-auth-oauthlib && python scripts/get_youtube_token.py
5. Sign in with the Google account that owns the channel (pick the channel /
   brand account if asked), then copy the three printed values into
   GitHub → Settings → Secrets and variables → Actions.
"""

import json
import sys
from pathlib import Path

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.upload",
          "https://www.googleapis.com/auth/youtube",
          # lets the pipeline move each day's ZIP to the Drive trash after publishing
          "https://www.googleapis.com/auth/drive"]

secret_file = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("client_secret.json"))
flow = InstalledAppFlow.from_client_secrets_file(str(secret_file), SCOPES)
creds = flow.run_local_server(port=0, access_type="offline", prompt="consent")

client = json.loads(secret_file.read_text())
client = client.get("installed") or client.get("web")
print("\nAdd these as GitHub repository secrets:\n")
print(f"YT_CLIENT_ID={client['client_id']}")
print(f"YT_CLIENT_SECRET={client['client_secret']}")
print(f"YT_REFRESH_TOKEN={creds.refresh_token}")
