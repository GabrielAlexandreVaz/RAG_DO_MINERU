"""
============================================================================
atos_pessoal.py · Job — extrai ATOS DE PESSOAL da edição e grava no Oracle
----------------------------------------------------------------------------
Versão genérica do exonerar.py: em vez de gerar um Excel, grava cada ato na
tabela Oracle DOERJ_ATOS_PESSOAL. É PARAMETRIZÁVEL POR TEMA (exoneração,
nomeação, ...): cada tema tem seu padrão de busca (FTS), a pergunta que orienta
a IA e o rótulo que vai na coluna RESPOSTA.

Reaproveita todo o pipeline existente:
  search.pages_matching    -> todas as páginas do tema na edição
  reader.answer_chunked    -> lista ESTRUTURADA (resumo + itens)
  oracle_db.save_atos      -> grava (idempotente) na tabela

Uso:
    python src/atos_pessoal.py                          # exonerações da última edição
    python src/atos_pessoal.py --tema nomeacao          # nomeações
    python src/atos_pessoal.py --tema all               # todos os temas
    python src/atos_pessoal.py --tema exoneracao --date 2026-07-14
============================================================================
"""
import argparse
import sys

import config
import oracle_db
import reader
from search import list_dates, pages_matching

# Cada tema: 'fts' (padrão de busca), 'label' (vai para a coluna RESPOSTA) e
# 'pergunta' (orienta a extração da IA). Para adicionar um tema, basta uma entrada.
TEMAS = {
    "exoneracao": {
        "fts": "exoner*",
        "label": "Exoneração",
        "pergunta": (
            "Liste TODAS as exoneracoes publicadas nesta edicao, uma por item, com nome da "
            "pessoa, cargo/simbolo, orgao/secretaria, data do ato, numero (se houver), "
            "processo (SEI) e pagina. Nao omita nenhuma exoneracao."
        ),
    },
    "nomeacao": {
        "fts": "nomea*",
        "label": "Nomeação",
        "pergunta": (
            "Liste TODAS as nomeacoes publicadas nesta edicao, uma por item, com nome da "
            "pessoa, cargo/simbolo, orgao/secretaria, data do ato, numero (se houver), "
            "processo (SEI) e pagina. Nao omita nenhuma nomeacao."
        ),
    },
}


def _resolver_edicao(date):
    """Devolve a edição a processar: a informada, ou a mais recente indexada."""
    if not config.DB_PATH.exists():
        sys.exit("[ERRO] indice nao encontrado. Rode antes: python src/index_build.py --latest")
    if date:
        return date
    datas = list_dates()
    if not datas:
        sys.exit("[ERRO] nenhuma edicao no indice.")
    return datas[0]


def gerar(tema, date=None, max_tokens=16000):
    """Extrai os atos de UM tema numa edição e grava no Oracle. Devolve o nº inserido (ou None)."""
    if tema not in TEMAS:
        sys.exit(f"[ERRO] tema desconhecido: {tema}. Temas: {', '.join(TEMAS)} (ou 'all').")
    info = TEMAS[tema]

    date = _resolver_edicao(date)
    print(f"[atos] tema={tema} ({info['label']}) | edicao: {date}")

    # 1) Todas as páginas da edição que mencionam o tema.
    paginas = pages_matching(info["fts"], date)
    print(f"[atos] {len(paginas)} paginas com '{info['fts']}' na edicao")
    if not paginas:
        print("[atos] nenhuma pagina do tema -> nada a gravar.")
        return None

    # 2) A IA devolve a lista ESTRUTURADA (resumo + itens).
    if not config.current_api_key():
        sys.exit("[ERRO] ANTHROPIC_API_KEY nao definido no .env (job precisa da chave).")
    print(f"[atos] lendo {len(paginas)} paginas com a IA ({config.MODEL})...")
    data, usage = reader.answer_chunked(info["pergunta"], paginas, chunk_size=2, max_tokens=max_tokens)
    itens = data.get("itens", [])
    print(f"[atos] {len(itens)} atos extraidos "
          f"(tokens: entrada {usage['input']} / saida {usage['output']})")
    if not itens:
        print("[atos] a IA nao encontrou atos -> nada a gravar.")
        return None

    # 3) Grava no Oracle (idempotente: limpa a edicao+tema antes).
    n = oracle_db.save_atos(info["label"], date, itens)
    print(f"[ok] {n} atos de '{info['label']}' -> Oracle ({config.oracle_settings()['table']}) "
          f"[edicao {date}]")
    return n


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # acentos no log do Windows
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Extrai atos de pessoal do DOERJ e grava no Oracle.")
    ap.add_argument("--tema", default="exoneracao",
                    help=f"Tema: {', '.join(TEMAS)} ou 'all' (padrao: exoneracao)")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--max-tokens", type=int, default=16000)
    args = ap.parse_args()

    temas = list(TEMAS) if args.tema == "all" else [args.tema]
    total = 0
    for t in temas:
        n = gerar(t, date=args.date, max_tokens=args.max_tokens)
        total += n or 0
    print(f"[done] {total} atos gravados no total ({len(temas)} tema(s)).")


if __name__ == "__main__":
    main()
