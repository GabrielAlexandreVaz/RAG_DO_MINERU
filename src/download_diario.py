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
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import config

_ULTIMA_EDICAO_RE = re.compile(r"[uú]ltima\s+edi[cç][aã]o", re.IGNORECASE)
_SELECIONA_RE = re.compile(r"do_seleciona_edicao\.php", re.IGNORECASE)
_CLIQUE_AQUI_RE = re.compile(r"clique\s+aqui", re.IGNORECASE)
_PDF_URL_RE = re.compile(r"mostra_pdf\.php", re.IGNORECASE)


# ------------------------------------------------------------ helpers de data/PDF
def _now():
    return datetime.now(ZoneInfo(config.DOWNLOAD_TZ))


def _hoje_iso():
    return _now().strftime("%Y-%m-%d")


def pdf_name(edition_date):
    """DOERJ_AAAA-MM-DD.pdf para uma data de edição YYYY-MM-DD."""
    return f"DOERJ_{edition_date}.pdf"


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
    ct = (content_type or "").lower()
    return bool(_PDF_URL_RE.search(url)) or "application/pdf" in ct


def _log(msg):
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
    deadline = time.monotonic() + timeout_ms / 1000
    while time.monotonic() < deadline:
        if captures:
            return max(captures, key=lambda c: len(c[1]))   # maior corpo = a edição
        viewer.wait_for_timeout(500)                        # pump de eventos
    return captures[0] if captures else None


def _tentar_botao_download(page, target):
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


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    result = download_diario()
    status = "ja existia" if result["already_existed"] else "baixado"
    print(f"[ok] {status}: {result['file_path']} ({result['bytes']} bytes)")


if __name__ == "__main__":
    main()
