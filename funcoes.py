# funcoes.py
# =========================
# Seções:
# 1) Imports e logging
# 2) Utilidades (texto/número/roteamento)
# 3) Evolution (envio de mensagem)
# 4) Banco de Dados (persistência do histórico)
# 5) LLM (Groq) - resposta simples e com contexto
# =========================

# 1) Imports e logging ----------------------
import os
import re
import json
import datetime
import logging
from typing import List, Dict, Tuple, Optional

import requests
import psycopg2
from psycopg2.pool import SimpleConnectionPool
from groq import Groq

log = logging.getLogger(__name__)

# 2) Utilidades -----------------------------

def only_digits(s: str) -> str:
    """Mantém apenas dígitos (útil para extrair telefone)."""
    return re.sub(r"\D", "", s or "")

def extract_text(msg: dict, payload: Optional[dict] = None) -> Optional[str]:
    """
    Extrai texto de vários formatos do Evolution v2.
    - message.conversation
    - message.extendedTextMessage.text
    - legendas de mídia
    - message.text
    - ephemeralMessage
    - fallback: payload['data']['message']
    """
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

def extract_number(msg: dict, payload: Optional[dict], my_number: str, from_me: bool) -> Tuple[str, str]:
    """
    Retorna (remoteJid-like, numero_only_digits). Ignora grupos (@g.us).
    Busca em: key.remoteJid | key.participant | msg.from | payload.sender | payload.data.{sender/from/remoteJid}
    """
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

def route_builtin(t: str) -> Optional[str]:
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

# 3) Evolution (envio) ----------------------

def enviar_texto(ev_base: str, ev_key: str, ev_inst: str, number: str, text: str, timeout: int = 20) -> requests.Response:
    """Envia texto via Evolution API /message/sendText/{instance}."""
    url = f"{ev_base.rstrip('/')}/message/sendText/{ev_inst}"
    headers = {"apikey": ev_key, "Content-Type": "application/json"}
    body = {"number": number, "text": text}
    r = requests.post(url, headers=headers, json=body, timeout=timeout)
    log.info("Envio reply -> %s [%s]: %s", number, getattr(r, "status_code", "?"), (getattr(r, "text", "") or "")[:300])
    return r

# 4) Banco de Dados (histórico) ------------

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL não definida")

# Pool simples (2..10 conexões)
POOL = SimpleConnectionPool(minconn=2, maxconn=10, dsn=DATABASE_URL)

def _get_conn():
    return POOL.getconn()

def _put_conn(conn):
    try:
        POOL.putconn(conn)
    except Exception:
        try:
            conn.close()
        except Exception:
            pass

def salvar_mensagem(numero: str, role: str, content: str) -> None:
    """Insere 1 linha no historico (numero, role: user/assistant, content)."""
    sql = """
        INSERT INTO historico (numero, role, content)
        VALUES (%s, %s, %s)
    """
    conn = _get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, (numero, role, content))
    finally:
        _put_conn(conn)

def carregar_contexto(numero: str, limite: int = 25) -> List[Dict[str, str]]:
    """
    Retorna as últimas N mensagens desse número, em ordem cronológica (antiga -> recente),
    no formato [{'role':'user'|'assistant','content':'...'}, ...]
    """
    sql = """
        SELECT role, content
        FROM historico
        WHERE numero = %s
        ORDER BY created_at DESC, id DESC
        LIMIT %s
    """
    conn = _get_conn()
    try:
        with conn, conn.cursor() as cur:
            cur.execute(sql, (numero, limite))
            rows = cur.fetchall()        # mais recente primeiro
            rows.reverse()               # inverte para antigo -> recente
            return [{"role": r, "content": c} for (r, c) in rows]
    finally:
        _put_conn(conn)

# 5) LLM (Groq) ----------------------------

def _groq_client() -> Optional[Groq]:
    api_key = os.environ.get("GROQ_API_KEY", "")
    if not api_key:
        return None
    return Groq(api_key=api_key)

def gerar_resposta_llm(prompt: str, system: Optional[str] = None) -> str:
    """
    Chamada simples à Groq (sem contexto).
    Config: LLM_MODEL (default: llama3-70b-8192), LLM_TEMPERATURE (default: 0.3).
    """
    client = _groq_client()
    if client is None:
        return "LLM indisponível: defina GROQ_API_KEY no ambiente."

    model = os.environ.get("LLM_MODEL", "llama3-70b-8192")
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0.3"))

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

def gerar_resposta_llm_com_contexto(mensagens: List[Dict[str, str]], system: Optional[str] = None) -> str:
    """
    Envia lista de mensagens já no formato [{'role':'user'|'assistant','content':'...'}]
    (por exemplo, resultante de carregar_contexto(numero, limite=25)).
    """
    client = _groq_client()
    if client is None:
        return "LLM indisponível: defina GROQ_API_KEY no ambiente."

    model = os.environ.get("LLM_MODEL", "llama3-70b-8192")
    temperature = float(os.environ.get("LLM_TEMPERATURE", "0.3"))

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.extend(mensagens)

    resp = client.chat.completions.create(
        model=model,
        messages=msgs,
        temperature=temperature,
        max_tokens=600,
        stream=False,
    )
    return (resp.choices[0].message.content or "").strip()