import os, re, datetime, json, logging
from flask import Flask, request, jsonify
import requests
from dotenv import load_dotenv
load_dotenv()

# -------- Config --------
EV_BASE = os.environ.get("EV_BASE", "").rstrip("/")
EV_KEY  = os.environ.get("EV_KEY", "")
EV_INST = os.environ.get("EV_INST", "")
MY_NUMBER = os.environ.get("MY_NUMBER", "")
SELF_TEST = os.environ.get("SELF_TEST", "0")

HDRS = {"apikey": EV_KEY, "Content-Type": "application/json"}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

def extract_text(msg: dict) -> str | None:
    m = msg.get("message", {}) or {}

    if "conversation" in m:
        return m["conversation"]
    if "extendedTextMessage" in m:
        return m["extendedTextMessage"].get("text")
    for k in ("imageMessage", "videoMessage", "documentMessage", "audioMessage"):
        if k in m and m[k].get("caption"):
            return m[k]["caption"]
    if "text" in m and isinstance(m["text"], str):
        return m["text"]
    if "ephemeralMessage" in m:
        inner = m["ephemeralMessage"].get("message", {})
        if "conversation" in inner:
            return inner["conversation"]
        if "extendedTextMessage" in inner:
            return inner["extendedTextMessage"].get("text")
    return None

def route_reply(t: str) -> str | None:
    t = (t or "").strip()
    tl = t.lower()
    if tl in ("ping", "/ping"): return "pong"
    if tl in ("hora", "/hora"):
        return "Hora: " + datetime.datetime.now().strftime("%H:%M:%S")
    if tl in ("data", "/data"):
        return "Data: " + datetime.datetime.now().strftime("%d/%m/%Y")
    if tl.startswith("/eco "):  return t[5:]
    if tl in ("ajuda", "/ajuda", "/help"):
        return ("Comandos: ping, hora, data, /eco <texto>, ajuda\n"
                "Obs: este bot responde apenas ao meu número 😉")
    return f"Você disse: {t}"

@app.route("/", methods=["GET"])
def health():
    return {
        "ok": True,
        "service": "julio-bot",
        "inst": EV_INST,
        "ev_base_set": bool(EV_BASE),
        "my_number_set": bool(MY_NUMBER),
    }, 200

@app.route("/webhook", methods=["GET"])
def webhook_get():
    return {"ok": True, "endpoint": "webhook"}, 200

@app.route("/webhook", methods=["POST"])
def webhook_post():
    payload = request.get_json(silent=True) or {}
    app.logger.info("Webhook payload: %s", json.dumps(payload)[:2000])

    # receber mensagens em diferentes formatos
    messages = []
    if isinstance(payload.get("messages"), list):
        messages = payload["messages"]
    elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("messages"), list):
        messages = payload["data"]["messages"]
    elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("message"), dict):
        messages = [payload["data"]["message"]]
    elif isinstance(payload.get("data"), dict):
        d = payload["data"]
        if isinstance(d, dict) and ("key" in d or "message" in d or "messageType" in d):
            messages = [d]

    replies = []

    for msg in messages:
        key = msg.get("key", {}) or {}
        from_me = bool(key.get("fromMe"))
        text = extract_text(msg) or ""

        # ignora mensagens do próprio bot, a menos que SELF_TEST esteja ativado
        if from_me:
            if SELF_TEST != "1":
                continue
            if text.lower().startswith("[bot]"):
                continue

        # --- extração robusta do número ---
        remote = (
            key.get("remoteJid")
            or key.get("participant")
            or msg.get("from")
            or (payload.get("sender") if isinstance(payload, dict) else "")
            or ""
        )

        if not remote and isinstance(payload.get("data"), dict):
            d = payload["data"]
            remote = d.get("sender") or d.get("from") or d.get("remoteJid") or ""

        number = only_digits(remote)

        if isinstance(remote, str) and remote.endswith("@g.us"):
            app.logger.info("Ignorando mensagem de grupo: %s", remote)
            continue

        if from_me and not number:
            number = only_digits(MY_NUMBER)

        app.logger.info("Debug número - remote: %r -> number extraído: %r ; MY_NUMBER: %r",
                        remote, number, only_digits(MY_NUMBER))

        if MY_NUMBER and number != only_digits(MY_NUMBER):
            app.logger.info("Ignorando mensagem de %s (não é MY_NUMBER)", number)
            continue

        reply = route_reply(text)
        if not reply:
            continue

        prefix = "[bot] " if from_me else ""
        body = {"number": number, "text": prefix + reply}
        url = f"{EV_BASE}/message/sendText/{EV_INST}"

        try:
            r = requests.post(url, headers=HDRS, json=body, timeout=15)
            app.logger.info("Envio reply -> %s [%s]: %s", number, r.status_code, r.text[:300])
            replies.append({"to": number, "status": r.status_code})
        except Exception as e:
            app.logger.exception("Falha ao enviar reply para %s: %s", number, e)
            replies.append({"to": number, "status": "error", "err": str(e)})

    return jsonify({"ok": True, "got": len(messages), "replied": replies})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
