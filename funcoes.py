# funcoes.py
# =========================
# Seções:
# 1) Imports e logging
# 2) Utilidades (texto/número/roteamento)
# 3) Evolution (envio de mensagem)
# 4) Banco de Dados (persistência do histórico)
# 5) LLM (Groq) - resposta simples e com contexto
# 6) Roteamento / Handoff (heurística + resumo + notificação)
# =========================

# 1) Imports e logging ----------------------
import os
import re
import json
import time
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

# 6) Roteamento / Handoff -------------------

# cooldown em memória por número
_HANDOFF_COOLDOWN: Dict[str, float] = {}

def _kw_list_from_env(var: str, default: str) -> List[str]:
    val = os.environ.get(var, "").strip()
    base = [s.strip().lower() for s in default.split("|") if s.strip()]
    if not val:
        return base
    extra = [s.strip().lower() for s in val.split("|") if s.strip()]
    return list(dict.fromkeys(base + extra))  # único e na ordem

def _cooldown_minutes() -> int:
    try:
        return int(os.environ.get("HANDOFF_COOLDOWN_MIN", "20"))
    except Exception:
        return 20

def em_cooldown(numero: str) -> bool:
    """Retorna True se o número ainda estiver no período de cooldown de handoff."""
    t0 = _HANDOFF_COOLDOWN.get(numero, 0.0)
    return (time.time() - t0) < (_cooldown_minutes() * 60)

def marcar_handoff(numero: str) -> None:
    """Marca/inicia a janela de cooldown para este número."""
    _HANDOFF_COOLDOWN[numero] = time.time()

def precisa_handoff(
    texto_usuario: str,
    resposta_llm: Optional[str],
    ctx_len: int
) -> Tuple[bool, str]:
    """
    Decide se deve escalar. Retorna (True/False, motivo).
    Regras:
      - Sempre permite handoff por palavra‑chave explícita do usuário.
      - Auto‑handoff só ocorre se HANDOFF_AUTO=1 **e** houver contexto suficiente.
      - Não verifica cooldown aqui (opcional checar via em_cooldown() no app).
    """
    # 1) pedido explícito do usuário
    t = (texto_usuario or "").strip().lower()
    kws = _kw_list_from_env(
        "ESCALATION_KEYWORDS",
        "humano|atendente|suporte|falar com alguém|transferir|pessoa|representante"
    )
    if any(kw in t for kw in kws):
        return True, "pedido_explicitamente_pelo_usuario"

    # 2) auto-handoff desligado?
    if os.environ.get("HANDOFF_AUTO", "0") != "1":
        return False, "auto_handoff_desligado"

    # 3) pouco contexto?
    try:
        min_turns = int(os.environ.get("HANDOFF_MIN_TURNS", "6"))
    except Exception:
        min_turns = 6
    if ctx_len < min_turns:
        return False, "pouco_contexto"

    # 4) heurística simples de baixa confiança
    r = (resposta_llm or "").strip().lower()
    low_conf_signals = (
        "não consigo", "não tenho acesso", "não tenho certeza",
        "desculpe", "não sei", "não entendi", "não está claro",
        "não posso ajudar", "fora do meu escopo", "não tenho memória",
        "conversa nova"
    )

    # resposta muito curta e negativa
    if len(r) < 12 and any(x in r for x in ("não", "desculpe")):
        return True, "resposta_muito_curta_e_negativa"

    # sinais fortes de incapacidade
    if any(sig in r for sig in low_conf_signals):
        return True, "baixa_confianca_llm"

    return False, ""

def resumir_conversa_para_humano(numero: str, carregar_ctx_fn, limite: int = 25) -> str:
    """Gera um resumo objetivo para o humano com base no histórico."""
    mensagens = carregar_ctx_fn(numero, limite=limite)
    if not mensagens:
        return f"Sem histórico para {numero}."
    system = (
        "Você é um assistente que resume uma conversa de WhatsApp para um humano assumir o atendimento. "
        "Produza um resumo curto (5-8 linhas), com: objetivo do usuário, fatos já coletados, "
        "tentativas do bot, pendências e próxima ação sugerida."
    )
    return gerar_resposta_llm_com_contexto(mensagens, system=system)

def notificar_dono(ev_base: str, ev_key: str, ev_inst: str,
                   owner_number: str, numero_usuario: str, resumo: str) -> None:
    """Envia o resumo para o dono (humano)."""
    cabecalho = (f"[Escalação automática]\n"
                 f"Usuário: {numero_usuario}\n"
                 f"Resumo da conversa:\n\n{resumo}\n\n"
                 f"Aja respondendo diretamente ao usuário.")
    try:
        enviar_texto(ev_base, ev_key, ev_inst, only_digits(owner_number), cabecalho)
    except Exception as e:
        log.exception("Falha ao notificar dono: %s", e)