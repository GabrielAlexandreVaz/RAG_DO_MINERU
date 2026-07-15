"""
============================================================================
highlight.py · Prepara os termos do marca-texto (destaque amarelo)
----------------------------------------------------------------------------
Quando você busca algo, o front-end pede a imagem da página já com as palavras
buscadas destacadas. Este módulo só CUIDA DOS TERMOS (limpa a lista de palavras
a procurar). O destaque em si (achar a posição e pintar o amarelo) é feito no
render.py, usando a busca nativa do PyMuPDF (page.search_for) — que garante o
alinhamento perfeito.

Por que não usamos o bbox do MinerU aqui: o bbox do content_list.json fica numa
escala de imagem própria do MinerU, que NÃO bate com a página renderizada (o
destaque saía desalinhado). O search_for do PyMuPDF trabalha no mesmo espaço do
render, então o marca-texto cai exatamente em cima da palavra.
============================================================================
"""
import re
import unicodedata      # normalizar texto (tirar acentos) para comparar

# Conectores e palavras curtas que NÃO devem virar termo de destaque (senão o
# amarelo pintaria "de", "por", "para" espalhados pela página inteira).
_SKIP = {"or", "and", "de", "do", "da", "dos", "das", "e", "em", "no", "na",
         "the", "que", "com", "por", "para", "os", "as", "um", "uma"}


def _norm(s):
    """Minúsculas e SEM acentos — usado só para comparar com a lista _SKIP e
    para evitar termos repetidos. (NFKD separa a letra do acento; depois
    descartamos os acentos.)"""
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.lower()


def terms(hl):
    """Quebra a string de busca numa lista de termos a destacar.

    Ex.: 'atos OR sefaz' -> ['atos', 'sefaz'];  'exoner*' -> ['exoner'].
    - remove o curinga '*' (o search_for casa por pedaço, então 'exoner' já
      pega 'exoneração');
    - descarta conectores (_SKIP) e termos com menos de 2 letras;
    - evita repetir o mesmo termo.
    IMPORTANTE: preserva os acentos do termo original (o search_for do PyMuPDF
    diferencia acento, então guardamos a palavra como o usuário digitou)."""
    out, vistos = [], set()
    for t in re.split(r"[,\s]+", hl or ""):          # separa por espaço/vírgula
        t = t.strip().rstrip("*")                    # tira espaços e o curinga do fim
        if len(t) < 2:
            continue
        n = _norm(t)                                 # versão sem acento, só para filtrar
        if n in _SKIP or n in vistos:                # pula conector ou repetido
            continue
        vistos.add(n)
        out.append(t)                                # guarda o termo ORIGINAL (com acento)
    return out
