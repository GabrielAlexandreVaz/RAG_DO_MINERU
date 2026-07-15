"""
============================================================================
exonerar.py · Job — extrai as EXONERAÇÕES da edição do dia e salva um Excel
----------------------------------------------------------------------------
Faz automaticamente o que o botão "Exonerações" faz no site, mas sem abrir o
navegador: pega a edição mais recente (já indexada), acha todas as páginas com
exonerações, pede à IA a lista ESTRUTURADA e grava um Excel
(exoneracoes_AAAA-MM-DD.xlsx) na pasta do OneDrive.

Reaproveita tudo o que já existe:
  search.pages_matching  -> todas as páginas com "exoner*" da edição
  reader.answer_from_pages -> resposta estruturada (resumo + itens)
  export.build_xlsx       -> os bytes do .xlsx (mesmas colunas do site)

Regras combinadas:
  - Um arquivo por dia (com a data no nome), acumulando histórico.
  - Se a edição não tiver exonerações, NÃO gera arquivo.

Uso:
    python src/exonerar.py                    # última edição indexada
    python src/exonerar.py --date 2026-07-08  # uma edição específica
    python src/exonerar.py --dir "D:/outra/pasta"
============================================================================
"""
import argparse
import sys
from pathlib import Path

import config
import export
import reader
from search import list_dates, pages_matching

# A pergunta que orienta a extração (pede a lista COMPLETA de exonerações).
PERGUNTA = (
    "Liste TODAS as exoneracoes publicadas nesta edicao, uma por item, com nome da "
    "pessoa, cargo/simbolo, orgao/secretaria, data do ato, numero (se houver), "
    "processo (SEI) e pagina. Nao omita nenhuma exoneracao."
)


def gerar(date=None, destino=None, max_tokens=32000):
    """Gera o Excel das exonerações de uma edição. Devolve o caminho salvo (ou None)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # acentos no log do Windows
    except Exception:
        pass

    # 1) Qual edição? A informada, ou a mais recente já indexada.
    if not config.DB_PATH.exists():
        sys.exit("[ERRO] indice nao encontrado. Rode antes: python src/index_build.py --latest")
    if not date:
        datas = list_dates()
        if not datas:
            sys.exit("[ERRO] nenhuma edicao no indice.")
        date = datas[0]
    print(f"[exonerar] edicao: {date}")

    # 2) Todas as páginas da edição que mencionam exoneração.
    paginas = pages_matching("exoner*", date)
    print(f"[exonerar] {len(paginas)} paginas com 'exoner*' na edicao")
    if not paginas:
        print("[exonerar] nenhuma pagina com exoneracoes -> nao gera arquivo.")
        return None

    # 3) A IA devolve a lista ESTRUTURADA (resumo + itens).
    if not config.current_api_key():
        sys.exit("[ERRO] ANTHROPIC_API_KEY nao definido no .env (job precisa da chave).")
    print(f"[exonerar] lendo {len(paginas)} paginas com a IA ({config.MODEL})...")
    data, usage = reader.answer_chunked(PERGUNTA, paginas, chunk_size=2, max_tokens=16000)
    itens = data.get("itens", [])
    print(f"[exonerar] {len(itens)} exoneracoes extraidas "
          f"(tokens: entrada {usage['input']} / saida {usage['output']})")
    if not itens:
        print("[exonerar] a IA nao encontrou exoneracoes -> nao gera arquivo.")
        return None

    # 4) Monta o Excel (mesmo formato estruturado do botão de download do site).
    xlsx = export.build_xlsx({
        "question": PERGUNTA,
        "edicao": date,
        "resumo": data.get("resumo", ""),
        "itens": itens,
    })

    # 5) Salva na pasta de destino (um arquivo por dia).
    destino = Path(destino) if destino else config.EXONERACOES_DIR
    destino.mkdir(parents=True, exist_ok=True)       # cria a pasta se não existir
    arquivo = destino / f"exoneracoes_{date}.xlsx"
    arquivo.write_bytes(xlsx)
    print(f"[ok] {len(itens)} exoneracoes -> {arquivo}")
    return arquivo


def main():
    ap = argparse.ArgumentParser(description="Extrai as exoneracoes do dia e salva um Excel.")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--dir", default=None, help="Pasta de destino (padrao: config.EXONERACOES_DIR)")
    ap.add_argument("--max-tokens", type=int, default=32000)
    args = ap.parse_args()
    gerar(date=args.date, destino=args.dir, max_tokens=args.max_tokens)


if __name__ == "__main__":
    main()
