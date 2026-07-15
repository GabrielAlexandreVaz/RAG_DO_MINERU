"""
============================================================================
render.py · "Fotografa" uma página do PDF (com marca-texto opcional)
----------------------------------------------------------------------------
Transforma uma página do PDF numa imagem PNG, usada como miniatura de
conferência no front-end. A resposta da IA vem do TEXTO (não daqui); estas
imagens servem só para você conferir visualmente a página de origem.

Marca-texto (destaque amarelo): em vez do bbox do MinerU (que fica numa escala
de imagem própria e desalinha), usamos a BUSCA NATIVA do PyMuPDF
(page.search_for). Os retângulos que ela devolve estão no MESMO sistema de
coordenadas do render -> o destaque fica sempre alinhado ao texto.
============================================================================
"""
from pathlib import Path

import fitz  # PyMuPDF — biblioteca que lê PDF, extrai texto e renderiza páginas

# Limite da aresta maior da imagem (px). Evita imagens gigantes/pesadas.
MAX_LONG_EDGE_PX = 2200


def render_page(pdf_path, page_number=1, dpi=150, terms=None, save_to=None):
    """Renderiza uma página (1-based) e devolve (png_bytes, (largura, altura), total_paginas).

    Parâmetros:
      page_number : número da página (começando em 1)
      dpi         : resolução (pontos por polegada); maior = mais nítido
      terms       : lista de palavras a destacar (marca-texto amarelo). Opcional.
      save_to     : se dado, também salva o PNG nesse caminho (para depuração)."""
    pdf_path = Path(pdf_path)
    doc = fitz.open(pdf_path)                        # abre o PDF
    try:
        # Valida o número da página.
        if page_number < 1 or page_number > doc.page_count:
            raise ValueError(
                f"Pagina {page_number} fora do intervalo (1..{doc.page_count}) em {pdf_path.name}"
            )
        page = doc[page_number - 1]                  # PyMuPDF é 0-based, por isso o -1

        # --- Marca-texto: procura cada termo e pinta um retângulo amarelo -----
        n_marcas = 0
        for t in (terms or []):
            try:
                # search_for devolve os retângulos (na coordenada da página) de
                # cada ocorrência do termo. É case-insensitive (ignora maiúsc./minúsc.).
                rects = page.search_for(t)
            except Exception:
                rects = []
            for rect in rects:
                page.draw_rect(
                    rect,
                    color=(0.90, 0.70, 0.0), width=0.5, stroke_opacity=0.5,   # borda dourada fininha
                    fill=(1.0, 0.90, 0.25), fill_opacity=0.35,                # preenchimento amarelo translúcido
                )
                n_marcas += 1
                if n_marcas >= 200:                  # trava de segurança contra termos muito comuns
                    break
            if n_marcas >= 200:
                break

        # --- Renderização: aplica o zoom (dpi) e vira imagem -------------------
        zoom = dpi / 72.0                            # 72 é o DPI "natural" do PDF
        long_pt = max(page.rect.width, page.rect.height)
        if long_pt * zoom > MAX_LONG_EDGE_PX:        # se ficar grande demais, reduz o zoom
            zoom = MAX_LONG_EDGE_PX / long_pt
        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)   # "tira a foto"
        png_bytes = pix.tobytes("png")               # converte para PNG (bytes)

        if save_to is not None:
            save_to = Path(save_to)
            save_to.parent.mkdir(parents=True, exist_ok=True)
            save_to.write_bytes(png_bytes)

        return png_bytes, (pix.width, pix.height), doc.page_count
    finally:
        doc.close()                                  # sempre fecha o PDF
