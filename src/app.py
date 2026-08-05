"""Front-end web do RAG do DOERJ com MinerU (Flask, local).

Sobe um servidor em http://127.0.0.1:5001 onde voce faz as buscas pelo navegador.
A resposta da IA vem do TEXTO limpo extraido pelo MinerU; as miniaturas das
paginas (render do PDF) servem so para conferencia visual.

Uso:
  python src/app.py
  (depois abra http://127.0.0.1:5001 no navegador)
"""
import socket
import sys
from pathlib import Path

import markdown as md
from flask import Flask, Response, jsonify, request, send_from_directory

import re

import config
import export
import highlight
import reader
from render import render_page
from search import list_dates, pages_matching, parse_date_from_query, search, to_fts_query

app = Flask(__name__)
INDEX_HTML = config.ROOT / "web" / "index.html"


@app.after_request
def _no_cache(resp):
    """Impede o navegador de cachear a página e as respostas da API.

    As imagens de página (/api/page) ficam de fora e mantêm o cache de 1 dia:
    são caras de renderizar e não mudam. Sem isto, o front mostraria a resposta
    de uma pergunta anterior ao voltar na navegação."""
    if request.path == "/" or request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def _safe_pdf(name):
    """Resolve o PDF dentro de DOWNLOADS_DIR (usar so o nome ja evita path traversal).
    NAO usamos resolve() porque arquivos online-only do OneDrive sao reparse points."""
    p = config.DOWNLOADS_DIR / Path(name).name
    return p if p.exists() else None


@app.get("/")
def index():
    """Serve o front-end (página única). Lido do disco a cada acesso, para
    editar o HTML sem reiniciar o servidor."""
    return Response(INDEX_HTML.read_text(encoding="utf-8"), mimetype="text/html")


@app.get("/assets/<path:fn>")
def assets(fn):
    """Serve as imagens de web/assets (o brasão). Path(fn).name descarta
    qualquer diretório no pedido, evitando sair da pasta."""
    return send_from_directory(config.ROOT / "web" / "assets", Path(fn).name)


@app.get("/api/page")
def page_image():
    """Renderiza UMA página do PDF em PNG, com marca-texto opcional.

    É a conferência visual: o usuário vê a página original com o termo buscado
    destacado em amarelo, exatamente sobre o texto. Parâmetros: `pdf` (só o nome
    do arquivo), `page`, `dpi` e `hl` (termos a destacar).

    Só funciona para a Parte I, a única com PDF salvo em disco — os cadernos
    leves são indexados em memória. Daí o 404 quando não há arquivo."""
    pdf = request.args.get("pdf", "")
    try:
        page = int(request.args.get("page", "1"))
        dpi = int(request.args.get("dpi", str(config.RENDER_DPI)))
    except ValueError:
        return Response("parametros invalidos", status=400)

    pdf_path = _safe_pdf(pdf)
    if not pdf_path:
        return Response("pdf nao encontrado", status=404)

    # hl = termos a destacar (marca-texto) na pagina; vazio = sem destaque.
    hl = request.args.get("hl", "")
    termos = highlight.terms(hl) if hl else None

    png_bytes, _, _ = render_page(pdf_path, page, dpi, terms=termos)
    return Response(png_bytes, mimetype="image/png", headers={"Cache-Control": "max-age=86400"})


@app.get("/api/dates")
def api_dates():
    """Edições disponíveis no índice, para o seletor de data do front."""
    dates = list_dates()
    return jsonify({"dates": dates, "latest": dates[0] if dates else None})


@app.post("/api/ask")
def api_ask():
    """A rota principal: pergunta em português -> busca -> resposta estruturada.

    Fluxo:
      1. ESCOPO — usa a data escolhida no front; "all" busca em tudo; senão tenta
         ler uma data citada na própria pergunta e cai na edição mais recente.
      2. BUSCA — normal traz as top-k páginas por relevância (BM25). Com
         `full=True` (os atalhos: Exonerações, Nomeações...) traz TODAS as páginas
         do tema, porque esses temas se espalham por muitas páginas e o top-k
         perderia itens.
      3. LEITURA — a IA devolve {resumo, itens}. Em modo `full` vai em blocos de
         2 páginas para o JSON não truncar.

    `search_only` para na etapa 2 (útil para ver o que a busca achou sem gastar
    IA). Erros da IA voltam no campo "error" do JSON, com HTTP 200, para o front
    exibir a mensagem sem quebrar."""
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    k = int(data.get("k", 4))
    search_only = bool(data.get("search_only", False))

    if not question:
        return jsonify({"error": "Digite uma pergunta."}), 400
    if not config.DB_PATH.exists():
        return jsonify({"error": "Indice nao encontrado. Rode: python src/index_build.py"}), 400

    dates = list_dates()
    latest = dates[0] if dates else None
    date_param = (data.get("date") or "latest")
    if date_param in dates:
        scope = date_param
    elif date_param == "all":
        scope = None
    else:
        scope = parse_date_from_query(question, dates) or latest

    fts_used = (data.get("fts") or to_fts_query(question) or "")
    # full=True (atalhos) -> traz TODAS as páginas do tema (não só as top-k),
    # para não perder itens espalhados por muitas páginas (ex.: exonerações).
    full = bool(data.get("full"))
    if full and scope:
        results = pages_matching(fts_used, scope)                 # todas as páginas da edição
    elif full:
        results = search(question, k=80, date=None, fts=(data.get("fts") or None))  # "todas edições": k alto
    else:
        results = search(question, k=k, date=scope, fts=(data.get("fts") or None))
    # Cadernos "leves" (IB/II/IV/V) não têm PDF salvo -> sem miniatura. O front
    # usa 'tem_imagem' para não chamar /api/page (que retornaria 404) nesses casos.
    for r in results:
        r["tem_imagem"] = _safe_pdf(r.get("pdf", "")) is not None
    out = {"question": question, "results": results,
           "resumo": "",                 # resumo em texto puro (para os downloads)
           "resumo_html": None,          # resumo (markdown -> HTML) para exibir
           "itens": [],                  # lista estruturada de atos (tabela + downloads)
           "usage": None, "error": None,
           "scope": {"date": scope, "all": scope is None},
           "terms": fts_used}            # termos p/ o marca-texto na imagem da pagina

    if not results:
        out["error"] = (
            f"Nada encontrado na edicao de {scope} para essa pergunta. Tente 'Todas as edicoes'."
            if scope else "Nenhuma pagina encontrada para essa pergunta."
        )
        return jsonify(out)

    if search_only:
        return jsonify(out)

    if not config.current_api_key():   # relê do .env (não usa cache antigo)
        out["error"] = "ANTHROPIC_API_KEY nao definido no .env (so a busca esta disponivel)."
        return jsonify(out)

    try:
        if full:
            # Atalhos leem muitas páginas -> processa em BLOCOS pequenos para não truncar.
            resposta, usage = reader.answer_chunked(question, results, chunk_size=2, max_tokens=16000)
        else:
            resposta, usage = reader.answer_from_pages(question, results, max_tokens=int(data.get("max_tokens", 8000)))
        # resposta = {"resumo": str, "itens": [ {campos...} ]}
        out["resumo"] = resposta.get("resumo", "")
        out["resumo_html"] = md.markdown(resposta.get("resumo", ""),
                                         extensions=["tables", "fenced_code", "nl2br", "sane_lists"])
        out["itens"] = resposta.get("itens", [])
        out["usage"] = usage
    except Exception as e:  # noqa: BLE001 - mostra o erro na UI
        out["error"] = f"Erro ao consultar a Claude: {e}"

    return jsonify(out)


@app.post("/api/export")
def api_export():
    """Gera o download da resposta estruturada em Excel, PDF ou JSON.

    Recebe {fmt, question, edicao, resumo, itens} e devolve o ARQUIVO (o front
    manda de volta os dados já exibidos, então não chamamos a IA de novo)."""
    data = request.get_json(force=True, silent=True) or {}
    fmt = (data.get("fmt") or "").lower()
    if fmt not in export.FORMATOS:
        return Response("formato invalido (use xlsx, pdf ou json)", status=400)

    func, ext, mime = export.FORMATOS[fmt]
    payload = {
        "question": data.get("question", ""),
        "edicao": data.get("edicao", ""),
        "resumo": data.get("resumo", ""),
        "itens": data.get("itens", []),
    }
    try:
        conteudo = func(payload)                       # bytes do arquivo
    except Exception as e:  # noqa: BLE001
        return Response(f"erro ao gerar {fmt}: {e}", status=500)

    # Nome do arquivo: doerj_<edicao>.<ext> (só caracteres seguros).
    base = re.sub(r"[^A-Za-z0-9_-]", "", str(payload["edicao"])) or "consulta"
    nome = f"doerj_{base}.{ext}"
    return Response(conteudo, mimetype=mime,
                    headers={"Content-Disposition": f'attachment; filename="{nome}"',
                             "Cache-Control": "no-store"})


def _port_in_use(host, port):
    """True se já há algo escutando nesta porta (outro servidor nosso, em geral)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex((host, port)) == 0


def main():
    """Sobe o site em 127.0.0.1:5001 (só a máquina local).

    Se a porta já estiver ocupada, avisa e sai em vez de estourar um erro feio —
    o caso comum é ter dado dois cliques no web.bat. Índice ausente ou chave da
    IA vazia geram só aviso: a busca continua funcionando sem a IA."""
    host = "127.0.0.1"
    port = 5001
    if _port_in_use(host, port):
        print(f"  [aviso] ja existe um servidor em http://{host}:{port} - abra no navegador. Saindo.")
        return
    print(f"  DOERJ (MinerU) web -> http://{host}:{port}   (Ctrl+C para parar)")
    if not config.DB_PATH.exists():
        print("  [aviso] indice ainda nao existe. Rode antes: python src\\index_build.py", file=sys.stderr)
    if not config.ANTHROPIC_API_KEY:
        print("  [aviso] ANTHROPIC_API_KEY vazio: a busca funciona, mas a resposta da IA nao.", file=sys.stderr)
    app.run(host=host, port=port, threaded=True)


if __name__ == "__main__":
    main()
