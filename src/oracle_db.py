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


def _normalize_dsn(dsn):
    """Aceita o DSN em Easy Connect (host:porta/service) OU colado de uma URL JDBC
    (jdbc:oracle:thin:@//host:porta/service) e devolve o formato do python-oracledb."""
    d = (dsn or "").strip()
    if d.lower().startswith("jdbc:oracle:thin:@"):
        d = d[len("jdbc:oracle:thin:@"):]
    return d.lstrip("/")                             # remove o // do Easy Connect longo


def get_connection():
    """Abre uma conexão Oracle (thin) com as credenciais do .env."""
    import oracledb                                  # import tardio: só quando for usar
    s = config.oracle_settings()
    if not (s["user"] and s["password"] and s["dsn"]):
        raise RuntimeError(
            "Configure ORACLE_USER, ORACLE_PASSWORD e ORACLE_DSN no .env "
            "(ex.: ORACLE_DSN=host:1521/SERVICE_NAME)."
        )
    return oracledb.connect(user=s["user"], password=s["password"], dsn=_normalize_dsn(s["dsn"]))


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


# ==========================================================================
#  MONITORAMENTO (8 seções) -> uma tabela por tema
# ==========================================================================
# seção (número) -> nome da tabela. Fonte da verdade dos nomes.
TABELAS_MONITOR = {
    1: "DOERJ_PRAZOS",
    2: "DOERJ_MOVIMENTACOES",
    3: "DOERJ_NOMES_MONITORADOS",
    4: "DOERJ_OBSERVACOES",
    5: "DOERJ_EXPEDIENTE",
    6: "DOERJ_TCE",
    7: "DOERJ_LEGISLATIVO",
    8: "DOERJ_MUNICIPALIDADES",
}

# Tamanho de cada coluna (as 8 tabelas têm o mesmo formato).
_LIM_MON = {
    "CADERNO": 30, "PAGINA": 8, "CATEGORIA": 40, "TIPO_ATO": 200, "NUMERO_ANO": 100,
    "ORGAO": 500, "PESSOA": 2000, "CARGO": 500, "PROCESSO": 150, "VIGENCIA": 150,
    "RESUMO": 4000,
}


def _qual(tabela):
    """Qualifica o nome com o schema do .env (ex.: COE_IA.DOERJ_PRAZOS), se houver."""
    schema = config.oracle_settings().get("schema") or ""
    nome = f"{schema}.{tabela}" if schema else tabela
    if not _TABELA_RE.match(nome):
        raise RuntimeError(f"Nome de tabela/schema invalido: {nome!r}")
    return nome


def _ddl_monitor(tabela):
    return (
        f"CREATE TABLE {_qual(tabela)} (\n"
        "    ID          NUMBER(19) GENERATED ALWAYS AS IDENTITY,\n"
        "    EDICAO      DATE,\n"
        "    CADERNO     VARCHAR2(30),\n"
        "    PAGINA      VARCHAR2(8),\n"
        "    CATEGORIA   VARCHAR2(40),\n"
        "    TIPO_ATO    VARCHAR2(200),\n"
        "    NUMERO_ANO  VARCHAR2(100),\n"
        "    ORGAO       VARCHAR2(500),\n"
        "    PESSOA      VARCHAR2(2000),\n"
        "    CARGO       VARCHAR2(500),\n"
        "    PROCESSO    VARCHAR2(150),\n"
        "    VIGENCIA    VARCHAR2(150),\n"
        "    RESUMO      VARCHAR2(4000),\n"
        "    DT_CARGA    TIMESTAMP DEFAULT SYSTIMESTAMP,\n"
        f"    CONSTRAINT PK_{tabela} PRIMARY KEY (ID)\n"
        ")"
    )


def criar_tabelas_monitor():
    """Cria as 8 tabelas do monitoramento (idempotente: ignora 'já existe' ORA-00955).

    Útil para o schema do próprio usuário. Em produção, o DBA roda o mesmo DDL."""
    con = get_connection()
    try:
        cur = con.cursor()
        criadas = []
        for tabela in TABELAS_MONITOR.values():
            try:
                cur.execute(_ddl_monitor(tabela))
                criadas.append(_qual(tabela))
            except Exception as e:  # noqa: BLE001
                if "ORA-00955" in str(e):     # name is already used by an existing object
                    continue
                raise
        con.commit()
        return criadas
    finally:
        con.close()


def salvar_monitoramento(edicao_iso, itens, categoria_secao):
    """Grava os itens do monitor nas 8 tabelas por seção. Idempotente por EDICAO.

    `categoria_secao`: dict categoria(str)->número da seção (de monitor_estruturado).
    Devolve {tabela: nº_linhas_inseridas}."""
    edicao_date = _to_date(edicao_iso)
    por_tabela = {t: [] for t in TABELAS_MONITOR.values()}
    for it in itens:
        sec = categoria_secao.get((it.get("categoria") or "").strip().upper())
        if not sec:
            continue
        por_tabela[TABELAS_MONITOR[sec]].append({
            "edicao": edicao_date,
            "caderno": _trunc(it.get("caderno"), _LIM_MON["CADERNO"]),
            "pagina": _trunc(it.get("pagina"), _LIM_MON["PAGINA"]),
            "categoria": _trunc(it.get("categoria"), _LIM_MON["CATEGORIA"]),
            "tipo_ato": _trunc(it.get("tipo_ato"), _LIM_MON["TIPO_ATO"]),
            "numero_ano": _trunc(it.get("numero_ano"), _LIM_MON["NUMERO_ANO"]),
            "orgao": _trunc(it.get("orgao"), _LIM_MON["ORGAO"]),
            "pessoa": _trunc(it.get("pessoa"), _LIM_MON["PESSOA"]),
            "cargo": _trunc(it.get("cargo"), _LIM_MON["CARGO"]),
            "processo": _trunc(it.get("processo"), _LIM_MON["PROCESSO"]),
            "vigencia": _trunc(it.get("vigencia"), _LIM_MON["VIGENCIA"]),
            "resumo": _trunc(it.get("resumo"), _LIM_MON["RESUMO"]),
        })

    con = get_connection()
    try:
        cur = con.cursor()
        resultados = {}
        for tabela, linhas in por_tabela.items():
            alvo = _qual(tabela)
            cur.execute(f"DELETE FROM {alvo} WHERE EDICAO = :ed", {"ed": edicao_date})
            if linhas:
                cur.executemany(
                    f"INSERT INTO {alvo} (EDICAO, CADERNO, PAGINA, CATEGORIA, TIPO_ATO, "
                    "NUMERO_ANO, ORGAO, PESSOA, CARGO, PROCESSO, VIGENCIA, RESUMO) VALUES "
                    "(:edicao, :caderno, :pagina, :categoria, :tipo_ato, :numero_ano, "
                    ":orgao, :pessoa, :cargo, :processo, :vigencia, :resumo)",
                    linhas,
                )
            resultados[tabela] = len(linhas)
        con.commit()
        return resultados
    finally:
        con.close()
