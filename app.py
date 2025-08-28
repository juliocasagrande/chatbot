# app.py
import os, json, logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from funcoes import (
    extract_text, extract_number, route_builtin, enviar_texto, only_digits,
    gerar_resposta_llm_com_contexto, salvar_mensagem, carregar_contexto
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

        # SELF_TEST estrito: só processa mensagens suas; alguns eventos vêm com fromMe=false,
        # então aceitamos também quando o sender do envelope é o seu JID
        if SELF_TEST == "1" and not from_me:
            sender_jid = ""
            if isinstance(payload, dict):
                sender_jid = str(payload.get("sender") or "")
            if only_digits(sender_jid) != only_digits(MY_NUMBER):
                app.logger.info("SELF_TEST ativo: ignorando mensagem de terceiros.")
                continue

        text = (extract_text(msg, payload) or "").strip()

        # número/remetente
        remote, number = extract_number(msg, payload, MY_NUMBER, from_me)
        if isinstance(remote, str) and remote.endswith("@g.us"):
            app.logger.info("Ignorando grupo: %s", remote)
            continue
        if MY_NUMBER and number != only_digits(MY_NUMBER):
            app.logger.info("Ignorando %s (não é MY_NUMBER)", number)
            continue

        # --- persistir mensagem do usuário
        if text:
            try:
                salvar_mensagem(number, "user", text)
            except Exception as e:
                app.logger.exception("Erro ao salvar mensagem do usuário: %s", e)

        # roteamento: comandos built-in primeiro
        reply = route_builtin(text)

        # LLM com contexto: /ai <texto> → envia últimas 25 mensagens do número
        if reply is None:
            tl = text.lower()
            if tl.startswith("/ai "):
                prompt = text[4:].strip()
                if not prompt:
                    reply = "Use assim: /ai sua pergunta aqui."
                else:
                    try:
                        ctx = carregar_contexto(number, limite=25)
                        ctx.append({"role": "user", "content": prompt})
                        reply = gerar_resposta_llm_com_contexto(
                            ctx,
                            system="Você é um assistente do WhatsApp útil, objetivo e em PT‑BR."
                        )
                    except Exception as e:
                        app.logger.exception("Erro LLM com contexto: %s", e)
                        reply = "Não consegui consultar a LLM agora."
            else:
                # fallback simples
                reply = f"Você disse: {text}"

        # prefixo anti-loop para mensagens enviadas por você
        prefix = "[bot] " if from_me else ""
        try:
            r = enviar_texto(EV_BASE, EV_KEY, EV_INST, number, prefix + reply)
            replies.append({"to": number, "status": getattr(r, "status_code", "NA")})
        except Exception as e:
            app.logger.exception("Falha ao enviar reply: %s", e)
            replies.append({"to": number, "status": "error"})

        # --- persistir mensagem do bot
        try:
            salvar_mensagem(number, "assistant", prefix + reply)
        except Exception as e:
            app.logger.exception("Erro ao salvar resposta do bot: %s", e)

    return jsonify({"ok": True, "got": len(messages), "replied": replies}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)