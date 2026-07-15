"""
============================================================================
search.py · A BUSCA (retrieval) no índice de texto
----------------------------------------------------------------------------
Dada uma pergunta, encontra as páginas mais relevantes no índice FTS5, usando
o ranking BM25 (quanto MENOR o score, mais relevante — é como uma "distância").
Também sabe restringir a busca a uma data (edição) específica.

É a etapa que decide QUAIS páginas a IA vai ler depois.
============================================================================
"""
import re
import sqlite3

import config

# Palavras muito comuns que não ajudam no ranking (removidas antes de buscar).
STOPWORDS = {
    "a", "o", "as", "os", "de", "do", "da", "dos", "das", "e", "ou", "em", "no",
    "na", "nos", "nas", "um", "uma", "por", "para", "com", "que", "qual", "quais",
    "ao", "aos", "se", "sao", "foi", "ha", "the", "dia", "edicao",
}

# Nome do mês -> número (para entender datas escritas por extenso na pergunta).
MESES = {
    "janeiro": 1, "fevereiro": 2, "marco": 3, "março": 3, "abril": 4, "maio": 5,
    "junho": 6, "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10,
    "novembro": 11, "dezembro": 12,
}


def to_fts_query(text):
    """Converte uma pergunta livre numa query válida para o FTS5.

    Ex.: "editais de licitação" -> "editais OR licitação" (o 'de' é stopword).
    O OR faz a busca casar páginas que tenham QUALQUER um dos termos."""
    # Pega palavras (com acento) de 2+ letras; permite curinga no fim (licita*).
    tokens = [t for t in re.findall(r"\w+\*?", text, flags=re.UNICODE) if len(t) >= 2]
    # Tira as stopwords; se sobrar vazio, mantém os originais (não zera a busca).
    tokens = [t for t in tokens if t.lower() not in STOPWORDS] or tokens
    if not tokens:
        return None
    return " OR ".join(tokens)


def list_dates():
    """Datas de edição disponíveis no índice (YYYY-MM-DD), mais recentes primeiro."""
    if not config.DB_PATH.exists():
        return []
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT DISTINCT date FROM pages WHERE date <> '' ORDER BY date DESC"
        ).fetchall()
    finally:
        con.close()                                 # sempre fecha a conexão
    return [r[0] for r in rows]


def _date_candidates(text):
    """Acha possíveis datas escritas na pergunta, em vários formatos:
    2026-07-07 · 07/07/2026 (ou 07/07) · '7 de julho de 2026'."""
    t = text.lower()
    out = []
    for y, m, d in re.findall(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", t):
        out.append((int(y), int(m), int(d)))
    for d, m, y in re.findall(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b", t):
        out.append((int(y) if y else None, int(m), int(d)))
    for d, mes, y in re.findall(r"\b(\d{1,2})\s+de\s+([a-zçã]+)(?:\s+de\s+(\d{4}))?\b", t):
        if mes in MESES:
            out.append((int(y) if y else None, MESES[mes], int(d)))
    return out


def parse_date_from_query(text, available_dates):
    """Se a pergunta cita uma data que EXISTE no índice, devolve 'YYYY-MM-DD';
    senão, None.

    Só aceita a data se ela existir no índice — isso evita falsos positivos
    (números de processo, de portaria etc., que parecem data mas não são)."""
    avail = set(available_dates)
    years = sorted({int(x[:4]) for x in available_dates}, reverse=True)
    for (y, m, d) in _date_candidates(text):
        # Se o ano não veio na pergunta, testa os anos disponíveis no índice.
        cand_years = [y if y >= 100 else 2000 + y] if y is not None else years
        for yy in cand_years:
            if 1 <= m <= 12 and 1 <= d <= 31:
                iso = f"{yy:04d}-{m:02d}-{d:02d}"
                if iso in avail:
                    return iso
    return None


def search(query, k=4, date=None, fts=None):
    """Busca BM25 e devolve até `k` páginas. Se `date` for dado, filtra a edição.

    `fts` permite passar uma query FTS5 pronta (ex.: os atalhos mandam "exoner*")."""
    fts = fts or to_fts_query(query)                # usa o fts pronto, ou monta a partir da pergunta
    if not fts:
        return []
    # snippet(...) devolve um pedacinho do texto com os termos entre [colchetes]
    # (o front troca [ ] por marca-texto amarelo). O 3 é o índice da coluna 'content'.
    snip = "snippet(pages, 3, '[', ']', ' ... ', 14)"
    con = sqlite3.connect(config.DB_PATH)
    try:
        if date:                                    # busca restrita a uma edição
            rows = con.execute(
                f"SELECT pdf, date, page, {snip} AS snip, bm25(pages) AS score "
                "FROM pages WHERE pages MATCH ? AND date = ? ORDER BY score LIMIT ?",
                (fts, date, k),
            ).fetchall()
        else:                                       # busca em todas as edições
            rows = con.execute(
                f"SELECT pdf, date, page, {snip} AS snip, bm25(pages) AS score "
                "FROM pages WHERE pages MATCH ? ORDER BY score LIMIT ?",
                (fts, k),
            ).fetchall()
    except sqlite3.OperationalError:
        rows = []                                   # query FTS malformada -> sem resultados (sem erro 500)
    finally:
        con.close()
    # Transforma cada linha do banco num dicionário fácil de usar no resto do app.
    return [
        {"pdf": r[0], "date": r[1], "page": int(r[2]), "snippet": r[3], "score": r[4]}
        for r in rows
    ]


def page_content(pdf, page):
    """Texto COMPLETO (limpo, do MinerU) de uma página — usado pelo reader para
    montar o contexto que a IA vai ler."""
    con = sqlite3.connect(config.DB_PATH)
    try:
        row = con.execute(
            "SELECT content FROM pages WHERE pdf=? AND page=?", (pdf, page)
        ).fetchone()
    finally:
        con.close()
    return row[0] if row else ""


def pages_matching(fts, date, limit=80):
    """TODAS as páginas de uma edição (date) que casam com a query FTS `fts`,
    ordenadas por página.

    Diferente de search(): NÃO corta pelos top-k por relevância — devolve tudo
    (até `limit`). Usado pelo job de exonerações, que precisa cobrir todas as
    páginas com exonerações do dia (não só as mais relevantes)."""
    if not config.DB_PATH.exists():
        return []
    snip = "snippet(pages, 3, '[', ']', ' ... ', 14)"
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            f"SELECT pdf, date, page, {snip} AS snip, bm25(pages) AS score "
            "FROM pages WHERE pages MATCH ? AND date = ? ORDER BY page LIMIT ?",
            (fts, date, limit),
        ).fetchall()
    except sqlite3.OperationalError:
        rows = []
    finally:
        con.close()
    return [
        {"pdf": r[0], "date": r[1], "page": int(r[2]), "snippet": r[3], "score": r[4]}
        for r in rows
    ]
