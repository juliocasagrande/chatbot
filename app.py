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

HDRS = {"apikey": EV_KEY, "Content-Type": "application/json"}

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

def extract_text(msg: dict) -> str | None:
    m = msg.get("message", {}) or {}

    # formatos comuns
    if "conversation" in m:
        return m["conversation"]
    if "extendedTextMessage" in m:
        return m["extendedTextMessage"].get("text")

    # imagem / vídeo / doc / áudio com legenda
    for k in ("imageMessage", "videoMessage", "documentMessage", "audioMessage"):
        if k in m and m[k].get("caption"):
            return m[k]["caption"]

    # alguns providers mandam em m["text"] simples
    if "text" in m and isinstance(m["text"], str):
        return m["text"]

    # fallback em estruturas tipo {"message":{"ephemeralMessage":{"message":{"conversation":...}}}}
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
    # mostrar um pouco do estado (sem vazar segredos)
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

    # Formatos comuns: { "messages":[...] } OU { "data": { "messages":[...] } } OU { "messages":[...], "event": ... }
    messages = []
    if isinstance(payload.get("messages"), list):
        messages = payload["messages"]
    elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("messages"), list):
        messages = payload["data"]["messages"]
    elif isinstance(payload.get("data"), dict) and isinstance(payload["data"].get("message"), dict):
        # alguns mandam uma única mensagem em data.message
        messages = [payload["data"]["message"]]

    replies = []

    for msg in messages:
        key = msg.get("key", {}) or {}

        # evita loop (mensagens enviadas pelo próprio bot)
        if key.get("fromMe"):
            continue

        # tenta extrair o número do remetente
        remote = (
            key.get("remoteJid")
            or key.get("participant")   # em grupos
            or msg.get("from")
            or ""
        )
        number = only_digits(remote)

        # responde apenas para o seu número (teste "comigo mesmo")
        if MY_NUMBER and number != only_digits(MY_NUMBER):
            app.logger.info("Ignorando mensagem de %s (não é MY_NUMBER)", number)
            continue

        text = extract_text(msg) or ""
        reply = route_reply(text)
        if not reply:
            continue

        url = f"{EV_BASE}/message/sendText/{EV_INST}"
        body = {"number": number, "text": reply}

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
