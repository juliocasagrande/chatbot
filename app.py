# app.py
import os, json, logging
from flask import Flask, request, jsonify
from dotenv import load_dotenv
from funcoes import (
    extract_text, extract_number, route_builtin, enviar_texto, only_digits,
    gerar_resposta_llm_com_contexto, salvar_mensagem, carregar_contexto,
    precisa_handoff, resumir_conversa_para_humano, notificar_dono, marcar_handoff
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
        # quando vem nesse formato, o 'key' costuma estar em data.key
        messages = [payload["data"]["message"]]
    elif isinstance(payload.get("data"), dict):
        d = payload["data"]
        if isinstance(d, dict) and ("key" in d or "message" in d or "messageType" in d):
            messages = [d]

    replies = []

    for msg in messages:
        data_env = payload.get("data") or {}
        # key pode estar em msg.key OU no envelope data.key
        key = (msg or {}).get("key") or (data_env.get("key") or {})

        # fromMe pode vir bool ou string
        _raw_from_me = key.get("fromMe")
        if isinstance(_raw_from_me, bool):
            from_me = _raw_from_me
        else:
            from_me = str(_raw_from_me).strip().lower() == "true"

        # número / JID
        remote, number = extract_number(msg, payload, MY_NUMBER, from_me)

        # ignora grupos
        if isinstance(remote, str) and remote.endswith("@g.us"):
            app.logger.info("Ignorando grupo: %s", remote)
            continue

        # SELF_TEST estrito: só processa msg que VOCÊ enviou para VOCÊ mesmo
        if SELF_TEST == "1":
            if not (from_me and number == only_digits(MY_NUMBER)):
                app.logger.info(
                    "SELF_TEST: ignorando (from_me=%s, number=%s, my=%s, remote=%s)",
                    from_me, number, only_digits(MY_NUMBER), remote
                )
                continue

        text = (extract_text(msg, payload) or "").strip()
        if not text:
            continue

        # salvar entrada do usuário
        try:
            salvar_mensagem(number, "user", text)
        except Exception as e:
            app.logger.exception("Erro ao salvar mensagem do usuário: %s", e)

        # comandos built-in
        reply = route_builtin(text)

        # LLM com contexto via /ai
        llm_reply = None
        if reply is None:
            tl = text.lower()
            if tl.startswith("/ai "):
                prompt = text[4:].strip()
                if not prompt:
                    reply = "Use assim: /ai sua pergunta aqui."
                else:
                    try:
                        ctx = carregar_contexto(number, limite=10)
                        ctx.append({"role": "user", "content": prompt})
                        llm_reply = gerar_resposta_llm_com_contexto(
                            ctx,
                            system="Você é um assistente do WhatsApp útil, objetivo e em PT‑BR."
                        )
                        reply = llm_reply
                    except Exception as e:
                        app.logger.exception("Erro LLM com contexto: %s", e)
                        reply = "Não consegui consultar a LLM agora."
            else:
                reply = f"Você disse: {text}"

        # -------- Handoff inteligente (com mínimos e cooldown) --------
        try:
            # tamanho do contexto atual (últimas 10)
            ctx_len = 0
            try:
                ctx_tmp = carregar_contexto(number, limite=10)
                ctx_len = len(ctx_tmp)
            except Exception:
                pass

            handoff, motivo = precisa_handoff(text, llm_reply or reply, ctx_len)

            OWNER_NUMBER = os.environ.get("OWNER_NUMBER", MY_NUMBER)
            if handoff and OWNER_NUMBER:
                aviso_user = (
                    "Vou te transferir para um atendente humano para te ajudar melhor. "
                    "Acabei de enviar um resumo da conversa. 👍"
                )
                prefix = "[bot] " if from_me else ""
                enviar_texto(EV_BASE, EV_KEY, EV_INST, number, prefix + aviso_user)

                resumo = resumir_conversa_para_humano(number, carregar_contexto, limite=10)
                notificar_dono(EV_BASE, EV_KEY, EV_INST, OWNER_NUMBER, number, resumo)

                marcar_handoff(number)  # inicia cooldown

                replies.append({"to": number, "status": "handoff", "reason": motivo})
                try:
                    salvar_mensagem(number, "assistant", "[handoff] " + aviso_user)
                except Exception as e:
                    app.logger.exception("Erro ao salvar aviso de handoff: %s", e)
                return jsonify({"ok": True, "got": len(messages), "replied": replies}), 200
        except Exception as e:
            app.logger.exception("Falha no roteamento/handoff: %s", e)
        # --------------------------------------------------------------

        # enviar resposta
        prefix = "[bot] " if from_me else ""
        try:
            r = enviar_texto(EV_BASE, EV_KEY, EV_INST, number, prefix + reply)
            replies.append({"to": number, "status": getattr(r, "status_code", "NA")})
        except Exception as e:
            app.logger.exception("Falha ao enviar reply: %s", e)
            replies.append({"to": number, "status": "error"})

        # salvar resposta do bot
        try:
            salvar_mensagem(number, "assistant", prefix + reply)
        except Exception as e:
            app.logger.exception("Erro ao salvar resposta do bot: %s", e)

    return jsonify({"ok": True, "got": len(messages), "replied": replies}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)