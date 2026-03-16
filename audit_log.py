"""
Módulo de Auditoria — Mobil Feira
Grava todas as ações do app num ficheiro JSON Lines (.jsonl)
para auditoria e rastreabilidade de operações.

Cada linha do ficheiro é um objeto JSON independente com:
- timestamp (ISO 8601)
- event (tipo de evento)
- user (utilizador da sessão, se disponível)
- data (dados da ação)
"""

import json
import os
import logging
from datetime import datetime
from threading import Lock

logger = logging.getLogger(__name__)

# Directório dos logs de auditoria
AUDIT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'audit_logs')
os.makedirs(AUDIT_DIR, exist_ok=True)

# Lock para escrita segura em multithread
_write_lock = Lock()


def _get_audit_file():
    """Retorna o caminho do ficheiro de auditoria do dia actual."""
    today = datetime.now().strftime('%Y-%m-%d')
    return os.path.join(AUDIT_DIR, f'audit_{today}.jsonl')


def log_event(event: str, data: dict, user: str = None):
    """
    Grava um evento de auditoria no ficheiro JSONL.

    Args:
        event: Tipo de evento (ex: 'SEARCH', 'CLIENT_UPDATE', 'CLIENT_CREATE',
               'PRESUPUESTO_CREATE', 'PRESUPUESTO_CONFIRM')
        data: Dicionário com os dados relevantes da ação
        user: Identificador do utilizador (opcional)
    """
    entry = {
        'timestamp': datetime.now().isoformat(),
        'event': event,
        'user': user or 'system',
        'data': _sanitize(data)
    }

    try:
        filepath = _get_audit_file()
        with _write_lock:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write(json.dumps(entry, ensure_ascii=False, default=str) + '\n')
    except Exception as e:
        logger.error(f"Erro ao gravar audit log: {e}")


def _sanitize(obj):
    """Remove valores None/False e converte tipos não serializáveis."""
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items() if v is not None}
    elif isinstance(obj, (list, tuple)):
        return [_sanitize(item) for item in obj]
    elif isinstance(obj, (int, float, str, bool)):
        return obj
    else:
        return str(obj)


def get_audit_files():
    """Lista todos os ficheiros de auditoria disponíveis."""
    if not os.path.exists(AUDIT_DIR):
        return []
    files = sorted([f for f in os.listdir(AUDIT_DIR) if f.endswith('.jsonl')], reverse=True)
    return files


def read_audit_file(filename: str):
    """Lê e retorna todos os eventos de um ficheiro de auditoria."""
    filepath = os.path.join(AUDIT_DIR, filename)
    if not os.path.exists(filepath):
        return []
    events = []
    with open(filepath, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events
