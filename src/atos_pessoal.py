"""
============================================================================
atos_pessoal.py · Job — extrai ATOS DE PESSOAL da edição e grava no Oracle
----------------------------------------------------------------------------
Grava cada ato numa linha da tabela Oracle 001A. É PARAMETRIZÁVEL POR TEMA
(exoneração, nomeação, ...): cada tema tem seu padrão de busca (FTS), a pergunta
que orienta a IA e o rótulo que vai na coluna RESPOSTA — os três derivados da
palavra-chave.

AS PALAVRAS-CHAVE (o que procurar no D.O.) vêm da tabela Oracle 001B — não estão
mais no código. Para passar a acompanhar um novo tipo de ato, basta um INSERT lá
('designar', 'aposentar'...); para parar, um UPDATE preenchendo DATA_FIM. Se o
banco não responder, o job usa a lista de reserva (PALAVRAS_PADRAO).

Reaproveita todo o pipeline existente:
  oracle_db.listar_palavras_chave -> as palavras vigentes na edição (001B)
  search.pages_matching    -> todas as páginas do tema na edição
  reader.answer_chunked    -> lista ESTRUTURADA (resumo + itens)
  oracle_db.save_atos      -> grava (idempotente) na tabela 001A

Uso:
    python src/atos_pessoal.py                          # TODAS as palavras da 001B
    python src/atos_pessoal.py --tema nomear            # só uma palavra-chave
    python src/atos_pessoal.py --tema exonerar --date 2026-07-14
============================================================================
"""
import argparse
import sys
import unicodedata

import config
import oracle_db
import reader
from search import list_dates, pages_matching

# Reserva usada só se a 001B não responder (mesmas palavras que estão lá hoje).
PALAVRAS_PADRAO = ["nomear", "exonerar"]

# Substantivos que a regra "-ar -> -acao" não acerta. Só precisa de entrada aqui
# quem foge do padrão (verbos em -er/-ir, formas irregulares).
ROTULOS_ESPECIAIS = {
    "aposentar": "Aposentadoria",
    "remover": "Remoção",
    "conceder": "Concessão",
    "transferir": "Transferência",
    "admitir": "Admissão",
    "suspender": "Suspensão",
    "rescindir": "Rescisão",
}


def _sem_acento(s):
    """minúsculo e sem acento (o índice FTS é criado com remove_diacritics)."""
    s = unicodedata.normalize("NFKD", (s or "").strip().lower())
    return "".join(c for c in s if not unicodedata.combining(c))


def _fts_de(palavra):
    """Padrão de busca FTS5 a partir da palavra-chave da 001B.

    Buscar a palavra LITERAL perderia atos: 'exonerar*' acha 2 páginas, mas
    'exonera*' acha 6 (pega tambem 'exoneração', 'exonerado'). Por isso tiramos o
    'r' final do infinitivo e buscamos por prefixo — 'exonerar' -> 'exonera*',
    'nomear' -> 'nomea*' (exatamente as buscas que o job usava fixas no código).

    Escape: se a palavra já vier com curinga ou mais de um termo (ex.: 'aposent*'
    ou 'exonera* OR dispensa*'), ela é usada COMO ESTÁ — assim dá para escrever o
    padrão à mão na tabela quando a regra não servir."""
    p = (palavra or "").strip()
    if "*" in p or " " in p:
        return p
    base = _sem_acento(p)
    if len(base) > 4 and base.endswith("r"):
        base = base[:-1]                 # exonerar -> exonera ; nomear -> nomea
    return base + "*"


def _rotulo_de(palavra):
    """Rótulo que vai para a coluna RESPOSTA da 001A ('Exoneração', 'Nomeação').

    Os verbos em -ar viram -ação (exonerar -> Exoneração, designar -> Designação),
    que é o rótulo já usado nas linhas gravadas até hoje. Fora desse padrão, usa
    ROTULOS_ESPECIAIS e, em último caso, a própria palavra capitalizada."""
    # Se veio um padrão à mão ('aposent*', 'exonera* OR dispensa*'), o rótulo sai
    # do 1º termo, sem o curinga — RESPOSTA é para leitura humana.
    p = (palavra or "").strip().split()[0].replace("*", "") if (palavra or "").strip() else ""
    if p.lower() in ROTULOS_ESPECIAIS:
        return ROTULOS_ESPECIAIS[p.lower()]
    if _sem_acento(p).endswith("ar") and len(p) > 3:
        return (p[:-1] + "ção").capitalize()      # exonerar -> Exoneração
    return p.capitalize()


def _pergunta_de(rotulo):
    """Pergunta que orienta a extração da IA, no mesmo formato usado até aqui."""
    alvo = _sem_acento(rotulo)
    # plural: -ao -> -oes (exoneracao -> exoneracoes); o resto ganha um 's'.
    alvo = alvo[:-2] + "oes" if alvo.endswith("ao") else alvo + "s"
    return (
        f"Liste TODOS os atos de {alvo} publicados nesta edicao, uma por item, com nome da "
        "pessoa, cargo/simbolo, orgao/secretaria, data do ato, numero (se houver), "
        f"processo (SEI) e pagina. Nao omita nenhum ato de {alvo}."
    )


def carregar_temas(date=None):
    """Temas a processar, montados a partir das palavras-chave vigentes na 001B.

    Devolve ({chave: {"fts","label","pergunta","palavra"}}, origem). Cai para
    PALAVRAS_PADRAO se o banco não responder — melhor rodar com a lista de
    ontem do que não gravar ato nenhum no dia."""
    origem = "Oracle (001B)"
    palavras = []
    if oracle_db.configurado() and config.oracle_settings().get("table_palavras"):
        try:
            palavras = oracle_db.listar_palavras_chave(date)
            if not palavras:
                print("[ATENCAO] a tabela 001B nao tem palavra-chave vigente nesta edicao -> "
                      "usando a lista de reserva.")
        except Exception as e:  # noqa: BLE001 - banco fora do ar nao pode derrubar o job
            print(f"[ATENCAO] falha ao ler as palavras-chave no Oracle (001B): "
                  f"{type(e).__name__}: {str(e)[:200]} -> usando a lista de reserva.")
    else:
        print("[atos] Oracle/001B nao configurado -> usando a lista de reserva.")
    if not palavras:
        palavras, origem = PALAVRAS_PADRAO, "lista de reserva (codigo)"

    temas = {}
    for p in palavras:
        rotulo = _rotulo_de(p)
        temas[_sem_acento(p)] = {"palavra": p, "fts": _fts_de(p), "label": rotulo,
                                 "pergunta": _pergunta_de(rotulo)}
    return temas, origem


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


def gerar(info, date=None, max_tokens=16000, force=False):
    """Extrai os atos de UMA palavra-chave numa edição e grava no Oracle (001A).

    `info` é uma entrada de carregar_temas(): {"palavra","fts","label","pergunta"}.
    Devolve o nº de linhas inseridas (ou None)."""
    date = _resolver_edicao(date)
    print(f"[atos] palavra-chave='{info['palavra']}' ({info['label']}) | edicao: {date}")

    # 0) TRAVA DE CUSTO: o job roda de hora em hora (o D.O. pode atrasar). Se esta
    #    edicao+tema ja esta no banco, sai ANTES de chamar a IA (consulta e barata,
    #    reler as paginas com a IA nao). --force reprocessa de proposito.
    if not force and oracle_db.ja_gravado(info["label"], date):
        print(f"[atos] edicao {date} / '{info['label']}' ja gravada no Oracle -> pulando "
              "(use --force para reprocessar).")
        return None

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

    # 3) Grava no Oracle (idempotente: limpa a edicao+tema antes). Uma falha de
    #    banco (ex.: sem permissao de escrita) avisa e NAO derruba o pipeline.
    try:
        n = oracle_db.save_atos(info["label"], date, itens)
        print(f"[ok] {n} atos de '{info['label']}' -> Oracle ({config.oracle_settings()['table']}) "
              f"[edicao {date}]")
        return n
    except Exception as e:  # noqa: BLE001
        print(f"[ATENCAO] falha ao gravar '{info['label']}' no Oracle: "
              f"{type(e).__name__}: {str(e)[:200]}")
        return None


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")     # acentos no log do Windows
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Extrai atos de pessoal do DOERJ e grava no Oracle.")
    ap.add_argument("--tema", default="all",
                    help="Uma palavra-chave da 001B (ex.: exonerar) ou 'all' (padrao: all)")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--max-tokens", type=int, default=16000)
    ap.add_argument("--force", action="store_true",
                    help="Reprocessa mesmo se a edicao/tema ja estiver no Oracle (gasta IA)")
    args = ap.parse_args()

    # Oracle nao configurado ainda? Avisa e sai SEM erro — assim o pipeline
    # agendado nao quebra por causa de um destino que ainda nao foi preenchido.
    if not oracle_db.configurado():
        print("[atos] AVISO: Oracle nao configurado no .env (ORACLE_USER/PASSWORD/DSN). "
              "Nada a gravar; pulando esta etapa.")
        return

    # As palavras-chave vigentes na EDICAO (por isso resolvemos a data antes).
    date = _resolver_edicao(args.date)
    temas, origem = carregar_temas(date)
    print(f"[atos] {len(temas)} palavra(s)-chave vigente(s) em {date} (fonte: {origem}): "
          + ", ".join(f"{t['palavra']} -> {t['fts']} / {t['label']}" for t in temas.values()))

    if args.tema == "all":
        alvo = list(temas)
    else:
        chave = _sem_acento(args.tema)
        if chave not in temas:
            sys.exit(f"[ERRO] palavra-chave desconhecida: {args.tema}. "
                     f"Vigentes na 001B: {', '.join(temas)} (ou 'all').")
        alvo = [chave]

    total = 0
    for t in alvo:
        n = gerar(temas[t], date=date, max_tokens=args.max_tokens, force=args.force)
        total += n or 0
    print(f"[done] {total} atos gravados no total ({len(alvo)} tema(s)).")


if __name__ == "__main__":
    main()
