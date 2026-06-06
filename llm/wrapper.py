"""
LLM Wrapper — single entry point for all agent language + judgment calls.

Backends:
  • Amazon Bedrock (Claude Haiku)  — the agents' real "brain" (when enabled + creds)
  • Offline mock                   — canned persona/lesson responses for fast demos

Bedrock is toggled at runtime (Setup tab / API), persisted to data/bedrock_cfg.json.
Claude on Bedrock lives in us-east-1 / us-west-2 (NOT ap-south-1), so the Bedrock
region is configured separately from S3/Translate. Any Bedrock error degrades
gracefully to the mock so the demo never breaks.
"""
import json
import os
import hashlib
from pathlib import Path

CACHE_DIR = Path(__file__).parent.parent / "data" / "llm_cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_CFG_FILE = Path(__file__).parent.parent / "data" / "bedrock_cfg.json"

cfg = {
    "enabled": os.environ.get("USE_BEDROCK", "").lower() in ("1", "true", "yes"),
    # Current active Bedrock model (Claude Haiku 4.5) via the us cross-region
    # inference profile — older Claude 3.x/3.5 IDs are now legacy/EOL, and the
    # bare model id requires the 'us.' inference-profile prefix.
    "model":   os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"),
    "region":  os.environ.get("BEDROCK_REGION", "us-east-1"),
}
if _CFG_FILE.exists():
    try:
        for k, v in json.loads(_CFG_FILE.read_text(encoding="utf-8")).items():
            if k in cfg and v is not None:
                cfg[k] = v
    except Exception:
        pass

last_call = {"backend": "none", "ok": None, "detail": None}

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


def _have_aws() -> bool:
    """True if boto3 can resolve credentials anywhere (env vars, ~/.aws, IAM role)."""
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True
    try:
        import boto3
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def set_config(**kw) -> dict:
    for k, v in kw.items():
        if k in cfg and v is not None:
            cfg[k] = v.strip() if isinstance(v, str) else v
    try:
        _CFG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass
    return public_config()


def public_config() -> dict:
    return {
        "enabled":    cfg["enabled"],
        "model":      cfg["model"],
        "region":     cfg["region"],
        "aws_creds":  _have_aws(),
        "active":     bool(cfg["enabled"] and _have_aws()),
        "last_call":  last_call,
    }


def bedrock_active() -> bool:
    """True when the agents should call Bedrock (enabled + creds present)."""
    return bool(cfg["enabled"] and _have_aws())


def _cache_key(system: str, user: str) -> str:
    return hashlib.md5(f"{cfg['model']}|{system}|||{user}".encode()).hexdigest()


def llm(system_prompt: str, user_prompt: str, json_mode: bool = True) -> dict:
    """Single entry point for all LLM calls (Bedrock when active, else mock)."""
    key = _cache_key(system_prompt, user_prompt)
    p = CACHE_DIR / f"{key}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))

    result = _bedrock_call(system_prompt, user_prompt) if bedrock_active() \
        else _mock_respond(system_prompt, user_prompt)
    p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    return result


def _mock_respond(system_prompt: str, user_prompt: str) -> dict:
    global last_call
    last_call = {"backend": "mock", "ok": True, "detail": None}
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
    global last_call
    try:
        import boto3
        client = boto3.client("bedrock-runtime", region_name=cfg["region"])
        body = json.dumps({
            "anthropic_version": "bedrock-2023-05-31",
            "max_tokens": 512,
            "temperature": 0.4,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
        })
        response = client.invoke_model(
            modelId=cfg["model"], contentType="application/json",
            accept="application/json", body=body,
        )
        raw = json.loads(response["body"].read())
        text = raw["content"][0]["text"].strip()
        last_call = {"backend": "bedrock", "ok": True, "detail": cfg["model"]}
        if "{" in text:
            return json.loads(text[text.index("{"):text.rindex("}") + 1])
        return {"action": "DECLINE", "params": {}, "say": text[:120]}
    except Exception as e:
        print(f"[llm] Bedrock error: {e} — falling back to mock")
        last_call = {"backend": "bedrock", "ok": False, "detail": str(e)[:160]}
        return _mock_respond(system_prompt, user_prompt)
