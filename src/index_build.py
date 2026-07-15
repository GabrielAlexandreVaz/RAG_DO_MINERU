"""
============================================================================
index_build.py · Etapa 2 do pipeline — TEXTO do MinerU -> ÍNDICE de busca
----------------------------------------------------------------------------
Pega o texto limpo que o MinerU extraiu (content_list.json), agrupa por PÁGINA
e grava tudo num índice de busca full-text (SQLite FTS5). É esse índice que
permite, depois, buscar por palavra-chave muito rápido, com ranking BM25.

Idempotente: pula PDFs que já foram indexados e não mudaram (compara tamanho e
data de modificação). Rápido (segundos), porque a parte lenta (o MinerU) já foi
feita na etapa 1.

Uso:
    python src/index_build.py            # indexa o que o MinerU já extraiu
    python src/index_build.py --extract  # extrai (MinerU) os pendentes e indexa
    python src/index_build.py --force    # reindexa tudo do zero
============================================================================
"""
import argparse
import json                 # ler o content_list.json
import re                   # extrair a data do nome do arquivo (regex)
import sqlite3              # banco SQLite (com FTS5, embutido no Python)
import sys

import fitz                 # PyMuPDF — texto da camada do PDF (rede de segurança)

import config
import extrair              # reaproveita a etapa 1 (para --extract e achar o JSON)

# Se o MinerU extrair MENOS que esta fração do texto do PDF numa página, essa
# página é considerada "perdida" pelo MinerU e usamos o texto do PDF (completo).
FALLBACK_RATIO = 0.5

# Regex que captura a data no nome do arquivo, ex.: DOERJ_2026-07-07.pdf -> 2026-07-07
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def connect():
    """Abre (ou cria) o banco do índice e garante que as tabelas existem."""
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    # Tabela virtual FTS5: guarda o texto por página e permite busca full-text.
    #   pdf/date/page = UNINDEXED -> guardados, mas não entram na busca textual;
    #   content = a coluna pesquisável;
    #   remove_diacritics 2 -> a busca ignora acentos (procurar "resolucao" acha "Resolução").
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5("
        "pdf UNINDEXED, date UNINDEXED, page UNINDEXED, content, "
        "tokenize='unicode61 remove_diacritics 2');"
    )
    # Tabela de controle: lembra quais PDFs já indexamos (para pular os repetidos).
    con.execute(
        "CREATE TABLE IF NOT EXISTS indexed_files("
        "pdf TEXT PRIMARY KEY, size INTEGER, mtime REAL, n_pages INTEGER);"
    )
    return con


def edition_date(name):
    """Extrai a data (YYYY-MM-DD) do nome do arquivo, ou '' se não achar."""
    m = DATE_RE.search(name)
    return m.group(1) if m else ""


def mais_recente(pdfs):
    """Escolhe o PDF mais atual da lista pela DATA no nome (ex.: 2026-07-08).

    Fallback: se algum arquivo não tiver data no nome, usa a data de modificação.
    Usado pelo job diário (--latest) para pegar sempre a edição do dia."""
    def chave(p):
        m = DATE_RE.search(p.name)
        return (1, m.group(1)) if m else (0, str(p.stat().st_mtime))
    return max(pdfs, key=chave)


def _elem_text(e):
    """Junta o texto pesquisável de UM elemento do content_list.

    O MinerU classifica cada bloco por 'type' (text, table, image...). Aqui
    reunimos o que interessa para busca: o texto, o corpo da tabela (HTML) e as
    legendas de tabela/imagem."""
    partes = []
    if e.get("text"):
        partes.append(e["text"])
    if e.get("table_body"):            # tabela -> HTML; ainda é útil para busca textual
        partes.append(e["table_body"])
    for chave in ("table_caption", "image_caption", "table_footnote", "image_footnote"):
        val = e.get(chave)
        if isinstance(val, list):
            partes.extend(str(x) for x in val if x)
        elif val:
            partes.append(str(val))
    return "\n".join(partes).strip()


def page_texts(content_list_path):
    """Lê o content_list.json e devolve {page_idx: texto_da_pagina}.

    page_idx é 0-based (a 1ª página é 0). Depois somamos 1 para exibir 1-based."""
    data = json.loads(content_list_path.read_text(encoding="utf-8"))
    por_pagina = {}
    for e in data:                                 # percorre cada bloco
        t = _elem_text(e)
        if t:
            # setdefault(...) cria a lista da página na 1ª vez; depois só anexa.
            por_pagina.setdefault(e.get("page_idx", 0), []).append(t)
    # Junta os blocos de cada página num texto só.
    return {pi: "\n".join(ps) for pi, ps in por_pagina.items()}


def index_pdf(con, pdf_path, force=False, extract=False):
    """Indexa UM PDF. Devolve quantas páginas foram inseridas (0 se pulou)."""
    name = pdf_path.name
    st = pdf_path.stat()                           # tamanho e data do arquivo
    row = con.execute("SELECT size, mtime FROM indexed_files WHERE pdf=?", (name,)).fetchone()
    # Já indexado e sem mudança? (mesmo tamanho e mesma data) -> pula.
    if row and not force and row[0] == st.st_size and abs(row[1] - st.st_mtime) < 1:
        return 0

    # Se o MinerU ainda não extraiu este PDF:
    if not extrair.ja_extraido(pdf_path):
        if not extract:
            print(f"[pula] {name}: ainda nao extraido pelo MinerU (use --extract).")
            return 0
        extrair.extrair(pdf_path)                  # com --extract, extrai agora (lento)

    cl = extrair.content_list_json(pdf_path)       # caminho do JSON do MinerU
    paginas = page_texts(cl)                        # {page_idx: texto do MinerU}

    # Apaga o que houver deste PDF (evita duplicar) e insere página a página.
    con.execute("DELETE FROM pages WHERE pdf=?", (name,))
    date = edition_date(name)

    # Rede de segurança: o MinerU às vezes PERDE blocos de uma página (o layout
    # daquela página derruba a extração). Abrimos o PDF e comparamos, página a
    # página, o texto do MinerU com o texto da camada do PDF. Se o MinerU pegou
    # bem menos que o PDF tem, usamos o texto do PDF (completo) para não perder
    # atos (ex.: exonerações). Nas páginas boas, mantemos o texto limpo do MinerU.
    doc = fitz.open(pdf_path)
    n = 0
    fallback = 0
    try:
        for page_idx in range(doc.page_count):
            m = (paginas.get(page_idx, "") or "").strip()      # texto do MinerU
            p = doc[page_idx].get_text("text").strip()         # texto do PDF
            if len(p) > 500 and len(m) < FALLBACK_RATIO * len(p):
                texto = p                                       # MinerU perdeu -> usa o PDF
                fallback += 1
            else:
                texto = m or p                                  # MinerU ok (ou vazio -> PDF)
            if texto.strip():
                con.execute(
                    "INSERT INTO pages(pdf, date, page, content) VALUES (?,?,?,?)",
                    (name, date, page_idx + 1, texto),   # +1 -> página 1-based na UI
                )
                n += 1
    finally:
        doc.close()
    if fallback:
        print(f"[fallback] {name}: {fallback} pagina(s) usaram o texto do PDF (MinerU perdeu conteudo)")
    # Marca este PDF como indexado (para pular na próxima vez).
    con.execute(
        "INSERT OR REPLACE INTO indexed_files(pdf, size, mtime, n_pages) VALUES (?,?,?,?)",
        (name, st.st_size, st.st_mtime, n),
    )
    con.commit()                                    # confirma as gravações no banco
    return n


def main():
    ap = argparse.ArgumentParser(description="Indexa o texto do MinerU no FTS5.")
    ap.add_argument("--force", action="store_true", help="Reindexa todos os PDFs")
    ap.add_argument("--extract", action="store_true", help="Extrai (MinerU) os pendentes antes de indexar")
    ap.add_argument("--latest", action="store_true",
                    help="Processa SÓ a edição mais recente (extraindo se preciso). Para o job diário.")
    args = ap.parse_args()

    con = connect()
    pdfs = sorted(config.DOWNLOADS_DIR.glob("*.pdf"))
    if not pdfs:
        print(f"[erro] nenhum PDF em {config.DOWNLOADS_DIR}", file=sys.stderr)
        sys.exit(1)

    # Job diário: limita à edição do dia e garante a extração (MinerU) dela.
    if args.latest:
        pdfs = [mais_recente(pdfs)]
        args.extract = True
        print(f"[latest] edicao do dia: {pdfs[0].name}")

    total_new = 0
    for p in pdfs:
        added = index_pdf(con, p, force=args.force, extract=args.extract)
        total_new += added
        if added:
            print(f"[ok] {p.name}: +{added} paginas")

    cnt = con.execute("SELECT count(*) FROM pages").fetchone()[0]   # total no índice
    con.close()
    print(f"[done] {total_new} paginas novas | indice com {cnt} paginas -> {config.DB_PATH}")


if __name__ == "__main__":
    main()
