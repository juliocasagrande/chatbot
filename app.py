# app.py
import os, json, logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from funcoes import (
    extract_text, extract_number, route_builtin, enviar_texto, gerar_resposta_llm
)

load_dotenv()
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)

# Env
EV_BASE   = os.environ.get("EV_BASE", "").rstrip("/")
EV_KEY    = os.environ.get("EV_KEY", "")
EV_INST   = os.environ.get("EV_INST", "")
MY_NUMBER = os.environ.get("MY_NUMBER", "")
SELF_TEST = os.environ.get("SELF_TEST", "0")

@app.get("/")
def health():
    return {
        "ok": True,
        "service": "julio-bot",
        "inst": EV_INST,
        "ev_base_set": bool(EV_BASE),
        "my_number_set": bool(MY_NUMBER),
        "self_test": SELF_TEST,
    }, 200

@app.get("/webhook")
def webhook_get():
    return {"ok": True, "endpoint": "webhook"}, 200

@app.post("/webhook")
def webhook_post():
    payload = request.get_json(silent=True) or {}
    app.logger.info("Webhook payload: %s", json.dumps(payload)[:2000])

    # normaliza "messages"
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
        key = (msg or {}).get("key", {}) or {}
        from_me = bool(key.get("fromMe"))

        # SELF_TEST estrito: só processa o que VOCÊ enviou
        if SELF_TEST == "1" and not from_me:
            app.logger.info("SELF_TEST ativo: ignorando mensagem de terceiros.")
            continue

        text = (extract_text(msg, payload) or "").strip()

        # número/remetente
        remote, number = extract_number(msg, payload, MY_NUMBER, from_me)
        if remote.endswith("@g.us"):
            app.logger.info("Ignorando grupo: %s", remote);  continue
        if MY_NUMBER and number != (''.join(filter(str.isdigit, MY_NUMBER))):
            app.logger.info("Ignorando %s (não é MY_NUMBER)", number);  continue

        # roteamento: comandos built-in primeiro
        reply = route_builtin(text)

        # comando LLM: /ai <texto> (só chama a Groq quando solicitado)
        if reply is None:
            tl = text.lower()
            if tl.startswith("/ai "):
                prompt = text[4:].strip()
                if not prompt:
                    reply = "Use assim: /ai sua pergunta aqui."
                else:
                    reply = gerar_resposta_llm(prompt, system="Você é um assistente do WhatsApp útil e conciso.")
            else:
                # fallback simples:
                reply = f"Você disse: {text}"

        # prefixo anti-loop para mensagens enviadas por você
        prefix = "[bot] " if from_me else ""
        r = enviar_texto(EV_BASE, EV_KEY, EV_INST, number, prefix + reply)
        replies.append({"to": number, "status": getattr(r, "status_code", "NA")})

    return jsonify({"ok": True, "got": len(messages), "replied": replies}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
