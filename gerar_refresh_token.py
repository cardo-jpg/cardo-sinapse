"""
Execute este script UMA VEZ para gerar o refresh token do Google Ads.
Ele vai abrir o navegador para você autorizar o acesso.

Execute: python gerar_refresh_token.py
"""
import os
from dotenv import load_dotenv
from google_auth_oauthlib.flow import InstalledAppFlow

load_dotenv()

CLIENT_ID = os.getenv("GOOGLE_ADS_CLIENT_ID")
CLIENT_SECRET = os.getenv("GOOGLE_ADS_CLIENT_SECRET")

if not CLIENT_ID or not CLIENT_SECRET:
    print("Preencha GOOGLE_ADS_CLIENT_ID e GOOGLE_ADS_CLIENT_SECRET no .env antes de rodar.")
    exit(1)

SCOPES = ["https://www.googleapis.com/auth/adwords"]

client_config = {
    "installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uris": ["http://localhost"],
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
    }
}

flow = InstalledAppFlow.from_client_config(client_config, scopes=SCOPES)
credentials = flow.run_local_server(port=0, prompt="consent", access_type="offline")

print("\n" + "="*60)
print("REFRESH TOKEN GERADO COM SUCESSO!")
print("="*60)
print(f"\nCopie e cole no seu .env:\n")
print(f"GOOGLE_ADS_REFRESH_TOKEN={credentials.refresh_token}")
print("\n" + "="*60)
