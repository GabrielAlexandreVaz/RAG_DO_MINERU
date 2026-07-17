"""
============================================================================
monitor_estruturado.py · Reproduz, ESTRUTURADO, o "Monitoramento DOERJ"
----------------------------------------------------------------------------
Faz o que o robo-doerj-monitor (SEFAZ) faz no e-mail diário, mas devolve os
achados de forma ESTRUTURADA num Excel com as 8 seções (uma aba cada).

Método (espelha o MAP deles): para cada caderno já indexado da edição, pede à
IA (Claude) a lista de atos relevantes para a SEFAZ, cada um com uma CATEGORIA
(1 das 9). Depois agrupa por categoria nas 8 seções e monta o .xlsx.

Uso:
    python src/monitor_estruturado.py                    # última edição
    python src/monitor_estruturado.py --date 2026-07-17
============================================================================
"""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

import config
from search import list_dates, page_content

# categoria (da IA) -> número da seção do relatório
CATEGORIA_SECAO = {
    "PRAZO_CRITICO": 1,
    "MOVIMENTACAO_PESSOAL": 2,
    "NOMES_MONITORADOS": 3,
    "OBSERVACAO_EXECUTIVA": 4,
    "DESTAQUE_CONTROLE_INTERNO": 4,
    "EXPEDIENTE_PONTO_FACULTATIVO": 5,
    "TCE_SEFAZ": 6,
    "LEGISLATIVO_FAZENDARIO": 7,
    "MUNICIPALIDADES_PEDIDO": 8,
}

SECOES = {
    1: "1 - Prazos Criticos",
    2: "2 - Movimentacoes de Pessoal",
    3: "3 - Nomes Monitorados",
    4: "4 - Observacoes Executivas",
    5: "5 - Expediente e Ponto Facultativo",
    6: "6 - Tribunal de Contas (IB)",
    7: "7 - Poder Legislativo (II)",
    8: "8 - Municipalidades e Pedido (IV-V)",
}

COLUNAS = ["Tipo do Ato", "Numero/Ano", "Orgao", "Pessoa", "Cargo",
           "Processo", "Vigencia", "Resumo", "Caderno", "Pagina"]
CHAVES = ["tipo_ato", "numero_ano", "orgao", "pessoa", "cargo",
          "processo", "vigencia", "resumo", "caderno", "pagina"]

SYSTEM = (
    "Voce e analista senior da SEFAZ-RJ com 15 anos lendo o DOERJ. Do TEXTO fornecido, "
    "extraia APENAS atos/publicacoes relevantes para a SEFAZ-RJ, Rioprevidencia ou Fundo "
    "Unico de Previdencia. JUCERJA e LOTERJ NAO sao SEFAZ (so inclua se o ato for conjunto "
    "com a SEFAZ).\n\n"
    "Responda EXCLUSIVAMENTE com um objeto JSON valido (sem texto antes/depois, sem ```), no "
    'formato: {"itens":[{"categoria":"","tipo_ato":"","numero_ano":"","orgao":"","pessoa":"",'
    '"cargo":"","processo":"","vigencia":"","resumo":"","pagina":""}]}\n\n'
    "categoria = UMA de: PRAZO_CRITICO | MOVIMENTACAO_PESSOAL | NOMES_MONITORADOS | "
    "OBSERVACAO_EXECUTIVA | DESTAQUE_CONTROLE_INTERNO | EXPEDIENTE_PONTO_FACULTATIVO | "
    "TCE_SEFAZ | LEGISLATIVO_FAZENDARIO | MUNICIPALIDADES_PEDIDO.\n"
    "Guia de categoria:\n"
    "- PRAZO_CRITICO: ato que gera acao/prazo para a SEFAZ (sessao, vencimento, verificacao, "
    "licenca com inicio/fim). So se o titular do prazo for a SEFAZ (nao terceiro).\n"
    "- MOVIMENTACAO_PESSOAL: nomeacao/exoneracao/designacao/remocao/cessao/afastamento de "
    "servidor fazendario (ou cargo de comando de outro poder). Membros de comissao NAO contam.\n"
    "- NOMES_MONITORADOS: ato concreto sobre Guilherme Merces (Secretario de Fazenda).\n"
    "- OBSERVACAO_EXECUTIVA: decretos com impacto orcamentario, resolucoes, portarias de "
    "superintendencia, atas de colegiado, Conselho de Contribuintes, termos aditivos, "
    "cancelamento de IE, instituicao de comissao.\n"
    "- DESTAQUE_CONTROLE_INTERNO: Controle Interno, Corregedoria Tributaria (CTCE), Auditoria "
    "Interna/AGE com vinculo SEFAZ, Tomada de Contas Especial da SEFAZ.\n"
    "- EXPEDIENTE_PONTO_FACULTATIVO: ponto facultativo/expediente/feriado/recesso estadual.\n"
    "- TCE_SEFAZ: decisao do TCE-RJ citando SEFAZ/Rioprevidencia/Fundo Unico.\n"
    "- LEGISLATIVO_FAZENDARIO: PL/indicacao/requerimento sobre tributo estadual, beneficio "
    "fiscal, receita, orcamento da SEFAZ ou estrutura fazendaria.\n"
    "- MUNICIPALIDADES_PEDIDO: ato onde a SEFAZ/Rioprevidencia e publicadora/contratante/conveniada.\n"
    "Preencha 'pagina' com o numero do rotulo [pagina N]. Deixe campos vazios como \"\". "
    "Nao invente. Se nada relevante, itens=[]."
)


def _paginas_do_caderno(caderno, date):
    """Lista (pdf, caderno, date, page) de todas as páginas de um caderno na edição."""
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT pdf, caderno, date, page FROM pages WHERE caderno=? AND date=? ORDER BY page",
            (caderno, date),
        ).fetchall()
    finally:
        con.close()
    return [{"pdf": r[0], "caderno": r[1], "date": r[2], "page": int(r[3])} for r in rows]


def _cadernos_da_edicao(date):
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT DISTINCT caderno FROM pages WHERE date=? ORDER BY caderno", (date,)
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _extrair_json(resp):
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        text = text[i:j + 1]
    return json.loads(text)


def _mapear_bloco(caderno, paginas, client, max_tokens=8000):
    """Uma chamada à IA sobre um bloco de páginas de um caderno. Devolve (itens, usage)."""
    blocos = []
    for p in paginas:
        txt = page_content(p["pdf"], p["page"]).strip()
        if txt:
            blocos.append(f"[pagina {p['page']}]\n{txt}")
    if not blocos:
        return [], {"input": 0, "output": 0}
    corpo = "\n\n----------\n\n".join(blocos)
    prompt = (f"Caderno: {caderno}\nEdicao do DOERJ.\n\nTrechos:\n\n{corpo}\n\n"
              "Lembre-se: responda apenas com o objeto JSON.")
    with client.messages.stream(
        model=config.MODEL, max_tokens=max_tokens, system=SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        resp = stream.get_final_message()
    try:
        data = _extrair_json(resp)
        itens = data.get("itens", []) or []
    except Exception:
        itens = []
    for it in itens:
        it["caderno"] = caderno
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return itens, usage


def gerar(date=None, destino=None, chunk=4):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not config.DB_PATH.exists():
        sys.exit("[ERRO] indice nao encontrado. Rode: python src/index_build.py --latest")
    if not date:
        datas = list_dates()
        if not datas:
            sys.exit("[ERRO] nenhuma edicao no indice.")
        date = datas[0]
    if not config.current_api_key():
        sys.exit("[ERRO] ANTHROPIC_API_KEY nao definido no .env.")

    client = config.get_client()
    todos = []
    uin = uout = 0
    for caderno in _cadernos_da_edicao(date):
        paginas = _paginas_do_caderno(caderno, date)
        print(f"[monitor] {caderno}: {len(paginas)} paginas ...")
        for i in range(0, len(paginas), chunk):
            itens, usage = _mapear_bloco(caderno, paginas[i:i + chunk], client)
            todos.extend(itens)
            uin += usage["input"]; uout += usage["output"]
    print(f"[monitor] {len(todos)} itens relevantes extraidos "
          f"(tokens: entrada {uin} / saida {uout})")

    destino = Path(destino) if destino else (config.ROOT / "relatorios")
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = destino / f"monitoramento_{date}.xlsx"
    _build_xlsx(arquivo, date, todos)
    print(f"[ok] Excel gerado -> {arquivo}")
    return arquivo


def _build_xlsx(arquivo, date, itens):
    import xlsxwriter
    wb = xlsxwriter.Workbook(str(arquivo))
    f_hdr = wb.add_format({"bold": True, "bg_color": "#0052cc", "font_color": "white",
                           "border": 1, "valign": "top"})
    f_wrap = wb.add_format({"text_wrap": True, "valign": "top", "border": 1})
    f_title = wb.add_format({"bold": True, "font_size": 13})

    # agrupa por seção
    por_secao = {n: [] for n in SECOES}
    for it in itens:
        sec = CATEGORIA_SECAO.get((it.get("categoria") or "").strip().upper())
        if sec:
            por_secao[sec].append(it)

    # aba Resumo
    ws = wb.add_worksheet("Resumo")
    ws.write(0, 0, f"Monitoramento DOERJ (estruturado) - {date}", f_title)
    ws.write(2, 0, "Secao", f_hdr); ws.write(2, 1, "Itens", f_hdr)
    for r, n in enumerate(SECOES, start=3):
        ws.write(r, 0, SECOES[n]); ws.write(r, 1, len(por_secao[n]))
    ws.set_column(0, 0, 40); ws.set_column(1, 1, 10)

    # uma aba por seção
    larg = [16, 16, 26, 26, 22, 20, 16, 50, 22, 8]
    for n in SECOES:
        ws = wb.add_worksheet(SECOES[n][:31])   # nome de aba <=31 chars
        for c, titulo in enumerate(COLUNAS):
            ws.write(0, c, titulo, f_hdr)
        for li, it in enumerate(por_secao[n], start=1):
            for c, chave in enumerate(CHAVES):
                ws.write(li, c, str(it.get(chave, "") or ""), f_wrap)
        for c, w in enumerate(larg):
            ws.set_column(c, c, w)
        ws.freeze_panes(1, 0)
        if not por_secao[n]:
            ws.write(1, 0, "(nenhum item nesta secao nesta edicao)", f_wrap)
    wb.close()


def main():
    ap = argparse.ArgumentParser(description="Gera o Monitoramento DOERJ estruturado em Excel.")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--dir", default=None, help="Pasta de destino (padrao: <projeto>/relatorios)")
    ap.add_argument("--chunk", type=int, default=4, help="Paginas por chamada de IA (padrao: 4)")
    args = ap.parse_args()
    gerar(date=args.date, destino=args.dir, chunk=args.chunk)


if __name__ == "__main__":
    main()
