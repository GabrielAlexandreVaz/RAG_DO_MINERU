"""
============================================================================
export.py · Gera os downloads (Excel, PDF, JSON) da resposta estruturada
----------------------------------------------------------------------------
Recebe o "payload" da resposta — {question, edicao, resumo, itens} — e devolve
os BYTES do arquivo em cada formato. Tudo gerado NO SERVIDOR, com bibliotecas
que já vêm no venv (xlsxwriter, reportlab). Nada depende de internet externa
(importante na rede SEFAZ).

CAMPOS / COLUNAS aqui são a "fonte única da verdade" da ordem e dos nomes das
colunas: o schema da IA (reader.py), a tabela do site e os arquivos usam a
mesma lista.
============================================================================
"""
import io
import json
from xml.sax.saxutils import escape   # deixa &, <, > seguros dentro do PDF

# Chave interna de cada campo (usada no JSON e no schema da IA).
CAMPOS = ("tipo", "numero", "data", "orgao", "pessoa", "cargo",
          "objeto", "processo", "pagina", "edicao")

# (chave, cabeçalho exibido) — a ORDEM aqui é a ordem das colunas em todo lugar.
COLUNAS = [
    ("tipo", "Tipo"), ("numero", "Número"), ("data", "Data"), ("orgao", "Órgão"),
    ("pessoa", "Pessoa"), ("cargo", "Cargo"), ("objeto", "Objeto"),
    ("processo", "Processo"), ("pagina", "Página"), ("edicao", "Edição"),]


def build_json(payload):
    """Devolve os bytes do JSON (a própria estrutura, identada e com acentos)."""
    dados = {
        "pergunta": payload.get("question", ""),
        "edicao": payload.get("edicao", ""),
        "resumo": payload.get("resumo", ""),
        "itens": payload.get("itens", []),
    }
    return json.dumps(dados, ensure_ascii=False, indent=2).encode("utf-8")


def build_xlsx(payload):
    """Devolve os bytes de uma planilha .xlsx (via xlsxwriter, em memória)."""
    import xlsxwriter

    buf = io.BytesIO()
    wb = xlsxwriter.Workbook(buf, {"in_memory": True})
    ws = wb.add_worksheet("Resposta")

    f_wrap = wb.add_format({"text_wrap": True, "valign": "top"})           # células com quebra de linha
    f_hdr = wb.add_format({"bold": True, "bg_color": "#DCE6F1",
                           "border": 1, "valign": "top"})                  # cabeçalho da tabela

    # Só a tabela: cabeçalho das colunas já na 1ª linha (sem o bloco
    # Pergunta/Edição/Resumo no topo).
    linha = 0
    for c, (_chave, titulo) in enumerate(COLUNAS):
        ws.write(linha, c, titulo, f_hdr)
    linha += 1

    # Uma linha por item.
    for item in payload.get("itens", []):
        for c, (chave, _titulo) in enumerate(COLUNAS):
            ws.write(linha, c, str(item.get(chave, "")), f_wrap)
        linha += 1

    # Larguras das colunas (aproximadas, em caracteres).
    larguras = [14, 12, 12, 22, 22, 22, 44, 20, 8, 12]
    for c, w in enumerate(larguras):
        ws.set_column(c, c, w)

    ws.freeze_panes(1, 0)   # mantém a linha de cabeçalho fixa ao rolar

    wb.close()
    return buf.getvalue()


def build_pdf(payload):
    """Devolve os bytes de um PDF (via reportlab): título + resumo + tabela."""
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import mm
    from reportlab.platypus import (Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    buf = io.BytesIO()
    margem = 12 * mm
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=margem, rightMargin=margem,
                            topMargin=margem, bottomMargin=margem)
    estilos = getSampleStyleSheet()
    st_cel = ParagraphStyle("cel", fontSize=7, leading=8.5)                 # texto das células
    st_hdr = ParagraphStyle("hdr", fontSize=7, leading=8.5,
                            textColor=colors.white, fontName="Helvetica-Bold")

    hist = []                                                               # o "story" do reportlab
    hist.append(Paragraph("Diário Oficial do RJ — Resposta", estilos["Title"]))
    if payload.get("question"):
        hist.append(Paragraph(f"<b>Pergunta:</b> {escape(payload['question'])}", estilos["Normal"]))
    if payload.get("edicao"):
        hist.append(Paragraph(f"<b>Edição:</b> {escape(str(payload['edicao']))}", estilos["Normal"]))
    if payload.get("resumo"):
        hist.append(Spacer(1, 4))
        hist.append(Paragraph(escape(payload["resumo"]), estilos["Normal"]))  # resumo como texto
    hist.append(Spacer(1, 8))

    # Monta a tabela: 1ª linha = cabeçalho; demais = itens. Paragraph em cada
    # célula para o texto quebrar linha (principalmente o 'objeto').
    dados = [[Paragraph(titulo, st_hdr) for (_c, titulo) in COLUNAS]]
    for item in payload.get("itens", []):
        dados.append([Paragraph(escape(str(item.get(chave, ""))), st_cel) for (chave, _t) in COLUNAS])

    # Larguras proporcionais à área útil (paisagem A4 - margens).
    util = landscape(A4)[0] - 2 * margem
    pesos = [9, 8, 8, 14, 14, 14, 28, 13, 5, 9]
    total = sum(pesos)
    col_w = [util * p / total for p in pesos]

    tab = Table(dados, colWidths=col_w, repeatRows=1)                       # repeatRows=1 -> cabeçalho em toda página
    tab.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#0052cc")),       # cabeçalho azul
        ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#b0b8c4")),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f2f5fa")]),
        ("LEFTPADDING", (0, 0), (-1, -1), 3), ("RIGHTPADDING", (0, 0), (-1, -1), 3),
        ("TOPPADDING", (0, 0), (-1, -1), 2), ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
    ]))
    hist.append(tab)

    doc.build(hist)
    return buf.getvalue()


# Mapa formato -> (função, extensão, mimetype). Usado pela rota /api/export.
FORMATOS = {
    "xlsx": (build_xlsx, "xlsx",
             "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    "pdf":  (build_pdf, "pdf", "application/pdf"),
    "json": (build_json, "json", "application/json"),
}
