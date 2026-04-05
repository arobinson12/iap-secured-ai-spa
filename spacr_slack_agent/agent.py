import os
import requests
from contextvars import ContextVar
from google.adk.agents import Agent
from google.adk.tools import ToolContext

request_state = ContextVar("request_state", default={})

# --- EXISTING TOOLS ---
def get_identity_info(context: ToolContext) -> dict:
    state = request_state.get()
    return {
        "authenticated_email": state.get("iap_email", "Unauthenticated"),
        "security_context": "Decoupled execution via SPACR BFF",
        "slack_token_status": "Loaded" if state.get("slack_obo_token") else "Missing"
    }

def verify_slack_obo(context: ToolContext) -> dict:
    state = request_state.get()
    obo_token = state.get("slack_obo_token", "").strip()
    if not obo_token: return {"status": "OBO Failed", "error": "Token dropped."}

    try:
        response = requests.post("https://slack.com/api/auth.test", headers={"Authorization": obo_token})
        data = response.json()
        if data.get("ok"):
            return {"status": "Verified", "workspace": data.get("team"), "user": data.get("user")}
        return {"status": "Failed", "error": data.get("error")}
    except Exception as e:
        return {"status": "Network Error", "details": str(e)}

# --- NEW SLACK CAPABILITIES ---
def post_slack_message(context: ToolContext, channel_name: str, message: str) -> dict:
    """Posts a message to a specific Slack channel on behalf of the user."""
    state = request_state.get()
    obo_token = state.get("slack_obo_token", "").strip()
    if not obo_token: return {"error": "No valid Slack token found."}

    # Format the channel name correctly
    clean_channel = channel_name if channel_name.startswith('#') else f"#{channel_name}"

    try:
        response = requests.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": obo_token},
            json={"channel": clean_channel, "text": message}
        )
        return response.json()
    except Exception as e:
        return {"error": str(e)}

def read_slack_messages(context: ToolContext, channel_name: str) -> dict:
    """Reads the most recent messages from a specified Slack channel."""
    state = request_state.get()
    obo_token = state.get("slack_obo_token", "").strip()
    if not obo_token: return {"error": "No valid Slack token found."}

    clean_name = channel_name.replace('#', '')

    try:
        # First, we have to look up the Channel ID (Slack requires IDs for reading history)
        list_resp = requests.get("https://slack.com/api/conversations.list", headers={"Authorization": obo_token})
        channels = list_resp.json().get("channels", [])
        
        target_id = next((c["id"] for c in channels if c["name"] == clean_name), None)
        
        if not target_id:
            return {"error": f"Could not find channel '{channel_name}'. Ensure the agent or user is in the channel."}
            
        # Second, fetch the history using the ID
        history_resp = requests.get(
            f"https://slack.com/api/conversations.history?channel={target_id}&limit=5",
            headers={"Authorization": obo_token}
        )
        return history_resp.json()
    except Exception as e:
        return {"error": str(e)}

# --- AGENT INITIALIZATION ---
root_agent = Agent(
    name='spacr_agent',
    model='gemini-3-flash-preview',
    instruction=(
        'You are the SPACR Agent deployed on Cloud Run. '
        'Greet the user naturally. '
        'If asked to verify connectivity, call get_identity_info and verify_slack_obo. '
        'If asked to post a message to Slack, use the post_slack_message tool. Ask the user for the channel name if they do not provide one. '
        'If asked to read messages from Slack, use the read_slack_messages tool. Summarize the messages cleanly for the user.'
    ),
    tools=[get_identity_info, verify_slack_obo, post_slack_message, read_slack_messages]
)