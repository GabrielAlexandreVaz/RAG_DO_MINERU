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
import re
import sqlite3
import sys
import time
import unicodedata
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
    "extraia APENAS atos/publicacoes relevantes para a SEFAZ-RJ (Fazenda), Rioprevidencia ou "
    "Fundo Unico de Previdencia.\n\n"
    "SIGLAS QUE SAO SEFAZ (trate como Fazenda): SEFAZ, SEFAZ-RJ, Fazenda, Secretaria de Estado "
    "de Fazenda, SUPFINF, SUPAT, SUPBF, SUPCC, SUPCT, SUPDIEF, SUPAQTIC, Subsecretaria de "
    "Estado de Receita (SER), Subsecretaria de Controle Interno, Subsecretaria do Tesouro, "
    "Conselho de Contribuintes (CC), Junta de Revisao Fiscal (JRF), Auditoria Fiscal.\n\n"
    "NAO SAO SEFAZ (EXCLUA o ato, a menos que seja EXPLICITAMENTE conjunto/bilateral com a "
    "SEFAZ): JUCERJA, LOTERJ, SEDEC (Desenvolvimento Economico), Casa Civil, DETRAN, INEA, "
    "Saude, Educacao (SEEDUC), Seguranca/Policia (SEPOL/PM/CBMERJ), Planejamento (SEPLAG), "
    "Ciencia e Tecnologia, Cultura, Esporte, Ambiente, AGENERSA e demais secretarias/orgaos "
    "sem efeito direto na Fazenda. Municipio (prefeitura) so entra se a SEFAZ for parte.\n\n"
    "Responda EXCLUSIVAMENTE com um objeto JSON valido (sem texto antes/depois, sem ```), no "
    'formato: {"itens":[{"categoria":"","tipo_ato":"","numero_ano":"","orgao":"","pessoa":"",'
    '"cargo":"","processo":"","vigencia":"","resumo":"","pagina":""}]}\n\n'
    "categoria = UMA de: PRAZO_CRITICO | MOVIMENTACAO_PESSOAL | NOMES_MONITORADOS | "
    "OBSERVACAO_EXECUTIVA | DESTAQUE_CONTROLE_INTERNO | EXPEDIENTE_PONTO_FACULTATIVO | "
    "TCE_SEFAZ | LEGISLATIVO_FAZENDARIO | MUNICIPALIDADES_PEDIDO.\n"
    "Guia de categoria:\n"
    "- PRAZO_CRITICO: ato que gera acao/prazo para a SEFAZ (sessao de julgamento, vencimento, "
    "verificacao, disponibilizacao de acordao no portal, licenca com inicio/fim). So se o "
    "titular do prazo for a SEFAZ (nao terceiro).\n"
    "- MOVIMENTACAO_PESSOAL: nomeacao/exoneracao/designacao/remocao/cessao/afastamento de "
    "servidor fazendario (ou cargo de comando de outro poder). Membros de comissao NAO contam.\n"
    "- NOMES_MONITORADOS: ato concreto sobre Guilherme Merces (Secretario de Fazenda) ou sobre "
    "o Chefe de Gabinete / Subsecretario / Subsecretario Adjunto da SEFAZ.\n"
    "- OBSERVACAO_EXECUTIVA: decretos com impacto orcamentario (credito suplementar/dotacao), "
    "resolucoes, portarias de superintendencia, atas de colegiado, Conselho de Contribuintes, "
    "termos aditivos, cancelamento de IE, instituicao de comissao.\n"
    "- DESTAQUE_CONTROLE_INTERNO: Controle Interno, Corregedoria Tributaria (CTCE), Auditoria "
    "Interna/AGE com vinculo SEFAZ, Tomada de Contas Especial da SEFAZ.\n"
    "- EXPEDIENTE_PONTO_FACULTATIVO: ponto facultativo/expediente/feriado/recesso estadual.\n"
    "- TCE_SEFAZ: decisao do TCE-RJ citando SEFAZ/Rioprevidencia/Fundo Unico.\n"
    "- LEGISLATIVO_FAZENDARIO (caderno Parte II): projeto de lei, indicacao, requerimento ou "
    "emenda que trate de tributo estadual, beneficio/renuncia fiscal, receita, orcamento da "
    "SEFAZ ou estrutura fazendaria; OU que tenha recebido MANIFESTACAO/PARECER da SEFAZ; OU "
    "cujo IMPACTO FINANCEIRO/aumento de despesa tenha sido avaliado pela SEFAZ. Inclua ainda "
    "que a mencao a SEFAZ seja como orgao que se manifestou.\n"
    "- MUNICIPALIDADES_PEDIDO: ato (Partes IV/V) onde a SEFAZ/Rioprevidencia e a publicadora, "
    "contratante ou conveniada. NAO inclua atos de prefeituras ou de outras secretarias (ex.: "
    "SEDEC) so por citarem 'Fazenda' generico.\n"
    "Preencha 'pagina' com o numero do rotulo [pagina N]. Deixe campos vazios como \"\". "
    "Nao invente. Na duvida sobre relevancia para a SEFAZ, NAO inclua. Se nada relevante, itens=[]."
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


# Palavras-chave de RELEVÂNCIA (normalizadas, sem acento). Uma página só vai para
# a IA se contiver ALGUMA delas. NÃO é só "SEFAZ": inclui itens transversais que
# importam mesmo sem citar a Fazenda (ponto facultativo, feriado, recesso,
# expediente, credito orçamentario). Assim o pré-filtro corta o irrelevante (atos
# de outras secretarias) sem perder o que é relevante por natureza estadual.
_KW_RELEVANCIA = (
    # --- SEFAZ / Fazenda / órgãos vinculados ---
    "fazenda", "sefaz", "rioprevid", "fundo unico", "tesouro", "supcc", "supat", "supbf",
    "supct", "supfinf", "supdief", "supaqtic", "receita estadual",
    "subsecretaria de estado de receita", "conselho de contribuintes", "junta de revisao",
    "auditor fiscal", "auditoria fiscal", "tributa", " icms", " itd", " ipva",
    "inscricao estadual", "controle interno", "corregedoria",
    # --- Orçamento / finanças (impacto na SEFAZ mesmo sem citá-la) ---
    "credito suplementar", "dotacao orcamentaria", "orcamento",
    # --- Transversais estaduais (valem mesmo sem mencionar a SEFAZ) ---
    "ponto facultativo", "expediente", "feriado", "recesso",
    "suspensao de expediente", "horario especial", "calamidade",
)


def _pagina_relevante(content):
    n = _norm(content)
    return any(k in n for k in _KW_RELEVANCIA)


def _paginas_relevantes(caderno, date):
    """Páginas do caderno que mencionam algum termo SEFAZ (pré-filtro de custo)."""
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT pdf, caderno, date, page, content FROM pages WHERE caderno=? AND date=? "
            "ORDER BY page", (caderno, date),
        ).fetchall()
    finally:
        con.close()
    return [{"pdf": r[0], "caderno": r[1], "date": r[2], "page": int(r[3])}
            for r in rows if _pagina_relevante(r[4])]


def _cadernos_da_edicao(date):
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT DISTINCT caderno FROM pages WHERE date=? ORDER BY caderno", (date,)
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _norm(s):
    """minúsculo e sem acento — para casar nomes de forma robusta."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


def _tokens_nome(nome):
    """Tokens úteis do nome (>=3 letras, sem 'de/da/do...')."""
    stop = {"de", "da", "do", "dos", "das", "e"}
    return [t for t in _norm(nome).split() if len(t) >= 3 and t not in stop]


def _casa_nome(texto_norm, toks):
    """Posição onde TODOS os tokens do nome aparecem EM ORDEM e próximos (cada
    token até ~35 chars após o anterior). Tolera nome do meio, mas rejeita pessoa
    diferente (ex.: 'Carlos Alberto Cincura Andrade' != 'Carlos Eduardo Andrade').
    -1 se não casar."""
    for m in re.finditer(r"\b" + re.escape(toks[0]) + r"\b", texto_norm):
        pos = m.end()
        ok = True
        for t in toks[1:]:
            mm = re.search(r"\b" + re.escape(t) + r"\b", texto_norm[pos:pos + 35])
            if not mm:
                ok = False
                break
            pos += mm.end()
        if ok:
            return m.start()
    return -1


def _scan_monitorados(date):
    """Varredura textual DETERMINÍSTICA dos nomes de monitorados.txt nos 5 cadernos
    da edição. Não usa IA. Devolve itens NOMES_MONITORADOS (um por nome+página)."""
    nomes = config.nomes_monitorados()
    if not nomes:
        return []
    con = sqlite3.connect(config.DB_PATH)
    try:
        linhas = con.execute(
            "SELECT caderno, page, content FROM pages WHERE date=? ORDER BY caderno, page", (date,)
        ).fetchall()
    finally:
        con.close()
    itens, vistos = [], set()
    for nome in nomes:
        toks = _tokens_nome(nome)
        if len(toks) < 2:
            continue
        for caderno, page, content in linhas:
            pos = _casa_nome(_norm(content), toks)
            if pos < 0:
                continue
            chave = (nome, caderno, page)
            if chave in vistos:
                continue
            vistos.add(chave)
            ini, fim = max(0, pos - 40), min(len(content), pos + 160)
            trecho = " ".join(content[ini:fim].split())
            itens.append({"categoria": "NOMES_MONITORADOS", "pessoa": nome,
                          "tipo_ato": "Mencao no D.O.", "caderno": caderno,
                          "pagina": str(page), "resumo": trecho})
    return itens


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
        return [], {"input": 0, "output": 0}, True
    corpo = "\n\n----------\n\n".join(blocos)
    prompt = (f"Caderno: {caderno}\nEdicao do DOERJ.\n\nTrechos:\n\n{corpo}\n\n"
              "Lembre-se: responda apenas com o objeto JSON.")
    # Resiliência: a Azure Foundry às vezes falha (timeout/5xx/rate-limit). Tenta
    # até 5x com backoff exponencial; se ainda falhar, PULA o bloco (ok=False) e
    # o relatório sai como PARCIAL (sem derrubar tudo). Devolve (itens, usage, ok).
    cli = client.with_options(timeout=180.0)
    resp = None
    for tentativa in range(1, 6):
        try:
            with cli.messages.stream(
                model=config.MONITOR_MODEL, max_tokens=max_tokens, system=SYSTEM,
                messages=[{"role": "user", "content": prompt}],
            ) as stream:
                resp = stream.get_final_message()
            break
        except Exception as e:  # noqa: BLE001
            if tentativa < 5:
                espera = min(60, 5 * (2 ** (tentativa - 1)))   # 5,10,20,40s
                print(f"[monitor]   [retry {tentativa}/5] falha na IA ({type(e).__name__}); "
                      f"aguardando {espera}s...", flush=True)
                time.sleep(espera)
    if resp is None:
        print(f"[monitor]   bloco de '{caderno}' PULADO apos 5 falhas de IA.", flush=True)
        return [], {"input": 0, "output": 0}, False
    try:
        data = _extrair_json(resp)
        itens = data.get("itens", []) or []
    except Exception:
        itens = []
    for it in itens:
        it["caderno"] = caderno
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return itens, usage, True


def gerar(date=None, destino=None, chunk=4, todas_paginas=False):
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
    print(f"[monitor] modelo da extracao: {config.MONITOR_MODEL}"
          f"{' (== site)' if config.MONITOR_MODEL == config.MODEL else ' (mais barato que o site)'}")
    todos = []
    uin = uout = 0
    blocos_total = blocos_falha = 0
    for caderno in _cadernos_da_edicao(date):
        # Pré-filtro de custo: só páginas com termos SEFAZ vão para a IA (a menos
        # que --todas-paginas). Cadernos sem página relevante são pulados.
        if todas_paginas:
            paginas = _paginas_do_caderno(caderno, date)
        else:
            paginas = _paginas_relevantes(caderno, date)
        if not paginas:
            print(f"[monitor] {caderno}: 0 paginas relevantes (SEFAZ) -> pulado")
            continue
        print(f"[monitor] {caderno}: {len(paginas)} pagina(s) para a IA ...")
        for i in range(0, len(paginas), chunk):
            itens, usage, ok = _mapear_bloco(caderno, paginas[i:i + chunk], client)
            blocos_total += 1
            blocos_falha += 0 if ok else 1
            todos.extend(itens)
            uin += usage["input"]; uout += usage["output"]
            time.sleep(2)                 # espaça as chamadas (evita rate-limit da Foundry)

    # NOMES_MONITORADOS vem da varredura DETERMINÍSTICA (confiável, sem depender
    # da IA): descarta o que a IA marcou nessa categoria e usa a lista de nomes.
    todos = [it for it in todos if (it.get("categoria") or "").strip().upper() != "NOMES_MONITORADOS"]
    monit = _scan_monitorados(date)
    todos.extend(monit)
    if monit:
        nomes_achados = sorted({m["pessoa"] for m in monit})
        print(f"[monitor] nomes monitorados: {len(monit)} mencao(oes) de {len(nomes_achados)} "
              f"nome(s) -> {', '.join(nomes_achados)}")

    completo = (blocos_falha == 0)
    print(f"[monitor] {len(todos)} itens relevantes extraidos | blocos: {blocos_total}, "
          f"falhas: {blocos_falha} (tokens: entrada {uin} / saida {uout})")

    # Não sobrescreve um relatório COMPLETO com um PARCIAL: parcial vai para
    # *_PARCIAL.xlsx; só uma rodada sem falhas grava o nome canônico.
    destino = Path(destino) if destino else (config.ROOT / "relatorios")
    destino.mkdir(parents=True, exist_ok=True)
    sufixo = "" if completo else "_PARCIAL"
    arquivo = destino / f"monitoramento_{date}{sufixo}.xlsx"
    _build_xlsx(arquivo, date, todos, blocos_total, blocos_falha)
    if completo:
        print(f"[ok] Excel COMPLETO gerado -> {arquivo}")
    else:
        print(f"[ATENCAO] relatorio PARCIAL ({blocos_falha}/{blocos_total} blocos falharam na IA). "
              f"NAO sobrescreveu o relatorio canonico; salvo como -> {arquivo}. "
              "Regenere quando a IA normalizar.")
    return arquivo


def _build_xlsx(arquivo, date, itens, blocos_total=None, blocos_falha=0):
    import xlsxwriter
    wb = xlsxwriter.Workbook(str(arquivo))
    f_hdr = wb.add_format({"bold": True, "bg_color": "#0052cc", "font_color": "white",
                           "border": 1, "valign": "top"})
    f_wrap = wb.add_format({"text_wrap": True, "valign": "top", "border": 1})
    f_title = wb.add_format({"bold": True, "font_size": 13})
    f_warn = wb.add_format({"bold": True, "font_color": "#b00020"})   # vermelho
    f_ok = wb.add_format({"font_color": "#127a2e"})                    # verde

    # agrupa por seção
    por_secao = {n: [] for n in SECOES}
    for it in itens:
        sec = CATEGORIA_SECAO.get((it.get("categoria") or "").strip().upper())
        if sec:
            por_secao[sec].append(it)

    # aba Resumo
    ws = wb.add_worksheet("Resumo")
    ws.write(0, 0, f"Monitoramento DOERJ (estruturado) - {date}", f_title)
    # Aviso de completude (o ponto-chave: deixar claro quando está incompleto).
    if blocos_falha:
        ws.write(1, 0, f"RELATORIO PARCIAL - {blocos_falha} de {blocos_total} blocos falharam "
                       "na IA (instavel). Secoes podem estar incompletas. Regenere quando "
                       "a IA normalizar.", f_warn)
    else:
        ws.write(1, 0, "Relatorio COMPLETO (todos os blocos processados).", f_ok)
    ws.write(3, 0, "Secao", f_hdr); ws.write(3, 1, "Itens", f_hdr)
    for r, n in enumerate(SECOES, start=4):
        ws.write(r, 0, SECOES[n]); ws.write(r, 1, len(por_secao[n]))
    ws.set_column(0, 0, 44); ws.set_column(1, 1, 10)

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
    ap.add_argument("--todas-paginas", action="store_true",
                    help="Desliga o pre-filtro SEFAZ e manda TODAS as paginas a IA (mais caro)")
    args = ap.parse_args()
    gerar(date=args.date, destino=args.dir, chunk=args.chunk, todas_paginas=args.todas_paginas)


if __name__ == "__main__":
    main()
