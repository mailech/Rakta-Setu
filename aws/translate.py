"""
Amazon Translate — "Rural Reach": translate the donor's urgency SMS into their
preferred language (Telugu / Hindi / Kannada / …) right before Twilio sends it.
An English SMS gets ignored by a rural donor; their own language doesn't.

Config (persisted to data/translate_cfg.json or env): region, enabled.
No AWS creds → returns the original English text (graceful, demo never breaks).
"""
import json
import os
from pathlib import Path

_CFG_FILE = Path(__file__).parent.parent / "data" / "translate_cfg.json"

# Amazon Translate language codes we support for the demo
LANGS = {
    "en": "English", "hi": "Hindi", "te": "Telugu",
    "kn": "Kannada", "ta": "Tamil", "mr": "Marathi", "bn": "Bengali",
}

cfg = {
    "enabled": os.environ.get("USE_TRANSLATE", "").lower() in ("1", "true", "yes"),
    "region":  os.environ.get("TRANSLATE_REGION") or os.environ.get("AWS_REGION") or "ap-south-1",
}
if _CFG_FILE.exists():
    try:
        for k, v in json.loads(_CFG_FILE.read_text(encoding="utf-8")).items():
            if k in cfg and v is not None:
                cfg[k] = v
    except Exception:
        pass

last = {"target": None, "status": "none", "detail": None}


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
        "enabled":   cfg["enabled"],
        "region":    cfg["region"],
        "aws_creds": _have_aws(),
        "active":    bool(cfg["enabled"] and _have_aws()),
        "languages": LANGS,
        "last":      last,
    }


def active() -> bool:
    return bool(cfg["enabled"] and _have_aws())


def translate(text: str, target_lang: str) -> dict:
    """
    Translate English `text` -> target_lang (code). Returns
    {text, lang, lang_name, translated: bool}. No-op for English / when inactive.
    """
    global last
    target_lang = (target_lang or "en").lower()
    name = LANGS.get(target_lang, target_lang)
    if target_lang == "en" or not active():
        last = {"target": target_lang, "status": "skipped" if target_lang != "en" else "english",
                "detail": None}
        return {"text": text, "lang": target_lang, "lang_name": name, "translated": False}
    try:
        import boto3
        out = boto3.client("translate", region_name=cfg["region"]).translate_text(
            Text=text, SourceLanguageCode="en", TargetLanguageCode=target_lang)
        translated = out["TranslatedText"]
        last = {"target": target_lang, "status": "translated", "detail": name}
        return {"text": translated, "lang": target_lang, "lang_name": name, "translated": True}
    except Exception as e:
        print(f"[translate] error: {e}")
        last = {"target": target_lang, "status": "error", "detail": str(e)[:140]}
        return {"text": text, "lang": target_lang, "lang_name": name, "translated": False}
