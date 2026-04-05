import os
import requests
import uvicorn
import urllib.parse
import hashlib
from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# 1. IMPORT FIRESTORE, AUTH & MODEL ARMOR LIBRARIES
from google.cloud import firestore
import google.auth.transport.requests
import google.oauth2.id_token
from google.cloud import modelarmor_v1
from google.api_core.client_options import ClientOptions

app = FastAPI(title="SPACR BFF API")

# CORS CONFIGURATION
ALLOWED_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "*"                       
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 2. INITIALIZE FIRESTORE & INFRASTRUCTURE CONFIG
db = firestore.Client()

# --- INFRASTRUCTURE VARIABLES ---
# These allow the app to be portable across different GCP projects and environments
LOCATION = os.environ.get("GCP_LOCATION", "us-central1")
PROJECT_ID = os.environ.get("GCP_PROJECT_ID", "YOUR_PROJECT_ID")
TEMPLATE_NAME = os.environ.get("MODEL_ARMOR_TEMPLATE", f"projects/{PROJECT_ID}/locations/{LOCATION}/templates/spacr-ma")
AGENT_SERVICE_URL = os.environ.get("AGENT_SERVICE_URL", "https://your-internal-agent-url.run.app")

# --- 3RD PARTY OAUTH VARIABLES ---
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID")
SLACK_CLIENT_SECRET = os.environ.get("SLACK_CLIENT_SECRET")
SLACK_REDIRECT_URI = os.environ.get("SLACK_REDIRECT_URI")
SPA_FRONTEND_URL = os.environ.get("SPA_FRONTEND_URL", "http://localhost:3000")

# Initialize Model Armor Client using the specific regional endpoint
ma_client = modelarmor_v1.ModelArmorClient(
    transport="rest",
    client_options=ClientOptions(api_endpoint=f"modelarmor.{LOCATION}.rep.googleapis.com")
)

class ChatRequest(BaseModel):
    prompt: str

# 3. FIRESTORE TOKEN HELPERS
def get_slack_token(email: str):
    doc = db.collection("obo_tokens").document(email).get()
    return doc.to_dict().get("slack_token") if doc.exists else None

def save_slack_token(email: str, token: str):
    db.collection("obo_tokens").document(email).set({"slack_token": token})

# --- CORE API ROUTES ---

@app.get("/me")
async def get_identity(request: Request):
    iap_email = request.headers.get("x-goog-authenticated-user-email", "Unauthenticated")
    has_slack_token = get_slack_token(iap_email) is not None

    saml_attributes = {}
    for k, v in request.headers.items():
        k_lower = k.lower()
        decoded_value = urllib.parse.unquote(v)
        
        if k_lower.startswith("x-goog-iap-attr-"):
            clean_key = k_lower.replace("x-goog-iap-attr-", "")
            saml_attributes[clean_key] = decoded_value
            
        elif k_lower.startswith("x-goog-authenticated-user-") and k_lower != "x-goog-authenticated-user-email":
            clean_key = k_lower.replace("x-goog-authenticated-user-", "")
            saml_attributes[clean_key] = decoded_value

    return {
        "iap_email": iap_email,
        "has_slack_token": has_slack_token,
        "saml_attributes": saml_attributes,
        "slack_auth_url": f"/auth/slack/login?user_email={iap_email}"
    }

@app.post("/chat")
async def chat_endpoint(request: Request, chat_req: ChatRequest):
    iap_email = request.headers.get("x-goog-authenticated-user-email", "local-tester@corp.com")
    slack_obo_token = get_slack_token(iap_email)

    if not slack_obo_token:
        raise HTTPException(status_code=401, detail="Slack connection required.")


    # --- 🛡️ SECURITY CHECKPOINT: MODEL ARMOR ---

    # A. Construct the Audit Correlation ID
    # We hash the email so PII is not leaked into Cloud Logging
    user_hash = hashlib.sha256(iap_email.encode()).hexdigest()[:8]
    spa_origin = request.headers.get("origin", "spacr-ui").replace("https://", "").replace("http://", "")
    agent_target = "spacr-slack-agent"
    
    correlation_id = f"usr-{user_hash}::spa-{spa_origin}::agt-{agent_target}"

    try:
        # B. Prepare the Request Payload
        ma_request = modelarmor_v1.SanitizeUserPromptRequest(
            name=TEMPLATE_NAME,
            user_prompt_data=modelarmor_v1.DataItem(text=chat_req.prompt)
        )
        
        # C. Call the API, passing the Correlation ID as a custom metadata header
        ma_response = ma_client.sanitize_user_prompt(
            request=ma_request,
            metadata=[("ma-client-correlation-id", correlation_id)]
        )
        
        # D. Evaluate the Verdict
        if ma_response.sanitization_result.filter_match_state == modelarmor_v1.FilterMatchState.MATCH_FOUND:
            print(f"[{correlation_id}] BLOCKED: Malicious prompt detected.")
            # Intercept and return the user-friendly warning directly to the UI chat
            return {"response": "Now you know darn well I can't help with that. Try again and keep it simple and focused on this architecture."}
            
    except Exception as e:
        print(f"Security scanning failed: {str(e)}")
        raise HTTPException(status_code=500, detail="Internal Security Check Failed.")

    # --- END SECURITY CHECKPOINT ---


    # Format the token exactly how your agent tools expect it
    formatted_slack_token = f"Bearer {slack_obo_token}" if not slack_obo_token.startswith("Bearer") else slack_obo_token

    # SECURE A2A HANDSHAKE
    try:
        auth_req = google.auth.transport.requests.Request()
        id_token = google.oauth2.id_token.fetch_id_token(auth_req, AGENT_SERVICE_URL)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate A2A token: {str(e)}")

    # PROXY TO AGENT
    try:
        response = requests.post(
            f"{AGENT_SERVICE_URL}/invoke",
            headers={
                "Authorization": f"Bearer {id_token}",
                "Content-Type": "application/json"
            },
            json={
                "prompt": chat_req.prompt,
                "user_email": iap_email,
                "slack_token": formatted_slack_token
            },
            timeout=45 
        )
        
        if not response.ok:
            raise HTTPException(status_code=response.status_code, detail=f"Agent Error: {response.text}")
            
        return response.json()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Broker failed to communicate with Agent: {str(e)}")


# --- OAUTH ROUTES ---

@app.get("/auth/slack/login")
def slack_login(user_email: str):
    user_scopes = "users:read,chat:write,channels:read,channels:history,groups:read,groups:history,im:read,im:history"
    slack_auth_url = (
        f"https://slack.com/oauth/v2/authorize?"
        f"client_id={SLACK_CLIENT_ID}&"
        f"user_scope={user_scopes}&"
        f"redirect_uri={SLACK_REDIRECT_URI}&"
        f"state={user_email}" 
    )
    return RedirectResponse(url=slack_auth_url)

@app.get("/auth/slack/callback")
def slack_callback(code: str, state: str):
    user_email = state 

    response = requests.post(
        "https://slack.com/api/oauth.v2.access",
        data={
            "client_id": SLACK_CLIENT_ID,
            "client_secret": SLACK_CLIENT_SECRET,
            "code": code,
            "redirect_uri": SLACK_REDIRECT_URI
        }
    )
    
    data = response.json()
    
    if not data.get("ok"):
        raise HTTPException(status_code=400, detail=f"Slack Auth Failed: {data.get('error')}")

    user_token = data.get("authed_user", {}).get("access_token")
    save_slack_token(user_email, user_token)

    return RedirectResponse(url=f"{SPA_FRONTEND_URL}?slack_auth=success")

@app.get("/health")
def health_check():
    return {"status": "ok", "message": "SPACR BFF is listening."}

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    uvicorn.run(app, host="0.0.0.0", port=port, proxy_headers=True, forwarded_allow_ips="*")