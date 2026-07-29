"""
============================================================================
ask.py · Perguntar ao DOERJ pelo TERMINAL (sem abrir o navegador)
----------------------------------------------------------------------------
Versão de linha de comando do mesmo fluxo do site: busca as páginas (FTS5) e,
se você quiser, manda o Claude ler o texto e responder. Útil para testar rápido
ou automatizar.

Por padrão consulta SÓ a edição do dia (a mais recente). Para outra data, use
--date ou cite a data na pergunta. --all busca em todas as edições.

Pré-requisito: rodar antes `python src/index_build.py`.

Exemplos:
  python src/ask.py "Quais atos da Sefaz sairam hoje?"
  python src/ask.py "editais de licitacao" --date 2026-07-08
  python src/ask.py "decretos do governador" --all
  python src/ask.py "portaria 7093" --search-only   # só a busca (não gasta a IA)
============================================================================
"""
import argparse
import sys

import config
import reader
from search import list_dates, parse_date_from_query, search


def main():
    """Linha de comando: pergunta -> busca -> resposta da IA no terminal.

    Mesma cadeia do site (search -> reader), sem servidor: útil para testar uma
    pergunta rápido ou usar dentro de um script."""
    # --- Define os argumentos aceitos na linha de comando --------------------
    ap = argparse.ArgumentParser(description="Pergunta ao DOERJ (busca por texto + leitura pela IA).")
    ap.add_argument("question", nargs="+", help="A pergunta")          # 1+ palavras
    ap.add_argument("--k", type=int, default=4, help="Numero de paginas candidatas (padrao 4)")
    ap.add_argument("--date", default=None, help="Edicao YYYY-MM-DD (padrao: a mais recente)")
    ap.add_argument("--all", action="store_true", help="Buscar em todas as edicoes")
    ap.add_argument("--model", default=config.MODEL)
    ap.add_argument("--max-tokens", type=int, default=8000)
    ap.add_argument("--search-only", action="store_true", help="So mostra o retrieval; nao chama a Claude")
    args = ap.parse_args()

    # No Windows, garante que a saída do terminal aceite acentos (UTF-8).
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    question = " ".join(args.question)              # junta as palavras numa frase só

    # Sem índice construído não há o que buscar.
    if not config.DB_PATH.exists():
        print(f"[erro] indice nao encontrado em {config.DB_PATH}. Rode: python src/index_build.py",
              file=sys.stderr)
        sys.exit(1)

    # --- Decide o ESCOPO de data (qual edição consultar) --------------------
    dates = list_dates()
    latest = dates[0] if dates else None
    if args.all:
        scope = None                                # todas as edições
    elif args.date:
        scope = args.date                           # data escolhida na mão
    else:
        # padrão: a data citada na pergunta (se existir no índice), senão a mais recente
        scope = parse_date_from_query(question, dates) or latest

    print(f"[escopo] {'todas as edicoes' if scope is None else 'edicao ' + str(scope)}")

    # --- Busca as páginas candidatas ----------------------------------------
    results = search(question, k=args.k, date=scope)
    if not results:
        alvo = "" if scope is None else f" na edicao {scope}"
        print(f"[info] nenhuma pagina encontrada{alvo}. (tente --all)")
        return

    print(f"[busca] {len(results)} paginas candidatas:")
    for r in results:
        print(f"   - {r['date']} p.{r['page']}  (score={r['score']:.2f})  {r['snippet'].strip()[:120]}")

    # --search-only: para por aqui (não chama a IA, não gasta tokens).
    if args.search_only:
        return

    # --- Leitura pela IA ----------------------------------------------------
    if not config.current_api_key():                # relê a chave do .env
        print("[erro] ANTHROPIC_API_KEY nao definido no .env.", file=sys.stderr)
        sys.exit(2)

    print(f"\n[..] Lendo {len(results)} paginas com {args.model}...")
    resposta, usage = reader.answer_from_pages(
        question, results, model=args.model, max_tokens=args.max_tokens
    )

    # resposta estruturada: um resumo + uma lista de itens (atos).
    print("\n===== RESUMO =====\n")
    print(resposta.get("resumo", ""))

    itens = resposta.get("itens", [])
    print(f"\n===== ITENS ({len(itens)}) =====")
    for i, it in enumerate(itens, 1):
        print(f"\n[{i}] {it.get('tipo','')} {it.get('numero','')}  "
              f"({it.get('data','')})  ed.{it.get('edicao','')} p.{it.get('pagina','')}")
        if it.get("orgao"):
            print(f"    orgao: {it['orgao']}")
        if it.get("pessoa"):
            print(f"    pessoa: {it['pessoa']}  cargo: {it.get('cargo','')}")
        if it.get("objeto"):
            print(f"    objeto: {it['objeto']}")
        if it.get("processo"):
            print(f"    processo: {it['processo']}")

    print(f"\n[uso] entrada={usage['input']} tok | saida={usage['output']} tok")


if __name__ == "__main__":
    main()
