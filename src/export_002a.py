"""
============================================================================
export_002a.py · Exporta para Excel os registros da 002A de uma edição
----------------------------------------------------------------------------
Lê a tabela do Oracle (não o Excel do monitor) e gera
`relatorios/002A_<data>.xlsx` — o formato que a área demandante já recebeu e
usou no comparativo de validação de 31/07/2026.

Duas abas:
  002A     · uma linha por registro, com todas as colunas da tabela e uma
             coluna "Secao" traduzindo o TIPO para a seção do relatório;
  Resumo   · a contagem por seção.

Por que existe, se o monitor já gera um Excel: são coisas diferentes. O Excel
do monitor é o que ACHAMOS na edição; este é o que FICOU GRAVADO na 002A. Se
a gravação no Oracle falhar, ou for parcial, a diferença aparece aqui.

Uso:
    python src/export_002a.py                     # última edição gravada
    python src/export_002a.py --date 2026-08-04
============================================================================
"""
import argparse
import sys
from pathlib import Path

import config
import oracle_db
from monitor_estruturado import CATEGORIA_SECAO, SECOES, mascarar_cpf

COLUNAS = ["ID", "ID_DOERJ", "TIPO", "TIPO_ATO", "NUMERO_ANO", "ORGAO", "PESSOA", "CARGO", "PROCESSO",
           "VIGENCIA", "RESUMO", "CADERNO", "PAGINA", "DATA_EDICAO", "DATA_ATO", "PRAZO"]
LARGURAS = [14, 14, 26, 24, 22, 26, 30, 26, 26, 18, 70, 24, 8, 12, 12, 12, 12]


def _secao(tipo):
    """TIPO da 002A -> nome da seção do relatório."""
    n = CATEGORIA_SECAO.get((tipo or "").strip().upper())
    return SECOES.get(n, "(sem secao)")


def gerar(date=None, destino=None):
    """Escreve o .xlsx da edição e devolve o caminho."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    if not oracle_db.configurado() or not config.oracle_settings().get("table_monitor"):
        sys.exit("[ERRO] Oracle/002A nao configurado no .env.")
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
    print(f"[export] {len(registros)} registro(s) lidos da 002A para a edicao {date}")

    import xlsxwriter
    destino = Path(destino) if destino else config.RELATORIOS_DIR
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = destino / f"002A_{date}.xlsx"
    wb = xlsxwriter.Workbook(str(arquivo))
    f_hdr = wb.add_format({"bold": True, "bg_color": "#0052cc", "font_color": "white",
                           "border": 1, "valign": "top"})
    f_cel = wb.add_format({"text_wrap": True, "valign": "top", "border": 1})
    f_dat = wb.add_format({"num_format": "dd/mm/yyyy", "valign": "top", "border": 1})
    f_tit = wb.add_format({"bold": True, "font_size": 13})

    ws = wb.add_worksheet("002A")
    cabec = ["Secao"] + COLUNAS
    for c, t in enumerate(cabec):
        ws.write_string(0, c, t, f_hdr)
    for li, r in enumerate(registros, start=1):
        ws.write_string(li, 0, _secao(r.get("TIPO")), f_cel)
        for c, col in enumerate(COLUNAS, start=1):
            v = r.get(col)
            if hasattr(v, "year"):                       # DATE do Oracle
                ws.write_datetime(li, c, v, f_dat)
            elif isinstance(v, (int, float)) and col in ("ID", "ID_DOERJ", "PAGINA"):
                ws.write_number(li, c, v, f_cel)
            else:
                # write_string sempre: texto que parece numero ou comeca com '='
                # nao pode virar numero/formula (ver monitor_estruturado._build_xlsx).
                # O CPF transcrito do D.O. sai mascarado, como no Excel do monitor.
                ws.write_string(li, c, mascarar_cpf(v), f_cel)
    for c, w in enumerate(LARGURAS[:len(cabec)]):
        ws.set_column(c, c, w)
    ws.freeze_panes(1, 0)
    ws.autofilter(0, 0, len(registros), len(cabec) - 1)

    ws2 = wb.add_worksheet("Resumo")
    ws2.write_string(0, 0, f"Itens gravados na 002A - edicao {date}", f_tit)
    ws2.write_string(2, 0, "Secao", f_hdr); ws2.write_string(2, 1, "Itens", f_hdr)
    contagem = {}
    for r in registros:
        n = CATEGORIA_SECAO.get((r.get("TIPO") or "").strip().upper())
        contagem[n] = contagem.get(n, 0) + 1
    for linha, n in enumerate(SECOES, start=3):
        ws2.write_string(linha, 0, SECOES[n])
        ws2.write_number(linha, 1, contagem.get(n, 0))
    ws2.write_string(len(SECOES) + 3, 0, "TOTAL", f_hdr)
    ws2.write_number(len(SECOES) + 3, 1, len(registros), f_hdr)
    ws2.set_column(0, 0, 44); ws2.set_column(1, 1, 10)
    wb.close()
    print(f"[ok] {arquivo}")
    return arquivo


def main():
    """Linha de comando: exporta a 002A de uma edição para Excel."""
    ap = argparse.ArgumentParser(description="Exporta os registros da 002A de uma edicao.")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--dir", default=None, help="Pasta de destino (padrao: <projeto>/relatorios)")
    args = ap.parse_args()
    gerar(date=args.date, destino=args.dir)


if __name__ == "__main__":
    main()
