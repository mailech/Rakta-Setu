"""
Amazon S3 audit trail — proof of AWS usage + the spec's "responsible data / audit"
requirement. Every closed negotiation (transcript + outcome + lessons) is written to
S3 as an immutable JSON object you can open in the AWS console live on stage.

Config (persisted to data/aws_cfg.json or env):
  AWS_S3_BUCKET, AWS_REGION   (creds via the standard boto3 chain: env / ~/.aws / IAM)
"""
import json
import os
from datetime import datetime
from pathlib import Path

_F = Path(__file__).parent.parent / "data" / "aws_cfg.json"

cfg = {
    "bucket":  os.environ.get("AWS_S3_BUCKET", ""),
    "region":  os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION") or "ap-south-1",
    "enabled": True,
}
if _F.exists():
    try:
        for k, v in json.loads(_F.read_text(encoding="utf-8")).items():
            if k in cfg and v not in (None, ""):
                cfg[k] = v
    except Exception:
        pass

last = {"status": "none", "key": None, "ts": None, "detail": None, "count": 0}


def _have_aws() -> bool:
    """True if boto3 can resolve credentials anywhere (env vars, ~/.aws, IAM role)."""
    if os.environ.get("AWS_ACCESS_KEY_ID") and os.environ.get("AWS_SECRET_ACCESS_KEY"):
        return True
    try:
        import boto3
        return boto3.Session().get_credentials() is not None
    except Exception:
        return False


def set_config(**kw):
    for k, v in kw.items():
        if k in cfg and v is not None:
            cfg[k] = v.strip() if isinstance(v, str) else v
    try:
        _F.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass
    return public_config()


def public_config() -> dict:
    return {
        "enabled":    cfg["enabled"],
        "bucket":     cfg["bucket"],
        "region":     cfg["region"],
        "aws_creds":  _have_aws(),
        "last":       last,
    }


def upload(neg_id: str, payload: dict, ts: str = None) -> dict:
    """Write one negotiation record to s3://<bucket>/rakta-setu/negotiations/<neg_id>.json"""
    global last
    ts = ts or datetime.utcnow().isoformat()
    if not cfg.get("enabled") or not cfg.get("bucket"):
        last = {**last, "status": "skipped", "ts": ts, "detail": "no bucket set"}
        return last
    key = f"rakta-setu/negotiations/{neg_id}.json"
    if not _have_aws():
        print(f"[s3:MOCK] would put s3://{cfg['bucket']}/{key}")
        last = {**last, "status": "mock", "key": key, "ts": ts, "count": last["count"] + 1}
        return last
    try:
        import boto3
        boto3.client("s3", region_name=cfg["region"]).put_object(
            Bucket=cfg["bucket"], Key=key,
            Body=json.dumps(payload, default=str, indent=2).encode(),
            ContentType="application/json",
        )
        print(f"[s3] put s3://{cfg['bucket']}/{key}")
        last = {"status": "uploaded", "key": key, "ts": ts, "detail": None, "count": last["count"] + 1}
    except Exception as e:
        print(f"[s3] error: {e}")
        last = {**last, "status": "error", "key": key, "ts": ts, "detail": str(e)}
    return last
