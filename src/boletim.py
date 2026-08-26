"""
============================================================================
boletim.py · O boletim HTML no formato do e-mail "[DOERJ] Monitoramento SEFAZ"
----------------------------------------------------------------------------
Mesma extração do dia, outro formato de saída. O `monitor_estruturado.py` grava
o Excel (uma aba por seção, para conferência) e a 002A; aqui os MESMOS itens
saem no layout do e-mail que a área já recebe todo dia:

  1. Prazos críticos          5. Observações executivas
  2. Nomeações e exonerações  6. Expediente / ponto facultativo
  3. Nomes monitorados        7. Varredura dos demais cadernos
  4. Controle interno                 + tabela de prazos e rodapé

Diferença para o `resumo_executivo.py`: aquele consolida séries homogêneas e
mostra a rastreabilidade (item -> registros da 002A), para a validação da área;
este é o boletim de LEITURA, item a item, no formato do e-mail.

Duas fontes possíveis, mesmo resultado:
  - chamado pelo monitor logo depois do Excel, com os itens em memória (não
    depende do Oracle estar de pé);
  - chamado pela linha de comando, lendo a 002A do Oracle (para refazer o
    boletim de uma edição já processada, sem gastar IA).

Todas as seções saem com a SEFAZ inteira. A seção 2 PODE ser recortada por
subsecretaria (SUBSEC_BOLETIM / --subsecretaria), mas o padrão é não recortar.

Uso:
    python src/boletim.py                      # última edição gravada na 002A
    python src/boletim.py --date 2026-08-17
    python src/boletim.py --subsecretaria SUBCINT  # seção 2 só da SUBCINT
============================================================================
"""
import argparse
import html
import re
import sqlite3
import sys
from pathlib import Path

import config
import oracle_db
# As funções auxiliares de casamento de nome e normalização já existem no monitor
# (e são as MESMAS usadas na seção 3): importar evita duas implementações que um
# dia divergem em silêncio.
from monitor_estruturado import (_carregar_monitorados, _casa_nome, _dehifenizar,
                                 _norm, _tokens_nome, mascarar_cpf)

# Identidade visual: o mesmo azul do resumo executivo e do front-end.
_AZUL = "#1F4E79"
_CINZA = "#6E7E8B"

DIAS_SEMANA = ["Segunda-feira", "Terça-feira", "Quarta-feira", "Quinta-feira",
               "Sexta-feira", "Sábado", "Domingo"]
MESES = ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho",
         "agosto", "setembro", "outubro", "novembro", "dezembro"]

# Título de cada seção do boletim, na redação do e-mail.
TITULOS = {
    1: "1. PRAZOS CRÍTICOS (AÇÃO INTERNA SEFAZ)",
    2: "2. NOMEAÇÕES E EXONERAÇÕES",
    3: "3. NOMES MONITORADOS",
    4: "4. CONTROLE INTERNO / AUDITORIA / OUVIDORIA / CORREGEDORIA — SEFAZ",
    5: "5. OBSERVAÇÕES EXECUTIVAS",
    6: "6. EXPEDIENTE / PONTO FACULTATIVO",
    7: "7. VARREDURA DOS DEMAIS CADERNOS",
}

# Frase de seção vazia. Dizer "nenhum" por extenso é o que separa "não houve" de
# "falhou" para quem lê — seção sem texto nenhum passa por erro do robô.
VAZIO = {
    1: "Nenhum prazo crítico para a SEFAZ nesta edição.",
    2: "Nenhuma nomeação ou exoneração de cargo em comissão na SEFAZ nesta edição.",
    4: "Nenhum ato de controle interno, auditoria, ouvidoria ou corregedoria da SEFAZ "
       "nesta edição.",
    5: "Nenhuma observação executiva nesta edição.",
    6: "Nenhum ponto facultativo ou expediente especial publicado.",
}

# Os cadernos da seção 7, com o rótulo que aparece no boletim.
CADERNOS_VARREDURA = [
    ("Parte IB", "Parte IB (TCE-RJ)"),
    ("Parte II", "Parte II (Poder Legislativo)"),
    ("Parte IV", "Parte IV (Municipalidades)"),
    ("Parte V", "Parte V (Publicações a Pedido)"),
]

# ============================================================================
#  Recorte da seção 2 por subsecretaria
# ============================================================================
# A área pediu a seção 2 (movimentação de pessoal) só com o que é da SUBCINT. A
# 002A continua gravando a SEFAZ inteira: o recorte é de LEITURA, não de coleta —
# o que a IA não extrai não volta, o que o boletim não mostra volta com uma flag.
#
# A coluna ORGAO da 002A NÃO serve para isso: nas 163 movimentações de julho/agosto
# ela vem quase sempre como "Secretaria de Estado de Fazenda" genérico, porque é
# transcrição fiel do D.O. (ver a regra de FIDELIDADE no SYSTEM do monitor). O
# recorte precisa de DOIS sinais, e os dois são indispensáveis - cada um pega um
# caso que o outro perde:
#
#   A) PREFIXO DO PROCESSO SEI. O próprio DOERJ publicou a tabela de prefixos por
#      unidade (edição de 04/08/2026, Parte I, p.4): "SEFAZ/SUBCINT SUBSECRETARIA
#      DE CONTROLE INTERNO | SEI-040005". PROCESSO está preenchido em 158 das 163
#      linhas. Sozinho, é o único sinal que pega a nomeação de REGINA CLAUDIA
#      (SEI-040005/000674/2026, 04/08): o D.O. publicou o ato dizendo apenas "da
#      Secretaria de Estado de Fazenda", sem a cadeia de lotação, então nenhum
#      ajuste de prompt a recuperaria.
#
#   B) CADEIA DE LOTAÇÃO NO TEXTO. O D.O. costuma escrever a hierarquia inteira
#      ("Corregedor Interno, da Corregedoria Interna, da Subsecretaria de Controle
#      Interno, da Secretaria de Estado de Fazenda"). Sozinho, é o único sinal que
#      pega a nomeação de AIRES FRANCISCO (30/07) como Ouvidor Geral da SUBCINT:
#      o processo dele é SEI-040006 (Receita), de onde ele veio.
#
# A união dos dois devolve 5 acertos em 160 movimentações, sem falso positivo.
SUBSECRETARIAS = {
    "SUBCINT": {
        "nome": "Subsecretaria de Controle Interno",
        # Prefixo SEI da unidade (sinal A).
        "prefixos": {"040005"},
        # Unidades subordinadas que denunciam a lotação (sinal B). Termos ANCORADOS:
        # um teste contra as 163 linhas mostrou que os genéricos arruinariam o
        # recorte - "auditoria" casa "Auditor Fiscal da Receita Estadual" (~20 falsos
        # positivos: é carreira, não unidade) e "corregedoria" solto casa "Corregedor
        # Auxiliar" da CTCE. Por isso "corregedoria interna" entra como frase inteira
        # e "auditoria" não entra de jeito nenhum. "ouvidoria" é seguro porque na
        # SEFAZ a Ouvidoria é subordinada à SUBCINT (2 de 2 corretos).
        "unidades": (
            "subsecretaria de controle interno", "subcint",
            "corregedoria interna", "ouvidoria",
            "assessoria de integridade e riscos",
            "assessoria especial de controle interno",
        ),
        # Vetam o item mesmo que um positivo tenha casado. A CTCE (Corregedoria
        # Tributária de Controle Externo) é justamente quem RECEBE as denúncias da
        # SUBCINT - são órgãos distintos. E a edição de 27/07 traz uma "Subsecretaria
        # de Controle Interno - PGE", que é de outro órgão.
        "negativos": ("corregedoria tributaria", "controle externo", "ctce",
                      "controle interno - pge", "controle interno-pge"),
    },
}

# Recorte padrão da seção 2 no boletim diário. None = SEFAZ inteira.
#
# Voltou para None em 20/08/2026, a pedido da área: a seção 2 tinha saído
# recortada na SUBCINT, que é ~3% da movimentação de pessoal da SEFAZ, e o que se
# quer ler é a movimentação da Secretaria inteira. A maquinaria do recorte
# (SUBSECRETARIAS, _e_da_subsecretaria) continua aqui e testada — para voltar a
# recortar, basta `--subsecretaria SUBCINT` na linha de comando ou repor a sigla
# nesta constante.
SUBSEC_BOLETIM = None

# Faixas de urgência do rótulo da seção 1, contadas da edição até o prazo.
# É rótulo de leitura, não de negócio: serve para o olho achar primeiro o que
# vence esta semana. O que manda é a DATA, que vem logo ao lado.
#
# Recalibradas em 21/08/2026: com 3 e 15 dias, os 12 prazos daquela edição saíram
# TODOS como "Longo prazo" — inclusive os PCAN, cujo prazo de 30 dias é o padrão
# do ato, não um vencimento distante. Rótulo que não separa nada não ajuda o olho.
CURTO_PRAZO_DIAS = 7
MEDIO_PRAZO_DIAS = 30

# Corte do resumo no corpo do boletim (o texto completo está no Excel e na 002A).
MAX_RESUMO = 420

# Palavras por minuto para a estimativa de leitura do cabeçalho.
PALAVRAS_POR_MINUTO = 200


# ============================================================================
#  Normalização dos itens (memória ou 002A -> um formato só)
# ============================================================================
def _txt(valor):
    """Valor de campo como texto limpo. DATE do Oracle vira DD/MM/AAAA.

    Passa por `mascarar_cpf`: todo campo do boletim entra por aqui, então este é o
    ponto único onde o CPF que veio transcrito do D.O. sai mascarado — vale para os
    itens em memória e para os relidos da 002A."""
    if valor is None:
        return ""
    if hasattr(valor, "strftime"):
        return valor.strftime("%d/%m/%Y")
    return mascarar_cpf(" ".join(str(valor).split()))


def de_002a(registros):
    """Linhas da 002A (Oracle) -> itens no formato que o monitor tem em memória."""
    mapa = {"categoria": "TIPO", "tipo_ato": "TIPO_ATO", "numero_ano": "NUMERO_ANO",
            "orgao": "ORGAO", "pessoa": "PESSOA", "cargo": "CARGO",
            "processo": "PROCESSO", "vigencia": "VIGENCIA", "resumo": "RESUMO",
            "caderno": "CADERNO", "pagina": "PAGINA", "data_ato": "DATA_ATO",
            "prazo": "PRAZO", "id_doerj": "ID_DOERJ"}
    return [{chave: _txt(r.get(col)) for chave, col in mapa.items()} for r in registros]


def _campo(item, chave):
    """Campo do item como texto limpo (aceita item de memória ou da 002A)."""
    return _txt(item.get(chave))


def _corta(texto, n=MAX_RESUMO):
    """Corta o texto no limite, sem partir palavra."""
    t = " ".join((texto or "").split())
    if len(t) <= n:
        return t
    corte = t.rfind(" ", 0, n)
    return t[:corte if corte > 0 else n] + "..."


# ============================================================================
#  Cabeçalho da edição (número, governador, secretário) — lido do índice
# ============================================================================
# Nome próprio: palavras capitalizadas, com as preposições em minúscula no meio
# ("Ricardo Couto de Castro"). Assim o casamento para no fim do nome e não
# arrasta o resto da linha do expediente.
_PALAVRA = r"(?:[A-ZÀ-Ý][a-zà-ÿ'’.-]+|d[aeo]s?|e)"
_NOME = rf"[A-ZÀ-Ý][a-zà-ÿ'’.-]+(?:\s+{_PALAVRA}){{1,5}}"

# O número da edição vem SEMPRE depois do ano romano, na tarja do IOERJ
# ("ANO LII - Nº 147"). Casar só por "Nº \d+" pegaria o primeiro decreto da
# página ("DECRETO Nº 50.426" virava edição nº 50).
# O sufixo de letra é o que distingue a EDIÇÃO EXTRA: a extra de 07/08/2026 saiu
# como "ANO LII - Nº 142-A", contra "Nº 142" da normal do mesmo dia. Sem capturar
# o "-A", os dois boletins do dia sairiam com o mesmo número de edição no topo.
_RX_NUMERO_EDICAO = re.compile(r"ANO\s+[IVXLCDM]+\s*[-–—]\s*N[ºo°]\s*(\d{1,4}(?:-[A-Z])?)\b",
                               re.IGNORECASE)
_RX_GOVERNADOR = re.compile(rf"GOVERNADOR(\s+EM\s+EXERC[ÍI]CIO)?\s+({_NOME})")
_RX_SECRETARIO = re.compile(rf"SECRETARIA DE ESTADO DE FAZENDA\s+({_NOME})")

# Só o alto da página 1 é expediente; mais abaixo já são os atos, onde
# "GOVERNADOR DO ESTADO..." aparece no corpo dos decretos.
_JANELA_EXPEDIENTE = 2500


def _paginas_um(date, extra=None):
    """[(caderno, texto da página 1)] da edição, em ordem de caderno.

    `extra` separa as duas publicações do mesmo dia (None = ambas, False = só a
    normal, True = só a extra). Sem isso, o boletim da edição extra leria o
    cabeçalho da normal, que vem primeiro na ordem alfabética do caderno."""
    if not config.DB_PATH.exists():
        return []
    filtro = {None: "", False: " AND caderno NOT LIKE ?", True: " AND caderno LIKE ?"}[extra]
    args = (date,) if extra is None else (date, f"%{config.EXTRA_CADERNO_SUFIXO}%")
    con = sqlite3.connect(config.DB_PATH)
    try:
        return con.execute(
            f"SELECT caderno, content FROM pages WHERE date=? AND page=1{filtro} "
            "ORDER BY caderno", args,
        ).fetchall()
    finally:
        con.close()


def cabecalho_edicao(date, extra=None):
    """Número da edição, governador e secretário de Fazenda -> dict.

    Tudo é OPCIONAL: se o MinerU não trouxe o cabeçalho daquele dia (acontece na
    Parte I, cujo topo às vezes vem como imagem), a chave sai vazia e a linha do
    boletim simplesmente não a menciona. Inventar número de edição seria pior."""
    dados = {"numero": "", "governador": "", "governador_exercicio": False,
             "secretario": ""}
    for caderno, texto in _paginas_um(date, extra):
        topo = (texto or "")[:_JANELA_EXPEDIENTE]
        if not dados["numero"]:
            m = _RX_NUMERO_EDICAO.search(topo)
            if m:
                dados["numero"] = m.group(1)
        if not dados["governador"]:
            m = _RX_GOVERNADOR.search(topo)
            if m:
                dados["governador"] = " ".join(m.group(2).split())
                dados["governador_exercicio"] = bool(m.group(1))
        if not dados["secretario"]:
            m = _RX_SECRETARIO.search(topo)
            if m:
                dados["secretario"] = " ".join(m.group(1).split())
    return dados


def _cadernos_com_nome(date, nome):
    """Cadernos da edição em que o nome aparece (varredura textual, sem IA).

    Serve à linha do secretário de Fazenda na seção 7. Aqui NÃO se descarta a
    menção do expediente (diferente da seção 3): a pergunta é "o nome consta
    neste caderno?", e no expediente ele consta."""
    toks = _tokens_nome(nome)
    if len(toks) < 2 or not config.DB_PATH.exists():
        return []
    con = sqlite3.connect(config.DB_PATH)
    try:
        linhas = con.execute(
            "SELECT caderno, content FROM pages WHERE date=? ORDER BY caderno, page", (date,)
        ).fetchall()
    finally:
        con.close()
    achados = []
    for caderno, content in linhas:
        curto = _caderno_curto(caderno)
        if curto in achados:
            continue
        if (_casa_nome(_norm(content), toks) >= 0
                or _casa_nome(_norm(_dehifenizar(content)), toks) >= 0):
            achados.append(curto)
    return achados


# ============================================================================
#  Datas e localização
# ============================================================================
def _data_br(date_iso):
    """'2026-08-17' -> '17/08/2026'."""
    try:
        a, m, d = str(date_iso).split("-")
        return f"{d}/{m}/{a}"
    except ValueError:
        return str(date_iso or "")


def _data_extenso(date_iso):
    """'2026-08-17' -> 'Segunda-feira, 17 de agosto de 2026' ('' se a data não presta)."""
    import datetime as dt
    try:
        d = dt.date(*(int(x) for x in str(date_iso).split("-")))
    except (ValueError, TypeError):
        return ""
    return f"{DIAS_SEMANA[d.weekday()]}, {d.day} de {MESES[d.month - 1]} de {d.year}"


def _dias_ate(date_iso, prazo_br):
    """Dias da edição até o prazo (DD/MM/AAAA). None se não der para calcular."""
    import datetime as dt
    try:
        ed = dt.date(*(int(x) for x in str(date_iso).split("-")))
        d, m, a = (int(x) for x in prazo_br.split("/"))
        return (dt.date(a, m, d) - ed).days
    except (ValueError, TypeError):
        return None


def _rotulo_prazo(date_iso, prazo_br):
    """'Curto prazo' / 'Médio prazo' / 'Longo prazo' / 'Prazo a apurar'."""
    if not prazo_br:
        return "Prazo a apurar"
    dias = _dias_ate(date_iso, prazo_br)
    if dias is None:
        return "Prazo a apurar"
    if dias <= CURTO_PRAZO_DIAS:
        return "Curto prazo"
    return "Médio prazo" if dias <= MEDIO_PRAZO_DIAS else "Longo prazo"


def _caderno_curto(caderno):
    """'Parte I (Poder Executivo)' -> 'Parte I'."""
    return _txt(caderno).split(" (")[0].strip()


def _local(item):
    """'(Parte I, p.1)' — de onde saiu o item."""
    caderno = _caderno_curto(item.get("caderno"))
    pagina = _campo(item, "pagina")
    partes = [x for x in (caderno, f"p.{pagina}" if pagina else "") if x]
    return f" ({', '.join(partes)})" if partes else ""


# ============================================================================
#  Redação dos itens
# ============================================================================
def _titulo(item):
    """Título em caixa alta do ato ('PORTARIA SUPFINF Nº 1766 DE 13/08/2026')."""
    tipo = _campo(item, "tipo_ato") or "Ato"
    numero = _campo(item, "numero_ano")
    data_ato = _campo(item, "data_ato")
    titulo = " ".join(x for x in (tipo, numero) if x).upper()
    # A data só entra se ainda não estiver no número/título (o D.O. costuma
    # trazer "PORTARIA Nº 1.160 DE 13 DE AGOSTO DE 2026" inteiro no número).
    if data_ato and data_ato not in titulo:
        titulo += f" DE {data_ato}"
    return titulo


def _cauda(item):
    """Processo + localização — o que fecha o parágrafo do item."""
    processo = _campo(item, "processo")
    return (f" — {processo}" if processo else "") + _local(item)


def _item_padrao(item):
    """Redação usada nas seções 4, 5, 6 e 7: TÍTULO — resumo — processo (caderno, p.).

    O resumo passa pelo _enxuga_comissao: onde houver rol de membros, entra a
    contagem. Quem não tem rol atravessa intacto, então vale para as quatro seções."""
    return (f'<b>{_e(_titulo(item))}</b> — '
            f'{_e(_corta(_enxuga_comissao(_campo(item, "resumo"))))}'
            f"{_e(_cauda(item))}")


def _item_prazo(item, date):
    """Redação da seção 1: rótulo de urgência, data, órgão e o que fazer."""
    prazo = _campo(item, "prazo")
    orgao = _campo(item, "orgao") or _campo(item, "tipo_ato")
    cabeca = f'<b>{_e(_rotulo_prazo(date, prazo))}</b> — [{_e(prazo or _data_br(date))}]'
    return (f'{cabeca} {_e(orgao)} — {_e(_corta(_campo(item, "resumo")))}'
            f"{_e(_cauda(item))}")


def _item_pessoal(item):
    """Redação da seção 2: o ato e a pessoa em primeiro lugar."""
    tipo = (_campo(item, "tipo_ato") or "Ato").upper()
    pessoa = _campo(item, "pessoa")
    cabeca = f"{tipo} — {pessoa}" if pessoa else tipo
    detalhes = " ".join(x for x in (_campo(item, "cargo"), _campo(item, "orgao")) if x)
    corpo = _corta(_campo(item, "resumo"))
    return (f"<b>{_e(cabeca)}</b>" + (f" — {_e(detalhes)}" if detalhes else "")
            + (f" — {_e(corpo)}" if corpo else "") + _e(_cauda(item)))


def _item_nome(item):
    """Redação da seção 3: quem, em que função, e o trecho publicado."""
    pessoa = _campo(item, "pessoa")
    cargo = _campo(item, "cargo")
    quem = pessoa + (f" ({cargo})" if cargo else "")
    return f'<b>{_e(quem)}</b> — {_e(_corta(_campo(item, "resumo")))}{_e(_local(item))}'


# ============================================================================
#  Montagem do HTML
# ============================================================================
def _e(s):
    """Escapa o que veio do D.O. (o texto tem &, < e > de tabela do MinerU)."""
    return html.escape(str(s or ""), quote=False)


def _lista(linhas_html):
    """<ul> com os itens já formatados (vazio -> string vazia)."""
    if not linhas_html:
        return ""
    itens = "".join(f"<li>{linha}</li>" for linha in linhas_html)
    return f"<ul>{itens}</ul>"


def _vazio(secao):
    return f'<p class="vazio">{_e(VAZIO[secao])}</p>'


def _por_categoria(itens, *categorias):
    """Itens de uma ou mais categorias, na ordem em que vieram."""
    alvo = {c.upper() for c in categorias}
    return [it for it in itens if _campo(it, "categoria").upper() in alvo]


_RX_PREFIXO_SEI = re.compile(r"sei-(\d{6})")


def _e_da_subsecretaria(item, sigla):
    """True se o ato de pessoal é da subsecretaria `sigla` (ver SUBSECRETARIAS).

    Basta UM dos dois sinais: o prefixo SEI do processo ou a cadeia de lotação no
    texto. Nenhum dos dois cobre sozinho (ver o comentário de SUBSECRETARIAS), e
    exigir os dois perderia tanto o Aires quanto a Regina Claudia."""
    regra = SUBSECRETARIAS.get((sigla or "").strip().upper())
    if not regra:
        return True                      # sigla desconhecida: não esconde nada

    prefixo = _RX_PREFIXO_SEI.search(_norm(_campo(item, "processo")))
    if prefixo and prefixo.group(1) in regra["prefixos"]:
        return True

    texto = _norm(" ".join(_campo(item, k) for k in ("resumo", "cargo", "orgao")))
    if any(x in texto for x in regra["negativos"]):
        return False
    return any(u in texto for u in regra["unidades"])


def _ordena_prazos(itens, date):
    """Prazos primeiro os que vencem antes; sem data, no fim."""
    def chave(it):
        dias = _dias_ate(date, _campo(it, "prazo"))
        return (1, 0) if dias is None else (0, dias)
    return sorted(itens, key=chave)


# ============================================================================
#  Pautas do Conselho de Contribuintes — N sessões, uma linha
# ============================================================================
# O Conselho publica várias pautas na mesma edição (uma por sessão) e cada uma
# virava um item com a MESMA redação: "Primeira Câmara: sessao de 08/09 as 14h
# com 3 recurso(s) pautado(s) (n. ...)". Na edição de 21/08/2026 foram 8 itens na
# seção 1 mais os MESMOS 8 no quadro do fim — 16 aparições e ~12% das palavras do
# boletim para uma informação que o leitor resolve numa linha. A área pediu a
# consolidação em 21/08/2026.
#
# A junção é de LEITURA, não de coleta: a 002A e o Excel continuam com uma linha
# por sessão, que é onde se confere horário e número de recurso. Mesmo princípio
# do recorte da seção 2 — o que o boletim não mostra continua gravado.
_RX_CONSELHO = re.compile(r"conselho de contribuintes")
_RX_QTD_RECURSOS = re.compile(r"(\d+)\s*recurso")


def _e_pauta_conselho(item):
    """True se o item é uma pauta de julgamento do Conselho de Contribuintes.

    Olha tipo_ato e orgao, que o _scan_pautas preenche, e NÃO o resumo: um ato
    qualquer que cite o Conselho de passagem não é uma pauta."""
    return bool(_RX_CONSELHO.search(
        _norm(f'{_campo(item, "tipo_ato")} {_campo(item, "orgao")}')))


def _chave_data(prazo_br):
    """'08/09/2026' -> (2026, 9, 8), para ordenar datas que estão em texto."""
    try:
        d, m, a = (int(x) for x in str(prazo_br).split("/"))
        return (a, m, d)
    except (ValueError, TypeError):
        return (9999, 99, 99)


def _consolida_conselho(prazos):
    """As N pautas do Conselho viram UM item; devolve a lista de prazos refeita.

    O item consolidado carrega o que o leitor precisa para decidir se vai atrás:
    quantas sessões, de quando a quando, quantos recursos ao todo e em que
    câmaras. Como ele é um item comum, entra na ordenação por urgência e aparece
    UMA vez na seção 1 e UMA no quadro, no lugar de oito em cada."""
    pautas = [it for it in prazos if _e_pauta_conselho(it)]
    if len(pautas) < 2:
        return list(prazos)                    # 0 ou 1 sessão: não há o que juntar

    datas, recursos, camaras, paginas = set(), 0, {}, set()
    for it in pautas:
        prazo = _campo(it, "prazo")
        if prazo:
            datas.add(prazo)
        # A quantidade está em numero_ano ("3 recurso(s)"); o resumo é a reserva.
        qtd = _RX_QTD_RECURSOS.search(_campo(it, "numero_ano") or _campo(it, "resumo"))
        recursos += int(qtd.group(1)) if qtd else 0
        # O resumo abre com a câmara ("Primeira Câmara: sessao de ...").
        resumo = _campo(it, "resumo")
        camara = resumo.split(":")[0].strip() if ":" in resumo else "Conselho de Contribuintes"
        camaras.setdefault(camara, set()).add(prazo)
        if _campo(it, "pagina"):
            paginas.add(_campo(it, "pagina"))

    datas = sorted(datas, key=_chave_data)
    periodo = (f"de {datas[0]} a {datas[-1]}" if len(datas) > 1
               else (f"em {datas[0]}" if datas else "sem data identificada"))
    por_camara = "; ".join(f"{c} em {', '.join(sorted(d, key=_chave_data))}"
                           for c, d in sorted(camaras.items()))
    paginas = sorted(paginas, key=lambda p: int(p) if p.isdigit() else 0)

    consolidado = {
        "categoria": "PRAZO_CRITICO",
        "tipo_ato": "Pauta de Julgamento do Conselho de Contribuintes",
        "orgao": "SEFAZ - Conselho de Contribuintes",
        "numero_ano": f"{recursos} recurso(s)",
        "pessoa": "", "cargo": "", "processo": "", "data_ato": "",
        "prazo": datas[0] if datas else "",
        "resumo": (f"{len(pautas)} sessões pautadas {periodo}, {recursos} recurso(s) "
                   f"no total ({por_camara}). Horários e números dos recursos no "
                   "Excel da edição."),
        "caderno": _campo(pautas[0], "caderno"),
        "pagina": f"{paginas[0]}-{paginas[-1]}" if len(paginas) > 1 else (
            paginas[0] if paginas else ""),
    }
    return [it for it in prazos if not _e_pauta_conselho(it)] + [consolidado]


# ============================================================================
#  Séries homogêneas de prazos — N atos iguais, uma linha
# ============================================================================
# O D.O. publica os atos em leva. Na edição de 21/08/2026, 10 dos 12 prazos eram
# a MESMA portaria repetida (SUPFINF 1771 a 1780, instauração de PCAN): mesma
# regra, mesmo prazo, mudando só o contribuinte e o processo. Eram ~430 das 681
# palavras da seção 1 dizendo dez vezes a mesma coisa.
#
# A junção é de LEITURA (a 002A e o Excel continuam com uma linha por ato) e
# obedece ao que o texto PROVA, não ao que o código supõe: só entram no resumo
# consolidado as frases que aparecem em TODOS os atos da série. O que varia de um
# para outro fica de fora, com o aviso de onde encontrá-lo.
_SERIE_MINIMA = 3          # abaixo disso, ler item a item é mais claro
# O corte exige maiúscula depois do ponto: sem isso, o ponto de "(Id. 4344242-0)"
# — o número funcional dos membros de comissão — parte a frase no meio do rol.
_RX_FRASE = re.compile(r"(?<=\.)\s+(?=[A-ZÀ-ÖØ-Þ])")
_RX_NUMERO_ANO = re.compile(r"^(.*?)(\d+)\s*/\s*(\d{2,4})$")


def _frases(texto):
    """Resumo -> lista de frases (corte no ponto final seguido de espaço)."""
    return [f.strip() for f in _RX_FRASE.split(_txt(texto)) if f.strip()]


def _chave_serie(item):
    """O que define 'atos iguais': mesmo tipo de ato, mesmo prazo, mesmo órgão."""
    return (_norm(_campo(item, "tipo_ato")), _campo(item, "prazo"),
            _norm(_campo(item, "orgao")))


def _faixa_numeros(itens):
    """['SUPFINF 1771/2026', ...] -> 'SUPFINF 1771 a 1780/2026' ('' se não der).

    Só forma a faixa quando TODOS têm o mesmo prefixo e o mesmo ano — senão a
    numeração seria uma invenção nossa."""
    partes = [_RX_NUMERO_ANO.match(_campo(it, "numero_ano")) for it in itens]
    if not all(partes):
        return ""
    prefixos = {p.group(1).strip() for p in partes}
    anos = {p.group(3) for p in partes}
    if len(prefixos) != 1 or len(anos) != 1:
        return ""
    numeros = sorted(int(p.group(2)) for p in partes)
    prefixo = prefixos.pop()
    return (f"{prefixo} {numeros[0]} a {numeros[-1]}/{anos.pop()}".strip()
            if numeros[0] != numeros[-1] else "")


def _prefixo_comum(textos):
    """Maior começo de frase igual em todos os textos (em palavras inteiras).

    Serve para o consolidado dizer DE QUE ATO se trata: nos PCAN, a primeira
    frase difere no contribuinte, mas todas abrem com 'Instauração de PCAN'."""
    listas = [t.split() for t in textos if t]
    if not listas:
        return ""
    comum = []
    for palavras in zip(*listas):
        if len(set(palavras)) != 1:
            break
        comum.append(palavras[0])
    return " ".join(comum).strip(" .,:;-")


def _consolida_series(prazos):
    """Junta cada série de atos iguais num item só. Devolve a lista refeita."""
    grupos = {}
    for it in prazos:
        grupos.setdefault(_chave_serie(it), []).append(it)

    saida = []
    for (_tipo, prazo, _orgao), itens in grupos.items():
        if len(itens) < _SERIE_MINIMA:
            saida.extend(itens)
            continue
        resumos = [_campo(it, "resumo") for it in itens]
        # Frases que aparecem em TODOS os atos — a regra que a série tem em comum.
        frases_de = [_frases(r) for r in resumos]
        comuns = [f for f in frases_de[0] if all(f in outras for outras in frases_de[1:])]
        abertura = _prefixo_comum(resumos)
        if not comuns and not abertura:
            saida.extend(itens)                # nada em comum: não há o que juntar
            continue
        faixa = _faixa_numeros(itens)
        titulo = " ".join(x for x in (_campo(itens[0], "tipo_ato"), faixa) if x)
        paginas = sorted({_campo(it, "pagina") for it in itens if _campo(it, "pagina")},
                         key=lambda p: int(p) if p.isdigit() else 0)
        cabeca = f"{len(itens)} atos iguais" + (f" ({titulo})" if titulo else "")
        corpo = " ".join(comuns) if comuns else ""
        saida.append({
            "categoria": "PRAZO_CRITICO",
            "tipo_ato": _campo(itens[0], "tipo_ato"),
            "numero_ano": faixa,
            "orgao": _campo(itens[0], "orgao"),
            "pessoa": "", "cargo": "", "processo": "", "data_ato": "",
            "prazo": prazo,
            "resumo": " ".join(x for x in (
                f"{cabeca} — {abertura}..." if abertura else f"{cabeca}:",
                corpo,
                "O que muda em cada ato está no Excel da edição.") if x),
            "caderno": _campo(itens[0], "caderno"),
            "pagina": (f"{paginas[0]}-{paginas[-1]}" if len(paginas) > 1
                       else (paginas[0] if paginas else "")),
        })
    return saida


# ============================================================================
#  Atos repetidos fora da seção 1 — N publicações de mesmo teor, uma linha
# ============================================================================
# Mesma ideia de _consolida_series, para as seções que não têm prazo. Na edição
# de 24/08/2026, 13 dos 22 acórdãos da seção 5 eram a MESMA frase mudando só o
# número do recurso ("Recurso de ofício nº X. ICMS. Recurso desprovido,
# confirmada a decisão do julgador de Primeira Instância").
#
# A série é definida pela FORMA do texto: dois atos entram na mesma quando o
# resumo fica idêntico depois de trocar todo número por '#'. É o teste mais
# conservador possível — os 9 acórdãos com decisão própria (decadência, recurso
# provido, mudança de vício) não casam com ninguém e saem inteiros.
_RX_NUMEROS = re.compile(r"\d+[\d.,\-/]*")


def _assinatura(texto):
    """Forma do texto: sem acento/caixa (via _norm) e sem número."""
    return " ".join(_RX_NUMEROS.sub("#", _norm(texto)).split())


def _lista_numeros(itens):
    """'20863, 20867, 21281' — os números dos atos da série.

    Existe porque _faixa_numeros não forma faixa aqui: o acórdão vem sem o ano
    no número ('20863', não '20863/2026'). Os números ficam listados porque é
    por eles que se procura o ato no D.O."""
    return ", ".join(_campo(it, "numero_ano") for it in itens
                     if _campo(it, "numero_ano"))


def _consolida_repetidos(itens):
    """Junta cada série de atos de mesmo teor num item só. Lista refeita."""
    grupos = {}
    for it in itens:
        chave = (_norm(_campo(it, "tipo_ato")), _assinatura(_campo(it, "resumo")))
        grupos.setdefault(chave, []).append(it)

    saida = []
    for grupo in grupos.values():
        if len(grupo) < _SERIE_MINIMA:
            saida.extend(grupo)
            continue
        resumos = [_campo(it, "resumo") for it in grupo]
        # Só entra no consolidado a frase que aparece em TODOS — o que varia de
        # um ato para outro (o número do recurso) fica de fora, com o aviso.
        frases_de = [_frases(r) for r in resumos]
        comuns = [f for f in frases_de[0] if all(f in outras for outras in frases_de[1:])]
        corpo = " ".join(comuns)
        if not corpo:
            # Sem frase inteira em comum sobra o começo igual, que termina no
            # ponto em que os atos divergem — portanto no meio de uma oração
            # ("...RIOPREVIDÊNCIA, valor R$"). Recua até a última vírgula: prometer
            # um valor que não vem é pior do que cortar antes.
            corpo = _prefixo_comum(resumos)
            if corpo:
                corte = max(corpo.rfind(","), corpo.rfind(";"))
                corpo = (corpo[:corte] if corte > 0 else corpo).rstrip(" ,;:-") + "..."
        if not corpo:
            saida.extend(grupo)                # nada em comum: não há o que juntar
            continue
        paginas = sorted({_campo(it, "pagina") for it in grupo if _campo(it, "pagina")},
                         key=lambda p: int(p) if p.isdigit() else 0)
        datas = {_campo(it, "data_ato") for it in grupo}
        faixa = _faixa_numeros(grupo)
        base = dict(grupo[0])
        base.update({
            "numero_ano": faixa,
            "processo": "",                    # cada ato tem o seu; nenhum vale pelo grupo
            # A data só sobrevive se for a mesma em todos: um "DE 18/08" no título
            # de uma série que vai de 04/08 a 18/08 seria informação falsa.
            "data_ato": grupo[0].get("data_ato") if len(datas) == 1 else "",
            "resumo": f"{corpo} ({len(grupo)} atos de mesmo teor: "
                      f"{faixa or _lista_numeros(grupo)}. "
                      "O que muda em cada um está no Excel da edição.)",
            "pagina": (f"{paginas[0]}-{paginas[-1]}" if len(paginas) > 1
                       else (paginas[0] if paginas else "")),
        })
        saida.append(base)
    return saida


# ============================================================================
#  Composição de comissão — a notícia é o ato, não o rol de nomes
# ============================================================================
# As portarias da CTCE trazem a comissão inteira: nome completo e Id funcional de
# cada membro. Na edição de 24/08/2026 isso era 346 das 640 palavras da seção 4 —
# mais da metade da seção para dizer QUEM compõe, quando o que muda o dia de quem
# lê é QUE houve instauração ou troca. Os nomes continuam no Excel e na 002A.
#
# A frase só é rol se ABRIR anunciando um. Reconhecê-lo pela simples MENÇÃO a um
# servidor engolia texto que não era rol nenhum — a RESOLUÇÃO 904/2026 de 20/08
# delega a uma servidora competência para movimentar conta bancária, e a ata da
# 845ª Sessão traz três decisões do colegiado; citar alguém não é listar comissão,
# e as duas viravam "Comissão de 1 membro".
_RX_ABRE_ROL = re.compile(
    r"^(comiss[ãa]o (?:integrada|composta) por|dispensad[oa]s|designad[oa]s"
    r"|novos membros)", re.I)
_RX_MEMBRO = re.compile(r"\(Id\.[^)]*\)|Corregedor(?:a)?-Auxiliar", re.I)


def _conta_membros(frase):
    """Quantos membros a frase nomeia: pelo Id funcional; sem Id, pelo cargo."""
    ids = re.findall(r"\(Id\.[^)]*\)", frase)
    return len(ids) or len(re.findall(r"Corregedor(?:a)?-Auxiliar", frase, re.I))


def _e_rol(frase):
    """A frase é um rol de membros? Tem de ABRIR como rol e nomear alguém."""
    return bool(_RX_ABRE_ROL.match(_norm(frase))) and _conta_membros(frase) > 0


def _rotulo_rol(frase, n):
    """Como contar este rol, pelo verbo que o abre."""
    baixa = _norm(frase)
    if baixa.startswith("dispensad"):
        return f"dispensados {n}"
    if baixa.startswith(("designad", "novos membros")):
        return f"designados {n}"
    return f"comissão de {n} membro" + ("s" if n > 1 else "")


def _enxuga_comissao(resumo):
    """Troca o rol de membros pela contagem, no lugar onde o rol estava.

    Resumo sem rol volta intacto, então vale para todas as seções. Frase que já
    vem contada da IA ("Dispensados 3 Corregedores-Auxiliares") não nomeia
    ninguém, não é contada de novo e atravessa como conteúdo."""
    frases = _frases(resumo)
    if not any(_e_rol(f) for f in frases):
        return resumo
    partes = [(("rol", _rotulo_rol(f, _conta_membros(f))) if _e_rol(f) else ("txt", f))
              for f in frases]
    saida, i = [], 0
    while i < len(partes):
        if partes[i][0] == "txt":
            saida.append(partes[i][1])
            i += 1
            continue
        rols = []                              # rols seguidos viram uma frase só
        while i < len(partes) and partes[i][0] == "rol":
            rols.append(partes[i][1])
            i += 1
        frase = "; ".join(rols)
        saida.append(frase[0].upper() + frase[1:] + " — nomes no Excel da edição.")
    return " ".join(saida)


def _redacao(item):
    """Redação de um item na seção 7, conforme a natureza dele.

    Nome monitorado mantém a redação da seção 3 (quem, função, trecho): "MENÇÃO
    NO D.O." como título de ato não diz nada a quem lê."""
    if _campo(item, "categoria").upper() == "NOMES_MONITORADOS":
        return _item_nome(item)
    return _item_padrao(item)


def _secao_varredura(itens, date, secretario):
    """Seção 7: uma linha por caderno + a conferência do secretário de Fazenda."""
    linhas = []
    for prefixo, rotulo in CADERNOS_VARREDURA:
        do_caderno = [it for it in itens if _caderno_curto(it.get("caderno")) == prefixo]
        if not do_caderno:
            linhas.append(f"<b>{_e(rotulo)}:</b> Sem ocorrências SEFAZ/nomes monitorados.")
            continue
        sub = "".join(f"<li>{_redacao(it)}</li>" for it in do_caderno)
        linhas.append(f"<b>{_e(rotulo)}:</b> {len(do_caderno)} ocorrência(s)."
                      f"<ul>{sub}</ul>")
    if secretario:
        onde = _cadernos_com_nome(date, secretario)
        situacao = ("Encontrado em " + ", ".join(onde) + "."
                    if onde else "Não encontrado em nenhum caderno desta edição.")
        linhas.append(f"<b>Secretário de Fazenda ({_e(secretario)}):</b> {_e(situacao)}")
    return _lista(linhas)


# Quanto do resumo entra na Descrição do quadro. Era 300 até 26/08/2026, e a
# 300 o quadro custava ~420 das ~1.970 palavras do corpo daquela edição — mais do
# que a seção 1 inteira (423). Era isso que o fazia não caber, e ele saía justo
# nas edições densas (24 e 26/08), que são as que mais pedem um fecho conferível.
# A coluna FICA (a área pediu o quadro com descrição em 26/08/2026); o que muda é
# o corte. A 120 caracteres a linha identifica o ato sem reler a seção 1, e o
# quadro custa ~200 palavras. Para voltar ao texto longo, basta subir este número
# — o teto se reequilibra sozinho, cortando mais cauda das seções 5, 7 e 4.
MAX_RESUMO_QUADRO = 120


def _tabela_prazos(itens, date):
    """O quadro que fecha o boletim: a seção 1 em ordem de vencimento.

    Sempre sai (ver _corta_no_teto); a Descrição vem cortada em
    MAX_RESUMO_QUADRO, o texto íntegro está na seção 1, no Excel e na 002A."""
    if not itens:
        return ""
    linhas = []
    for it in _ordena_prazos(itens, date):
        prazo = _campo(it, "prazo") or _data_br(date)
        orgao = _campo(it, "orgao") or _campo(it, "tipo_ato") or "SEFAZ"
        local = _local(it).strip(" ()") or "-"
        resumo = _corta(_campo(it, "resumo"), MAX_RESUMO_QUADRO)
        linhas.append(f"<tr><td>{_e(prazo)}</td><td>{_e(resumo)}</td>"
                      f"<td>{_e(orgao)}</td><td>{_e(local)}</td></tr>")
    return ("<h2>PRAZOS EM QUADRO</h2><table>"
            "<tr><th>Prazo</th><th>Descrição</th><th>Órgão / Unidade</th>"
            "<th>Caderno / Pág.</th></tr>" + "".join(linhas) + "</table>")


_RX_TAG = re.compile(r"<[^>]+>")


# A IDENTIFICAÇÃO do ato continua no boletim, mas não conta como leitura: número
# do processo, caderno/página, número do ato e data no título. Ninguém lê
# "SEI-040006/010431/2026" a 200 palavras por minuto — passa o olho para conferir.
# Contá-los penalizava justamente a edição densa em processo e página, que é a que
# mais precisa de uma estimativa honesta.
#
# TUDO AQUI É SENSÍVEL À CAIXA, e não por descuido: o título traz "DE 20/08/2026"
# em maiúscula, mas os resumos trazem "a partir de 17/08/2026" em minúscula, que é
# texto lido. Um re.IGNORECASE aqui comeria a data de vigência dos atos.
_RX_NAO_LIDO = re.compile(
    r"SEI-\S+"                         # número do processo
    r"|\(\s*Parte[^)]*\)"              # (Parte I, p.18)
    r"|\bN[ºo°]\s*[\d./-]+"            # Nº 1.164/2026, só no título
    r"|\bDE\s+\d{2}/\d{2}/\d{4}\b"     # DE 20/08/2026, só no título
)


def _minutos_leitura(corpo_html):
    """Estimativa de leitura em minutos (mínimo 1), a partir do texto sem tags.

    Desconta a identificação do ato (ver _RX_NAO_LIDO): ela fica no boletim, mas
    não é prosa que se leia."""
    texto = _RX_NAO_LIDO.sub(" ", _RX_TAG.sub(" ", corpo_html))
    return max(1, round(len(texto.split()) / PALAVRAS_POR_MINUTO))


# ============================================================================
#  Teto de leitura — o boletim cabe em TETO_MINUTOS, o resto vai para o Excel
# ============================================================================
# Pedido da área: o e-mail não pode passar de 10 minutos de leitura. Aparar texto
# não resolve — medido nas edições de agosto/2026, o corpo varia de 7 a 16 min e a
# seção que incha muda de dia para dia (a 5 em 24/08, a 7 em 18/08, a 2 em 20 e
# 21/08, a 1 em 19/08). Só limite rígido garante o teto todo dia.
#
# NÃO se perde conteúdo: cada item cortado continua íntegro no Excel da edição e
# na 002A. O boletim deixa de ser o arquivo e passa a ser a chamada dele.
#
# O corte é pela CAUDA da seção, e o aviso diz isso. Seria pior fingir um ranking
# de relevância: os campos do item não sustentam um — filtrar a seção 5 por "não é
# da SEFAZ", por exemplo, economiza 1,4 min e joga fora decretos de crédito
# suplementar e portarias de orçamento, que são o que mais interessa à casa.
TETO_MINUTOS = 10

# Quantas palavras contadas o corpo pode ter. Espelha _minutos_leitura: o que não
# conta como leitura (SEI, página, número e data do ato) também não ocupa o teto.
_TETO_PALAVRAS = TETO_MINUTOS * PALAVRAS_POR_MINUTO

# Nenhuma seção cedente desaparece por inteiro: um "0 itens" seria lido como
# "não houve movimentação", que é diferente de "não coube".
_MIN_ITENS_SECAO = 3


def _custo(linha_html):
    """Palavras que CONTAM (ver _RX_NAO_LIDO) numa linha já renderizada."""
    return len(_RX_NAO_LIDO.sub(" ", _RX_TAG.sub(" ", linha_html)).split())


def _aviso_transbordo(n):
    """A linha que fecha uma seção que não coube inteira."""
    return (f'<p class="contagem">Mais {n} ato(s) desta seção nesta edição. '
            f"O texto completo de cada um está na planilha anexa e na base (002A); "
            f"foram omitidos aqui para o boletim caber em {TETO_MINUTOS} minutos "
            f"de leitura.</p>")


def _corta_no_teto(cedentes, custo_fixo):
    """Reduz o que pode ceder até o corpo caber no teto.

    `cedentes` é [(chave, itens, renderizador)] NA ORDEM EM QUE CEDEM espaço;
    `custo_fixo` é o que nunca se corta (seções 1, 2, 3 e 6, o quadro de prazos e
    os cabeçalhos). Devolve {chave: (itens_mantidos, n_omitidos)}.

    O QUADRO DE PRAZOS NÃO CEDE. Até 26/08/2026 ele era a primeira coisa a sair,
    por duplicar a seção 1 — e saía exatamente nas edições densas (24 e 26/08),
    que são as que mais pedem um fecho conferível. A área pediu o quadro fixo em
    26/08/2026. Parte do espaço veio da própria tabela: com a Descrição cortada
    mais curta ela custa ~200 palavras em vez de ~420 (ver MAX_RESUMO_QUADRO); o
    resto sai da cauda das seções 5, 7 e 4, como qualquer outro excesso."""
    custos = {ch: [_custo(render(it)) for it in itens] for ch, itens, render in cedentes}
    total = custo_fixo + sum(sum(v) for v in custos.values())
    saida = {ch: (list(itens), 0) for ch, itens, _r in cedentes}
    if total <= _TETO_PALAVRAS:
        return saida
    for chave, itens, _render in cedentes:
        mantidos, omitidos, custo = list(itens), 0, custos[chave]
        while total > _TETO_PALAVRAS and len(mantidos) > _MIN_ITENS_SECAO:
            mantidos.pop()                     # sai a última, a cauda da seção
            total -= custo[len(mantidos)]
            omitidos += 1
        saida[chave] = (mantidos, omitidos)
        if total <= _TETO_PALAVRAS:
            break
    return saida


def render(date, itens, n_monitorados=None, parcial=False, blocos_falha=0,
           blocos_total=0, subsec=SUBSEC_BOLETIM, extra=False):
    """Monta o HTML do boletim da edição. Devolve a página inteira, em texto.

    `n_monitorados` é quantos nomes estavam VIGENTES na 002N naquela edição — o
    denominador da seção 3 ("2 de 19 nomes"). Se não vier, é lido do banco.
    `parcial` marca no topo que a rodada da IA teve bloco falhado: um boletim
    incompleto que se anuncia completo é pior do que não ter boletim.
    `subsec` recorta APENAS a seção 2 por subsecretaria (None = SEFAZ inteira).
    `extra` marca que este é o boletim da EDIÇÃO EXTRA — um COMPLEMENTO ao do
    dia, não um substituto."""
    itens = list(itens or [])
    if n_monitorados is None:
        try:
            monitorados, _origem = _carregar_monitorados(date)
            n_monitorados = len(monitorados)
        except Exception:  # noqa: BLE001 - o boletim não cai por causa do denominador
            n_monitorados = 0

    cab = cabecalho_edicao(date, extra=True if extra else None)
    nomes = _por_categoria(itens, "NOMES_MONITORADOS")
    # Duas juntadas antes de ordenar: as pautas do Conselho (que têm datas
    # diferentes entre si) e as séries de atos iguais (mesmo tipo, prazo e órgão).
    prazos = _ordena_prazos(
        _consolida_series(
            _consolida_conselho(_por_categoria(itens, "PRAZO_CRITICO"))), date)
    # Seção 2: guarda o total SEFAZ antes de recortar, para poder dizer quantos
    # atos ficaram fora. Sem esse número, uma seção 2 vazia não se distingue de
    # uma edição sem movimentação nenhuma nem de uma rodada que falhou.
    regra_subsec = SUBSECRETARIAS.get((subsec or "").strip().upper())
    pessoal_sefaz = _por_categoria(itens, "MOVIMENTACAO_PESSOAL")
    pessoal = ([it for it in pessoal_sefaz if _e_da_subsecretaria(it, subsec)]
               if regra_subsec else pessoal_sefaz)
    fora_do_recorte = len(pessoal_sefaz) - len(pessoal)
    # Seções 4 e 5 também vêm em leva (13 acórdãos de mesmo teor em 24/08/2026),
    # então passam pela mesma juntada de séries da seção 1 — pela FORMA do texto.
    controle = _consolida_repetidos(_por_categoria(itens, "DESTAQUE_CONTROLE_INTERNO"))
    executivas = _consolida_repetidos(_por_categoria(itens, "OBSERVACAO_EXECUTIVA"))
    expediente = _por_categoria(itens, "EXPEDIENTE_PONTO_FACULTATIVO")
    # A seção 7 é "o que saiu nos demais cadernos": tanto os itens que o monitor
    # já classificou por caderno (6/7/8 do Excel) quanto qualquer outro item que
    # tenha vindo de IB/II/IV/V — inclusive nome monitorado.
    prefixos = {p for p, _r in CADERNOS_VARREDURA}
    varredura = [it for it in itens if _caderno_curto(it.get("caderno")) in prefixos]

    achados = sorted({_campo(it, "pessoa") for it in nomes if _campo(it, "pessoa")})
    corpo = []

    corpo.append(f"<h2>{_e(TITULOS[1])}</h2>")
    corpo.append(_lista([_item_prazo(it, date) for it in prazos]) or _vazio(1))

    titulo2 = TITULOS[2] + (f" — {subsec.upper()}" if regra_subsec else "")
    corpo.append(f"<h2>{_e(titulo2)}</h2>")
    if regra_subsec:
        # O recorte tem de estar VISÍVEL no boletim: a SUBCINT é ~3% da movimentação
        # de pessoal da SEFAZ, então na maioria dos dias esta seção sai vazia, e quem
        # lê precisa saber que é recorte, não falha.
        aviso_recorte = (f'Recorte: {regra_subsec["nome"]} ({subsec.upper()}).'
                         + (f" {fora_do_recorte} ato(s) de pessoal da SEFAZ nesta "
                            "edição ficaram fora deste recorte."
                            if fora_do_recorte else ""))
        corpo.append(f'<p class="contagem">{_e(aviso_recorte)}</p>')
    vazio2 = (f'<p class="vazio">Nenhuma nomeação ou exoneração na '
              f'{_e(subsec.upper())} nesta edição.</p>' if regra_subsec else _vazio(2))
    corpo.append(_lista([_item_pessoal(it) for it in pessoal]) or vazio2)

    corpo.append(f"<h2>{_e(TITULOS[3])} ({n_monitorados} nomes)</h2>")
    corpo.append(f'<p class="contagem">{len(achados)} de {n_monitorados} '
                 f"nome(s) encontrado(s) nesta edição"
                 f'{" — " + _e("; ".join(achados)) if achados else ""}.</p>')
    if nomes:
        corpo.append(_lista([_item_nome(it) for it in nomes]))

    # Teto de leitura: o que já foi montado (seções 1 a 3) é intocável; as seções
    # 5, 7 e 4 cedem espaço nessa ordem, da cauda para o começo, até o corpo caber
    # em TETO_MINUTOS. O que sai continua íntegro no Excel e na 002A.
    quadro = _tabela_prazos(prazos, date)
    # A reserva é o que ainda entra no corpo DEPOIS desta conta: os títulos das
    # seções 4 a 7 e, se alguma ceder, o aviso de transbordo. Sem reservá-los o
    # corpo passa do teto por algumas dezenas de palavras — em 21/08/2026 fechava
    # em 2.115, dezesseis palavras acima do que ainda arredonda para 10 min.
    reserva = (sum(_custo(f"<h2>{TITULOS[n]}</h2>") for n in (4, 5, 6, 7))
               + 3 * _custo(_aviso_transbordo(99)))
    fixo = sum(_custo(x) for x in corpo) + reserva + _custo(quadro)
    coube = _corta_no_teto(
        [("5", executivas, _item_padrao),
         ("7", varredura, _redacao),
         ("4", controle, _item_padrao)], fixo)
    controle, omit4 = coube["4"]
    executivas, omit5 = coube["5"]
    varredura, omit7 = coube["7"]

    corpo.append(f"<h2>{_e(TITULOS[4])}</h2>")
    corpo.append(_lista([_item_padrao(it) for it in controle]) or _vazio(4))
    if omit4:
        corpo.append(_aviso_transbordo(omit4))

    corpo.append(f"<h2>{_e(TITULOS[5])}</h2>")
    corpo.append(_lista([_item_padrao(it) for it in executivas]) or _vazio(5))
    if omit5:
        corpo.append(_aviso_transbordo(omit5))

    corpo.append(f"<h2>{_e(TITULOS[6])}</h2>")
    corpo.append(_lista([_item_padrao(it) for it in expediente]) or _vazio(6))

    corpo.append(f"<h2>{_e(TITULOS[7])}</h2>")
    corpo.append(_secao_varredura(varredura, date, cab["secretario"]))
    if omit7:
        corpo.append(_aviso_transbordo(omit7))
    corpo.append(quadro)
    corpo_html = "\n".join(corpo)

    # Linha de identificação da edição, como no e-mail.
    identificacao = [x for x in (
        f"Edição Nº {cab['numero']}" if cab["numero"] else "",
        _data_extenso(date) or _data_br(date),
        (f"Governador{' em Exercício' if cab['governador_exercicio'] else ''}: "
         f"{cab['governador']}") if cab["governador"] else "",
        f"Secretário de Fazenda: {cab['secretario']}" if cab["secretario"] else "",
    ) if x]

    aviso = ""
    # A tarja da edição extra vem PRIMEIRO e é obrigatória: a extra costuma ter
    # poucas páginas, então quase todas as seções saem com "Nenhum ... nesta
    # edição". Sem dizer que é complemento, este boletim se parece com um boletim
    # diário que falhou.
    if extra:
        aviso += ('<p class="extra">EDIÇÃO EXTRA — este boletim cobre APENAS o caderno '
                  f"extra publicado em {_e(_data_br(date))}. O boletim da edição normal "
                  "do dia foi enviado à parte; as seções vazias aqui significam que o "
                  "caderno extra não trouxe nada daquele tipo.</p>")
    if parcial:
        aviso += ('<p class="parcial">BOLETIM PARCIAL — '
                  f"{blocos_falha} de {blocos_total} blocos falharam na leitura por IA. "
                  "Seções podem estar incompletas; regenere quando a IA normalizar.</p>")

    titulo = ("[DOERJ] Monitoramento SEFAZ" + (" - EDICAO EXTRA" if extra else "")
              + f" - {_data_br(date)}")
    return f"""<!DOCTYPE html><html lang="pt-BR"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_e(titulo)}</title>
<style>
 body{{font-family:Segoe UI,Calibri,Arial,sans-serif;color:#2b2b2b;max-width:900px;
      margin:0 auto;padding:24px;line-height:1.55}}
 h1{{font-size:21px;color:{_AZUL};margin:0 0 4px}}
 .sub{{color:{_CINZA};font-size:13.5px;margin:0 0 22px}}
 h2{{font-size:15px;color:{_AZUL};margin:26px 0 10px;padding-bottom:5px;
     border-bottom:2px solid {_AZUL}}}
 ul{{margin:0;padding-left:20px}} li{{margin-bottom:10px;font-size:14px}}
 ul ul{{margin-top:8px}}
 .vazio{{font-style:italic;color:{_CINZA};font-size:14px;margin:0}}
 .contagem{{font-style:italic;color:{_CINZA};font-size:14px;margin:0 0 10px}}
 .parcial{{background:#fdecea;border-left:4px solid #b00020;color:#b00020;
          font-weight:bold;font-size:13.5px;padding:9px 12px;margin:0 0 18px}}
 .extra{{background:#fff4e0;border-left:4px solid #b06a00;color:#7a4a00;
        font-weight:bold;font-size:13.5px;padding:9px 12px;margin:0 0 18px}}
 table{{border-collapse:collapse;margin-top:8px;font-size:13px;width:100%}}
 td,th{{border:1px solid #DCE3EA;padding:6px 10px;text-align:left;vertical-align:top}}
 th{{background:#F0F5F9;color:#41525F}}
 .rodape{{margin-top:28px;color:{_CINZA};font-size:12px;border-top:1px solid #DCE3EA;
         padding-top:12px}}
</style></head><body>
<h1>[DOERJ] Monitoramento SEFAZ-RJ{" — EDIÇÃO EXTRA" if extra else ""}</h1>
<div class="sub">{_e(" | ".join(identificacao))} · leitura ~{_minutos_leitura(corpo_html)} min</div>
{aviso}
{corpo_html}
<div class="rodape">Ajude a aprimorar este Boletim. O Diário Oficial é um documento variável;
contamos com múltiplas percepções para melhorar continuamente a capacidade de síntese e a
sensibilidade entre o que é publicado e o que é relevante para o nosso trabalho setorial. Caso
note uma omissão, um falso positivo ou algo que possa ser melhor classificado, responda a este
e-mail com sua sugestão.<br><br>
Gerado automaticamente a partir da leitura do DOERJ (MinerU + IA) — {len(itens)} item(ns)
estruturado(s) na edição de {_e(_data_br(date))}.</div>
</body></html>"""


def gerar(date=None, itens=None, destino=None, n_monitorados=None, parcial=False,
          blocos_falha=0, blocos_total=0, subsec=SUBSEC_BOLETIM, extra=False):
    """Escreve `relatorios/boletim_<data>.html` e devolve o caminho.

    Sem `itens`, lê a edição na 002A do Oracle (é o caminho da linha de comando).
    Rodada parcial sai como `boletim_<data>_PARCIAL.html`, pela mesma razão do
    Excel: não sobrescrever um boletim completo por um pela metade.
    `subsec` recorta a seção 2 (None = SEFAZ inteira).
    `extra` gera o boletim da EDIÇÃO EXTRA (`boletim_<data>_EXTRA.html`), que é um
    arquivo à parte — o da edição normal do dia continua intacto."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not date:
        from search import list_dates
        datas = list_dates()
        if not datas:
            sys.exit("[ERRO] nenhuma edicao no indice.")
        date = datas[0]

    if itens is None:
        if not oracle_db.configurado() or not config.oracle_settings().get("table_monitor"):
            sys.exit("[ERRO] Oracle/002A nao configurado no .env - sem itens para o boletim.")
        registros = oracle_db.listar_monitoramento(date, extra=extra)
        if not registros:
            sys.exit(f"[ERRO] a 002A nao tem registro da edicao {date}"
                     f"{' (EDICAO EXTRA)' if extra else ''}. "
                     "Rode antes: python src/monitor_estruturado.py")
        print(f"[boletim] {len(registros)} registro(s) lidos da 002A para a edicao {date}"
              f"{' (EDICAO EXTRA)' if extra else ''}")
        itens = de_002a(registros)

    destino = Path(destino) if destino else config.RELATORIOS_DIR
    destino.mkdir(parents=True, exist_ok=True)
    arquivo = (destino /
               f"boletim_{date}{'_EXTRA' if extra else ''}{'_PARCIAL' if parcial else ''}.html")
    arquivo.write_text(
        render(date, itens, n_monitorados=n_monitorados, parcial=parcial,
               blocos_falha=blocos_falha, blocos_total=blocos_total, subsec=subsec,
               extra=extra),
        encoding="utf-8")
    print(f"[ok] {arquivo}")
    return arquivo


def main():
    """Linha de comando: refaz o boletim de uma edição a partir da 002A."""
    ap = argparse.ArgumentParser(
        description="Gera o boletim HTML (formato do e-mail) de uma edicao do DOERJ.")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--dir", default=None, help="Pasta de destino (padrao: <projeto>/relatorios)")
    ap.add_argument("--extra", action="store_true",
                    help="Boletim da EDICAO EXTRA do dia (boletim_<data>_EXTRA.html)")
    ap.add_argument("--subsecretaria", default=SUBSEC_BOLETIM,
                    help="Recorta a secao 2 por subsecretaria (padrao: "
                         f"{SUBSEC_BOLETIM or 'TODAS, a SEFAZ inteira'}). "
                         f"Siglas: {', '.join(SUBSECRETARIAS)}")
    args = ap.parse_args()
    subsec = args.subsecretaria
    if (subsec or "").strip().upper() in ("TODAS", "TODOS", "SEFAZ", "NENHUMA", ""):
        subsec = None
    elif subsec.strip().upper() not in SUBSECRETARIAS:
        sys.exit(f"[ERRO] subsecretaria '{subsec}' desconhecida. "
                 f"Use TODAS ou uma de: {', '.join(SUBSECRETARIAS)}")
    gerar(date=args.date, destino=args.dir, subsec=subsec, extra=args.extra)


if __name__ == "__main__":
    main()

