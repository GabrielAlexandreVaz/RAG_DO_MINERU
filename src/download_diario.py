"""
============================================================================
download_diario.py · Etapa 0 do pipeline — BAIXA o D.O. do portal do IOERJ
----------------------------------------------------------------------------
O portal do IOERJ NÃO expõe uma URL estável para o PDF. O caminho até o arquivo
(mostra_pdf.php) depende de: cookie PHPSESSID válido, um token `session`
efêmero (base64^3 de GUID+timestamp) e a URL final montada por JavaScript no
visualizador pdf.js. Replicar via HTTP puro é frágil.

Estratégia robusta (portada do projeto diario-rj):
  1) Dirigir um Chromium real (Playwright) pelo fluxo: portal -> "última edição"
     -> do_seleciona_edicao -> caderno "Parte I (Poder Executivo)".
  2) INTERCEPTAR a rede e capturar a resposta real do PDF (mostra_pdf.php /
     application/pdf) — sem hardcodar endpoint, parâmetro nem hash.
  3) Fallback: acionar o botão de download do visualizador.

Salva DOERJ_AAAA-MM-DD.pdf em config.DOWNLOADS_DIR, nomeado pela DATA REAL da
edição lida do portal (em feriado/fim de semana o portal devolve a última
edição publicada; como o nome usa a data da edição, a dedup evita recriar um
arquivo com data errada).

Pré-requisito (uma vez):  python -m playwright install chromium

Uso:
    python src/download_diario.py            # baixa a última edição
============================================================================
"""
import base64
import re
import sys
import time
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from zoneinfo import ZoneInfo

import config

_ULTIMA_EDICAO_RE = re.compile(r"[uú]ltima\s+edi[cç][aã]o", re.IGNORECASE)
_SELECIONA_RE = re.compile(r"do_seleciona_edicao\.php", re.IGNORECASE)
_CLIQUE_AQUI_RE = re.compile(r"clique\s+aqui", re.IGNORECASE)
_PDF_URL_RE = re.compile(r"mostra_pdf\.php", re.IGNORECASE)


# ------------------------------------------------------------ helpers de data/PDF
def _now():
    """Agora no fuso do D.O. (America/Sao_Paulo), e não no fuso da máquina.

    Importa em servidor configurado em UTC: perto da meia-noite, a data local
    seria a do dia seguinte e o PDF sairia com o nome errado."""
    return datetime.now(ZoneInfo(config.DOWNLOAD_TZ))


def _hoje_iso():
    """Data de hoje (AAAA-MM-DD) no fuso do D.O. Usada só como último recurso,
    quando não dá para ler a data da edição no próprio portal."""
    return _now().strftime("%Y-%m-%d")


def pdf_name(edition_date):
    """DOERJ_AAAA-MM-DD.pdf para uma data de edição YYYY-MM-DD."""
    return f"DOERJ_{edition_date}.pdf"


def _sem_acento(s):
    """'EDIÇÃO' -> 'EDICAO'. O rótulo do caderno extra sai do texto do portal, que
    vem acentuado; no índice ele está gravado sem acento desde 07/08/2026."""
    return "".join(c for c in unicodedata.normalize("NFKD", s or "")
                   if not unicodedata.combining(c))


def edition_date_from_data_param(data_b64):
    """Decodifica o parâmetro `data` do portal (base64 de AAAAMMDD) -> AAAA-MM-DD."""
    try:
        raw = base64.b64decode(data_b64, validate=True).decode("ascii")
    except Exception:
        return None
    if len(raw) == 8 and raw.isdigit():
        return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    return None


def _looks_like_pdf(data):
    """Assinatura mágica %PDF- no início do conteúdo."""
    return len(data) >= 5 and data[:5] == b"%PDF-"


def _is_pdf_response(url, content_type):
    """True se esta resposta da rede parece ser o PDF do Diário.

    Aceita por dois caminhos — a URL casar com mostra_pdf.php OU o content-type
    ser application/pdf — porque o portal nem sempre manda o cabeçalho certo."""
    ct = (content_type or "").lower()
    return bool(_PDF_URL_RE.search(url)) or "application/pdf" in ct


def _log(msg):
    """Log com o prefixo [download]. flush=True para a linha aparecer na hora no
    logs\\pipeline.log, e não só quando o buffer encher."""
    print(f"[download] {msg}", flush=True)


# -------------------------------------------------------------------- download
def download_diario(download_dir=None):
    """Baixa a Parte I da última edição. Devolve dict com caminho, bytes e via.

    dict = {file_path, bytes, via, pages, edition_date, already_existed}"""
    from playwright.sync_api import (
        Response,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )

    out_dir = Path(download_dir) if download_dir else config.DOWNLOADS_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    config.SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    captures = []   # lista de (url, body) de respostas que parecem PDF

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.DOWNLOAD_HEADLESS)
        context = browser.new_context(
            ignore_https_errors=True,        # proxy TLS da SEFAZ: ignora erro de cert
            accept_downloads=True,
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        context.set_default_timeout(config.NAV_TIMEOUT_MS)
        context.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)

        def on_response(response):
            """Guarda em `captures` toda resposta que for realmente um PDF.

            É o coração da estratégia: em vez de adivinhar a URL do arquivo (que
            depende de sessão e token efêmeros), escutamos a rede e pegamos o PDF
            que o próprio visualizador baixou. Erros são ignorados porque este
            handler roda para CADA resposta da página — uma falha aqui não pode
            interromper a navegação."""
            try:
                url = response.url
                if not _is_pdf_response(url, response.headers.get("content-type")) or not response.ok:
                    return
                body = response.body()
                if _looks_like_pdf(body):
                    captures.append((url, body))
                    _log(f"PDF capturado na rede: {url} ({len(body)} bytes)")
            except Exception:
                pass

        context.on("response", on_response)
        page = context.new_page()
        viewer = page

        try:
            _goto_cadernos(page)

            edition_date = _parse_edition_date_from_url(page.url) or _hoje_iso()
            target = out_dir / pdf_name(edition_date)
            _log(f"data da edicao: {edition_date} -> {target.name}")

            # Dedup pela data da edição (feriado/fim de semana/re-execução).
            if target.exists() and target.stat().st_size > 1024:
                _log(f"edicao {edition_date} ja baixada: {target}. Pulando.")
                return {"file_path": str(target), "bytes": target.stat().st_size,
                        "via": "cache", "pages": None, "edition_date": edition_date,
                        "already_existed": True}

            viewer = _selecionar_caderno(context, page)

            best, via = _obter_pdf(viewer, captures, target)
            if best is None:
                raise RuntimeError("Nao foi possivel obter o PDF (rede e botao falharam).")
            url_pdf, body = best
            if via == "network":
                target.write_bytes(body)
            if not _looks_like_pdf(body) or len(body) < 1024:
                raise RuntimeError(f"Arquivo baixado nao parece PDF valido ({len(body)} bytes).")

            pages = _count_pages(body)
            _log(f"PDF salvo: {target} ({len(body)} bytes"
                 f"{f', {pages} paginas' if pages else ''}, via {via})")
            return {"file_path": str(target), "bytes": len(body), "via": via,
                    "pages": pages, "edition_date": edition_date, "already_existed": False}
        except Exception:
            _screenshot(viewer, "falha")
            raise
        finally:
            context.close()
            browser.close()


def _parse_edition_date_from_url(url):
    """Extrai o parâmetro `data` (base64 de AAAAMMDD) da URL -> AAAA-MM-DD."""
    try:
        data = parse_qs(urlparse(url).query).get("data", [None])[0]
    except Exception:
        return None
    return edition_date_from_data_param(data) if data else None


# ---------------------------------------------------------------- navegação
def _goto_cadernos(page):
    """Navega do portal até a página que lista os cadernos da última edição.

    O caminho tem três formas de dar certo, tentadas em ordem, porque o portal
    muda de comportamento: clicar no botão "ÚLTIMA EDIÇÃO"; se ele não existir,
    ir direto na URL de fallback; e se o redirecionamento automático não
    acontecer, clicar no link "clique aqui". Levanta RuntimeError se nenhuma
    funcionar."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    _log(f"acessando portal: {config.PORTAL_URL}")
    page.goto(config.PORTAL_URL, wait_until="domcontentloaded")

    if _click_by_text(page, _ULTIMA_EDICAO_RE):
        _log('clique em "ULTIMA EDICAO" realizado.')
    else:
        _log('botao "ULTIMA EDICAO" nao encontrado; usando URL de fallback.')
        page.goto(config.ULTIMA_EDICAO_URL, wait_until="domcontentloaded")

    # do_ultima_edicao.php redireciona (auto ou via "clique aqui") p/ a seleção.
    try:
        page.wait_for_url(_SELECIONA_RE, timeout=config.NAV_TIMEOUT_MS)
    except PlaywrightTimeoutError:
        if not _click_anchor_href(page, _SELECIONA_RE):
            if not _click_by_text(page, _CLIQUE_AQUI_RE):
                raise RuntimeError("Nao foi possivel chegar aos cadernos (do_seleciona_edicao).")
        page.wait_for_url(_SELECIONA_RE, timeout=config.NAV_TIMEOUT_MS)

    page.wait_for_load_state("domcontentloaded")
    _log(f"pagina de cadernos: {page.url}")


def _selecionar_caderno(context, page):
    """Abre o caderno alvo (Parte I) e devolve a aba onde o visualizador carregou.

    O link pode abrir numa aba nova ou navegar na mesma — daí o expect_page com
    timeout curto: se nenhuma aba surgir em 8s, é porque navegou aqui mesmo."""
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    pattern = re.compile(re.escape(config.CADERNO_ALVO), re.IGNORECASE)
    link = page.locator("a", has_text=pattern).first
    link.wait_for(state="visible", timeout=config.NAV_TIMEOUT_MS)

    viewer = page
    try:
        with context.expect_page(timeout=8000) as popup_info:
            link.click()
        viewer = popup_info.value            # abriu em nova aba
    except PlaywrightTimeoutError:
        viewer = page                        # navegou na mesma aba

    _log(f'clique no caderno "{config.CADERNO_ALVO}" realizado.')
    try:
        viewer.wait_for_load_state("domcontentloaded")
    except PlaywrightTimeoutError:
        pass
    _log(f"visualizador aberto: {viewer.url}")
    return viewer


def _obter_pdf(viewer, captures, target):
    """Tenta capturar por rede; se falhar, tenta o botão de download."""
    best = _wait_for_capture(viewer, captures, config.NAV_TIMEOUT_MS)
    if best is not None:
        return best, "network"
    _log("nenhum PDF interceptado; tentando botao de download do visualizador.")
    downloaded = _tentar_botao_download(viewer, target)
    if downloaded is not None:
        return (downloaded, target.read_bytes()), "download-button"
    return None, "network"


def _wait_for_capture(viewer, captures, timeout_ms):
    """Espera o PDF aparecer na lista de respostas interceptadas da rede.

    `captures` é preenchida pelo handler on_response enquanto o visualizador
    carrega. Quando há mais de uma captura, fica com a MAIOR: o pdf.js costuma
    pedir pedaços do arquivo, e o corpo maior é a edição inteira.

    O wait_for_timeout serve para dar vez aos eventos do Playwright — sem ele o
    laço giraria sem nunca receber as respostas."""
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if captures:
            return max(captures, key=lambda c: len(c[1]))   # maior corpo = a edição
        viewer.wait_for_timeout(500)                        # pump de eventos
    return captures[0] if captures else None


def _tentar_botao_download(page, target):
    """Plano B: aciona o botão de download do visualizador e salva em `target`.

    Só é usado quando a interceptação de rede não pegou nada. Varre uma lista de
    seletores porque o visualizador não expõe um id estável — cada tentativa que
    falha é ignorada e passa para a próxima. Devolve a URL do download, ou None
    se nenhum seletor funcionou."""
    candidates = [
        "a[download]", "#download", "button#download",
        '[title*="ownload" i]', '[aria-label*="ownload" i]',
        'a[href*="mostra_pdf" i]', "text=/baixar|download|salvar/i",
    ]
    for selector in candidates:
        el = page.locator(selector).first
        try:
            if el.count() == 0:
                continue
            with page.expect_download(timeout=20000) as dl_info:
                el.click(timeout=5000)
            download = dl_info.value
            download.save_as(str(target))
            _log(f"download via botao concluido ({selector}).")
            return download.url or page.url
        except Exception:
            continue
    return None


# ----------------------------------------------------------------- helpers
def _click_by_text(page, pattern):
    """Clica no primeiro elemento cujo TEXTO casa com `pattern`. True se clicou.

    Tenta quatro formas de localizar (link, botão, âncora, texto solto) porque o
    portal não marca os elementos de forma consistente — o mesmo "ÚLTIMA EDIÇÃO"
    já apareceu como link e como botão."""
    locators = [
        page.get_by_role("link", name=pattern),
        page.get_by_role("button", name=pattern),
        page.locator("a", has_text=pattern),
        page.get_by_text(pattern),
    ]
    for loc in locators:
        first = loc.first
        try:
            if first.count() > 0:
                first.click(timeout=8000)
                return True
        except Exception:
            continue
    return False


def _click_anchor_href(page, href_re):
    """Clica no primeiro link cujo HREF casa com `href_re`. True se clicou.

    Complementa o _click_by_text: às vezes o link certo não tem texto previsível,
    mas o endereço sim (ex.: do_seleciona_edicao.php)."""
    links = page.locator("a[href]")
    for i in range(links.count()):
        href = links.nth(i).get_attribute("href") or ""
        if href_re.search(href):
            try:
                links.nth(i).click(timeout=8000)
                return True
            except Exception:
                return False
    return False


def _screenshot(page, tag):
    """Salva um print da página em SCREENSHOT_DIR para diagnosticar uma falha.

    O download roda headless num job agendado: quando o portal muda de layout, o
    print é a única forma de ver o que apareceu na tela. Engole qualquer exceção
    de propósito — falhar ao registrar um erro não pode virar um segundo erro."""
    if page is None:
        return
    try:
        ts = _now().strftime("%Y-%m-%d_%H-%M-%S")
        path = config.SCREENSHOT_DIR / f"{tag}-{ts}.png"
        page.screenshot(path=str(path), full_page=True)
        _log(f"screenshot de falha salvo: {path}")
    except Exception:
        pass


def _count_pages(pdf_bytes):
    """Conta páginas via PyMuPDF (só para log)."""
    try:
        import fitz
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            return doc.page_count
    except Exception:
        return None


# ---------------------------------------------------- múltiplos cadernos (em memória)
def _coletar_links_cadernos(page, alvos):
    """Mapeia chave->URL absoluta do link de cada caderno alvo na página de seleção.

    Casa pelo texto do link (ex.: 'Tribunal de Contas'); o href leva o token
    `session` (efêmero) daquele caderno."""
    out = {}
    anchors = page.locator("a[href*='mostra_edicao']")
    for i in range(anchors.count()):
        a = anchors.nth(i)
        try:
            txt = (a.inner_text() or "").strip()
            href = a.get_attribute("href") or ""
        except Exception:
            continue
        if not href:
            continue
        for c in alvos:
            if c["chave"] in out:
                continue
            if re.search(re.escape(c["match"]), txt, re.IGNORECASE):
                out[c["chave"]] = urljoin(page.url, href)
    return out


def _rotulos_dos_links(page):
    """[(rótulo, href)] de cada link de caderno da página de seleção.

    O RÓTULO é o texto do elemento que CONTÉM o link, não o do link. Foi o que a
    página de 07/08/2026 mostrou: nos dias com edição extra o portal repete um
    link "Parte I (Poder Executivo)" idêntico ao normal, e a distinção — o
    "EDIÇÃO EXTRA" — fica na célula ao redor. Casar pelo texto da âncora deixaria
    as duas Partes I indistinguíveis."""
    saida = []
    anchors = page.locator("a[href*='mostra_edicao']")
    for i in range(anchors.count()):
        a = anchors.nth(i)
        try:
            texto = " ".join((a.inner_text() or "").split())
            href = a.get_attribute("href") or ""
            volta = a.evaluate(
                "e => { const p = e.closest('td,li,p,div,tr'); return p ? p.innerText : ''; }")
        except Exception:  # noqa: BLE001 - um link ilegível não pode parar a varredura
            continue
        rotulo = " ".join((volta or "").split()) or texto
        # Só aceita o texto do pai se ele CONTIVER o do link (se o pai for um
        # bloco grande com vários links, o texto dele não descreve este link).
        if texto and texto not in rotulo:
            rotulo = texto
        if rotulo and href:
            saida.append((rotulo, href))
    return saida


def _cadernos_extra(page, conhecidos=None):
    """Links da página de seleção que são de EDIÇÃO EXTRA -> pseudo-cadernos.

    A edição extra aparece como um link A MAIS na mesma página (ver o comentário
    de config.EXTRA_MATCH). Devolve entradas no mesmo formato de config.CADERNOS,
    para seguirem pelo MESMO caminho dos cadernos leves — inclusive a trava de
    idempotência, que passa a valer por (rótulo do extra, data) sem alteração.

    O rótulo herda o texto do PORTAL: "Parte I (Poder Executivo) EDIÇÃO EXTRA"
    vira "Parte I (Poder Executivo) EDICAO EXTRA" — exatamente o que já está
    gravado no índice para 07/08/2026.

    LOGA o rótulo de TODOS os links, reconhecidos ou não: é assim que uma redação
    nova do portal aparece no log, em vez de se perder."""
    links = _rotulos_dos_links(page)
    _log("links da pagina de selecao: " + " | ".join(r for r, _h in links))
    if not config.EXTRA_ATIVO:
        return []

    conhecidos = conhecidos if conhecidos is not None else config.CADERNOS
    rx_extra = re.compile(config.EXTRA_MATCH, re.IGNORECASE)
    reclamados, extras = set(), []
    for rotulo, href in links:
        if rx_extra.search(rotulo):
            extras.append((rotulo, href))
            continue
        # Cada caderno conhecido reclama UM link (o primeiro que casar): a página
        # repete rótulos, e sem isso o segundo "Poder Executivo" viraria o normal.
        for c in conhecidos:
            if c["chave"] not in reclamados and re.search(re.escape(c["match"]), rotulo,
                                                          re.IGNORECASE):
                reclamados.add(c["chave"])
                break
        else:
            _log(f"link nao reconhecido na pagina de selecao: '{rotulo}' "
                 "(nao casa caderno conhecido nem o padrao de edicao extra) -> "
                 "se for edicao extra, ajuste DOERJ_EXTRA_MATCH no .env")

    saida = []
    for n, (rotulo, href) in enumerate(extras, start=1):
        chave = "extra" if n == 1 else f"extra_{n}"
        # Rótulo do índice: o texto do portal sem acento, com o sufixo canônico no
        # lugar da redação que veio ("EDIÇÃO EXTRA", "Edicao Extra", ...).
        base = re.sub(config.EXTRA_MATCH, "", _sem_acento(rotulo),
                      flags=re.IGNORECASE).strip(" -–—")
        nome = f"{base or config.CADERNO_PARTE_I} {config.EXTRA_CADERNO_SUFIXO}"
        if n > 1:
            nome += f" {n}"
        saida.append({"chave": chave, "nome": " ".join(nome.split()), "match": rotulo,
                      "ativo": True, "estrategia": "leve", "extra": True,
                      "texto_link": rotulo, "url": urljoin(page.url, href)})
        _log(f"*** EDICAO EXTRA no portal: '{rotulo}' -> caderno '{saida[-1]['nome']}' ***")
    return saida


def baixar_cadernos(estrategias=("leve",), pular=None, incluir_extra=False):
    """Baixa em MEMÓRIA os cadernos de config.CADERNOS cuja 'estrategia' esteja em
    `estrategias`. NÃO salva nada em disco. Uma sessão do navegador para todos.

    `pular(nome, edition_date) -> bool`: se devolver True, o caderno NÃO é baixado
    (ex.: já está indexado) — evita rebaixar toda hora no job agendado.

    `incluir_extra=True` acrescenta os cadernos de EDIÇÃO EXTRA que estiverem na
    página de seleção naquele dia (ver `_cadernos_extra`). Na maioria dos dias não
    há nenhum e nada muda; nos dias em que há, custa um `goto` a mais.

    Devolve {"edition_date": "AAAA-MM-DD", "cadernos": [{...caderno, "bytes": b|None}]};
    os cadernos de edição extra vêm com "extra": True."""
    from playwright.sync_api import (
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )

    alvos = [c for c in config.CADERNOS if c.get("ativo") and c.get("estrategia") in estrategias]
    if not alvos:
        return {"edition_date": None, "cadernos": []}

    captures = []
    resultados = []
    edition_date = None
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.DOWNLOAD_HEADLESS)
        context = browser.new_context(
            ignore_https_errors=True, accept_downloads=True,
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        )
        context.set_default_timeout(config.NAV_TIMEOUT_MS)
        context.set_default_navigation_timeout(config.NAV_TIMEOUT_MS)

        def on_response(response):
            """Idem ao handler do download da Parte I, mas sem log: aqui passam
            vários cadernos na mesma sessão e a linha por captura poluiria o log."""
            try:
                if not _is_pdf_response(response.url, response.headers.get("content-type")) or not response.ok:
                    return
                body = response.body()
                if _looks_like_pdf(body):
                    captures.append((response.url, body))
            except Exception:
                pass

        context.on("response", on_response)
        page = context.new_page()
        try:
            _goto_cadernos(page)
            edition_date = _parse_edition_date_from_url(page.url) or _hoje_iso()
            # Edição EXTRA: entra na MESMA lista dos cadernos leves, então herda a
            # trava de idempotência, o download em memória e a indexação sem custo
            # de uma segunda sessão de navegador. Na maioria dos dias não há
            # nenhuma, e isto devolve lista vazia.
            candidatos = list(alvos)
            if incluir_extra:
                candidatos += _cadernos_extra(page)
            # Idempotência ANTES de baixar: se já está indexado, nem abre o caderno.
            pendentes = [c for c in candidatos if not (pular and pular(c["nome"], edition_date))]
            for c in candidatos:
                if c not in pendentes:
                    _log(f"caderno '{c['nome']}' ja indexado ({edition_date}) -> nao baixa.")
                    resultados.append({**c, "bytes": None})
            if not pendentes:
                _log("todos os cadernos ja indexados; nada a baixar.")
                # 'page' ja carregou a selecao; nada mais a fazer.
            # O caderno extra já traz a URL do próprio link; os fixos são casados
            # pelo texto, como sempre.
            links = _coletar_links_cadernos(page, [c for c in pendentes if not c.get("extra")])
            links.update({c["chave"]: c["url"] for c in pendentes if c.get("extra")})
            for c in pendentes:
                url = links.get(c["chave"])
                if not url:
                    _log(f"caderno '{c['nome']}' nao encontrado na pagina; pulando.")
                    resultados.append({**c, "bytes": None})
                    continue
                captures.clear()
                _log(f"abrindo caderno: {c['nome']}")
                try:
                    page.goto(url, wait_until="domcontentloaded")
                except PlaywrightTimeoutError:
                    pass
                best = _wait_for_capture(page, captures, config.NAV_TIMEOUT_MS)
                if best and _looks_like_pdf(best[1]):
                    _log(f"  {c['nome']}: {len(best[1])} bytes")
                    resultados.append({**c, "bytes": best[1]})
                else:
                    _log(f"  {c['nome']}: PDF nao capturado")
                    resultados.append({**c, "bytes": None})
        finally:
            context.close()
            browser.close()
    return {"edition_date": edition_date, "cadernos": resultados}


def main():
    """Linha de comando: baixa a última edição e informa o resultado.

    Ponto de entrada do passo 1/5 do pipeline. Uma exceção aqui vira código de
    saída 1, que o run_pipeline registra como aviso e segue — o PDF pode já estar
    baixado de uma execução anterior."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    result = download_diario()
    status = "ja existia" if result["already_existed"] else "baixado"
    print(f"[ok] {status}: {result['file_path']} ({result['bytes']} bytes)")


if __name__ == "__main__":
    main()
