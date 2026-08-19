
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
import difflib
import json
import re
import sqlite3
import sys
import time
import unicodedata
from pathlib import Path

import config
import oracle_db
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

_CATEGORIAS_VALIDAS = list(CATEGORIA_SECAO.keys())

# Cadernos cuja SEÇÃO é definida pelo próprio caderno, não pelo julgamento da IA:
# as seções 6, 7 e 8 do relatório SÃO "o que saiu na Parte IB / II / IV-V". O
# caderno é conhecido de forma determinística (veio do índice), então vale mais
# que a categoria devolvida pelo modelo.
CADERNO_CATEGORIA = {
    "ib": "TCE_SEFAZ",                  # seção 6
    "ii": "LEGISLATIVO_FAZENDARIO",     # seção 7
    "iv": "MUNICIPALIDADES_PEDIDO",     # seção 8
    "v": "MUNICIPALIDADES_PEDIDO",      # seção 8
}

_RX_CADERNO = re.compile(r"^parte\s+(ib|iv|ii|i|v)\b")


def _categoria_do_caderno(caderno):
    """Categoria obrigatória do caderno, ou None se o caderno não impõe seção.

    Só Parte I fica livre (tem atos de todas as naturezas); IB, II, IV e V têm
    seção própria no relatório."""
    m = _RX_CADERNO.match(_norm(caderno or "").strip())
    return CADERNO_CATEGORIA.get(m.group(1)) if m else None


def _forcar_categoria_por_caderno(itens):
    """Corrige a categoria dos itens de IB/II/IV/V pelo caderno. Devolve nº de correções.

    Sem isto, um ato da Parte V que a IA classificou como OBSERVACAO_EXECUTIVA cai
    na seção 4 e a seção 8 sai VAZIA — foi o que aconteceu com o SINAVAL e com a
    declaração da FIRJAN na edição de 31/07/2026, que o monitoramento de referência
    trouxe na seção 8.

    NOMES_MONITORADOS é exceção: vem da varredura determinística e vale em qualquer
    caderno (a seção 3 é por pessoa, não por caderno)."""
    n = 0
    for it in itens:
        if it.get("categoria") == "NOMES_MONITORADOS":
            continue
        obrigatoria = _categoria_do_caderno(it.get("caderno"))
        if obrigatoria and it.get("categoria") != obrigatoria:
            it["categoria"] = obrigatoria
            n += 1
    return n


# Vocabulário fechado de TIPO_ATO para movimentação de pessoal. O rótulo do ato é
# derivado do RESUMO, não aceito da IA: o SYSTEM descreve a `categoria` em detalhe
# mas nunca definiu `tipo_ato`, e o modelo preenchia por conta própria. Na edição de
# 07/08/2026 a exoneração de ANTONIO CARLOS DE BARROS BASILIO (SEI-040008/000792/2026)
# abria uma sequência de 8 nomeações na p.3 e saiu rotulada "Nomeação" — com o resumo
# dizendo "Exoneração do cargo em comissão". A 001A, cujo rótulo vem do código e não
# do modelo, acertou o mesmo ato. Mesma regra do prazo: o que o código consegue
# conferir, o código confere.
#
# Cada entrada é (rótulo canônico, radicais que o denunciam no texto). A ordem importa
# só para o desempate abaixo; o critério real é qual radical aparece PRIMEIRO no resumo.
_TIPOS_PESSOAL = [
    ("Exoneração",    ("exonera",)),
    ("Nomeação",      ("nomea", "nomeia")),
    ("Designação",    ("designa",)),
    ("Cessão",        ("cede ", "cessao", "cedid")),
    ("Remoção",       ("remove", "remocao", "removid")),
    ("Transferência", ("transfer",)),
    ("Aposentadoria", ("aposenta",)),
    ("Dispensa",      ("dispensa",)),
]


# Marcadores de ato que DESFAZ outro ato. Sem isto a regra abaixo inverteria o
# sentido: "Cessados os efeitos da designação para substituir o Titular da JRF"
# (RENATA CARNEIRO DA SILVA RIBEIRO, 28/07/2026) casa o radical "designa" e viraria
# "Designação" — o oposto do que o D.O. publicou. Quando um destes vem ANTES do
# radical do ato, o código se cala e mantém o rótulo da IA, que nesses casos veio
# certo ("Cessação de Designação").
_DESFAZ = ("cessad", "cessa os efeitos", "cessacao", "torna sem efeito",
           "tornado sem efeito", "tornar sem efeito", "revoga", "anula")


def _tipo_pelo_resumo(resumo):
    """Rótulo canônico do ato a partir do resumo, ou None se não der para afirmar.

    Vale o radical que aparece MAIS CEDO no texto: o resumo de uma exoneração cita a
    nomeação seguinte ("Exoneração ... em vaga a ser preenchida por nomeação"), e vice-
    versa. O ato é o que a frase abre, não o que ela menciona de passagem.

    Devolve None quando o ato é a CESSAÇÃO/REVOGAÇÃO de outro ato — o vocabulário não
    tem rótulo para isso e um palpite aqui inverteria o sentido."""
    txt = _norm(resumo)
    if not txt.strip():
        return None
    achados = []
    for rotulo, radicais in _TIPOS_PESSOAL:
        pos = min((txt.find(r) for r in radicais if txt.find(r) >= 0), default=-1)
        if pos >= 0:
            achados.append((pos, rotulo))
    if not achados:
        return None
    pos_ato = min(achados)[0]
    desfaz = min((txt.find(d) for d in _DESFAZ if txt.find(d) >= 0), default=-1)
    if 0 <= desfaz < pos_ato:
        return None
    return min(achados)[1]


def _ato_no_texto(rotulo, resumo):
    """True se algum radical do ato `rotulo` aparece no resumo (em qualquer posição)."""
    txt = _norm(resumo)
    radicais = dict(_TIPOS_PESSOAL).get(rotulo, ())
    return any(r in txt for r in radicais)


def _normalizar_tipo_ato(itens):
    """Reescreve o `tipo_ato` das movimentações de pessoal a partir do resumo.

    Devolve (nº de inversões corrigidas, nº de rótulos só padronizados). A separação
    existe para o log: inversão é erro de leitura da IA e merece destaque; grafia
    ('NOMEAÇÃO', 'Nomeação para cargo em comissão') é só ruído de vocabulário, que
    antes chegava ao banco — a 002A acumulou 21 rótulos distintos para ~8 tipos.

    Só age quando o resumo nomeia um ato do vocabulário. Item cujo resumo não casa
    (portaria de lotação, ato do secretário) fica com o rótulo da IA: sem evidência
    no texto, o palpite do modelo é melhor do que um rótulo inventado pelo código."""
    invertidos = padronizados = 0
    for it in itens:
        if (it.get("categoria") or "").strip().upper() != "MOVIMENTACAO_PESSOAL":
            continue
        canon = _tipo_pelo_resumo(it.get("resumo"))
        if not canon:
            continue
        atual = (it.get("tipo_ato") or "").strip()
        if atual == canon:
            continue
        it["tipo_ato"] = canon
        # Inversão = o rótulo antigo nomeava um ato que NÃO ESTÁ no resumo (a IA disse
        # "Nomeação" para um texto que só fala em exoneração). Se o resumo cita os dois
        # — o ato composto "Exoneração do cargo X e nomeação para o cargo Y" — o rótulo
        # antigo era defensável e isto é só padronização, não erro de leitura.
        outro = _tipo_pelo_resumo(atual)
        if outro not in (None, canon) and not _ato_no_texto(outro, it.get("resumo")):
            invertidos += 1
            print(f"[monitor]   tipo_ato corrigido: a IA disse '{atual}', o resumo diz "
                  f"'{canon}' ({(it.get('pessoa') or '?')[:60]})")
        else:
            padronizados += 1
    return invertidos, padronizados


def _canon_categoria(cat):
    """Normaliza a categoria vinda da IA para o conjunto canonico. O modelo as vezes
    erra a grafia (ex.: 'OBSERVACAO_EXECUTIVE' sem o 'A'); aqui casamos pelo mais
    parecido. Categoria irreconhecivel cai em OBSERVACAO_EXECUTIVA (nunca descarta o
    item nem cria um TIPO invalido no banco / seção fantasma no Excel)."""
    c = (cat or "").strip().upper().replace(" ", "_").replace("-", "_")
    if c in CATEGORIA_SECAO:
        return c
    aprox = difflib.get_close_matches(c, _CATEGORIAS_VALIDAS, n=1, cutoff=0.8)
    return aprox[0] if aprox else "OBSERVACAO_EXECUTIVA"


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

COLUNAS = ["Tipo do Ato", "Numero/Ano", "Orgao", "Pessoa", "Cargo", "Processo",
           "Vigencia", "Data do Ato", "Prazo", "Resumo", "Caderno", "Pagina"]
CHAVES = ["tipo_ato", "numero_ano", "orgao", "pessoa", "cargo", "processo",
          "vigencia", "data_ato", "prazo", "resumo", "caderno", "pagina"]

SYSTEM = (
    "Voce e analista senior da SEFAZ-RJ com 15 anos lendo o DOERJ. Do TEXTO fornecido, "
    "extraia APENAS atos/publicacoes relevantes para a SEFAZ-RJ (Fazenda), Rioprevidencia ou "
    "Fundo Unico de Previdencia.\n\n"
    "SIGLAS QUE SAO SEFAZ (trate como Fazenda): SEFAZ, SEFAZ-RJ, Fazenda, Secretaria de Estado "
    "de Fazenda, SUPFINF, SUPAT, SUPBF, SUPCC, SUPCT, SUPDIEF, SUPAQTIC, Subsecretaria de "
    "Estado de Receita (SER), Subsecretaria de Controle Interno, Subsecretaria do Tesouro, "
    "Conselho de Contribuintes (CC), Junta de Revisao Fiscal (JRF), Auditoria Fiscal.\n\n"
    "NAO SAO SEFAZ (EXCLUA o ato, a menos que seja EXPLICITAMENTE conjunto/bilateral com a "
    "SEFAZ): JUCERJA, LOTERJ, SEDEC/SEDEICS (Desenvolvimento Economico), Casa Civil, DETRAN, "
    "INEA, Saude, Educacao (SEEDUC), Seguranca/Policia (SEPOL/PM/CBMERJ), Planejamento "
    "(SEPLAG), CGE/Controladoria Geral do Estado, SEOBRAS/SEIOP (Obras), DER-RJ, Ciencia e "
    "Tecnologia, Cultura, Esporte, Ambiente, AGENERSA e demais secretarias/orgaos sem efeito "
    "direto na Fazenda. Municipio (prefeitura) so entra se a SEFAZ for parte.\n"
    "REGRA DE OURO (evita falso positivo): julgue CADA ato pelo ORGAO DELE, nao pela pagina. "
    "Uma pagina pode misturar atos da SEFAZ com atos de outros orgaos; extraia SO os da SEFAZ. "
    "Atos INTERNOS de outro orgao (aposentadoria, PAD, sindicancia, PAR, nomeacao/exoneracao de "
    "servidor de OUTRO orgao, PCAN/pauta de outro orgao) NAO entram, mesmo que a mesma pagina "
    "tenha materia da SEFAZ, e mesmo que o servidor um dia tenha passado pela Fazenda. Excecao: "
    "cessao/permuta em que a SEFAZ, o Rioprevidencia OU o Fundo Unico de Previdencia e a origem "
    "ou o destino do servidor. Vale mesmo que o OUTRO lado seja um orgao de fora: a cessao de um "
    "servidor da SEEDUC PARA o Rioprevidencia ENTRA (o destino esta no escopo).\n\n"
    "Responda EXCLUSIVAMENTE com um objeto JSON valido (sem texto antes/depois, sem ```), no "
    'formato: {"itens":[{"categoria":"","tipo_ato":"","numero_ano":"","orgao":"","pessoa":"",'
    '"cargo":"","processo":"","vigencia":"","data_ato":"","prazo":"","dias_carencia":"",'
    '"dias_prazo":"","dias_uteis":"","resumo":"","pagina":""}]}\n\n'
    "categoria = UMA de: PRAZO_CRITICO | MOVIMENTACAO_PESSOAL | NOMES_MONITORADOS | "
    "OBSERVACAO_EXECUTIVA | DESTAQUE_CONTROLE_INTERNO | EXPEDIENTE_PONTO_FACULTATIVO | "
    "TCE_SEFAZ | LEGISLATIVO_FAZENDARIO | MUNICIPALIDADES_PEDIDO.\n"
    "tipo_ato = o que o ato E, em 1-3 palavras (Exoneracao, Nomeacao, Designacao, Cessao, "
    "Remocao, Transferencia, Aposentadoria, Dispensa, Portaria, Decreto, Termo Aditivo, "
    "Apostila, Despacho, Edital de Convocacao, Pregao Eletronico, Projeto de Lei...). "
    "ATENCAO nos atos de pessoal: uma pagina do D.O. traz varias nomeacoes seguidas e UMA "
    "exoneracao no meio - leia o VERBO DO ATO que voce esta extraindo, nao o dos vizinhos. "
    "Se o resumo comeca com 'Exoneracao de...', tipo_ato e 'Exoneracao'.\n"
    "Guia de categoria:\n"
    "- PRAZO_CRITICO: ato SEFAZ/Rioprevidencia que gera acao/prazo (sessao de julgamento, "
    "vencimento, verificacao, disponibilizacao de acordao no portal, licenca com inicio/fim, "
    "recurso de concurso/processo, convocacao de beneficiario/herdeiro com prazo). INCLUA aqui "
    "licitacoes/pregoes/concorrencias/chamamentos em que a SEFAZ e a contratante/promotora, "
    "quando houver data de abertura de sessao, entrega/abertura de propostas ou habilitacao "
    "(mesmo que quem cumpra o prazo sejam os licitantes: a SEFAZ conduz a sessao). So se o "
    "titular OU o condutor do prazo for a SEFAZ/Rioprevidencia (nao terceiro).\n"
    "NAO E PRAZO_CRITICO (vao para OBSERVACAO_EXECUTIVA): ato JA CONSUMADO sem acao pendente - "
    "deferimento/concessao de isencao, concessao de aposentadoria/pensao, defesa ja julgada. "
    "So e PRAZO_CRITICO se houver uma ACAO A CUMPRIR ate uma data futura.\n"
    "PRAZO: 'prazo' e uma DATA (DD/MM/AAAA) ou \"\". NUNCA escreva texto relativo nele "
    "('30 dias', '15 dias apos a publicacao'): a coluna e DATE e o valor se perde.\n"
    "  a) o texto da a DATA-LIMITE explicita (sessao, vencimento, abertura) -> ponha em "
    "'prazo' e deixe os tres campos de dias vazios;\n"
    "  b) o prazo e CONTADO EM DIAS -> NAO CALCULE. Deixe 'prazo' vazio e preencha:\n"
    "     'dias_carencia' = dias ate o prazo COMECAR a correr ('apos 15 dias da publicacao "
    "inicia-se o prazo' -> 15). Se comeca na propria publicacao, 0.\n"
    "     'dias_prazo'    = o tamanho do prazo em si ('prazo de 60 dias para pagamento' -> 60).\n"
    "     'dias_uteis'    = 'sim' se o texto disser dias UTEIS; senao 'nao'.\n"
    "     Exemplo: 'Apos 15 dias da publicacao inicia-se o prazo de 60 dias para pagamento' -> "
    "dias_carencia=15, dias_prazo=60, dias_uteis='nao', prazo=\"\". Quem soma e o sistema, a "
    "partir da data da edicao - nao faca a conta, so leia os numeros;\n"
    "  c) sem data-limite futura e sem prazo em dias -> nao e PRAZO_CRITICO, reclassifique.\n"
    "SEMPRE descreva a contagem no 'resumo' ('60 dias para pagamento, iniciados 15 dias apos a "
    "publicacao') - o leitor precisa conferir a regra.\n"
    "- MOVIMENTACAO_PESSOAL: nomeacao/exoneracao/designacao/remocao/TRANSFERENCIA/permuta/"
    "cessao/afastamento de servidor fazendario (ou cargo de comando de outro poder). "
    "TRANSFERENCIA inclui mudanca de lotacao ou de colegiado - ex.: Auditor Tributario "
    "transferido de uma Turma de Julgamento para outra na Junta de Revisao Fiscal (Portaria "
    "JRF): gere UM item por servidor transferido. Membros de comissao NAO contam.\n"
    "- NOMES_MONITORADOS: ato concreto sobre Guilherme Merces (Secretario de Fazenda) ou sobre "
    "o Chefe de Gabinete / Subsecretario / Subsecretario Adjunto da SEFAZ.\n"
    "- OBSERVACAO_EXECUTIVA: decretos com impacto orcamentario (credito suplementar/dotacao), "
    "leis e leis complementares de interesse fazendario (LDO, LOA, diretrizes orcamentarias), "
    "MENSAGENS DE VETO a dispositivos com impacto fazendario (gere um item PROPRIO para o veto, "
    "SEPARADO da lei sancionada, citando os artigos vetados e o motivo do veto), resolucoes, "
    "portarias de superintendencia, atas de colegiado, Conselho de Contribuintes, termos "
    "aditivos, cancelamento de IE, instituicao de comissao, DESPACHOS do Representante da "
    "Fazenda (ex.: declaracao de identidade parcial entre litigio administrativo e judicial de "
    "um contribuinte - gere UM item por despacho, citando o contribuinte e o tema tributario) e "
    "DELEGACAO de competencia ou de poderes a autoridade/servidor (delegar nao e movimentacao "
    "de pessoal: o servidor nao muda de cargo, so recebe atribuicao - vai aqui).\n"
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
    "ANEXOS E TABELAS: anexo de decreto (Anexo I, II, V, VI...), quadro de dotacao orcamentaria, "
    "limite de empenho ou alteracao de modalidade de aplicacao NAO gera item proprio - o decreto "
    "ja e o item. Cite no resumo DO DECRETO o objeto e o valor GLOBAL. Se a tabela estiver "
    "ilegivel no texto (celulas quebradas, numeros partidos), escreva 'valores no anexo do "
    "decreto' e NAO tente reconstruir cifra nenhuma: numero inventado e pior que numero ausente.\n"
    "CONSOLIDACAO: quando UM MESMO ato (ex.: um despacho do Secretario, um edital) decide/julga "
    "VARIOS itens homogeneos de uma vez (ex.: 'julgamento de N recursos', lista de varios "
    "processos/contribuintes), gere UM UNICO item informando a QUANTIDADE no 'resumo' "
    "(ex.: '13 recursos julgados, todos providos...') e citando as principais partes, em vez de "
    "um item por sub-ato ou de apenas um exemplo.\n"
    "FIDELIDADE: transcreva 'cargo', 'pessoa' e 'orgao' EXATAMENTE como aparecem no D.O., sem "
    "corrigir grafia nem alterar genero (ex.: se o texto diz 'Auditor Fiscal', nao escreva "
    "'Auditora Fiscal').\n"
    "Preencha 'pagina' com o numero do rotulo [pagina N].\n"
    "'data_ato' = data em que o ato foi assinado/publicado, se houver (formato DD/MM/AAAA).\n"
    "'prazo' = data-limite explicita; prazo contado em dias vai nos campos de dias (regra PRAZO).\n"
    "Deixe campos vazios como \"\". Nao invente datas nem numeros de dias: transcreva o que "
    "o texto diz. "
    "Na duvida sobre relevancia para a SEFAZ, NAO inclua. Se nada relevante, itens=[]."
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
#
# A lista VIGENTE vem da tabela Oracle 002B (ver _carregar_kw_relevancia); esta
# tupla é a RESERVA, usada só quando o banco não responde.
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


def _carregar_kw_relevancia(date):
    """Termos do pré-filtro vigentes na edição -> (lista, origem).

    Fonte da verdade: tabela Oracle 002B (incluir/retirar um termo é INSERT/UPDATE
    lá). Se o banco não responder, usa a tupla _KW_RELEVANCIA de reserva — rodar
    com a lista de ontem é melhor do que mandar a edição inteira para a IA (caro)
    ou não mandar nada (relatório vazio)."""
    if oracle_db.configurado() and config.oracle_settings().get("table_filtro"):
        try:
            kws = oracle_db.listar_palavras_filtro(date)
            if kws:
                return kws, "Oracle (002B)"
            print("[ATENCAO] a tabela 002B nao tem termo vigente nesta edicao -> "
                  "usando a lista de reserva do codigo.")
        except Exception as e:  # noqa: BLE001 - banco fora do ar nao pode derrubar o job
            print(f"[ATENCAO] falha ao ler o pre-filtro no Oracle (002B): "
                  f"{type(e).__name__}: {str(e)[:200]} -> usando a lista de reserva.")
    else:
        print("[monitor] Oracle/002B nao configurado -> usando a lista de reserva.")
    return list(_KW_RELEVANCIA), "lista de reserva (codigo)"


def _compilar_kw(kws):
    """Compila os termos num regex único de prefixo-com-limite-de-palavra.

    Por que \\b e não `termo in texto`: termos curtos precisam começar em palavra
    ('icms' não pode casar dentro de outro token). Antes isso era feito com um
    espaço no INÍCIO do termo (" icms"), o que numa tabela editada à mão some sem
    ninguém ver. O \\b faz o mesmo, e o prefixo continua valendo à direita
    ('tributa' casa 'tributacao', 'tributario')."""
    termos = [re.escape(_norm(k).strip()) for k in kws if _norm(k).strip()]
    if not termos:
        return None
    return re.compile(r"\b(?:" + "|".join(termos) + r")")


def _pagina_relevante(content, rx):
    """True se o texto da página casa com algum termo do pré-filtro (`rx` de _compilar_kw).

    Sem regex compilado (lista vazia), devolve False — melhor não mandar nada para
    a IA do que mandar a edição inteira por engano."""
    return bool(rx.search(_norm(content))) if rx else False


def _paginas_relevantes(caderno, date, rx):
    """Páginas do caderno que mencionam algum termo do pré-filtro (corte de custo)."""
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT pdf, caderno, date, page, content FROM pages WHERE caderno=? AND date=? "
            "ORDER BY page", (caderno, date),
        ).fetchall()
    finally:
        con.close()
    return [{"pdf": r[0], "caderno": r[1], "date": r[2], "page": int(r[3])}
            for r in rows if _pagina_relevante(r[4], rx)]


def _cadernos_da_edicao(date):
    """Nomes dos cadernos já indexados nesta edição (Parte I, IB, II, IV, V).

    Vem do índice, e não de config.CADERNOS, para o monitor processar só o que
    de fato foi lido — se um caderno falhou no download, ele simplesmente não
    aparece aqui."""
    con = sqlite3.connect(config.DB_PATH)
    try:
        rows = con.execute(
            "SELECT DISTINCT caderno FROM pages WHERE date=? ORDER BY caderno", (date,)
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def _data_br(date_iso):
    """'2026-08-03' -> '03/08/2026' (formato que vai no prompt)."""
    try:
        a, m, d = str(date_iso).split("-")
        return f"{d}/{m}/{a}"
    except ValueError:
        return str(date_iso or "")


def _norm(s):
    """minúsculo e sem acento — para casar nomes de forma robusta."""
    s = unicodedata.normalize("NFKD", s or "")
    return "".join(c for c in s if not unicodedata.combining(c)).lower()


# Hífen de fim de linha entre dois pedaços de palavra ("verbica- rio").
_RE_HIFEN = re.compile(r"(\w)-\s+(\w)")


def _dehifenizar(texto):
    """Junta as palavras que o PDF quebrou com hífen no fim da linha.

    O DOERJ é diagramado em colunas justificadas, então nomes são partidos ao
    meio: 'FABIO ROCHA VERBICÁ- RIO'. Sem isto, o casamento por palavra inteira
    não acha 'verbicario' e o monitorado some do relatório — foi o que aconteceu
    com a licença-prêmio do Verbicário em 29/07/2026. Vale para 37 das 62 páginas
    de uma edição típica da Parte I."""
    return _RE_HIFEN.sub(r"\1\2", texto or "")


# O MinerU devolve as TABELAS do D.O. em HTML. Quando o monitorado aparece dentro
# de uma (listas de servidores, quadros de licença), o trecho recortado vem cheio
# de <td rowspan=...> e fica ilegível no relatório — foi o caso de Diana Cabral
# Siqueira na edição de 03/08/2026.
_RE_TAG = re.compile(r"<[^>]*>")


def _limpar_html(texto):
    """Tira as tags HTML do trecho e normaliza os espaços.

    O trecho é recortado por posição, então costuma começar e terminar NO MEIO de
    uma tag ('=1>50069349...' é o rabo de um '<td colspan=1>'). Esses cacos não
    casam com _RE_TAG (falta o '<' ou o '>'), então são cortados à parte."""
    t = texto or ""
    fim_caco = t.find(">")
    if fim_caco != -1 and (t.find("<") == -1 or fim_caco < t.find("<")):
        t = t[fim_caco + 1:]                      # caco de tag aberta antes do recorte
    ini_caco = t.rfind("<")
    if ini_caco != -1 and t.find(">", ini_caco) == -1:
        t = t[:ini_caco]                          # tag que ficou sem fechar no fim
    return " ".join(_RE_TAG.sub(" ", t).split())


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


# O EXPEDIENTE (lista de secretarios no alto da Parte I, p.1) cita dezenas de nomes
# que não são ato nenhum — o Secretário de Fazenda aparece ali TODO dia, gerando uma
# menção falsa por edição. Ali "SECRETARIA DE ESTADO ..." se repete a cada ~60 chars;
# num ato de verdade aparece 1x ou 2x, como cabeçalho da matéria. Medido na edição de
# 30/07/2026: 14 ocorrências na janela do expediente contra 0-1 nas menções reais
# (Salvetti assinando como Secretário em exercício, Zenni presidindo a CTCE) — daí o 4.
_RX_EXPEDIENTE = re.compile(r"secretaria de estado")
_EXPEDIENTE_JANELA = 600
_EXPEDIENTE_MIN = 4


def _e_expediente(texto_norm, pos):
    """True se a posição `pos` cai no bloco do expediente, e não num ato de verdade."""
    ini = max(0, pos - _EXPEDIENTE_JANELA)
    fim = min(len(texto_norm), pos + _EXPEDIENTE_JANELA)
    return len(_RX_EXPEDIENTE.findall(texto_norm[ini:fim])) >= _EXPEDIENTE_MIN


def _carregar_monitorados(date):
    """Lista de monitorados VIGENTES na edição -> ([{"nome","funcao"}], origem).

    Fonte da verdade: tabela Oracle 002N (incluir/retirar um nome é INSERT/UPDATE
    lá, sem mexer em arquivo nem reiniciar nada). Se o banco não responder, cai
    para monitorados.txt: melhor uma lista possivelmente desatualizada do que um
    relatório sem a seção 3."""
    cfg = config.oracle_settings()
    if oracle_db.configurado() and cfg.get("table_monitorados"):
        try:
            nomes = oracle_db.listar_monitorados(date)
            if nomes:
                return nomes, "Oracle (002N)"
            print("[ATENCAO] a tabela 002N nao tem nome vigente nesta edicao -> "
                  "usando monitorados.txt.")
        except Exception as e:  # noqa: BLE001 - banco fora do ar nao pode derrubar o job
            print(f"[ATENCAO] falha ao ler os monitorados no Oracle (002N): "
                  f"{type(e).__name__}: {str(e)[:200]} -> usando monitorados.txt.")
    else:
        print("[monitor] Oracle/002N nao configurado -> usando monitorados.txt.")
    return ([{"nome": n, "funcao": ""} for n in config.nomes_monitorados()],
            "arquivo monitorados.txt")


def _scan_monitorados(date):
    """Varredura textual DETERMINÍSTICA dos nomes monitorados nos 5 cadernos da
    edição. Não usa IA. Devolve (itens NOMES_MONITORADOS - um por nome+página -,
    origem da lista, nº de nomes vigentes)."""
    monitorados, origem = _carregar_monitorados(date)
    if not monitorados:
        return [], origem, 0
    con = sqlite3.connect(config.DB_PATH)
    try:
        linhas = con.execute(
            "SELECT caderno, page, content FROM pages WHERE date=? ORDER BY caderno, page", (date,)
        ).fetchall()
    finally:
        con.close()
    # Normaliza cada página UMA vez (antes era 1x por nome x página, sobre o mesmo
    # texto). Guarda duas leituras: como veio e sem as quebras hifenizadas.
    paginas = [(caderno, page, content, _norm(content), _dehifenizar(content))
               for caderno, page, content in linhas]
    paginas = [(c, p, txt, n, deh, _norm(deh)) for c, p, txt, n, deh in paginas]

    itens, vistos = [], set()
    for m in monitorados:
        nome = m["nome"]
        toks = _tokens_nome(nome)
        if len(toks) < 2:
            continue
        for caderno, page, content, n_txt, deh, n_deh in paginas:
            # Tenta primeiro o texto como veio; só então o de-hifenizado (juntar
            # pedaços pode, em tese, colar um sobrenome no seguinte).
            pos, base, norm = _casa_nome(n_txt, toks), content, n_txt
            if pos < 0:
                pos, base, norm = _casa_nome(n_deh, toks), deh, n_deh
            if pos < 0:
                continue
            # Menção no expediente (lista de secretarios) não é ato: descarta.
            if _e_expediente(norm, pos):
                continue
            chave = (nome, caderno, page)
            if chave in vistos:
                continue
            vistos.add(chave)
            ini, fim = max(0, pos - 40), min(len(base), pos + 160)
            trecho = _limpar_html(base[ini:fim])
            itens.append({"categoria": "NOMES_MONITORADOS", "pessoa": nome,
                          "cargo": m.get("funcao", ""),
                          "tipo_ato": "Mencao no D.O.", "caderno": caderno,
                          "pagina": str(page), "resumo": trecho})
    return itens, origem, len(monitorados)


# --------------------------------------------------------------------------
# Pautas do Conselho de Contribuintes (seção 1) — contagem DETERMINÍSTICA
# --------------------------------------------------------------------------
# A IA erra a quantidade de recursos: na edição de 31/07/2026 devolveu 7 e 13
# recursos onde havia 13 e 15, e ainda juntou as três sessões do dia num item só,
# perdendo os horários. Contar "Recurso nº" dentro do bloco de cada pauta é
# trivial e exato — não é trabalho para o modelo.
_MESES = {"janeiro": 1, "fevereiro": 2, "marco": 3, "abril": 4, "maio": 5, "junho": 6,
          "julho": 7, "agosto": 8, "setembro": 9, "outubro": 10, "novembro": 11,
          "dezembro": 12}

# O cabecalho varia: "Pauta de Julgamento" (o comum), "Pauta Aditiva de Julgamento"
# (04/08/2026, que passou batido), "PAUTA PARA JULGAMENTO" e "PAUTA DE CONTINUACAO
# DE JULGAMENTO". Aceita ate 3 palavras entre "Pauta" e "Julgamento".
_RX_PAUTA = re.compile(
    r"Pauta(?:\s+[A-Za-zÀ-ÿ]+){0,3}\s+Julgamento"
    r"[^\n]{0,120}?do dia\s+(\d{1,2})\s+de\s+([A-Za-zÀ-ÿ]+)\s+de\s+(\d{4})"
    r"\s*,?\s*(?:[àa]s\s+(\d{1,2})\s*(?:h|horas)\s*(\d{2})?\s*(?:min)?)?",
    re.IGNORECASE)
_RX_CAMARA = re.compile(
    r"(?:PRIMEIRA|SEGUNDA|TERCEIRA|QUARTA|QUINTA|SEXTA)\s+C[ÂA]MARA"
    r"|C[ÂA]MARA\s+ESPECIAL|CONSELHO\s+PLENO", re.IGNORECASE)
# "Recurso nº 83744", mas também "Recurso Voluntário nº 78640" e "Recurso de
# Ofício nº 78257" — as três formas convivem na mesma pauta. Aceita até 3 palavras
# entre "Recurso" e o número (o qualificador), o que exclui frases soltas.
_RX_RECURSO = re.compile(
    r"Recurso(?:\s+[A-Za-zÀ-ÿ]+){0,3}\s+n[º°o.]?\s*(\d+)", re.IGNORECASE)
_RX_FIM_PAUTA = re.compile(r"NOTA\s+EXPLICATIVA", re.IGNORECASE)

# Janela antes do cabeçalho da pauta onde procuramos "Conselho de Contribuintes"
# e o nome da câmara. O cabeçalho vem imediatamente antes ("SECRETARIA DE ESTADO
# DE FAZENDA CONSELHO DE CONTRIBUINTES QUARTA CÂMARA"), mas o texto do MinerU às
# vezes carrega o fim da matéria anterior — 400 chars cobrem com folga.
_PAUTA_JANELA = 400


def _texto_do_caderno(date, caderno):
    """(texto colado de todas as páginas, [(offset_inicial, pagina)]) do caderno.

    Cola as páginas para que uma pauta que começa numa e termina na seguinte seja
    lida como um bloco só — foi o caso da sessão das 13h de 17/08 na edição de
    31/07/2026, partida entre as páginas 8 e 9."""
    con = sqlite3.connect(config.DB_PATH)
    try:
        linhas = con.execute(
            "SELECT page, content FROM pages WHERE date=? AND caderno=? ORDER BY page",
            (date, caderno),
        ).fetchall()
    finally:
        con.close()
    partes, mapa, pos = [], [], 0
    for page, content in linhas:
        mapa.append((pos, int(page)))
        partes.append(content or "")
        pos += len(content or "") + 1          # +1 do "\n" da junção
    return "\n".join(partes), mapa


def _pagina_do_offset(mapa, offset):
    """Número da página que contém esse offset no texto colado do caderno."""
    pagina = mapa[0][1] if mapa else 0
    for ini, page in mapa:
        if ini > offset:
            break
        pagina = page
    return pagina


def _scan_pautas(date):
    """Pautas do Conselho de Contribuintes da edição, uma por SESSÃO (data + hora).

    Determinístico, sem IA: acha cada cabeçalho de pauta, corta o bloco na próxima
    pauta ou na NOTA EXPLICATIVA e conta os "Recurso nº" de dentro. Devolve itens
    PRAZO_CRITICO já no formato do relatório."""
    itens, vistos = [], set()
    for caderno in _cadernos_da_edicao(date):
        texto, mapa = _texto_do_caderno(date, caderno)
        if not texto:
            continue
        marcas = list(_RX_PAUTA.finditer(texto))
        for i, m in enumerate(marcas):
            antes = texto[max(0, m.start() - _PAUTA_JANELA):m.start()]
            # Só as pautas do Conselho de Contribuintes (SEFAZ). Outros órgãos
            # também publicam "Pauta de Julgamento" e não entram na seção 1.
            if "conselho de contribuintes" not in _norm(antes):
                continue
            fim = marcas[i + 1].start() if i + 1 < len(marcas) else len(texto)
            bloco = texto[m.end():fim]
            corte = _RX_FIM_PAUTA.search(bloco)
            if corte:
                bloco = bloco[:corte.start()]
            recursos = _RX_RECURSO.findall(bloco)
            if not recursos:
                continue                       # cabeçalho sem lista: não conta
            mes = _MESES.get(_norm(m.group(2)))
            if not mes:
                continue
            data_sessao = f"{int(m.group(1)):02d}/{mes:02d}/{m.group(3)}"
            # 'as 14h' ou 'as 14h30min' - o horario faz parte da conferencia do prazo.
            hora = f"{int(m.group(4))}h{m.group(5)}" if m.group(4) and m.group(5) \
                else (f"{int(m.group(4))}h" if m.group(4) else "")
            cam = _RX_CAMARA.search(antes)
            camara = " ".join(cam.group(0).split()).title() if cam else "Conselho de Contribuintes"
            chave = (data_sessao, hora, _norm(camara))
            if chave in vistos:
                continue                       # mesma sessão repetida no caderno
            vistos.add(chave)
            itens.append({
                "categoria": "PRAZO_CRITICO",
                "tipo_ato": "Pauta de Julgamento do Conselho de Contribuintes",
                "numero_ano": f"{len(recursos)} recurso(s)",
                "orgao": "SEFAZ - Conselho de Contribuintes",
                "pessoa": "", "cargo": "", "processo": "", "vigencia": "",
                "data_ato": "", "prazo": data_sessao,
                "resumo": (f"{camara}: sessao de {data_sessao}"
                           f"{' as ' + hora if hora else ''} com {len(recursos)} recurso(s) "
                           f"pautado(s) (n. {', '.join(recursos)})."),
                "caderno": caderno,
                "pagina": str(_pagina_do_offset(mapa, m.start())),
            })
    return itens


def _e_item_pauta(it):
    """True se o item veio da IA descrevendo uma pauta de julgamento (será substituído
    pelo item determinístico de _scan_pautas)."""
    txt = _norm(f"{it.get('tipo_ato', '')} {it.get('resumo', '')}")
    return "pauta" in txt and ("julgamento" in txt or "sessao" in txt)


# --------------------------------------------------------------------------
# Prazo em dias -> data (a conta é do CÓDIGO, não da IA)
# --------------------------------------------------------------------------
# Metade dos prazos do D.O. é contada em dias a partir da publicação, e o campo
# PRAZO da 002A é DATE: texto relativo ("15 dias apos a publicacao") virava NULL.
# Pedir a data pronta à IA também não serve — no teste de 03/08/2026 ela somou os
# 60 dias direto na edição e ignorou os 15 de carência, adiantando o prazo de um
# ITD em 15 dias. Então a IA lê os NÚMEROS e quem soma é esta função.
_RX_DIAS = re.compile(r"\d+")


def _int_ou_zero(valor):
    """Primeiro inteiro do campo ('15', '15 dias', '', None) -> int. 0 se não houver."""
    m = _RX_DIAS.search(str(valor or ""))
    return int(m.group()) if m else 0


def _calcular_prazos(itens, date):
    """Preenche 'prazo' a partir de dias_carencia/dias_prazo e da data da edição.

    Devolve (nº calculado, nº deixado em branco por ser em dias úteis). Dias úteis
    não são calculados de propósito: dependeriam do calendário de feriados, e uma
    data errada num prazo é pior do que um campo vazio — o resumo continua com a
    regra por extenso."""
    import datetime as dt
    try:
        base = dt.date(*(int(x) for x in str(date).split("-")))
    except (ValueError, TypeError):
        return 0, 0

    calculados = uteis = 0
    for it in itens:
        dias = _int_ou_zero(it.get("dias_prazo"))
        if not dias:
            continue
        if _norm(it.get("dias_uteis")).startswith("s"):
            uteis += 1
            continue
        carencia = _int_ou_zero(it.get("dias_carencia"))
        fim = base + dt.timedelta(days=carencia + dias)
        anterior = (it.get("prazo") or "").strip()
        it["prazo"] = fim.strftime("%d/%m/%Y")
        calculados += 1
        # Deixa a conta visível no relatório: quem lê confere sem abrir o D.O.
        conta = f"[prazo calculado: edicao {_data_br(date)}"
        conta += f" + {carencia} dias de carencia" if carencia else ""
        conta += f" + {dias} dias corridos]"
        it["resumo"] = f"{(it.get('resumo') or '').strip()} {conta}".strip()
        if anterior and anterior != it["prazo"]:
            print(f"[monitor]   prazo recalculado: a IA disse {anterior}, a conta deu "
                  f"{it['prazo']} ({carencia}+{dias} dias)")
    return calculados, uteis


# --------------------------------------------------------------------------
# Id do próprio DOERJ (coluna ID_DOERJ da 002A)
# --------------------------------------------------------------------------
# O IOERJ fecha cada matéria publicada com "Id: 2753789". Guardar esse número
# liga o nosso registro à PUBLICAÇÃO OFICIAL — quem contesta um item do relatório
# chega no D.O. sem procurar página por página. Não confundir com a coluna ID,
# que é nossa: o Id do IOERJ identifica a MATÉRIA e se repete quando uma matéria
# vira vários registros (a Portaria JRF nº 182 virou 3 linhas em 03/08/2026).
#
# Como casamos: procuramos uma âncora do item (o nº do processo resolve 165 dos
# 178 casos) no texto da edição e pegamos o primeiro "Id:" DEPOIS dela — que é o
# fecho daquela matéria. Medido em 4 edições (208 registros): 85% casam. Os 15%
# restantes são registros sem processo, sem número e sem pessoa (anexos de
# decreto, avisos genéricos) — ficam nulos, e tudo bem.
_RX_ID_DOERJ = re.compile(r"Id:\s*(\d+)")
_MIN_ANCORA = 10                      # âncora curta casaria em qualquer lugar


def _so_alnum(s):
    """Só letras e dígitos — o D.O. quebra 'SEI-040006/033029/2026' com espaços e hífens."""
    return re.sub(r"[^0-9a-zA-Z]", "", s or "")


def _indice_edicao(date):
    """(texto colado da edição, versão só-alfanumérica, mapa de posições).

    O mapa liga cada caractere alfanumérico à sua posição no texto original, para
    a busca ignorar pontuação/quebras e ainda assim achar o offset real."""
    con = sqlite3.connect(config.DB_PATH)
    try:
        partes = [t or "" for (t,) in con.execute(
            "SELECT content FROM pages WHERE date=? ORDER BY caderno, page", (date,))]
    finally:
        con.close()
    texto = "\n".join(partes)
    plano, buf = [], []
    for i, ch in enumerate(texto):
        if ch.isalnum():
            buf.append(ch)
            plano.append(i)
    return texto, "".join(buf), plano


def _casar_id_doerj(itens, date):
    """Preenche 'id_doerj' em cada item. Devolve o nº de itens casados."""
    texto, plano_txt, plano = _indice_edicao(date)
    if not texto:
        return 0
    casados = 0
    for it in itens:
        achado = None
        for campo in ("processo", "numero_ano", "pessoa", "resumo"):
            for pedaco in re.split(r"[;,]", str(it.get(campo) or ""))[:4]:
                chave = _so_alnum(pedaco)
                if len(chave) < _MIN_ANCORA:
                    continue
                j = plano_txt.find(chave)
                if j < 0:
                    continue
                m = _RX_ID_DOERJ.search(texto, plano[j])
                if m:
                    achado = m.group(1)
                    break
            if achado:
                break
        it["id_doerj"] = achado
        casados += 1 if achado else 0
    return casados


# --------------------------------------------------------------------------
# Conferência dos números contra a fonte (anti-alucinação)
# --------------------------------------------------------------------------
# Auditoria de 05/08/2026: 3 registros afirmavam cifras que NÃO existem no texto
# da edição. A causa não foi o modelo inventar do nada — foi a tabela do Anexo V
# de 28/07 sair partida do MinerU ("<td>10.00</td><td>0 10.000</td>") e a IA
# "reconstruir" números plausíveis. É o pior erro possível: parece específico e
# correto. Aqui conferimos cada número do resumo contra o texto da edição.
#
# O que isto NÃO pega: dígito trocado que por acaso exista noutro ponto da
# edição. É rede contra invenção, não contra erro de leitura.
_RX_MARCADOR = re.compile(r"\[[^\]]*\]")           # trechos que NÓS acrescentamos
_RX_DATA = re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b")
_RX_NUMERO_TXT = re.compile(r"\d[\d.,]{3,}\d")
_MIN_DIGITOS = 5


def _numeros_do_resumo(resumo):
    """Números do resumo que devem existir na fonte -> [(texto original, só dígitos)].

    Ignora o que o próprio código escreveu (marcadores entre colchetes, como
    '[prazo calculado: ...]'), datas e anos soltos."""
    texto = _RX_MARCADOR.sub(" ", resumo or "")
    texto = _RX_DATA.sub(" ", texto)
    saida, vistos = [], set()
    for m in _RX_NUMERO_TXT.finditer(texto):
        bruto = m.group()
        digitos = re.sub(r"\D", "", bruto)
        if len(digitos) < _MIN_DIGITOS or re.fullmatch(r"(19|20)\d{2}", digitos):
            continue
        if digitos not in vistos:
            vistos.add(digitos)
            saida.append((bruto, digitos))
    return saida


def _validar_numeros(itens, date):
    """Marca os itens cujos números não aparecem na edição. Devolve (itens, números).

    NÃO descarta o item: nos casos vistos o ATO é real e só a cifra é que não se
    sustenta (o edital de autos de infração de 23/07 existe; o total é que não
    confere). Descartar perderia ato verdadeiro, e em silêncio."""
    texto, _, _ = _indice_edicao(date)
    if not texto:
        return 0, 0
    digitos_fonte = re.sub(r"\D", "", texto)
    n_itens = n_numeros = 0
    for it in itens:
        faltando = [bruto for bruto, dig in _numeros_do_resumo(it.get("resumo"))
                    if dig not in digitos_fonte]
        if not faltando:
            continue
        n_itens += 1
        n_numeros += len(faltando)
        it["resumo"] = (f"{(it.get('resumo') or '').strip()} "
                        f"[valores nao localizados na publicacao: {'; '.join(faltando[:6])}]")
    return n_itens, n_numeros


def _extrair_json(resp):
    """Extrai o objeto JSON da resposta da IA, tolerando o que ela costuma acrescentar.

    O prompt pede JSON puro, mas o modelo às vezes embrulha em ```json ... ``` ou
    escreve uma frase antes/depois. Aqui tiramos a cerca de crase e cortamos do
    primeiro '{' até o último '}'. Levanta a exceção do json.loads se ainda assim
    não for JSON válido — quem chama trata isso como bloco sem itens."""
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    i, j = text.find("{"), text.rfind("}")
    if i != -1 and j != -1 and j > i:
        text = text[i:j + 1]
    return json.loads(text)


# Teto de tokens da RESPOSTA de um bloco. A saída é um JSON com um objeto por ato,
# então um bloco denso enche isso depressa: na edição de 10/08/2026 o bloco das
# páginas 31-33 (13 portarias de PCAN da SUPFINF + 2 de sindicância da CTCE, quase
# idênticas entre si) fechou em 7.962 tokens de saída — a 38 do teto antigo, de
# 8.000. Na rodada do job aquele bloco estourou o teto, o JSON voltou cortado no
# meio e os 26 itens foram descartados EM SILÊNCIO, com o relatório saindo
# "COMPLETO". Daí o teto mais alto E o tratamento de truncamento abaixo: número
# fixo nenhum resolve sozinho, porque a edição seguinte pode trazer 40 portarias.
MAX_TOKENS_BLOCO = 16000

# Quantas vezes um bloco de UMA página pode ter o teto dobrado antes de desistir.
# Só entra em ação quando não há mais como partir o bloco (uma página não se divide).
_MAX_DOBRAS_TETO = 2


def _chamar_ia(caderno, paginas, client, max_tokens, date):
    """UMA chamada à IA sobre um bloco. Devolve (itens, usage, status).

    `status` é "ok" (JSON válido), "truncado" (a resposta bateu no teto de tokens e
    veio cortada) ou "falha" (a API não respondeu depois das tentativas, ou
    respondeu algo que não é JSON). Separar "truncado" de "falha" é o ponto: o
    truncado TEM conserto — partir o bloco e refazer — e a falha não.

    `date` (a edição) vai no prompt porque metade dos prazos do D.O. é contada "a
    partir da publicação": sem a data da edição o modelo não tem como fechar a
    conta e acabava escrevendo "15 dias após a publicação" no campo `prazo`, que é
    DATE na 002A e virava NULL."""
    blocos = []
    for p in paginas:
        txt = page_content(p["pdf"], p["page"]).strip()
        if txt:
            blocos.append(f"[pagina {p['page']}]\n{txt}")
    if not blocos:
        return [], {"input": 0, "output": 0}, "ok"
    corpo = "\n\n----------\n\n".join(blocos)
    edicao = f" publicada em {_data_br(date)}" if date else ""
    prompt = (f"Caderno: {caderno}\nEdicao do DOERJ{edicao}.\n\nTrechos:\n\n{corpo}\n\n"
              "Lembre-se: responda apenas com o objeto JSON.")
    # Resiliência: a Azure Foundry às vezes falha (timeout/5xx/rate-limit). Tenta
    # até 5x com backoff exponencial; se ainda falhar, PULA o bloco e o relatório
    # sai como PARCIAL (sem derrubar tudo).
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
        return [], {"input": 0, "output": 0}, "falha"

    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    paginas_rotulo = ", ".join(str(p["page"]) for p in paginas)

    # Resposta cortada no teto: NÃO tente ler o JSON (ele está incompleto por
    # definição). Antes o código só tentava o parse e engolia a exceção.
    if resp.stop_reason == "max_tokens":
        print(f"[ATENCAO] resposta TRUNCADA no teto de {max_tokens} tokens "
              f"(paginas {paginas_rotulo} de '{caderno}').", flush=True)
        return [], usage, "truncado"

    try:
        data = _extrair_json(resp)
        itens = data.get("itens", []) or []
    except Exception as e:  # noqa: BLE001
        # Não é truncamento e não é JSON: o bloco vira FALHA, e não zero itens em
        # silêncio. Com ok=False a rodada sai PARCIAL, que não sobrescreve o
        # relatório canônico nem grava na 002A.
        texto = "".join(b.text for b in resp.content if b.type == "text")
        print(f"[ATENCAO] resposta da IA nao e JSON valido "
              f"(paginas {paginas_rotulo} de '{caderno}'): {type(e).__name__}: {str(e)[:120]}. "
              f"Inicio da resposta: {texto[:160]!r}", flush=True)
        return [], usage, "falha"

    for it in itens:
        it["caderno"] = caderno
    return itens, usage, "ok"


def _somar_usage(*usos):
    """Soma os tokens de várias chamadas (um bloco partido gasta em mais de uma)."""
    return {"input": sum(u["input"] for u in usos), "output": sum(u["output"] for u in usos)}


def _mapear_bloco(caderno, paginas, client, max_tokens=MAX_TOKENS_BLOCO, date=None,
                  _dobras=0):
    """Extrai os itens de um bloco de páginas, partindo o bloco se a saída truncar.

    Devolve (itens, usage, ok). Quando a resposta bate no teto de tokens, o bloco é
    dividido ao meio e cada metade refeita — o custo cresce só onde a densidade
    exige, em vez de pagar um teto gigante em todo bloco. Numa página única, que
    não se divide, o teto é dobrado até `_MAX_DOBRAS_TETO` vezes.

    ok=False só quando ainda assim não deu: aí a rodada inteira sai PARCIAL."""
    itens, usage, status = _chamar_ia(caderno, paginas, client, max_tokens, date)
    if status == "ok":
        return itens, usage, True
    if status == "falha":
        return [], usage, False

    # --- truncado: primeiro tenta partir o bloco ---
    if len(paginas) > 1:
        meio = len(paginas) // 2
        print(f"[monitor]   partindo o bloco em {[p['page'] for p in paginas[:meio]]} + "
              f"{[p['page'] for p in paginas[meio:]]} e refazendo.", flush=True)
        itens_a, uso_a, ok_a = _mapear_bloco(caderno, paginas[:meio], client, max_tokens, date)
        itens_b, uso_b, ok_b = _mapear_bloco(caderno, paginas[meio:], client, max_tokens, date)
        return itens_a + itens_b, _somar_usage(usage, uso_a, uso_b), (ok_a and ok_b)

    # --- página única: não há o que partir, então dobra o teto ---
    if _dobras < _MAX_DOBRAS_TETO:
        novo = max_tokens * 2
        print(f"[monitor]   pagina {paginas[0]['page']} sozinha truncou; "
              f"repetindo com teto de {novo} tokens.", flush=True)
        itens_r, uso_r, ok_r = _mapear_bloco(caderno, paginas, client, novo, date,
                                             _dobras=_dobras + 1)
        return itens_r, _somar_usage(usage, uso_r), ok_r

    print(f"[ATENCAO] pagina {paginas[0]['page']} de '{caderno}' continua truncando com "
          f"{max_tokens} tokens de teto -> bloco PERDIDO. Relatorio sai PARCIAL.", flush=True)
    return [], usage, False


def _boletim_da_002a(date, destino):
    """Gera o boletim HTML da edição a partir da 002A, se ele ainda não existir.

    Usado no caminho da trava de idempotência (o job roda de hora em hora e a
    edição já foi lida): o Excel e a 002A estão prontos, o boletim é só outro
    formato dos mesmos itens — não custa IA nenhuma."""
    arquivo = Path(destino) / f"boletim_{date}.html"
    if arquivo.exists():
        return
    if not (oracle_db.configurado() and config.oracle_settings().get("table_monitor")):
        return
    try:
        import boletim
        registros = oracle_db.listar_monitoramento(date)
        if registros:
            boletim.gerar(date=date, itens=boletim.de_002a(registros), destino=destino)
    except Exception as e:  # noqa: BLE001 - formato a mais nunca derruba o job
        print(f"[ATENCAO] falha ao gerar o boletim HTML a partir da 002A: "
              f"{type(e).__name__}: {str(e)[:200]}")


def gerar(date=None, destino=None, chunk=4, todas_paginas=False, force=False):
    """Monta o monitoramento de UMA edição: Excel com 8 abas + gravação na 002A.

    O caminho completo, na ordem:
      1. trava de custo — se o Excel canônico do dia já existe e não veio --force,
         sai antes de chamar a IA (o job roda de hora em hora);
      2. para cada caderno indexado, seleciona as páginas pelo pré-filtro da 002B
         (ou todas, com `todas_paginas`) e manda em blocos de `chunk` páginas à IA;
      3. normaliza a categoria de cada item devolvido (o modelo erra a grafia);
      4. DESCARTA o que a IA marcou como NOMES_MONITORADOS e põe no lugar a
         varredura determinística da 002N — nomes não dependem do modelo;
      5. grava o .xlsx e, só se a rodada foi completa, a tabela 002A.

    Rodada PARCIAL (algum bloco falhou na IA após 5 tentativas) sai como
    *_PARCIAL.xlsx e NÃO grava no Oracle: melhor não ter o dado do que ter o dado
    pela metade, que passaria despercebido.

    `date` vazio = edição mais recente do índice. Devolve o caminho do Excel."""
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

    # Trava de idempotencia (para o pipeline horario): se o Excel CANONICO desta
    # edicao ja existe e nao e --force, nao re-extrai (economiza IA). O canonico so
    # e gravado numa rodada COMPLETA, entao existir = ja foi processada com sucesso.
    destino = Path(destino) if destino else config.RELATORIOS_DIR
    destino.mkdir(parents=True, exist_ok=True)
    canonico = destino / f"monitoramento_{date}.xlsx"
    if canonico.exists() and not force:
        print(f"[monitor] {canonico.name} ja existe -> pulando (use --force para refazer).")
        # O boletim HTML nao gasta IA: se ele ainda nao existe (edicao processada
        # antes deste modulo, ou execucao anterior sem Oracle), monta a partir da
        # 002A em vez de deixar o dia sem boletim.
        _boletim_da_002a(date, destino)
        return canonico

    client = config.get_client()
    print(f"[monitor] modelo da extracao: {config.MONITOR_MODEL}"
          f"{' (== site)' if config.MONITOR_MODEL == config.MODEL else ' (mais barato que o site)'}")
    todos = []
    uin = uout = 0
    blocos_total = blocos_falha = 0

    # Termos do pré-filtro (002B): lidos UMA vez por execução e compilados.
    rx_kw = None
    if not todas_paginas:
        kws, origem_kw = _carregar_kw_relevancia(date)
        rx_kw = _compilar_kw(kws)
        print(f"[monitor] pre-filtro: {len(kws)} termo(s) vigente(s) (fonte: {origem_kw})")

    for caderno in _cadernos_da_edicao(date):
        # Pré-filtro de custo: só páginas com termos SEFAZ vão para a IA (a menos
        # que --todas-paginas). Cadernos sem página relevante são pulados.
        if todas_paginas:
            paginas = _paginas_do_caderno(caderno, date)
        else:
            paginas = _paginas_relevantes(caderno, date, rx_kw)
        if not paginas:
            print(f"[monitor] {caderno}: 0 paginas relevantes (SEFAZ) -> pulado")
            continue
        print(f"[monitor] {caderno}: {len(paginas)} pagina(s) para a IA ...")
        for i in range(0, len(paginas), chunk):
            itens, usage, ok = _mapear_bloco(caderno, paginas[i:i + chunk], client, date=date)
            blocos_total += 1
            blocos_falha += 0 if ok else 1
            todos.extend(itens)
            uin += usage["input"]; uout += usage["output"]
            time.sleep(2)                 # espaça as chamadas (evita rate-limit da Foundry)

    # Normaliza a categoria da IA para o conjunto canônico (corrige typos do modelo,
    # ex.: OBSERVACAO_EXECUTIVE -> OBSERVACAO_EXECUTIVA) ANTES de gravar/filtrar.
    for it in todos:
        it["categoria"] = _canon_categoria(it.get("categoria"))

    # Prazo contado em dias -> data. A IA leu os números; a soma é nossa.
    n_prazo, n_uteis = _calcular_prazos(todos, date)
    if n_prazo or n_uteis:
        print(f"[monitor] prazos: {n_prazo} data(s) calculada(s) a partir da edicao"
              + (f"; {n_uteis} em dias uteis deixado(s) em branco (feriados)" if n_uteis else ""))

    # NOMES_MONITORADOS vem da varredura DETERMINÍSTICA (confiável, sem depender
    # da IA): descarta o que a IA marcou nessa categoria e usa a lista de nomes.
    todos = [it for it in todos if it.get("categoria") != "NOMES_MONITORADOS"]
    monit, origem, n_vigentes = _scan_monitorados(date)
    todos.extend(monit)

    # Pautas do Conselho de Contribuintes: idem. A IA erra a contagem de recursos
    # e junta as sessões do dia; a varredura conta exato e separa por sessão. Só
    # descarta os itens de pauta da IA se a varredura achou pauta nesta edição —
    # se não achou, o item do modelo é melhor do que seção vazia.
    pautas = _scan_pautas(date)
    if pautas:
        antes = len(todos)
        todos = [it for it in todos if not _e_item_pauta(it)]
        todos.extend(pautas)
        print(f"[monitor] pautas do Conselho: {len(pautas)} sessao(oes) contada(s) por varredura "
              f"({antes - len(todos) + len(pautas)} item(ns) da IA substituido(s))")

    # A seção de IB/II/IV-V é definida pelo caderno, não pelo palpite do modelo.
    n_corr = _forcar_categoria_por_caderno(todos)
    if n_corr:
        print(f"[monitor] {n_corr} item(ns) reclassificado(s) pela secao do proprio caderno.")

    # O rótulo do ato de pessoal sai do resumo, não do palpite da IA.
    n_inv, n_pad = _normalizar_tipo_ato(todos)
    if n_inv:
        print(f"[ATENCAO] {n_inv} ato(s) de pessoal com tipo_ato INVERTIDO pela IA -> "
              "corrigido(s) pelo resumo (ver linhas acima).")
    if n_pad:
        print(f"[monitor] {n_pad} tipo(s) de ato padronizado(s) no vocabulario canonico.")

    # Id da matéria no DOERJ: liga o registro à publicação oficial.
    n_id = _casar_id_doerj(todos, date)
    print(f"[monitor] Id do DOERJ: {n_id}/{len(todos)} registro(s) casado(s) com a materia")

    # Confere os números do resumo contra o texto da edição (anti-alucinação).
    n_marc, n_num = _validar_numeros(todos, date)
    if n_marc:
        print(f"[ATENCAO] {n_marc} item(ns) com {n_num} numero(s) que NAO existem no texto da "
              f"edicao -> marcados no resumo (conferir na publicacao).")
    print(f"[monitor] lista de monitorados: {n_vigentes} nome(s) vigente(s) em {date} "
          f"(fonte: {origem})")
    if monit:
        nomes_achados = sorted({m["pessoa"] for m in monit})
        print(f"[monitor] nomes monitorados: {len(monit)} mencao(oes) de {len(nomes_achados)} "
              f"nome(s) -> {', '.join(nomes_achados)}")

    completo = (blocos_falha == 0)
    print(f"[monitor] {len(todos)} itens relevantes extraidos | blocos: {blocos_total}, "
          f"falhas: {blocos_falha} (tokens: entrada {uin} / saida {uout})")

    # Não sobrescreve um relatório COMPLETO com um PARCIAL: parcial vai para
    # *_PARCIAL.xlsx; só uma rodada sem falhas grava o nome canônico (destino já
    # criado na trava acima).
    sufixo = "" if completo else "_PARCIAL"
    arquivo = destino / f"monitoramento_{date}{sufixo}.xlsx"
    _build_xlsx(arquivo, date, todos, blocos_total, blocos_falha)
    # Mesma extracao, outro formato: o boletim HTML no layout do e-mail. Sai dos
    # itens em MEMORIA (nao depende do Oracle) e segue a mesma regra do Excel:
    # rodada parcial vira boletim_<data>_PARCIAL.html.
    try:
        import boletim
        boletim.gerar(date=date, itens=todos, destino=destino, n_monitorados=n_vigentes,
                      parcial=not completo, blocos_falha=blocos_falha,
                      blocos_total=blocos_total)
    except Exception as e:  # noqa: BLE001 - o boletim e um formato a mais, nao derruba o job
        print(f"[ATENCAO] falha ao gerar o boletim HTML: {type(e).__name__}: {str(e)[:200]}")
    if completo:
        print(f"[ok] Excel COMPLETO gerado -> {arquivo}")
    else:
        print(f"[ATENCAO] relatorio PARCIAL ({blocos_falha}/{blocos_total} blocos falharam na IA). "
              f"NAO sobrescreveu o relatorio canonico; salvo como -> {arquivo}. "
              "Regenere quando a IA normalizar.")

    # Grava no Oracle (002A) só quando a rodada foi COMPLETA (evita dados parciais).
    if completo:
        if oracle_db.configurado() and config.oracle_settings().get("table_monitor"):
            try:
                n = oracle_db.salvar_monitoramento(date, todos)
                print(f"[ok] {n} itens gravados no Oracle (002A) para a edicao {date}.")
            except Exception as e:  # noqa: BLE001 - nao derruba o job por falha de banco
                print(f"[ATENCAO] falha ao gravar no Oracle (002A): {type(e).__name__}: {str(e)[:200]}")
        else:
            print("[monitor] Oracle/002A nao configurado -> gravado so o Excel.")
    return arquivo


def _build_xlsx(arquivo, date, itens, blocos_total=None, blocos_falha=0):
    """Escreve o .xlsx: uma aba "Resumo" + uma aba por seção do relatório.

    Os itens são distribuídos pelas 8 seções por CATEGORIA_SECAO (duas categorias
    caem na mesma seção 4). Seção sem item recebe uma linha dizendo isso, para o
    leitor distinguir "não houve" de "falhou".

    O aviso de completude no topo do Resumo é o ponto-chave: em vermelho quando
    `blocos_falha` > 0, verde quando a rodada leu tudo. Sem isso, um relatório
    incompleto passaria por completo."""
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
                # write_string, NUNCA write: o write() do xlsxwriter despacha por
                # aparencia — texto comecando com '=' vira FORMULA (a celula sai
                # vazia no Excel) e texto que parece numero vira numero. Um resumo
                # recortado do meio de uma tabela pode comecar com '=' (aconteceu
                # em 03/08/2026). Aqui tudo e texto, sempre.
                ws.write_string(li, c, str(it.get(chave, "") or ""), f_wrap)
        for c, w in enumerate(larg):
            ws.set_column(c, c, w)
        ws.freeze_panes(1, 0)
        if not por_secao[n]:
            ws.write(1, 0, "(nenhum item nesta secao nesta edicao)", f_wrap)
    wb.close()


def main():
    """Linha de comando: lê os argumentos e chama gerar(). Ponto de entrada do passo 5/5."""
    ap = argparse.ArgumentParser(description="Gera o Monitoramento DOERJ estruturado em Excel.")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--dir", default=None, help="Pasta de destino (padrao: <projeto>/relatorios)")
    ap.add_argument("--chunk", type=int, default=4, help="Paginas por chamada de IA (padrao: 4)")
    ap.add_argument("--todas-paginas", action="store_true",
                    help="Desliga o pre-filtro SEFAZ e manda TODAS as paginas a IA (mais caro)")
    ap.add_argument("--force", action="store_true",
                    help="Reprocessa mesmo se o Excel canonico do dia ja existir (gasta IA)")
    args = ap.parse_args()
    gerar(date=args.date, destino=args.dir, chunk=args.chunk,
          todas_paginas=args.todas_paginas, force=args.force)


if __name__ == "__main__":
    main()
