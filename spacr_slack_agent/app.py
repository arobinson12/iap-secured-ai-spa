import os
from fastapi import FastAPI, Request, HTTPException
from google.genai import types

# 1. Bring back the ADK Engine components
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

# Import your agent and state context
from agent import root_agent, request_state

app = FastAPI()

# 2. Re-initialize the Runner for this microservice
session_service = InMemorySessionService()
runner = Runner(app_name="spacr_agent", agent=root_agent, session_service=session_service)

@app.post("/invoke")
async def invoke_agent(request: Request):
    payload = await request.json()
    
    user_email = payload.get("user_email")
    prompt_text = payload.get("prompt")
    
    # Inject the state (Slack token and Identity) passed down from the Broker
    request_state.set({
        "iap_email": user_email,
        "slack_obo_token": payload.get("slack_token")
    })
    
    try:
        # 3. Create an execution session
        session = await session_service.create_session(app_name="spacr_agent", user_id=user_email)
        
        # 4. Format the prompt for ADK
        content = types.Content(role="user", parts=[types.Part(text=prompt_text)])
        
        # 5. Run the agent stream
        events = runner.run_async(
            user_id=user_email,
            session_id=session.id,
            new_message=content
        )
        
        final_text = "The agent did not return a response."
        
        # Collect the final output
        async for event in events:
            if event.is_final_response() and event.content and event.content.parts:
                final_text = event.content.parts[0].text
                
        return {"response": final_text}
        
    except Exception as e:
        print(f"Agent Execution Error: {str(e)}") # This will print to Cloud Run logs
        raise HTTPException(status_code=500, detail=str(e))