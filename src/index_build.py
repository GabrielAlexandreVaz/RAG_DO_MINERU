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


def _migrate_caderno(con):
    """Se a tabela 'pages' existe SEM a coluna 'caderno', recria (com reindex).

    Reindexar a Parte I é barato: o texto vem do content_list.json do MinerU já
    salvo (não roda o MinerU de novo). Ao dropar 'pages' também limpamos
    indexed_files para forçar o repovoamento com a coluna nova."""
    row = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='pages'"
    ).fetchone()
    if not row:
        return
    cols = [r[1] for r in con.execute("PRAGMA table_info(pages)").fetchall()]
    if "caderno" not in cols:
        con.execute("DROP TABLE pages")
        try:
            con.execute("DELETE FROM indexed_files")
        except sqlite3.OperationalError:
            pass
        con.commit()
        print("[migracao] indice recriado com a coluna 'caderno' (Parte I sera reindexada).")


def connect():
    """Abre (ou cria) o banco do índice e garante que as tabelas existem."""
    config.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(config.DB_PATH)
    _migrate_caderno(con)
    # Tabela virtual FTS5: guarda o texto por página e permite busca full-text.
    #   pdf/caderno/date/page = UNINDEXED -> guardados, mas não entram na busca;
    #   content = a coluna pesquisável (índice 4 -> usado no snippet do search.py);
    #   remove_diacritics 2 -> a busca ignora acentos (procurar "resolucao" acha "Resolução").
    con.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS pages USING fts5("
        "pdf UNINDEXED, caderno UNINDEXED, date UNINDEXED, page UNINDEXED, content, "
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
        """Ordena os PDFs: os que têm data no nome vêm primeiro (tupla começando
        em 1) e, entre eles, pela data; o resto cai para a data de modificação."""
        m = DATE_RE.search(p.name)
        return (1, m.group(1)) if m else (0, str(p.stat().st_mtime))
    return max(pdfs, key=chave)


# ============================================================================
#  Lixo de fonte: glifos que vazam como caracteres de controle
# ============================================================================
# A tarja do IOERJ no alto de cada página do DOERJ usa uma fonte embutida (subset)
# sem ToUnicode CMap utilizável. Com MINERU_METHOD=txt (camada de texto do PDF,
# sem OCR) o que sai daquele bloco não são letras: são os ÍNDICES DE GLIFO crus,
# U+0001..U+001B. "DIÁRIO OFICIAL DO ESTADO DO RIO DE JANEIRO" chega como
# "\x01-Á\x03-\x04 \x04\x05-\x06-\x07\x08 ...", onde \x01=D, '-'=I, \x03=R, \x04=O.
# Acentuadas como Á e Ç escapam porque vêm de outra fonte.
#
# É sistemático (4.466 blocos header/page_number nas 23 edições extraídas), e o
# estrago não é estético: o cabeçalho fica, na ordem do content_list, ENTRE o
# último ato de uma página e a continuação dele na página seguinte. A IA, que
# transcreve com FIDELIDADE, copiava o lixo para dentro do RESUMO como se fosse a
# continuação do ato (edição de 18/08, ato do Subsecretário Adjunto: o texto
# terminava em "para," e emendava o cabeçalho). No HTML nada disso aparece — o
# navegador não renderiza U+0001..U+001F —, então o boletim saía com um rastro de
# hifens e acentos soltos e ninguém via a causa.
#
# Não dá para decodificar: o índice de glifo muda de fonte para fonte. Mas também
# não se pode descartar todo bloco 'header': em 12 das 23 edições a linha
# "ANO LII - Nº 149" da página 1 — de onde `boletim.cabecalho_edicao` tira o número
# da edição — vem num 'header' LEGÍVEL, e às vezes num bloco misto (parte glifo
# cru, parte texto bom). Por isso a regra é por CONTEÚDO, não por tipo: limpa
# sempre e só descarta o bloco quando sobrou menos do que se jogou fora.
_RX_CONTROLE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def limpar_controle(texto):
    """Tira os caracteres de controle do texto (mantém \\t, \\n e \\r)."""
    return _RX_CONTROLE.sub("", texto or "")


def _e_lixo_de_fonte(texto):
    """True se o bloco é cabeçalho ilegível: mais glifo cru do que letra legível."""
    controle = len(_RX_CONTROLE.findall(texto or ""))
    if not controle:
        return False
    legivel = sum(c.isalnum() for c in limpar_controle(texto))
    return controle > legivel


def _elem_text(e):
    """Junta o texto pesquisável de UM elemento do content_list.

    O MinerU classifica cada bloco por 'type' (text, table, image...). Aqui
    reunimos o que interessa para busca: o texto, o corpo da tabela (HTML) e as
    legendas de tabela/imagem. Bloco que é só glifo cru sai como '' e a página
    nem o vê (ver _e_lixo_de_fonte)."""
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
    texto = "\n".join(partes).strip()
    if _e_lixo_de_fonte(texto):
        return ""
    return limpar_controle(texto).strip()


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
            # O PyMuPDF lê a MESMA camada de texto do PDF, então o cabeçalho vem
            # com o mesmo glifo cru: limpar aqui também, ou o fallback reintroduz
            # o lixo justamente nas páginas em que o MinerU já falhou.
            p = limpar_controle(doc[page_idx].get_text("text")).strip()   # texto do PDF
            if len(p) > 500 and len(m) < FALLBACK_RATIO * len(p):
                texto = p                                       # MinerU perdeu -> usa o PDF
                fallback += 1
            else:
                texto = m or p                                  # MinerU ok (ou vazio -> PDF)
            if texto.strip():
                con.execute(
                    "INSERT INTO pages(pdf, caderno, date, page, content) VALUES (?,?,?,?,?)",
                    (name, config.CADERNO_PARTE_I, date, page_idx + 1, texto),  # +1 -> 1-based
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


def caderno_indexado(con, caderno, date):
    """True se já há páginas deste caderno+edição no índice (idempotência das partes leves)."""
    row = con.execute(
        "SELECT 1 FROM pages WHERE caderno=? AND date=? LIMIT 1", (caderno, date)
    ).fetchone()
    return row is not None


def index_caderno_bytes(con, caderno, date, pdf_bytes, chave="parte"):
    """Indexa o TEXTO de um caderno a partir dos BYTES do PDF (via PyMuPDF), SEM salvar arquivo.

    Usado para as Partes IB/II/IV/V (estratégia "leve"): lê o texto nativo do PDF
    em memória e grava no FTS5 com o rótulo do caderno. Idempotente por (caderno, date).
    Devolve o nº de páginas inseridas (0 se já estava indexado)."""
    if caderno_indexado(con, caderno, date):
        return 0
    # 'pdf' é um identificador sintético (não existe arquivo em disco); serve para
    # page_content() localizar o texto e para o site saber que NÃO há miniatura.
    pdf_id = f"DOERJ_{date}_{chave}.pdf"
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    n = 0
    try:
        for page_idx in range(doc.page_count):
            texto = limpar_controle(doc[page_idx].get_text("text")).strip()
            if texto:
                con.execute(
                    "INSERT INTO pages(pdf, caderno, date, page, content) VALUES (?,?,?,?,?)",
                    (pdf_id, caderno, date, page_idx + 1, texto),
                )
                n += 1
    finally:
        doc.close()
    con.commit()
    return n


def main():
    """Linha de comando do passo 2/5: texto do MinerU -> índice FTS5.

    --latest processa só a edição mais recente (o que o pipeline usa todo dia);
    --extract roda o MinerU antes de indexar; --force reindexa tudo do zero."""
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
