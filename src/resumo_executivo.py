"""
============================================================================
resumo_executivo.py · O resumo gerencial GERADO A PARTIR DA 002A
----------------------------------------------------------------------------
Lê as linhas que o monitor gravou no Oracle (002A) e produz:

  1. resumo_executivo_<data>.html  - o resumo em blocos de leitura (8 seções),
     no formato do e-mail que a área demandante já recebe;
  2. resumo_executivo_<data>.txt   - o mesmo conteúdo em texto puro;
  3. rastreabilidade_<data>.xlsx   - a MATRIZ DE RASTREABILIDADE: cada item do
     resumo com os registros que o sustentam — ID (nosso), ID_DOERJ (a matéria
     no IOERJ) e as colunas de negócio (pessoa, processo, caderno, página).

Por que assim: a validação da área (31/07/2026) deixou de exigir paridade de
linhas e passou a exigir que todo item executivo seja explicável por registros
estruturados. Gerando o resumo A PARTIR do banco, a matriz sai por construção —
nenhum item do resumo pode existir sem registro, e nenhum registro some sem
aparecer em algum item.

O que este módulo NÃO faz (de propósito): não descarta registro por relevância.
A regra editorial (o que sobe para o resumo e o que fica só na base) ainda não
foi definida com a área; até lá, tudo que está na 002A aparece no resumo, e o
agrupamento reduz o volume sem perder dado.

Uso:
    python src/resumo_executivo.py                     # última edição gravada
    python src/resumo_executivo.py --date 2026-08-03
============================================================================
"""
import argparse
import html
import re
import sys
import unicodedata
from pathlib import Path

import config
import oracle_db
from monitor_estruturado import CATEGORIA_SECAO, SECOES, mascarar_cpf

# Frase de seção vazia, no tom do e-mail da área ("varredura integral realizada").
VAZIO = {
    1: "Nenhum prazo critico identificado nesta edicao.",
    2: "Nenhuma movimentacao de pessoal identificada nesta edicao.",
    3: "Nenhum ato sobre nome monitorado identificado nesta edicao. Varredura integral realizada.",
    4: "Nenhuma observacao executiva identificada nesta edicao.",
    5: "Nenhuma alteracao de expediente ou ponto facultativo identificada nesta edicao.",
    6: "Nenhuma decisao do Tribunal de Contas relativa a SEFAZ ou RIOPREVIDENCIA nesta edicao. "
       "Parte IB integralmente varrida.",
    7: "Nenhum projeto legislativo relativo a Fazenda nesta edicao. Parte II integralmente varrida.",
    8: "Nenhum ato relativo a Fazenda nas Partes IV e V. Ambos os cadernos integralmente varridos.",
}

# A partir de quantos atos homogeneos vale consolidar num item so. 4 e conservador
# de proposito: agrupa serie de rotina (as 6 portarias SUPFINF de cancelamento de
# IE, os 9 avisos de nota de lancamento) e deixa separado o que e pouco e pesa
# (os 3 decretos de credito suplementar de 03/08, cada um com valor proprio).
MIN_CONSOLIDAR = 4


def _norm(s):
    """minúsculo, sem acento — para comparar assunto de atos."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


def _data_br(valor):
    """date/datetime do Oracle -> DD/MM/AAAA ('' se vazio)."""
    return valor.strftime("%d/%m/%Y") if hasattr(valor, "strftime") else ""


def _txt(item, chave):
    """Valor de uma coluna da 002A como texto limpo, com o CPF mascarado.

    Ponto único de leitura das colunas: o CPF que o D.O. publica por extenso não
    sai no HTML nem no TXT (ver monitor_estruturado.mascarar_cpf)."""
    return mascarar_cpf(" ".join(str(item.get(chave) or "").split()))


_RE_NUMERO = re.compile(r"^(.*?)(\d[\d.]*)\s*/?\s*(\d{4})?$")


def _partes_numero(numero_ano):
    """'SUPFINF 1733/2026' -> ('SUPFINF', 1733, '2026'). ('', None, '') se não casar.

    O prefixo é o que identifica a SÉRIE (SUPFINF, SUPBF, SEFAZ/CTCE): atos da
    mesma série e do mesmo assunto são os candidatos naturais a virar um item só."""
    m = _RE_NUMERO.match((numero_ano or "").strip())
    if not m:
        return "", None, ""
    prefixo = m.group(1).strip(" nºo°.-/") if m.group(1) else ""
    try:
        numero = int(m.group(2).replace(".", ""))
    except (TypeError, ValueError):
        numero = None
    return prefixo, numero, (m.group(3) or "")


def _assunto(item, palavras=3):
    """As primeiras palavras do resumo, normalizadas — a 'impressão digital' do assunto."""
    return " ".join(_norm(_txt(item, "RESUMO")).split()[:palavras])


def _chave_serie(item):
    """Chave de consolidação: mesma seção, mesmo tipo de ato, mesma série, mesmo assunto."""
    prefixo, _, _ = _partes_numero(_txt(item, "NUMERO_ANO"))
    return (_norm(_txt(item, "TIPO_ATO")), _norm(prefixo), _assunto(item))


def _pessoas(itens, limite=6):
    """'Fulano; Beltrano e mais 3' — os envolvidos de um item consolidado."""
    nomes, vistos = [], set()
    for it in itens:
        p = _txt(it, "PESSOA")
        if p and _norm(p) not in vistos:
            vistos.add(_norm(p))
            nomes.append(p)
    if not nomes:
        return ""
    if len(nomes) <= limite:
        return "; ".join(nomes)
    return "; ".join(nomes[:limite]) + f" e mais {len(nomes) - limite}"


def _corta(texto, n=320):
    """Corta o resumo no limite, sem cortar palavra ao meio."""
    t = " ".join((texto or "").split())
    return t if len(t) <= n else t[:t.rfind(" ", 0, n)] + "..."


def _local(itens):
    """'(Parte I, p. 8)' ou '(Parte I, p. 8, 9, 25)' — de onde saiu o item."""
    cadernos, paginas = [], []
    for it in itens:
        cad = _txt(it, "CADERNO").split(" (")[0]
        if cad and cad not in cadernos:
            cadernos.append(cad)
        pag = it.get("PAGINA")
        if pag is not None and pag not in paginas:
            paginas.append(pag)
    partes = []
    if cadernos:
        partes.append(", ".join(cadernos))
    if paginas:
        plural = "p." if len(paginas) == 1 else "pp."
        partes.append(f"{plural} " + ", ".join(str(int(p)) for p in sorted(paginas)))
    return f" ({'; '.join(partes)})" if partes else ""


def _referencia(registros, lim=3):
    """'MANOEL ANTONIO BENTO · SEI-040002/002011/2026 · Parte I, p. 59'.

    É a âncora do item no banco. No texto do resumo usamos as colunas de NEGÓCIO
    (pessoa, processo, página), que é como a área pediu para conferir ('validar
    por pessoa + processo + tipo de ato') e o que uma pessoa consegue ler. Os
    identificadores — ID (nosso) e ID_DOERJ (da matéria no IOERJ) — ficam na
    matriz de rastreabilidade, para quem for cruzar com o banco ou com o D.O."""
    def distintos(chave):
        vistos, saida = set(), []
        for r in registros:
            v = _txt(r, chave)
            if v and _norm(v) not in vistos:
                vistos.add(_norm(v))
                saida.append(v)
        return saida

    partes = []
    for chave in ("PESSOA", "PROCESSO"):
        vals = distintos(chave)
        if vals:
            texto = "; ".join(vals[:lim])
            if len(vals) > lim:
                texto += f" (+{len(vals) - lim})"
            partes.append(texto)
    local = _local(registros).strip(" ()")
    if local:
        partes.append(local)
    return " · ".join(partes)


def _texto_item_unico(secao, it):
    """Redação de um item que corresponde a UM registro da 002A."""
    tipo = _txt(it, "TIPO_ATO") or "Ato"
    num = _txt(it, "NUMERO_ANO")
    orgao = _txt(it, "ORGAO")
    pessoa = _txt(it, "PESSOA")
    cargo = _txt(it, "CARGO")
    proc = _txt(it, "PROCESSO")
    resumo = _corta(_txt(it, "RESUMO"))

    if secao == 1:                                   # prazos: a data manda
        prazo = _data_br(it.get("PRAZO"))
        cabeca = f"Prazo {prazo}" if prazo else "Prazo a apurar"
        corpo = " - ".join(x for x in (orgao, tipo) if x)
        return f"{cabeca} - {corpo}: {resumo}" + (f" Processo {proc}." if proc else "")

    if secao == 2:                                   # pessoal: pessoa em primeiro lugar
        cabeca = f"{tipo.upper()}: {pessoa}" if pessoa else tipo.upper()
        detalhes = [x for x in (cargo, orgao) if x]
        txt = cabeca + (", " + ", ".join(detalhes) if detalhes else "")
        return txt + (f", {proc}" if proc else "") + "."

    if secao == 3:                                   # nomes monitorados
        quem = pessoa + (f" ({cargo})" if cargo else "")
        return f"{quem}: {resumo}"

    cabeca = " ".join(x for x in (tipo, num) if x)
    return f"{cabeca}: {resumo}" + (f" Processo {proc}." if proc else "")


def _texto_item_serie(itens):
    """Redação de um item que consolida VÁRIOS registros homogêneos."""
    tipo = _txt(itens[0], "TIPO_ATO") or "Atos"
    prefixo, _, ano = _partes_numero(_txt(itens[0], "NUMERO_ANO"))
    # Prefixo longo não é código de série, é o próprio nome do ato repetido
    # ('2º Termo Aditivo ao Termo de Compromisso de Estági') — repetiria o tipo.
    if len(prefixo) > 20:
        prefixo = ""
    # Numeração só entra se for numeração de verdade: 'nº 0' é campo não preenchido
    # pela IA e virava a faixa absurda 'nos 0 a 0'.
    numeros = sorted(n for n in
                     (_partes_numero(_txt(i, "NUMERO_ANO"))[1] for i in itens)
                     if n is not None and n > 0)
    faixa = ""
    if numeros:
        faixa = f"nos {numeros[0]} a {numeros[-1]}" if len(numeros) > 1 else f"no {numeros[0]}"
        if ano:
            faixa += f"/{ano}"
    cabeca = " ".join(x for x in (tipo, prefixo, faixa) if x)
    texto = f"{cabeca} - {len(itens)} atos: {_corta(_txt(itens[0], 'RESUMO'), 220)}"
    envolvidos = _pessoas(itens)
    return texto + (f" Envolvidos: {envolvidos}." if envolvidos else "")


def montar_itens(registros):
    """Registros da 002A -> {secao: [item executivo]}.

    Cada item executivo é {"texto", "registros": [linhas da 002A]} — é essa lista
    que vira a matriz de rastreabilidade. Nenhum registro é descartado: ou vira um
    item próprio, ou entra num item consolidado."""
    por_secao = {n: [] for n in SECOES}
    for r in registros:
        sec = CATEGORIA_SECAO.get((r.get("TIPO") or "").strip().upper())
        if sec:
            por_secao[sec].append(r)

    saida = {}
    for secao, linhas in por_secao.items():
        # Séries homogêneas só valem a pena consolidar nas seções descritivas;
        # prazo, pessoal e nome monitorado são lidos um a um.
        grupos = {}
        if secao in (4, 6, 7, 8):
            for r in linhas:
                grupos.setdefault(_chave_serie(r), []).append(r)

        itens, consolidados = [], set()
        for chave, grupo in grupos.items():
            if len(grupo) >= MIN_CONSOLIDAR:
                itens.append({"texto": _texto_item_serie(grupo), "registros": grupo})
                consolidados.add(chave)
        for r in linhas:
            if secao in (4, 6, 7, 8) and _chave_serie(r) in consolidados:
                continue
            itens.append({"texto": _texto_item_unico(secao, r), "registros": [r]})
        saida[secao] = itens
    return saida


def _resumo_por_caderno(registros):
    """[(caderno, nº de registros)] — o rodapé de cobertura do e-mail."""
    contagem = {}
    for r in registros:
        cad = _txt(r, "CADERNO") or "(sem caderno)"
        contagem[cad] = contagem.get(cad, 0) + 1
    return sorted(contagem.items())


def gerar_html(date, itens_por_secao, registros):
    """Monta o HTML do resumo (pronto para colar num e-mail)."""
    e = html.escape
    p = [f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<style>
 body{{font-family:Segoe UI,Calibri,Arial,sans-serif;color:#2b2b2b;max-width:900px;
      margin:0 auto;padding:24px;line-height:1.5}}
 h1{{font-size:22px;color:#1F4E79;margin:0}}
 .sub{{color:#6E7E8B;font-size:14px;margin:4px 0 22px}}
 h2{{font-size:15px;color:#1F4E79;margin:26px 0 10px;text-transform:uppercase}}
 ul{{margin:0;padding-left:20px}} li{{margin-bottom:9px;font-size:14px}}
 .vazio{{font-style:italic;color:#6E7E8B;font-size:14px}}
 .ids{{color:#9AA7B2;font-size:11px}}
 table{{border-collapse:collapse;margin-top:8px;font-size:13px}}
 td,th{{border:1px solid #DCE3EA;padding:5px 10px;text-align:left}}
 th{{background:#F0F5F9;color:#41525F}}
 .rodape{{margin-top:26px;color:#6E7E8B;font-size:12px;border-top:1px solid #DCE3EA;
         padding-top:10px}}
</style></head><body>
<h1>[DOERJ] Monitoramento SEFAZ</h1>
<div class="sub">Diario Oficial do Estado do Rio de Janeiro - edicao de {e(_data_br_iso(date))}</div>"""]

    for n in SECOES:
        p.append(f"<h2>{e(SECOES[n].split(' - ', 1)[1] if ' - ' in SECOES[n] else SECOES[n])}</h2>")
        itens = itens_por_secao.get(n, [])
        if not itens:
            p.append(f'<div class="vazio">{e(VAZIO[n])}</div>')
            continue
        p.append("<ul>")
        for it in itens:
            ref = _referencia(it["registros"])
            n = len(it["registros"])
            origem = f"{n} registros da 002A" if n > 1 else "002A"
            p.append(f'<li>{e(it["texto"])}'
                     f'<br><span class="ids">{e(origem)} &middot; {e(ref)}</span></li>')
        p.append("</ul>")

    p.append("<h2>Resumo por caderno</h2><table><tr><th>Caderno</th><th>Registros</th></tr>")
    for cad, qtd in _resumo_por_caderno(registros):
        p.append(f"<tr><td>{e(cad)}</td><td>{qtd}</td></tr>")
    p.append("</table>")
    total_itens = sum(len(v) for v in itens_por_secao.values())
    p.append(f'<div class="rodape">Gerado a partir da tabela 002A (Oracle): '
             f'{len(registros)} registros estruturados consolidados em {total_itens} itens de '
             f'leitura. Cada item aponta pessoa, processo e pagina dos registros que o '
             f'sustentam - toda linha da base aparece em '
             f'algum item, e nenhum item existe sem registro. Fonte: DOERJ, varredura dos cinco '
             f'cadernos.</div></body></html>')
    return "\n".join(p)


def _data_br_iso(date):
    """'2026-08-03' -> '03/08/2026'."""
    try:
        a, m, d = str(date).split("-")
        return f"{d}/{m}/{a}"
    except ValueError:
        return str(date)


def gerar_txt(date, itens_por_secao, registros):
    """Mesma coisa em texto puro (para colar em e-mail simples ou chat)."""
    linhas = [f"[DOERJ] Monitoramento SEFAZ - edicao de {_data_br_iso(date)}", ""]
    for n in SECOES:
        linhas.append(SECOES[n].upper())
        itens = itens_por_secao.get(n, [])
        if not itens:
            linhas += [f"  {VAZIO[n]}", ""]
            continue
        for it in itens:
            n = len(it["registros"])
            origem = f"{n} registros" if n > 1 else "1 registro"
            linhas.append(f"  - {it['texto']}")
            linhas.append(f"      [{origem} da 002A · {_referencia(it['registros'])}]")
        linhas.append("")
    linhas.append("RESUMO POR CADERNO")
    for cad, qtd in _resumo_por_caderno(registros):
        linhas.append(f"  {cad}: {qtd} registro(s)")
    return "\n".join(linhas)


def gerar_matriz(arquivo, date, itens_por_secao, registros):
    """Escreve a matriz de rastreabilidade (item executivo -> registros da 002A)."""
    import xlsxwriter
    wb = xlsxwriter.Workbook(str(arquivo))
    f_hdr = wb.add_format({"bold": True, "bg_color": "#1F4E79", "font_color": "white",
                           "border": 1, "valign": "top"})
    f_cel = wb.add_format({"text_wrap": True, "valign": "top", "border": 1})
    f_tit = wb.add_format({"bold": True, "font_size": 13})

    ws = wb.add_worksheet("Rastreabilidade")
    # As colunas de conferencia sao as de NEGOCIO (pessoa, processo, tipo do ato,
    # caderno, pagina) - o criterio que a area propos na validacao de 31/07.
    cabec = ["Secao", "Item executivo", "Registros", "IDs (002A)", "Ids no DOERJ",
             "Pessoas", "Processos", "Tipos de ato", "Cadernos", "Paginas"]
    for c, t in enumerate(cabec):
        ws.write_string(0, c, t, f_hdr)

    def juntar(regs, chave, lim=900):
        return "; ".join(sorted({_txt(r, chave) for r in regs if _txt(r, chave)}))[:lim]

    li = 1
    for n in SECOES:
        for it in itens_por_secao.get(n, []):
            regs = it["registros"]
            ws.write_string(li, 0, SECOES[n], f_cel)
            ws.write_string(li, 1, it["texto"], f_cel)
            ws.write_number(li, 2, len(regs), f_cel)
            ws.write_string(li, 3, ", ".join(str(r["ID"]) for r in regs
                                             if r.get("ID") is not None), f_cel)
            ws.write_string(li, 4, ", ".join(sorted({str(r["ID_DOERJ"]) for r in regs
                                                     if r.get("ID_DOERJ") is not None})), f_cel)
            ws.write_string(li, 5, juntar(regs, "PESSOA"), f_cel)
            ws.write_string(li, 6, juntar(regs, "PROCESSO"), f_cel)
            ws.write_string(li, 7, juntar(regs, "TIPO_ATO", 300), f_cel)
            ws.write_string(li, 8, juntar(regs, "CADERNO"), f_cel)
            ws.write_string(li, 9, ", ".join(str(int(r["PAGINA"])) for r in regs
                                             if r.get("PAGINA") is not None), f_cel)
            li += 1
    for c, w in enumerate([32, 76, 11, 30, 24, 30, 32, 24, 22, 12]):
        ws.set_column(c, c, w)
    ws.freeze_panes(1, 0)
    if li > 1:
        ws.autofilter(0, 0, li - 1, len(cabec) - 1)

    # Conferência: a soma dos registros citados tem de fechar com a 002A.
    ws2 = wb.add_worksheet("Conferencia")
    citados = sum(len(it["registros"]) for v in itens_por_secao.values() for it in v)
    ws2.write_string(0, 0, f"Rastreabilidade da edicao {_data_br_iso(date)}", f_tit)
    linhas_conf = [
        ("Registros na 002A", len(registros)),
        ("Registros citados em algum item", citados),
        ("Itens executivos gerados", sum(len(v) for v in itens_por_secao.values())),
    ]
    for r, (rot, val) in enumerate(linhas_conf, start=2):
        ws2.write_string(r, 0, rot)
        ws2.write_number(r, 1, val)
    ws2.write_string(6, 0, "Situacao", f_hdr)
    ws2.write_string(6, 1, "OK - todo registro aparece em algum item"
                     if citados == len(registros)
                     else "ATENCAO - ha registro fora do resumo", f_hdr)
    ws2.set_column(0, 0, 40); ws2.set_column(1, 1, 46)
    wb.close()


def gerar(date=None, destino=None):
    """Gera os três arquivos da edição. Devolve (html, txt, xlsx)."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not oracle_db.configurado() or not config.oracle_settings().get("table_monitor"):
        sys.exit("[ERRO] Oracle/002A nao configurado no .env - o resumo nasce do banco.")
    if not date:
        from search import list_dates
        datas = list_dates()
        if not datas:
            sys.exit("[ERRO] nenhuma edicao no indice.")
        date = datas[0]

    registros = oracle_db.listar_monitoramento(date)
    if not registros:
        sys.exit(f"[ERRO] a 002A nao tem registro da edicao {date}. "
                 "Rode antes: python src/monitor_estruturado.py")
    print(f"[resumo] {len(registros)} registro(s) lidos da 002A para a edicao {date}")

    itens_por_secao = montar_itens(registros)
    total = sum(len(v) for v in itens_por_secao.values())
    citados = sum(len(it["registros"]) for v in itens_por_secao.values() for it in v)
    print(f"[resumo] {total} item(ns) de leitura | {citados}/{len(registros)} registros citados")
    if citados != len(registros):
        print("[ATENCAO] ha registro da 002A que nao entrou em nenhum item do resumo.")

    destino = Path(destino) if destino else config.RELATORIOS_DIR
    destino.mkdir(parents=True, exist_ok=True)
    f_html = destino / f"resumo_executivo_{date}.html"
    f_txt = destino / f"resumo_executivo_{date}.txt"
    f_xlsx = destino / f"rastreabilidade_{date}.xlsx"
    f_html.write_text(gerar_html(date, itens_por_secao, registros), encoding="utf-8")
    f_txt.write_text(gerar_txt(date, itens_por_secao, registros), encoding="utf-8")
    gerar_matriz(f_xlsx, date, itens_por_secao, registros)
    for f in (f_html, f_txt, f_xlsx):
        print(f"[ok] {f}")
    return f_html, f_txt, f_xlsx


def main():
    """Linha de comando: gera o resumo executivo e a matriz de uma edição."""
    ap = argparse.ArgumentParser(
        description="Gera o resumo executivo e a matriz de rastreabilidade a partir da 002A.")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--dir", default=None, help="Pasta de destino (padrao: <projeto>/relatorios)")
    args = ap.parse_args()
    gerar(date=args.date, destino=args.dir)


if __name__ == "__main__":
    main()
