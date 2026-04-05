# SPACR: Secured SPA on Cloud Run with integrated Slack Agent

SPACR is a decoupled, serverless generative AI architecture deployed on Google Cloud. It demonstrates a highly scalable enterprise pattern for securely delivering AI agents to end-users via a Backend-For-Frontend (BFF) microservice mesh. 

By leveraging Google Identity-Aware Proxy (IAP), federated IdP (Okta), and Cloud Run, this architecture ensures that AI agents remain completely isolated from the public internet while securely acting on behalf of the authenticated human user.

## Architecture Overview

*(Insert your Architecture Diagram here)*
![SPACR Architecture](./images/architecture-diagram.png)

This platform is split into three distinct Cloud Run containers, enforcing the principle of least privilege:
1. **`spacr-spa`**: A stateless React/HTML frontend. Contains no secrets and performs no AI logic.
2. **`spacr-bff`**: The Identity Broker and Security Gateway.
3. **`spacr_slack_agent`**: The isolated Gemini execution engine.

### The Identity Identity & Routing Flow
By leveraging Google Identity-Aware Proxy (IAP) integrated with **Cloud Identity for SAML SSO (via Okta)**, this architecture ensures that AI agents remain completely isolated from the public internet while securely acting on behalf of the authenticated human user. 

To achieve this, **IAP is configured with SAML propagation enabled**. When a user authenticates via Okta, IAP intercepts the session, extracts the federated SAML claims, injects them into secure `x-goog-iap-attr-*` HTTP headers, and forwards them downstream to the BFF backend for processing.

### The BFF Core Responsibilities
The `spacr-bff` acts as the central nervous system of this architecture, handling six critical functions so the downstream agents don't have to:

1. **Identity & SAML Parsing**: Reads the injected `x-goog-iap-attr-` headers securely passed down by IAP, decodes the URL-encoded strings, and packages them into a JSON response for the UI.
2. **3P OAuth 2.0 Flow**: Generates the 3rd-party (Slack) authorization URL, catches the `/callback` redirect, exchanges the auth code for an access token, and returns the user to the SPA.
3. **State Management & Token Vaulting**: Securely reads and writes the user's On-Behalf-Of (OBO) Slack tokens to a serverless Firestore database, keeping the BFF horizontally scalable and stateless.
4. **A2A Authentication**: Utilizes Google Auth libraries to generate an ephemeral OIDC identity token, attaching it as a Bearer token to prove its identity to the internal, locked-down Agent container.
5. **Context Injection**: Securely passes the authenticated user's email and OBO token down to the Agent payload, granting the AI isolated access to execute tools.
6. **Model Armor with Correlation**: Hashes the user email, SPA origin, and target agent to create a secure `ma-client-correlation-id`. It makes a synchronous call to the Model Armor API to scan for jailbreaks/injections and blocks malicious requests with a custom 403 response before they ever reach the Agent.

---

## The User Experience Flow

The architecture provides a seamless, Zero-Trust user journey:

1. **Federated Authentication:** The user authenticates via Okta.
![Okta Login](./images/okta-login.png)

2. **Dashboard Initialization:** The SPA loads, fetching SAML claims from IAP. If no OBO token is vaulted, access to the agent is restricted.
![SPA No Token](./images/spa-no-token.png)

3. **Invisible SSO & Consent:** The user connects to Slack. Because the Workspace is federated, Google SSO handles the handshake, only prompting for required OAuth scopes.
`![Google SSO](./images/google-sso.png)`
![Slack Consent](./images/slack-consent.png)

4. **Secure Execution & AI Firewall:** The token is vaulted. The user can successfully invoke the agent to read/write to Slack. If a malicious prompt is detected, Model Armor intercepts and rejects it instantly.
![SPA Execution](./images/spa-execution.png)

---

## Prerequisites & Infrastructure Responsibilities

This repository contains **application code only**. The deployer is responsible for provisioning the underlying Google Cloud infrastructure and networking. 

Before deploying these containers, you must have the following configured:

* **Google Cloud Project** with billing enabled.
* **APIs Enabled**: Cloud Run, Firestore, Secret Manager, Model Armor.
* **Identity & Networking**:
  * An Internal HTTP(S) Load Balancer.
  * Identity-Aware Proxy (IAP) enabled on the Load Balancer backend service.
  * Google Cloud Identity configured for SAML SSO and federated with Okta.
  * **CRITICAL:** IAP must have **SAML propagation enabled** so that Okta claims are successfully injected into the headers and passed to the backend.
* **Database & Secrets**:
  * A Firestore Database running in Native Mode.
  * Secret Manager populated with your `GEMINI_API_KEY`, `SLACK_CLIENT_ID`, and `SLACK_CLIENT_SECRET`.
* **Slack Integration**:
  * A Slack App created at `api.slack.com` with the required user scopes (`chat:write`, `channels:history`, etc.).
  * The Slack Redirect URI configured to point to your internal Load Balancer domain: `https://<YOUR_INTERNAL_DOMAIN>/auth/slack/callback`.

## Deployment Guide

Deploy the microservices in the following order to ensure dependencies are met.

### 1. Deploy the Agent (`spacr_slack_agent`)
The agent must be deployed first so you can capture its internal URL.
* **Ingress**: Internal Only.
* **Authentication**: Require Authentication (`--no-allow-unauthenticated`).
* **Secrets**: Mount the `GEMINI_API_KEY` from Secret Manager as an environment variable.

### 2. Deploy the BFF (`spacr_bff`)
* **Ingress**: Internal Load Balancing.
* **Authentication**: Allow Unauthenticated (IAP handles the human authentication at the LB level).
* **Configuration**: Set the following environment variables:
  * `AGENT_SERVICE_URL`: The internal Cloud Run URL of the Agent deployed in Step 1.
  * `GCP_PROJECT_ID`: Your Google Cloud Project ID.
  * `GCP_LOCATION`: The region for your Model Armor template (e.g., `us-central1`).
  * `SLACK_REDIRECT_URI`: Your exact ILB callback URL.

### 3. Deploy the SPA (`spacr_spa`)
* **Ingress**: Internal Load Balancing.
* **Authentication**: Allow Unauthenticated.
* Ensure your `index.html` API variables point to the root domain of your Load Balancer.

### 4. Configure IAM & Routing
Finally, map the services and apply the strict IAM boundaries:
* Route `/*` on the Load Balancer to the SPA container.
* Route `/chat`, `/me`, and `/auth/*` on the Load Balancer to the BFF container.
* Grant the BFF Service Account the `roles/datastore.user` role to read/write the token vault.
* Grant the BFF Service Account the `roles/run.invoker` role on the Agent container to allow A2A communication.
* Grant the BFF Service Account the `roles/modelarmor.user` role to evaluate prompts.