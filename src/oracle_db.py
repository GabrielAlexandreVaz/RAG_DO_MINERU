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

Schema esperado :
  ID, RESPOSTA(150), TIPO(150), NUMERO(300), DATA_ATO DATE, ORGAO(1000),
  PESSOA(500), ID_FUNCIONAL(40), CARGO(2000), OBJETO(4000), PROCESSO(100),
  PAGINA(8), EDICAO DATE, DT_CARGA TIMESTAMP DEFAULT SYSTIMESTAMP.
ID_FUNCIONAL = matrícula/IF do servidor (identificador único da pessoa).
DT_CARGA é preenchido pelo próprio banco (default SYSTIMESTAMP).
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
    "ID_FUNCIONAL": 40, "CARGO": 2000, "OBJETO": 4000, "PROCESSO": 100, "PAGINA": 8,
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


def _qualificar(nome):
    """Qualifica com o schema e coloca a TABELA entre ASPAS DUPLAS.

    Nomes que começam com dígito (ex.: 0001IA_IOERJ_...) exigem aspas no Oracle,
    senão dá ORA-00942/erro de sintaxe. Resultado: COE_IA."0001IA_..." (schema
    sem aspas). O nome é validado (evita injeção pelo valor do .env)."""
    if not _TABELA_RE.match(nome or ""):
        raise RuntimeError(f"Nome de tabela invalido: {nome!r}")
    schema = config.oracle_settings().get("schema") or ""
    return f'{schema}."{nome}"' if schema else f'"{nome}"'


def _tabela():
    """Nome qualificado+aspas da tabela 001A (atos de pessoal), do .env."""
    return _qualificar(config.oracle_settings()["table"])


def _tabela_monitor():
    """Nome qualificado+aspas da tabela 002A (monitor). Erro se não configurada."""
    nome = config.oracle_settings().get("table_monitor")
    if not nome:
        raise RuntimeError("ORACLE_TABLE_MONITOR nao configurado no .env (tabela 002A).")
    return _qualificar(nome)


def _tabela_filtro():
    """Nome qualificado+aspas da tabela 002B (pré-filtro do monitor)."""
    nome = config.oracle_settings().get("table_filtro")
    if not nome:
        raise RuntimeError("ORACLE_TABLE_FILTRO nao configurado no .env (tabela 002B).")
    return _qualificar(nome)


def listar_palavras_filtro(edicao_iso=None):
    """Palavras do PRÉ-FILTRO do monitor (002B) VIGENTES na data da edição.

    São os termos que decidem quais páginas vão para a IA no monitor estruturado
    ('fazenda', 'sefaz', 'ponto facultativo'...). Incluir um termo = mais páginas
    lidas (mais recall, mais custo); retirar = menos páginas. Devolve a lista de
    strings na ordem do ID."""
    ref = _to_date(edicao_iso) or datetime.date.today()
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT PALAVRA_CHAVE FROM {_tabela_filtro()} "
            "WHERE PALAVRA_CHAVE IS NOT NULL "
            "  AND (DATA_INI IS NULL OR DATA_INI <= :ed) "
            "  AND (DATA_FIM IS NULL OR DATA_FIM >= :ed) "
            "ORDER BY ID",
            {"ed": ref},
        )
        return [(p or "").strip() for (p,) in cur.fetchall() if (p or "").strip()]
    finally:
        con.close()


def _tabela_monitorados():
    """Nome qualificado+aspas da tabela 002N (nomes monitorados). Erro se não configurada."""
    nome = config.oracle_settings().get("table_monitorados")
    if not nome:
        raise RuntimeError("ORACLE_TABLE_MONITORADOS nao configurado no .env (tabela 002N).")
    return _qualificar(nome)


def _tabela_palavras():
    """Nome qualificado+aspas da tabela 001B (palavras-chave). Erro se não configurada."""
    nome = config.oracle_settings().get("table_palavras")
    if not nome:
        raise RuntimeError("ORACLE_TABLE_PALAVRAS nao configurado no .env (tabela 001B).")
    return _qualificar(nome)


def listar_palavras_chave(edicao_iso=None):
    """Palavras-chave dos atos de pessoal (001B) VIGENTES na data da edição.

    Mesma ideia da 002N: para procurar um novo tipo de ato no D.O., basta um
    INSERT ('designar', 'aposentar'...); para parar, um UPDATE preenchendo
    DATA_FIM. Devolve a lista de strings na ordem do ID."""
    ref = _to_date(edicao_iso) or datetime.date.today()
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT PALAVRA_CHAVE FROM {_tabela_palavras()} "
            "WHERE PALAVRA_CHAVE IS NOT NULL "
            "  AND (DATA_INI IS NULL OR DATA_INI <= :ed) "
            "  AND (DATA_FIM IS NULL OR DATA_FIM >= :ed) "
            "ORDER BY ID",
            {"ed": ref},
        )
        return [(p or "").strip() for (p,) in cur.fetchall() if (p or "").strip()]
    finally:
        con.close()


def listar_monitorados(edicao_iso=None):
    """Lê a lista de pessoas monitoradas da tabela 002N, VIGENTES na data da edição.

    Esta é a fonte da verdade dos nomes: para incluir alguém, basta um INSERT na
    tabela; para retirar, um UPDATE preenchendo DATA_FIM (não precisa apagar a
    linha — assim o histórico fica preservado e reprocessar uma edição antiga
    continua usando a lista que valia NAQUELE dia).

    Vigente na edição D = (DATA_INI nula ou <= D) E (DATA_FIM nula ou >= D).
    Devolve [{"nome": ..., "funcao": ...}] na ordem do ID."""
    ref = _to_date(edicao_iso) or datetime.date.today()
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT NOME, FUNCAO FROM {_tabela_monitorados()} "
            "WHERE NOME IS NOT NULL "
            "  AND (DATA_INI IS NULL OR DATA_INI <= :ed) "
            "  AND (DATA_FIM IS NULL OR DATA_FIM >= :ed) "
            "ORDER BY ID",
            {"ed": ref},
        )
        return [{"nome": (n or "").strip(), "funcao": (f or "").strip()}
                for n, f in cur.fetchall() if (n or "").strip()]
    finally:
        con.close()


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
            "id_funcional": _trunc(it.get("id_funcional"), _LIMITES["ID_FUNCIONAL"]),
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
                "(RESPOSTA, TIPO, NUMERO, DATA_ATO, ORGAO, PESSOA, ID_FUNCIONAL, CARGO, "
                " OBJETO, PROCESSO, PAGINA, EDICAO) VALUES "
                "(:resposta, :tipo, :numero, :data_ato, :orgao, :pessoa, :id_funcional, "
                " :cargo, :objeto, :processo, :pagina, :edicao)",
                linhas,
            )
        con.commit()
        return len(linhas)
    finally:
        con.close()


# ==========================================================================
#  MONITORAMENTO (8 seções) -> UMA tabela única (002A)
# ==========================================================================
# Tamanhos das colunas da 002A (mais apertados que os do Excel).
_LIM_002 = {
    "TIPO": 26, "TIPO_ATO": 120, "NUMERO_ANO": 56, "ORGAO": 128, "PESSOA": 360,
    "CARGO": 104, "PROCESSO": 200, "VIGENCIA": 56, "RESUMO": 704, "CADERNO": 48,
}


def _pagina_num(valor):
    """Primeiro inteiro do texto de página ('20-21' -> 20, '' -> None). PAGINA é NUMBER na 002A."""
    m = re.search(r"\d+", str(valor or ""))
    return int(m.group()) if m else None


def ja_gravado_monitor(edicao_iso):
    """True se a 002A já tem linhas desta edição (trava de custo no pipeline)."""
    alvo = _tabela_monitor()
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(f"SELECT COUNT(*) FROM {alvo} WHERE DATA_EDICAO = :ed",
                    {"ed": _to_date(edicao_iso)})
        return cur.fetchone()[0] > 0
    finally:
        con.close()


def listar_monitoramento(edicao_iso):
    """Lê de volta as linhas da 002A de UMA edição -> [{coluna: valor}], ordenadas.

    Existe para o resumo executivo nascer do BANCO, e não do Excel local: a área
    demandante valida a 002A, então é ela a fonte da verdade. ID vem no SELECT, mas
    hoje é sempre nulo (ver salvar_monitoramento): a rastreabilidade do resumo é
    feita por PESSOA + PROCESSO + PAGINA."""
    cols = ["ID", "TIPO", "TIPO_ATO", "NUMERO_ANO", "ORGAO", "PESSOA", "CARGO",
            "PROCESSO", "VIGENCIA", "RESUMO", "CADERNO", "PAGINA", "DATA_EDICAO",
            "DATA_ATO", "PRAZO"]
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT {', '.join(cols)} FROM {_tabela_monitor()} "
            "WHERE DATA_EDICAO = :ed ORDER BY TIPO, PAGINA, ID",
            {"ed": _to_date(edicao_iso)},
        )
        return [dict(zip(cols, linha)) for linha in cur.fetchall()]
    finally:
        con.close()


def salvar_monitoramento(edicao_iso, itens, categoria_secao=None):
    """Grava os itens do monitor na tabela ÚNICA 002A. Idempotente por DATA_EDICAO
    (DELETE da edição + INSERT). `TIPO` recebe a categoria técnica do item.
    Devolve o nº de linhas inseridas. (`categoria_secao` mantido só por compat.)"""
    alvo = _tabela_monitor()
    edicao_date = _to_date(edicao_iso)
    # ID fica NULO de proposito (03/08/2026). A tabela nao tem sequence nem identity,
    # e chegamos a preencher um numero nosso (AAAAMMDD9999) para o resumo executivo
    # citar. Foi descartado: a rastreabilidade que a area pediu se resolve com as
    # colunas de negocio que ja existem - PESSOA + PROCESSO + PAGINA identificam 94%
    # dos registros -, e um numero sintetico nao diz nada a quem le o relatorio.
    # A ideia em avaliacao para esta coluna e outra: guardar o "Id: NNNNNNN" que o
    # proprio DOERJ publica no fim de cada materia, que leva a publicacao oficial.
    # Ele ainda nao serve: identifica a MATERIA, nao a linha (uma portaria vira 3
    # registros nossos), e so conseguimos casar 59% dos registros com ele. Enquanto
    # nao houver decisao, nao inventamos chave.
    linhas = [
        {
            "tipo": _trunc((it.get("categoria") or "").strip().upper(), _LIM_002["TIPO"]),
            "tipo_ato": _trunc(it.get("tipo_ato"), _LIM_002["TIPO_ATO"]),
            "numero_ano": _trunc(it.get("numero_ano"), _LIM_002["NUMERO_ANO"]),
            "orgao": _trunc(it.get("orgao"), _LIM_002["ORGAO"]),
            "pessoa": _trunc(it.get("pessoa"), _LIM_002["PESSOA"]),
            "cargo": _trunc(it.get("cargo"), _LIM_002["CARGO"]),
            "processo": _trunc(it.get("processo"), _LIM_002["PROCESSO"]),
            "vigencia": _trunc(it.get("vigencia"), _LIM_002["VIGENCIA"]),
            "resumo": _trunc(it.get("resumo"), _LIM_002["RESUMO"]),
            "caderno": _trunc(it.get("caderno"), _LIM_002["CADERNO"]),
            "data_edicao": edicao_date,
            "data_ato": _to_date(it.get("data_ato")),
            "prazo": _to_date(it.get("prazo")),
            "pagina": _pagina_num(it.get("pagina")),
        }
        for it in itens
    ]

    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(f"DELETE FROM {alvo} WHERE DATA_EDICAO = :ed", {"ed": edicao_date})
        if linhas:
            cur.executemany(
                f"INSERT INTO {alvo} (TIPO, TIPO_ATO, NUMERO_ANO, ORGAO, PESSOA, CARGO, "
                "PROCESSO, VIGENCIA, RESUMO, CADERNO, DATA_EDICAO, DATA_ATO, PRAZO, PAGINA) "
                "VALUES (:tipo, :tipo_ato, :numero_ano, :orgao, :pessoa, :cargo, "
                ":processo, :vigencia, :resumo, :caderno, :data_edicao, :data_ato, :prazo, :pagina)",
                linhas,
            )
        con.commit()
        return len(linhas)
    finally:
        con.close()
