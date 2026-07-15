"""
============================================================================
reader.py · A LEITURA pela IA (Claude devolve a resposta ESTRUTURADA)
----------------------------------------------------------------------------
Recebe a pergunta + as páginas que a busca achou, monta um "contexto" com o
TEXTO limpo dessas páginas e pede ao Claude uma resposta ESTRUTURADA: um resumo
curto + uma lista de itens (atos), cada um com campos fixos.

Como garantimos a estrutura: pedimos ao modelo, no prompt, para responder APENAS
com um objeto JSON no formato combinado, e fazemos json.loads. (Não usamos o
recurso nativo "structured outputs" porque ele não está habilitado na Azure
Foundry da SEFAZ — mas o Sonnet 5 segue o JSON por instrução com confiabilidade.)

Funciona com os DOIS provedores (config.get_client decide):
  - Anthropic direta, ou
  - Claude na Azure/Microsoft Foundry.
============================================================================
"""
import json

# Em rede corporativa com proxy de inspeção TLS (SEFAZ), confia no cofre do Windows.
try:
    import truststore
    truststore.inject_into_ssl()
except ImportError:
    pass

import config
from export import CAMPOS          # a lista de campos de cada item (fonte única da verdade)
from search import page_content

# Descreve o formato JSON esperado (um item de exemplo com todos os campos vazios).
_EXEMPLO_ITEM = "{" + ", ".join(f'"{c}": ""' for c in CAMPOS) + "}"

# Instrução mestra: define o comportamento E o formato JSON exato da resposta.
SYSTEM = (
    "Voce estrutura informacoes do Diario Oficial do Estado do Rio de Janeiro "
    "(DOERJ, Parte I - Poder Executivo). Use APENAS os trechos de texto fornecidos.\n\n"
    "Responda EXCLUSIVAMENTE com um objeto JSON valido (sem nenhum texto antes ou "
    "depois, sem blocos de codigo ```), exatamente neste formato:\n"
    '{"resumo": "<sintese curta em portugues, markdown simples>", '
    '"itens": [' + _EXEMPLO_ITEM + ", ...]}\n\n"
    "Regras:\n"
    "- 'itens': um objeto por ato/publicacao relevante para a pergunta.\n"
    "- 'tipo': ex.: Decreto, Portaria, Resolucao, Nomeacao, Exoneracao, Edital, Despacho.\n"
    "- 'pagina' e 'edicao': tire dos rotulos [Edicao ... - pagina ...] dos trechos.\n"
    "- Deixe o campo como \"\" (string vazia) quando a informacao nao existir. NAO invente.\n"
    "- Se nada for encontrado, 'itens' deve ser [].\n"
    "- Nao omita nenhum ato relevante que esteja nos trechos."
)


def _build_prompt(question, results):
    """Monta o texto: cada página vira um bloco [Edição data - página N], e no fim a pergunta."""
    blocos = []
    for r in results:
        texto = page_content(r["pdf"], r["page"]).strip()
        if not texto:
            continue
        blocos.append(f"[Edicao {r['date']} - pagina {r['page']}]\n{texto}")
    corpo = "\n\n----------\n\n".join(blocos)
    return (
        f"Trechos do Diario Oficial:\n\n{corpo}\n\n"
        f"Pergunta: {question}\n\n"
        "Lembre-se: responda apenas com o objeto JSON."
    )


def _extrair_json(resp):
    """Pega o texto da resposta e extrai o objeto JSON de forma tolerante
    (remove cercas ``` e pega do primeiro '{' ao último '}')."""
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    if text.startswith("```"):                       # remove cerca ```json ... ```
        text = text.strip("`")
        if text[:4].lower() == "json":
            text = text[4:]
    i, j = text.find("{"), text.rfind("}")           # recorta só o objeto JSON
    if i != -1 and j != -1 and j > i:
        text = text[i:j + 1]
    return json.loads(text)


def answer_from_pages(question, results, model=None, max_tokens=8000):
    """Chama o Claude e devolve (data, usage).

    data = {"resumo": str, "itens": [ {campos...} ]}
    usage = {"input": int, "output": int}"""
    # Relê a chave do .env AGORA (evita chave antiga em cache -> 401).
    if not config.current_api_key():
        raise RuntimeError("ANTHROPIC_API_KEY nao definido no .env")
    model = model or config.MODEL
    client = config.get_client()                     # Anthropic OU Azure Foundry (conforme .env)

    # Streaming: permite max_tokens alto sem estourar timeout (listas grandes de
    # exonerações podem gerar um JSON longo). get_final_message() junta tudo no fim.
    with client.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=SYSTEM,
        messages=[{"role": "user", "content": _build_prompt(question, results)}],
    ) as stream:
        resp = stream.get_final_message()

    try:
        data = _extrair_json(resp)
    except (json.JSONDecodeError, ValueError):
        raise RuntimeError(
            f"resposta da IA veio incompleta/invalida (stop_reason={getattr(resp,'stop_reason',None)}); "
            "tente aumentar max_tokens ou reduzir o numero de paginas (k)."
        )
    data.setdefault("resumo", "")
    data.setdefault("itens", [])
    usage = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return data, usage


def _fmt_data(iso):
    """2026-07-10 -> 10/07/2026 (para o resumo)."""
    try:
        y, m, d = iso.split("-")
        return f"{d}/{m}/{y}"
    except Exception:
        return iso or ""


def _resumo_itens(itens, edicao):
    """Monta um resumo DESCRITIVO da lista (tipo, quantidade e órgãos), sem
    gastar tokens da IA. Ex.: 'O DOERJ de 10/07/2026 traz 92 atos de Nomeação,
    abrangendo 15 órgãos (ex.: Casa Civil, Fazenda, ...).'"""
    from collections import Counter
    n = len(itens)
    if n == 0:
        return "Nenhum item encontrado nesta edição."
    tipos = Counter((it.get("tipo") or "ato").strip() or "ato" for it in itens)
    tipo_princ, qtd_princ = tipos.most_common(1)[0]

    orgaos, vistos = [], set()
    for it in itens:
        o = (it.get("orgao") or "").strip()
        if o and o.lower() not in vistos:
            vistos.add(o.lower())
            orgaos.append(o)

    ed = f"de {_fmt_data(edicao)} " if edicao else ""
    frase = f"O DOERJ {ed}traz {n} atos do tipo \"{tipo_princ}\""
    if len(tipos) > 1:
        frase += f" (e mais {n - qtd_princ} de outros tipos)"
    if orgaos:
        exemplos = "; ".join(orgaos[:4])
        frase += f", abrangendo {len(orgaos)} órgão(s) — ex.: {exemplos}"
    return frase + "."


def _resumo_ia(itens, edicao):
    """Pede à IA UM parágrafo de resumo, a partir de uma versão CONDENSADA dos
    itens (só tipos e órgãos) — gasta poucos tokens. Devolve (texto, usage).
    Se a IA falhar, cai no resumo local (_resumo_itens)."""
    if not itens:
        return "Nenhum item encontrado nesta edição.", {"input": 0, "output": 0}
    from collections import Counter
    tipos = Counter((it.get("tipo") or "").strip() for it in itens if (it.get("tipo") or "").strip())
    orgaos = Counter((it.get("orgao") or "").strip() for it in itens if (it.get("orgao") or "").strip())
    ctx = (
        f"Edicao do DOERJ: {_fmt_data(edicao)}. Total de atos extraidos: {len(itens)}.\n"
        "Tipos (com contagem):\n" + "\n".join(f"- {t}: {n}" for t, n in tipos.most_common()) +
        "\n\nOrgaos/secretarias (top 20, com contagem):\n" +
        "\n".join(f"- {o}: {n}" for o, n in orgaos.most_common(20))
    )
    prompt = (
        ctx + "\n\nEscreva UM paragrafo curto (2 a 4 frases), em portugues, resumindo esses "
        "atos da edicao: o que predomina, e os principais orgaos/secretarias envolvidos. "
        "Responda APENAS o paragrafo, sem titulo e sem listar tudo."
    )
    try:
        client = config.get_client()
        r = client.messages.create(
            model=config.MODEL, max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        txt = "".join(b.text for b in r.content if b.type == "text").strip()
        if txt:
            return txt, {"input": r.usage.input_tokens, "output": r.usage.output_tokens}
    except Exception:
        pass
    return _resumo_itens(itens, edicao), {"input": 0, "output": 0}


def answer_chunked(question, results, chunk_size=2, max_tokens=16000):
    """Como answer_from_pages, mas processa as páginas em BLOCOS e junta os itens.

    Para listas grandes (ex.: todas as exonerações do dia), uma resposta única
    ficaria enorme e poderia truncar. Quebrando em blocos de poucas páginas, cada
    chamada devolve um JSON pequeno (sem truncar) e somamos tudo no fim.
    Devolve (data, usage) no mesmo formato de answer_from_pages."""
    itens = []
    uin = uout = 0
    for i in range(0, len(results), chunk_size):
        bloco = results[i:i + chunk_size]
        data, usage = answer_from_pages(question, bloco, max_tokens=max_tokens)
        itens.extend(data.get("itens", []))
        uin += usage["input"]
        uout += usage["output"]
    edicao = results[0].get("date", "") if results else ""
    resumo, r_usage = _resumo_ia(itens, edicao)      # a IA escreve o resumo (chamada curta)
    uin += r_usage["input"]
    uout += r_usage["output"]
    return {"resumo": resumo, "itens": itens}, {"input": uin, "output": uout}
