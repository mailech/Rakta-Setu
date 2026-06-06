# 🩸 RAKTA-SETU — Autonomous Multi-Agent Blood-Bridge Negotiation Network

> *Everyone built bots that call donors. We gave every donor a bot of their own — and taught the bots to negotiate, so a transfusion gets covered in seconds with one notification instead of days and dozens of calls.*

**AI For Good 2.0 Hackathon · Blood Warriors problem statement**

---

## What it is

A society of AI agents that coordinate blood donations for Thalassemia patients:

- **Proxy Agent** — one per donor, loyal to the *donor*. Decides (eligibility, fatigue, preferences) whether to even disturb its human, and shields their health data.
- **Guardian Agent** — one per patient, loyal to the *patient*. Ranks donors, asks the best ones, escalates if short.
- **Exchange Agent** — the network broker. Borrows donors from other bridges, taps the emergency/reactivation pools, arbitrates conflicts.

The agents negotiate **machine-to-machine** through a **LangGraph** state machine (the "Floor"). The LLM is used only for language/judgment; all facts (eligibility, distance, fatigue, blood compatibility) are deterministic — fast, cheap, and impossible to hallucinate.

A patient/hospital describes a need → the system finds every compatible donor **within 10 km** → their Proxies respond live → the donor gets a **real SMS, and a phone call if they don't answer** → on confirm, the **patient is texted** the donor's details.

## Architecture

```
React (Vite) UI ──REST/WebSocket──► FastAPI
                                      │
                          LangGraph "Floor" (agents/graph.py)
            broadcast → collect → resolve → (escalate) → confirm → learn
                │            │         │          │          │        │
            Guardian     Proxies   assignment  Exchange   human    failure
                                                          (SMS/call) learning
                                      │
   Deterministic intelligence: eligibility · propensity · fatigue · churn (LightGBM) · haversine
                                      │
   Data: SQLite event_log · donors.json · bridges.json · Amazon S3 audit trail
```

## Tech

- **Agents:** LangGraph `StateGraph` (`agents/graph.py`) + Proxy/Guardian/Exchange classes
- **Backend:** FastAPI + WebSocket, SQLite event log
- **ML:** LightGBM/logistic churn model (`ml/churn.py`)
- **Real comms:** Twilio (donor SMS + escalation call, two-way), with the patient notified by SMS
- **AWS:** Amazon S3 audit trail of every negotiation; LLM wrapper ready for Amazon Bedrock
- **Frontend:** React + Recharts — Floor, Bridge Board, Prevention, Setup

## Quick start

```bash
# 1. backend
cd rakta-setu
pip install -r requirements.txt
python -m data.engine          # builds donors.json / bridges.json (if not present)
python -m uvicorn api.main:app --port 8000

# 2. frontend (separate terminal)
cd frontend
npm install
npm run dev                     # http://localhost:5173
```

CLI negotiation: `python -m sim.run --bridge 0`
Failure-learning replay: `python -m sim.run --bridge 0 --replay`

## Real phone alerts (optional — Twilio)

Set in the **⚙️ Setup** tab (or env): Twilio Account SID, Auth Token, a Voice+SMS number, and a public tunnel URL (`ngrok http 8000` → Auto-detect). Add real donor numbers to `data/live_phones.json` (see `live_phones.example.json`). Donor confirms by **answering the call and pressing 1** (SMS replies from India to a US number aren't supported by carriers).

## AWS (optional)

Create an S3 bucket + IAM access key, then:
```bash
$env:AWS_ACCESS_KEY_ID="..."; $env:AWS_SECRET_ACCESS_KEY="..."; $env:AWS_REGION="ap-south-1"
```
Set the bucket name in Setup → every negotiation's transcript is written to S3 as an audit record.

## Secrets

Credentials and PII are **git-ignored** (`data/twilio_cfg.json`, `data/live_phones.json`, `data/aws_cfg.json`, `.env`). Never commit them; configure via the Setup tab or environment variables.
