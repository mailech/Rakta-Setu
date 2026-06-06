"""
LLM Wrapper — single function for all Bedrock / mock LLM calls.
Swap provider in ONE place. Mock mode for offline/demo.
"""
import json
import os
import hashlib
from pathlib import Path

MOCK_LLM = os.environ.get("MOCK_LLM", "true").lower() in ("true", "1", "yes")
CACHE_DIR = Path(__file__).parent.parent / "data" / "llm_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "anthropic.claude-3-haiku-20240307-v1:0")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")

MOCK_PROXY_RESPONSES = [
    {"action": "REQUEST_HUMAN_CONFIRM", "params": {}, "say": "My donor is available — checking in with them now."},
    {"action": "CONDITIONAL_OFFER", "params": {"condition": "morning_slot"}, "say": "My donor can help, but prefers a morning slot before 10 AM."},
    {"action": "COUNTER", "params": {"reason": "prefers_weekend"}, "say": "My donor can commit, but weekends work much better."},
    {"action": "OFFER", "params": {}, "say": "Ready to help — happy to confirm immediately."},
]

MOCK_LESSON_RESPONSE = {
    "outcome": "PARTIAL",
    "failures": [
        {"type": "DECLINED_SHORT_NOTICE", "fix": {"target": "guardian", "field": "lead_days", "new": 10}},
        {"type": "OVERASKED_SEGMENT", "fix": {"target": "guardian", "field": "overbook_factor", "new": 2.0}},
    ],
    "summary": "Two donors declined due to short notice. Guardian should open negotiation earlier."
}


def _cache_key(system: str, user: str) -> str:
    return hashlib.md5(f"{system}|||{user}".encode()).hexdigest()


def llm(system_prompt: str, user_prompt: str, json_mode: bool = True) -> dict:
    """Single entry point for all LLM calls."""
    key = _cache_key(system_prompt, user_prompt)
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))

    result = _mock_respond(system_prompt, user_prompt) if MOCK_LLM else _bedrock_call(system_prompt, user_prompt)
    p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _mock_respond(system_prompt: str, user_prompt: str) -> dict:
    sp = system_prompt.lower()
    if "lesson" in sp or "failure" in sp or "summary" in sp:
        return MOCK_LESSON_RESPONSE
    if "representative" in sp or "proxy" in sp or "donor" in sp:
        idx = hash(system_prompt + user_prompt) % len(MOCK_PROXY_RESPONSES)
        resp = dict(MOCK_PROXY_RESPONSES[idx])
        import re
        dates = re.findall(r'\d{4}-\d{2}-\d{2}', user_prompt)
        if dates:
            resp.setdefault("params", {})["date"] = dates[0]
        return resp
    return {"action": "OFFER", "params": {}, "say": "Ready to assist."}


def _bedrock_call(system_prompt: str, user_prompt: str) -> dict:
    try:
        import boto3
        client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "temperature": 0.4,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}]
        })
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID, contentType="application/json",
            accept="application/json", body=body
        )
        raw = json.loads(response["body"].read())
        text = raw["content"][0]["text"].strip()
        if "{" in text:
            start = text.index("{")
            end = text.rindex("}") + 1
            return json.loads(text[start:end])
        return {"action": "DECLINE", "params": {}, "say": text[:100]}
    except Exception as e:
        print(f"[llm] Bedrock error: {e} — falling back to mock")
        return _mock_respond(system_prompt, user_prompt)
