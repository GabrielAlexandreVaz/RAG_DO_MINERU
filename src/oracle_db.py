"""
============================================================================
oracle_db.py · Grava os atos ESTRUTURADOS numa tabela Oracle
----------------------------------------------------------------------------
Recebe a lista de itens que a IA extraiu (mesmo formato de reader/export) e
grava cada ato numa linha da tabela DOERJ_ATOS_PESSOAL.

Conexão: python-oracledb em modo THIN (Python puro, sem precisar instalar o
Oracle Instant Client — importante na máquina travada da SEFAZ). As credenciais
vêm do .env (config.oracle_settings), relidas na hora, como config.get_client().

Idempotência: antes de inserir, apaga as linhas da MESMA edição + tema
(EDICAO + RESPOSTA). Assim, rodar o job 2x no mesmo dia não duplica — igual ao
"1 arquivo por dia" do job de Excel.

Schema esperado (fornecido pelo usuário):
  ID identity, RESPOSTA(150), TIPO(150), NUMERO(300), DATA_ATO DATE,
  ORGAO(1000), PESSOA(500), CARGO(2000), OBJETO(4000), PROCESSO(100),
  PAGINA(8), EDICAO DATE, DT_CARGA TIMESTAMP DEFAULT SYSTIMESTAMP.
ID e DT_CARGA são preenchidos pelo próprio banco (identity / default).
============================================================================
"""
import datetime
import re

import config
from search import MESES          # nome do mês -> número (reaproveitado da busca)

# Tamanho máximo de cada coluna VARCHAR2 (do schema). Usado para cortar o texto
# antes de inserir e evitar ORA-12899 (value too large for column).
_LIMITES = {
    "RESPOSTA": 150, "TIPO": 150, "NUMERO": 300, "ORGAO": 1000, "PESSOA": 500,
    "CARGO": 2000, "OBJETO": 4000, "PROCESSO": 100, "PAGINA": 8,
}

# Só nomes de tabela "sãos" (evita SQL injection pelo nome vindo do .env).
_TABELA_RE = re.compile(r"^[A-Za-z0-9_$.]+$")


def configurado():
    """True se as credenciais do Oracle estão preenchidas no .env.

    Permite ao job PULAR a gravação (com aviso) em vez de derrubar o pipeline
    inteiro quando o banco ainda não foi configurado."""
    s = config.oracle_settings()
    return bool(s["user"] and s["password"] and s["dsn"]
                and s["dsn"] != "host:1521/SERVICE_NAME")   # o placeholder do .env.example


def get_connection():
    """Abre uma conexão Oracle (thin) com as credenciais do .env."""
    import oracledb                                  # import tardio: só quando for usar
    s = config.oracle_settings()
    if not (s["user"] and s["password"] and s["dsn"]):
        raise RuntimeError(
            "Configure ORACLE_USER, ORACLE_PASSWORD e ORACLE_DSN no .env "
            "(ex.: ORACLE_DSN=host:1521/SERVICE_NAME)."
        )
    return oracledb.connect(user=s["user"], password=s["password"], dsn=s["dsn"])


def _safe_date(y, mo, d):
    """Monta um datetime.date, devolvendo None se a data for inválida (ex.: 31/02)."""
    try:
        return datetime.date(y, mo, d)
    except ValueError:
        return None


def _to_date(valor):
    """Converte texto de data em datetime.date; None se vazio/ilegível (NUNCA inventa).

    Aceita: 2026-07-14 · 14/07/2026 (ou 14/07/26) · '14 de julho de 2026'."""
    if not valor:
        return None
    t = str(valor).strip()
    if not t:
        return None
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)                 # ISO
    if m:
        y, mo, d = (int(g) for g in m.groups())
        return _safe_date(y, mo, d)
    m = re.search(r"(\d{1,2})[/.](\d{1,2})[/.](\d{2,4})", t)         # DD/MM/AAAA
    if m:
        d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if y < 100:
            y += 2000
        return _safe_date(y, mo, d)
    m = re.search(r"(\d{1,2})\s+de\s+([a-zçã]+)(?:\s+de\s+(\d{4}))?", t, re.IGNORECASE)
    if m and m.group(2).lower() in MESES:                            # 14 de julho de 2026
        d, mo = int(m.group(1)), MESES[m.group(2).lower()]
        if m.group(3):
            return _safe_date(int(m.group(3)), mo, d)
    return None


def _trunc(valor, n):
    """Corta a string em n caracteres; devolve None para vazio (vira NULL no Oracle)."""
    s = ("" if valor is None else str(valor)).strip()
    return s[:n] if s else None


def _tabela():
    """Nome da tabela do .env, validado (evita SQL injection pelo nome)."""
    tabela = config.oracle_settings()["table"]
    if not _TABELA_RE.match(tabela):
        raise RuntimeError(f"Nome de tabela invalido em ORACLE_TABLE: {tabela!r}")
    return tabela


def ja_gravado(resposta, edicao_iso):
    """True se JÁ existem linhas desta edição + tema na tabela.

    Serve de TRAVA de custo: o job roda de hora em hora (o D.O. pode atrasar), e
    sem isto a IA seria chamada de novo a cada execução sobre a MESMA edição.
    Consultar o banco é barato; reler as páginas com a IA, não."""
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT COUNT(*) FROM {_tabela()} WHERE EDICAO = :ed AND RESPOSTA = :r",
            {"ed": _to_date(edicao_iso), "r": _trunc(resposta, _LIMITES["RESPOSTA"])},
        )
        return cur.fetchone()[0] > 0
    finally:
        con.close()


def save_atos(resposta, edicao_iso, itens):
    """Grava os `itens` na tabela, para a edição `edicao_iso` (AAAA-MM-DD) e tema `resposta`.

    Apaga antes as linhas da mesma EDICAO+RESPOSTA (idempotência). Devolve o nº inserido."""
    tabela = _tabela()
    edicao_date = _to_date(edicao_iso)
    resposta = _trunc(resposta, _LIMITES["RESPOSTA"])

    linhas = [
        {
            "resposta": resposta,
            "tipo": _trunc(it.get("tipo"), _LIMITES["TIPO"]),
            "numero": _trunc(it.get("numero"), _LIMITES["NUMERO"]),
            "data_ato": _to_date(it.get("data")),
            "orgao": _trunc(it.get("orgao"), _LIMITES["ORGAO"]),
            "pessoa": _trunc(it.get("pessoa"), _LIMITES["PESSOA"]),
            "cargo": _trunc(it.get("cargo"), _LIMITES["CARGO"]),
            "objeto": _trunc(it.get("objeto"), _LIMITES["OBJETO"]),
            "processo": _trunc(it.get("processo"), _LIMITES["PROCESSO"]),
            "pagina": _trunc(it.get("pagina"), _LIMITES["PAGINA"]),
            "edicao": edicao_date,
        }
        for it in itens
    ]

    con = get_connection()
    try:
        cur = con.cursor()
        # Idempotência: limpa a edição+tema antes de reinserir.
        cur.execute(
            f"DELETE FROM {tabela} WHERE EDICAO = :ed AND RESPOSTA = :r",
            {"ed": edicao_date, "r": resposta},
        )
        if linhas:
            cur.executemany(
                f"INSERT INTO {tabela} "
                "(RESPOSTA, TIPO, NUMERO, DATA_ATO, ORGAO, PESSOA, CARGO, OBJETO, "
                " PROCESSO, PAGINA, EDICAO) VALUES "
                "(:resposta, :tipo, :numero, :data_ato, :orgao, :pessoa, :cargo, "
                " :objeto, :processo, :pagina, :edicao)",
                linhas,
            )
        con.commit()
        return len(linhas)
    finally:
        con.close()
