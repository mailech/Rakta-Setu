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
import json
from notify import twilio_channel as twi

LANG = {  # code -> (BCP47, display, Twilio <Say> attrs)
    "te": ("te-IN", "Telugu",        'language="te-IN"'),
    "hi": ("hi-IN", "Hindi",         'voice="Polly.Kajal" language="hi-IN"'),
    "en": ("en-IN", "Indian English",'voice="Polly.Kajal" language="en-IN"'),
    "kn": ("kn-IN", "Kannada",       'language="kn-IN"'),
    "ta": ("ta-IN", "Tamil",         'language="ta-IN"'),
}

# per-call conversation history: (neg_id, user_id) -> {"messages":[...], "ctx":{...}}
conversations = {}


def _lang_for(ctx) -> str:
    return (twi.cfg.get("call_language") or ctx.get("lang") or "te").lower()


def _say(text: str, lang: str) -> str:
    attrs = LANG.get(lang, LANG["en"])[2]
    return f"<Say {attrs}>{twi._xml(text)}</Say>"


def _gather(neg_id: str, user_id: str, lang: str, inner: str) -> str:
    from urllib.parse import quote
    base = twi.cfg["public_base"].strip().rstrip("/")
    bcp = LANG.get(lang, LANG["en"])[0]
    action = f"{base}/twilio/voice/converse/{quote(neg_id, safe='')}/{quote(user_id, safe='')}"
    return (f'<Gather input="speech" language="{bcp}" speechTimeout="auto" '
            f'action="{action}" method="POST">{inner}</Gather>')


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
    reply = _bedrock(conv["messages"], _system_prompt(ctx, LANG.get(lang, LANG["en"])[1]))
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
    twiml = (f'<Response>{_say(spoken, lang)}'
             f'{_gather(neg_id, user_id, lang, "")}'
             f'<Say>Goodbye.</Say></Response>')
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
    """A donor spoke; return (twiml, intent, spoken, donor_text)."""
    key = (neg_id, user_id)
    ctx = conversations.get(key, {}).get("ctx", {})
    lang = _lang_for(ctx)
    donor_text = speech_result or "(silence)"
    spoken, intent = _turn(neg_id, user_id, ctx, donor_text)
    if intent in ("CONFIRM", "DECLINE"):
        from agents.orchestrator import human_confirm
        human_confirm(neg_id, user_id, intent == "CONFIRM")
        twi.clear_pending(neg_id, user_id)
        conversations.pop(key, None)
        twiml = f'<Response>{_say(spoken, lang)}<Hangup/></Response>'
    else:
        twiml = f'<Response>{_gather(neg_id, user_id, lang, _say(spoken, lang))}<Say>Goodbye.</Say></Response>'
    return twiml, intent, spoken, donor_text
