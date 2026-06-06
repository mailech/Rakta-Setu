"""
Conversational voice agent for the escalation call (Bedrock-powered, multilingual).

Instead of "press 1 to confirm", the donor's phone rings and they have a REAL
spoken conversation:
  • Twilio <Gather input="speech" language="..-IN"> transcribes what they say
  • Amazon Bedrock (Claude) understands it, replies in their language, and decides
    CONFIRM / DECLINE / keep talking
  • Twilio <Say> speaks Bedrock's reply back  → multi-turn loop

Language is configurable (Setup → call language). Telugu speech recognition + a
Telugu-thinking Bedrock work; Telugu *voice* depends on Twilio's te-IN TTS, so
Hindi (Polly Kajal) is the reliable fallback. The agent always understands the
donor regardless of the spoken-reply voice.
"""
import hashlib
import json
from notify import twilio_channel as twi

# Map a clean URL token -> (neg_id, user_id). Donor user_ids contain literal
# backslashes (\x..) which break webhook URLs (double-encoded -> 404), so we never
# put them in the path.
tokens = {}


def _token(neg_id, user_id) -> str:
    tok = hashlib.md5(f"{neg_id}|{user_id}".encode()).hexdigest()[:16]
    tokens[tok] = (neg_id, user_id)
    return tok


def resolve_token(tok):
    return tokens.get(tok, (None, None))

# code -> (STT lang for understanding, display/REPLY language, TTS lang code)
# Twilio & Amazon Polly have NO Telugu/Kannada/Tamil voice, so we synthesize the
# audio ourselves (Google TTS) and <Play> it — letting the bot speak ANY language.
LANG = {
    "en": ("en-IN", "Indian English", "en"),
    "hi": ("hi-IN", "Hindi",          "hi"),
    "te": ("te-IN", "Telugu",         "te"),
    "kn": ("kn-IN", "Kannada",        "kn"),
    "ta": ("ta-IN", "Tamil",          "ta"),
}

# per-call conversation history: (neg_id, user_id) -> {"messages":[...], "ctx":{...}}
conversations = {}


def _lang_for(ctx) -> str:
    return (twi.cfg.get("call_language") or ctx.get("lang") or "te").lower()


def _eleven_mp3(text: str) -> bytes:
    """Natural multilingual TTS via ElevenLabs (Telugu/Hindi/Tamil all native)."""
    import urllib.request
    key = twi.cfg.get("elevenlabs_key")
    voice = twi.cfg.get("elevenlabs_voice") or "EXAVITQu4vr4xnSDxMaL"
    body = json.dumps({"text": text, "model_id": "eleven_multilingual_v2",
                       "voice_settings": {"stability": 0.4, "similarity_boost": 0.8}}).encode()
    req = urllib.request.Request(
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}",
        data=body, method="POST",
        headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
    return urllib.request.urlopen(req, timeout=15).read()


def _google_mp3(text: str, tl: str) -> bytes:
    import urllib.request, urllib.parse
    chunks = [text[i:i + 190] for i in range(0, len(text), 190)] or [text]
    audio = b""
    for ch in chunks:
        u = ("https://translate.google.com/translate_tts?ie=UTF-8&client=tw-ob&tl="
             + urllib.parse.quote(tl) + "&q=" + urllib.parse.quote(ch))
        audio += urllib.request.urlopen(
            urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=10).read()
    return audio


def _voice(text: str, lang: str) -> str:
    """Speak `text` in `lang`. Preference: ElevenLabs (natural) -> Google TTS ->
    Polly.Aditi. Audio is hosted on S3 (presigned) and <Play>ed."""
    tl = LANG.get(lang, LANG["en"])[2]
    try:
        import boto3
        from aws import s3_audit
        if not s3_audit.cfg.get("bucket"):
            raise RuntimeError("no S3 bucket for audio")
        # ElevenLabs free-tier voices are Western (foreign accent on Indian
        # languages); its native Indian voices need a paid plan. Google TTS gives
        # NATIVE pronunciation for Indian languages. So: ElevenLabs only for English,
        # Google for Indian languages. (Set ELEVEN_FOR_ALL=1 if you upgrade ElevenLabs.)
        import os
        use_11 = twi.cfg.get("elevenlabs_key") and (
            lang == "en" or os.environ.get("ELEVEN_FOR_ALL", "").lower() in ("1", "true", "yes"))
        if use_11:
            audio = _eleven_mp3(text); src = "11l"
        else:
            audio = _google_mp3(text, tl); src = "ggl"
        key = "tts/" + hashlib.md5((src + tl + text).encode()).hexdigest()[:16] + ".mp3"
        s3 = boto3.client("s3", region_name=s3_audit.cfg["region"])
        s3.put_object(Bucket=s3_audit.cfg["bucket"], Key=key, Body=audio, ContentType="audio/mpeg")
        url = s3.generate_presigned_url("get_object",
              Params={"Bucket": s3_audit.cfg["bucket"], "Key": key}, ExpiresIn=3600)
        return f'<Play>{url.replace("&", "&amp;")}</Play>'   # & must be XML-escaped
    except Exception as e:
        twi._p(f"[voice] TTS fallback ({e}); using Polly")
        return f'<Say voice="Polly.Aditi">{twi._xml(text)}</Say>'


# back-compat alias
def _say(text: str, lang: str) -> str:
    return _voice(text, lang)


def _gather(neg_id: str, user_id: str, lang: str, inner: str) -> str:
    base = twi.cfg["public_base"].strip().rstrip("/")
    bcp = LANG.get(lang, LANG["en"])[0]
    action = f"{base}/twilio/voice/converse/{_token(neg_id, user_id)}"   # clean token, no backslashes
    # prompt goes INSIDE the gather (plays while listening); actionOnEmptyResult makes
    # silence still call back (so we can re-prompt instead of hanging up).
    return (f'<Gather input="speech" language="{bcp}" speechTimeout="auto" timeout="7" '
            f'actionOnEmptyResult="true" action="{action}" method="POST">{inner}</Gather>')


def _bedrock(messages, system) -> str:
    """One Bedrock turn (free-form text reply). Falls back to a canned line on error."""
    try:
        import boto3
        from llm.wrapper import cfg as bcfg
        c = boto3.client("bedrock-runtime", region_name=bcfg["region"])
        body = json.dumps({"anthropic_version": "bedrock-2023-05-31", "max_tokens": 200,
                           "temperature": 0.5, "system": system, "messages": messages})
        r = c.invoke_model(modelId=bcfg["model"], body=body)
        return json.loads(r["body"].read())["content"][0]["text"].strip()
    except Exception as e:
        print(f"[voice] bedrock error: {e}")
        return "Thank you. Please reply yes if you can donate.\nINTENT=CONTINUE"


def _system_prompt(ctx, lang_name) -> str:
    return (
        f"You are RAKTA-SETU, a warm assistant phoning a blood donor on behalf of a "
        f"thalassemia patient. Converse ONLY in {lang_name}. Keep every reply to ONE "
        f"short, kind sentence. Goal: ask if they can donate {ctx.get('bg','')} blood at "
        f"{ctx.get('hospital','the hospital')} on {ctx.get('date','')}. Answer their "
        f"questions simply (eligibility, timing). Be decisive: the MOMENT the donor "
        f"shows willingness in any language (e.g. 'yes', 'avunu', 'haan', 'I can', 'sure'), "
        f"thank them and treat it as agreement. After your sentence, output on a NEW line "
        f"exactly one tag: INTENT=CONFIRM if they agreed/are willing, INTENT=DECLINE if "
        f"they refuse or can't, or INTENT=CONTINUE only if it's still genuinely unclear."
    )


def _turn(neg_id, user_id, ctx, user_text):
    """Run one Bedrock turn; returns (spoken_text, intent)."""
    key = (neg_id, user_id)
    conv = conversations.setdefault(key, {"messages": [], "ctx": ctx})
    conv["messages"].append({"role": "user", "content": user_text})
    lang = _lang_for(ctx)
    reply_lang = LANG.get(lang, LANG["en"])[1]   # reply in the SELECTED language
    reply = _bedrock(conv["messages"], _system_prompt(ctx, reply_lang))
    conv["messages"].append({"role": "assistant", "content": reply})
    intent = "CONTINUE"
    spoken_lines = []
    for line in reply.splitlines():
        if line.strip().upper().startswith("INTENT="):
            intent = line.split("=", 1)[1].strip().upper()
        else:
            spoken_lines.append(line)
    return " ".join(spoken_lines).strip() or "...", intent


def place_conversational_call(neg_id, user_id, phone, ctx) -> dict:
    """Ring the donor and open the spoken conversation (greeting + first question)."""
    phone = twi._norm(phone)
    lang = _lang_for(ctx)
    spoken, _ = _turn(neg_id, user_id, ctx, "(The donor just answered the phone. Greet them and ask.)")
    # Prompt is INSIDE the gather so it plays while listening.
    twiml = f'<Response>{_gather(neg_id, user_id, lang, _say(spoken, lang))}</Response>'
    if not (twi.cfg.get("account_sid") and twi.cfg.get("auth_token") and twi.cfg.get("call_from")):
        twi._p(f"[voice:MOCK call] -> {phone} ({LANG.get(lang,['','?'])[1]}): {spoken[:60]}")
        return {"status": "mock_voice", "to": phone}
    try:
        call = twi._client().calls.create(from_=twi.cfg["call_from"], to=phone, twiml=twiml)
        twi._p(f"[voice] conversational call placed to {phone} (sid={call.sid}, lang={lang})")
        return {"status": "call_placed", "to": phone, "detail": call.sid}
    except Exception as e:
        twi._p(f"[voice] call error: {e}")
        return {"status": "error", "detail": str(e)}


def handle_turn(neg_id, user_id, speech_result):
    """A donor spoke (or was silent); return (twiml, intent, spoken, donor_text)."""
    key = (neg_id, user_id)
    conv = conversations.setdefault(key, {"messages": [], "ctx": {}})
    ctx = conv.get("ctx", {})
    lang = _lang_for(ctx)

    # Track silence so we don't loop forever on a dead line.
    conv["turns"] = conv.get("turns", 0) + 1
    if speech_result and speech_result.strip():
        conv["silent"] = 0
        donor_text = speech_result.strip()
    else:
        conv["silent"] = conv.get("silent", 0) + 1
        donor_text = "(silence)"

    if conv["silent"] >= 3 or conv["turns"] > 8:
        conversations.pop(key, None)
        bye = "Sorry, I couldn't hear you. We'll text you instead. Goodbye."
        return f'<Response>{_say(bye, lang)}<Hangup/></Response>', "CONTINUE", bye, donor_text

    spoken, intent = _turn(neg_id, user_id, ctx, donor_text)
    if intent in ("CONFIRM", "DECLINE"):
        from agents.orchestrator import human_confirm
        human_confirm(neg_id, user_id, intent == "CONFIRM")
        twi.clear_pending(neg_id, user_id)
        conversations.pop(key, None)
        twiml = f'<Response>{_say(spoken, lang)}<Hangup/></Response>'
    else:
        twiml = f'<Response>{_gather(neg_id, user_id, lang, _say(spoken, lang))}</Response>'
    return twiml, intent, spoken, donor_text
