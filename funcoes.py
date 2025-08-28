# funcoes.py
import os, re, datetime, json, logging
import requests
from groq import Groq

log = logging.getLogger(__name__)

def only_digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")

def extract_text(msg: dict, payload: dict | None = None) -> str | None:
    """Extrai texto de vários formatos possíveis do Evolution."""
    m = (msg or {}).get("message", {}) or {}

    # formatos mais comuns
    if "conversation" in m:
        return m["conversation"]
    if "extendedTextMessage" in m:
        return (m["extendedTextMessage"] or {}).get("text")

    # legendas de mídia
    for k in ("imageMessage", "videoMessage", "documentMessage", "audioMessage"):
        if k in m and (m[k] or {}).get("caption"):
            return m[k]["caption"]

    # alguns providers mandam em m["text"]
    if isinstance(m.get("text"), str):
        return m["text"]

    # mensagens efêmeras
    if "ephemeralMessage" in m:
        inner = (m["ephemeralMessage"] or {}).get("message", {}) or {}
        if "conversation" in inner:
            return inner["conversation"]
        if "extendedTextMessage" in inner:
            return (inner["extendedTextMessage"] or {}).get("text")

    # fallback: envelope Evolution v2
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        dm = (payload["data"] or {}).get("message") or {}
        if isinstance(dm, dict):
            return (
                dm.get("conversation")
                or (dm.get("extendedTextMessage") or {}).get("text")
                or (dm.get("imageMessage") or {}).get("caption")
                or (dm.get("videoMessage") or {}).get("caption")
                or (dm.get("documentMessage") or {}).get("caption")
            )

    return None

def extract_number(msg: dict, payload: dict | None, my_number: str, from_me: bool) -> tuple[str, str]:
    """Retorna (remote, numero_only_digits). Ignora grupos."""
    key = (msg or {}).get("key", {}) or {}
    remote = (
        key.get("remoteJid")
        or key.get("participant")
        or msg.get("from")
        or ((payload or {}).get("sender") if isinstance(payload, dict) else "")
        or ""
    )
    if (not remote) and isinstance((payload or {}).get("data"), dict):
        d = payload["data"]
        remote = d.get("sender") or d.get("from") or d.get("remoteJid") or ""

    # ignora grupos neste protótipo
    if isinstance(remote, str) and remote.endswith("@g.us"):
        return (remote, "")

    number = only_digits(remote)
    if from_me and not number:
        number = only_digits(my_number)
    return (remote, number)

def route_builtin(t: str) -> str | None:
    """Comandos built-in rápidos."""
    t = (t or "").strip()
    tl = t.lower()
    if tl in ("ping", "/ping"): return "pong"
    if tl in ("hora", "/hora"):
        return "Hora: " + datetime.datetime.now().strftime("%H:%M:%S")
    if tl in ("data", "/data"):
        return "Data: " + datetime.datetime.now().strftime("%d/%m/%Y")
    if tl.startswith("/eco "):  return t[5:]
    if tl in ("ajuda", "/ajuda", "/help"):
        return ("Comandos: ping, hora, data, /eco <texto>, /ai <pergunta>\n"
                "Obs: em SELF_TEST=1, responde só às suas mensagens 😉")
    return None

def enviar_texto(ev_base: str, ev_key: str, ev_inst: str, number: str, text: str, timeout: int = 20) -> requests.Response:
    """Envia texto pelo Evolution."""
    url = f"{ev_base.rstrip('/')}/message/sendText/{ev_inst}"
    headers = {"apikey": ev_key, "Content-Type": "application/json"}
    body = {"number": number, "text": text}
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    log.info("Envio reply -> %s [%s]: %s", number, r.status_code, (r.text or "")[:300])
    return r

# -------- Groq LLM --------
def gerar_resposta_llm(prompt: str, system: str | None = None) -> str:
    """Chama a Groq LLM (model em LLM_MODEL, default llama3-70b-8192)."""
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return "LLM indisponível: defina GROQ_API_KEY no ambiente."

    model = os.environ.get("LLM_MODEL", "llama3-70b-8192")
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0.3"))

    client = Groq(api_key=api_key)
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})

    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=512,
        stream=False,
    )
    return (resp.choices[0].message.content or "").strip()