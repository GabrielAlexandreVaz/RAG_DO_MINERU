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
# TIPO era VARCHAR2(26) e cortava EXPEDIENTE_PONTO_FACULTATIVO (28 caracteres), a
# unica das 9 categorias que nao cabia. Como resumo_executivo e export_002a casam
# o TIPO por igualdade exata contra CATEGORIA_SECAO, o item truncado caia em
# "(sem secao)" e sumia da secao 5. Nunca apareceu porque a 002A so recebeu a
# primeira linha dessa categoria em 07/08/2026 (Decreto 50.418, ponto facultativo
# publicado em edicao extra). Coluna alargada para 32 no banco.
_LIM_002 = {
    "TIPO": 32, "TIPO_ATO": 120, "NUMERO_ANO": 56, "ORGAO": 128, "PESSOA": 360,
    "CARGO": 104, "PROCESSO": 200, "VIGENCIA": 56, "RESUMO": 704, "CADERNO": 48,
}


def _pagina_num(valor):
    """Primeiro inteiro do texto de página ('20-21' -> 20, '' -> None). PAGINA é NUMBER na 002A."""
    m = re.search(r"\d+", str(valor or ""))
    return int(m.group()) if m else None


def _so_numero(valor):
    """Valor inteiro ou None (ID_DOERJ é NUMBER; o casamento pode não achar a matéria)."""
    m = re.fullmatch(r"\s*(\d{1,12})\s*", str(valor or ""))
    return int(m.group(1)) if m else None


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


def listar_monitoramento(edicao_iso, extra=None):
    """Lê de volta as linhas da 002A de UMA edição -> [{coluna: valor}], ordenadas.

    Existe para o resumo executivo nascer do BANCO, e não do Excel local: a área
    demandante valida a 002A, então é ela a fonte da verdade.

    `extra` separa as duas publicações que dividem a mesma DATA_EDICAO (ver
    _faixa_id): None = tudo, False = só a edição normal, True = só a extra."""
    cols = ["ID", "ID_DOERJ", "TIPO", "TIPO_ATO", "NUMERO_ANO", "ORGAO", "PESSOA", "CARGO",
            "PROCESSO", "VIGENCIA", "RESUMO", "CADERNO", "PAGINA", "DATA_EDICAO",
            "DATA_ATO", "PRAZO"]
    edicao_date = _to_date(edicao_iso)
    args = {"ed": edicao_date}
    filtro = ""
    if extra is not None:
        ini, fim = _faixa_id(edicao_date, extra)
        args.update({"ini": ini, "fim": fim, "cad": f"%{_CADERNO_EXTRA}%"})
        # DOIS sinais, e são complementares. A FAIXA DE ID classifica tudo que este
        # código grava. O CADERNO alcança as linhas gravadas ANTES da separação
        # existir: em 07/08/2026 a edição extra foi indexada à mão e o monitor,
        # que então varria a data inteira, gravou o ato dela na faixa normal.
        # Sem o segundo sinal, aquele ato apareceria no boletim da edição normal.
        # (No DELETE de salvar_monitoramento vale só o ID: a coluna CADERNO vem
        # item a item e às vezes chega vazia — uma linha assim não seria alcançada
        # por DELETE nenhum e sobreviveria para sempre.)
        filtro = (" AND (ID BETWEEN :ini AND :fim OR UPPER(CADERNO) LIKE :cad)" if extra else
                  " AND (ID IS NULL OR ID BETWEEN :ini AND :fim)"
                  " AND (CADERNO IS NULL OR UPPER(CADERNO) NOT LIKE :cad)")
    con = get_connection()
    try:
        cur = con.cursor()
        cur.execute(
            f"SELECT {', '.join(cols)} FROM {_tabela_monitor()} "
            f"WHERE DATA_EDICAO = :ed{filtro} ORDER BY TIPO, PAGINA, ID", args,
        )
        return [dict(zip(cols, linha)) for linha in cur.fetchall()]
    finally:
        con.close()


# Onde a faixa de ID da edição EXTRA começa, dentro dos 10.000 reservados por dia.
# A edição normal fica em base+1..base+4999 e a extra em base+5000..base+9999.
# Por que partir a faixa, em vez de apagar por CADERNO: a coluna CADERNO é
# preenchida item a item (vem da IA ou da varredura) e às vezes chega vazia — uma
# linha assim não seria alcançada por nenhum dos dois DELETEs e sobreviveria para
# sempre. O ID é chave NOSSA, determinística, e ainda deixa a origem legível:
# MOD(ID,10000) >= 5000 veio de edição extra.
# Compatível com o histórico: o maior dia até aqui teve 76 itens, todos em
# base+1..base+76 — nada a migrar.
_ID_EXTRA_INICIO = 5000
_ID_POR_EDICAO = 10000
# Marca do caderno de edição extra na coluna CADERNO (ver config.EXTRA_CADERNO_SUFIXO;
# em MAIÚSCULAS porque a comparação no Oracle é feita sobre UPPER(CADERNO)).
_CADERNO_EXTRA = "EDICAO EXTRA"


def _faixa_id(edicao_date, extra=False):
    """(primeiro, último) ID reservado para esta publicação naquela data."""
    base = int(edicao_date.strftime("%Y%m%d")) * _ID_POR_EDICAO
    if extra:
        return base + _ID_EXTRA_INICIO, base + _ID_POR_EDICAO - 1
    return base + 1, base + _ID_EXTRA_INICIO - 1


def e_da_edicao_extra(id_linha):
    """True se a linha da 002A veio da EDIÇÃO EXTRA (pela faixa do ID)."""
    try:
        return int(id_linha) % _ID_POR_EDICAO >= _ID_EXTRA_INICIO
    except (TypeError, ValueError):
        return False


def salvar_monitoramento(edicao_iso, itens, categoria_secao=None, extra=False):
    """Grava os itens do monitor na tabela ÚNICA 002A. Idempotente por edição.
    `TIPO` recebe a categoria técnica do item. Devolve o nº de linhas inseridas.
    (`categoria_secao` mantido só por compat.)

    `extra=True` grava a EDIÇÃO EXTRA do mesmo dia. As duas publicações convivem
    na mesma DATA_EDICAO, então a faixa de ID é PARTIDA (ver _faixa_id) e o DELETE
    apaga só a faixa correspondente. Apagar por DATA_EDICAO, como era, faria a
    rodada da extra levar junto as linhas da edição normal do dia."""
    alvo = _tabela_monitor()
    edicao_date = _to_date(edicao_iso)
    # ID: chave NOSSA, para localizar a linha (a tabela nao tem sequence nem
    # identity, e ate 04/08/2026 a coluna estava nula em todas as linhas). Formato
    # AAAAMMDD9999 - legivel, unico entre edicoes, estavel dentro da edicao.
    # Reprocessar a edicao renumera, o que e coerente com o DELETE+INSERT.
    # Nao confundir com o Id do proprio DOERJ (o "Id: NNNNNNN" no fim de cada
    # materia): esse identifica a MATERIA publicada, se repete quando uma materia
    # vira varios registros nossos, e por isso vai em coluna propria.
    ini_id, fim_id = _faixa_id(edicao_date, extra)
    if len(itens) > (fim_id - ini_id + 1):       # nunca chegou perto (76 no maior dia)
        raise ValueError(f"{len(itens)} itens numa edicao estoura a faixa de ID reservada")
    linhas = [
        {
            "id": ini_id + i - 1,
            "id_doerj": _so_numero(it.get("id_doerj")),
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
        for i, it in enumerate(itens, start=1)
    ]

    con = get_connection()
    try:
        cur = con.cursor()
        # DELETE só da FAIXA desta publicação: a edição normal e a extra do mesmo
        # dia dividem a DATA_EDICAO, e apagar por data levaria a outra junto.
        cur.execute(f"DELETE FROM {alvo} WHERE DATA_EDICAO = :ed AND ID BETWEEN :ini AND :fim",
                    {"ed": edicao_date, "ini": ini_id, "fim": fim_id})
        if linhas:
            cur.executemany(
                f"INSERT INTO {alvo} (ID, ID_DOERJ, TIPO, TIPO_ATO, NUMERO_ANO, ORGAO, PESSOA, "
                "CARGO, PROCESSO, VIGENCIA, RESUMO, CADERNO, DATA_EDICAO, DATA_ATO, PRAZO, PAGINA) "
                "VALUES (:id, :id_doerj, :tipo, :tipo_ato, :numero_ano, :orgao, :pessoa, :cargo, "
                ":processo, :vigencia, :resumo, :caderno, :data_edicao, :data_ato, :prazo, :pagina)",
                linhas,
            )
        con.commit()
        return len(linhas)
    finally:
        con.close()
