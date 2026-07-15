"""
============================================================================
extrair.py · Etapa 1 do pipeline — PDF -> TEXTO (com o MinerU)
----------------------------------------------------------------------------
O MinerU lê o PDF do Diário e devolve o texto LIMPO, na ordem de leitura certa
(coluna por coluna), incluindo tabelas. O resultado que usamos é o
"..._content_list.json": cada trecho de texto com o número da página.

Processamento em FATIAS: editais grandes (79, 100+ páginas) numa rodada só
podem estourar a memória e derrubar o worker do MinerU. Por isso quebramos o
PDF em fatias de ~30 páginas (config.MINERU_SLICE), rodamos o MinerU em cada
fatia (uso de RAM controlado) e juntamos tudo num content_list.json só. Como a
numeração de página dentro de uma fatia é RELATIVA (0,1,2...), somamos o início
da fatia para virar o número ABSOLUTO da página.

Contornos da rede da SEFAZ (tratados aqui):
  - NO_PROXY  -> o proxy corporativo não atrapalha o serviço interno do MinerU;
  - truststore (no sitecustomize do venv) -> o Python confia no certificado da
    SEFAZ, permitindo baixar os modelos.

Uso:
    python src/extrair.py                 # extrai os PDFs ainda não extraídos
    python src/extrair.py --force         # reextrai todos
    python src/extrair.py "C:/.../DOERJ_2026-07-08.pdf"   # um PDF específico
============================================================================
"""
import argparse
import json
import os
import shutil          # remover pastas temporárias das fatias
import subprocess
import sys
from pathlib import Path

import fitz            # PyMuPDF — usado só para contar as páginas do PDF

import config


def content_list_json(pdf):
    """Caminho do *_content_list.json (já juntado) deste PDF. Pode não existir ainda.

    Procuramos em saida/<nome>/<metodo>/... — de propósito NÃO casa as pastas de
    fatia (que ficam em saida/_slices/...), para não pegar um JSON parcial."""
    stem = Path(pdf).stem
    achados = list(config.SAIDA_DIR.glob(f"{stem}/*/{stem}_content_list.json"))
    if achados:
        return achados[0]
    return config.SAIDA_DIR / stem / config.MINERU_METHOD / f"{stem}_content_list.json"


def ja_extraido(pdf):
    """True se o content_list.json (juntado) deste PDF já existe."""
    return content_list_json(pdf).exists()


def _mineru_bin():
    """Executável 'mineru' do MESMO ambiente (venv) deste script (não um global)."""
    bindir = Path(sys.executable).parent
    exe = bindir / ("mineru.exe" if os.name == "nt" else "mineru")
    return str(exe) if exe.exists() else "mineru"


def _env():
    """Variáveis de ambiente com os ajustes da rede SEFAZ (device + NO_PROXY)."""
    env = os.environ.copy()
    env["MINERU_DEVICE_MODE"] = config.MINERU_DEVICE
    env["NO_PROXY"] = config.NO_PROXY_HOSTS
    env["no_proxy"] = config.NO_PROXY_HOSTS
    return env


def _page_count(pdf):
    """Quantidade de páginas do PDF (via PyMuPDF)."""
    doc = fitz.open(pdf)
    try:
        return doc.page_count
    finally:
        doc.close()


def _rodar_fatia(pdf, out_dir, start, end):
    """Roda o MinerU numa fatia de páginas [start..end] (0-based, inclusivo).

    Devolve o content_list.json daquela fatia (numeração de página relativa)."""
    stem = Path(pdf).stem
    cmd = [
        _mineru_bin(),
        "-p", str(pdf),
        "-o", str(out_dir),
        "-b", "pipeline",
        "-m", config.MINERU_METHOD,
        "-f", config.MINERU_FORMULA,
        "-t", config.MINERU_TABLE,
        "-s", str(start),     # -s = página inicial (0-based)
        "-e", str(end),       # -e = página final (inclusiva)
    ]
    subprocess.run(cmd, check=True, env=_env())
    hits = list(Path(out_dir).glob(f"{stem}/*/{stem}_content_list.json"))
    return hits[0] if hits else None


def extrair(pdf, saida=None):
    """Extrai um PDF em fatias e grava o content_list.json juntado. Devolve o caminho."""
    pdf = Path(pdf)
    if not pdf.exists():
        sys.exit(f"[ERRO] PDF nao encontrado: {pdf}")
    saida = Path(saida) if saida else config.SAIDA_DIR
    stem = pdf.stem

    n = _page_count(pdf)
    passo = max(1, config.MINERU_SLICE)
    # Pasta temporária das fatias (fora do padrão saida/<stem>/, p/ não confundir).
    slices_root = saida / "_slices" / stem
    shutil.rmtree(slices_root, ignore_errors=True)   # começa limpo

    print(f"[..] MinerU: {pdf.name}  ({n} paginas em fatias de {passo}, device={config.MINERU_DEVICE})")
    merged = []                                       # aqui juntamos todos os elementos
    for start in range(0, n, passo):
        end = min(start + passo - 1, n - 1)           # última página da fatia (inclusiva)
        out_dir = slices_root / f"s{start:04d}"

        # Roda a fatia com 1 tentativa extra (o worker pode cair esporadicamente).
        cl = None
        for tentativa in (1, 2):
            try:
                cl = _rodar_fatia(pdf, out_dir, start, end)
                break
            except FileNotFoundError:
                sys.exit("[ERRO] 'mineru' nao encontrado. Instale: pip install -r requirements.txt")
            except subprocess.CalledProcessError as e:
                if tentativa == 2:
                    sys.exit(f"[ERRO] MinerU falhou na fatia p.{start+1}-{end+1} (codigo {e.returncode}).")
                print(f"   [retry] fatia p.{start+1}-{end+1} falhou; tentando de novo...")
                shutil.rmtree(out_dir, ignore_errors=True)
        if not cl:
            sys.exit(f"[ERRO] fatia p.{start+1}-{end+1} nao gerou content_list.json.")

        # Lê a fatia e converte page_idx relativo -> absoluto (soma o início).
        data = json.loads(cl.read_text(encoding="utf-8"))
        for e in data:
            e["page_idx"] = e.get("page_idx", 0) + start
        merged.extend(data)
        print(f"   fatia p.{start+1}-{end+1}: {len(data)} elementos")

    # Grava o content_list.json juntado na pasta padrão (onde o index_build procura).
    dest_dir = saida / stem / config.MINERU_METHOD
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem}_content_list.json"
    dest.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")

    shutil.rmtree(slices_root, ignore_errors=True)    # limpa as fatias temporárias
    print(f"[ok] {pdf.name}: {len(merged)} elementos -> {dest}")
    return dest


def main():
    ap = argparse.ArgumentParser(description="Extrai o texto dos PDFs do DOERJ com MinerU (em fatias).")
    ap.add_argument("pdf", nargs="?", default=None, help="PDF especifico (opcional)")
    ap.add_argument("--force", action="store_true", help="Reextrai mesmo se ja existir")
    args = ap.parse_args()

    if args.pdf:                                       # um PDF específico
        extrair(args.pdf)
        return

    pdfs = sorted(config.DOWNLOADS_DIR.glob("*.pdf"))  # todos os pendentes
    if not pdfs:
        sys.exit(f"[ERRO] nenhum PDF em {config.DOWNLOADS_DIR}")
    for p in pdfs:
        if ja_extraido(p) and not args.force:
            print(f"[skip] {p.name}: ja extraido")
            continue
        extrair(p)


if __name__ == "__main__":
    main()
