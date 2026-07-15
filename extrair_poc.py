"""
Prova de Conceito — Extração do Diário Oficial com MinerU (CPU / PDF com texto)
------------------------------------------------------------------------------
Pega UM PDF, roda o MinerU no modo pipeline em CPU e mostra onde ficou o
Markdown limpo (pronto para, na fase 2, virar chunks + embeddings + RAG).

Uso:
    python extrair_poc.py "C:/caminho/para/diario_oficial.pdf"
    python extrair_poc.py "C:/caminho/para/diario.pdf" --saida ./saida

Por que via CLI (subprocess) e não import direto do SDK:
    A CLI `mineru` é a interface mais estável entre versões. Para uma PoC,
    é o caminho com menos surpresa. Na fase 2 trocamos pelo SDK Python se
    quisermos integrar tudo num processo só.
"""

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

# Pasta onde o job salva o Diário (DOERJ) todo dia. Pode sobrescrever com --pasta.
PASTA_PADRAO = r"C:\Users\Gsilva11\OneDrive - SEFAZ-RJ\DIRETORIO_AGENTE_DO_A01"

# Nome dos arquivos: DOERJ_AAAA-MM-DD.pdf
PADRAO_DATA = re.compile(r"(\d{4}-\d{2}-\d{2})")


def pdf_mais_recente(pasta: Path) -> Path:
    """Escolhe o PDF mais atual da pasta pela DATA no nome (fallback: data de modificação)."""
    if not pasta.is_dir():
        sys.exit(f"[ERRO] Pasta não encontrada: {pasta}")

    pdfs = list(pasta.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"[ERRO] Nenhum PDF em: {pasta}")

    def chave(p: Path):
        m = PADRAO_DATA.search(p.name)
        # (tem_data, data_no_nome, mtime) — ordena por data do nome; sem data, cai no mtime
        return (1, m.group(1)) if m else (0, str(p.stat().st_mtime))

    escolhido = max(pdfs, key=chave)
    print(f">> Diário mais recente encontrado: {escolhido.name}\n")
    return escolhido


def extrair(pdf: Path, saida: Path, metodo: str = "txt", device: str = "cpu",
            formula: bool = False, tabela: bool = True) -> Path:
    """Roda o MinerU e devolve o caminho do .md gerado."""
    if not pdf.exists():
        sys.exit(f"[ERRO] PDF não encontrado: {pdf}")

    saida.mkdir(parents=True, exist_ok=True)

    # Usa o 'mineru' do MESMO ambiente que está rodando este script (o venv),
    # e não um eventual 'mineru' global quebrado no PATH.
    bindir = Path(sys.executable).parent            # .venv/Scripts
    mineru_exe = bindir / ("mineru.exe" if os.name == "nt" else "mineru")
    mineru_bin = str(mineru_exe) if mineru_exe.exists() else "mineru"

    # metodo=txt -> usa a camada de texto do PDF (rápido em CPU, sem OCR pesado).
    #   Ideal para o seu caso: Diário com texto selecionável.
    # Se um dia chegar um PDF escaneado, troque para --method ocr.
    # backend=pipeline -> leve, roda local sem VLM (o default 'hybrid-engine' é pesado demais p/ CPU).
    cmd = [
        mineru_bin,
        "-p", str(pdf),
        "-o", str(saida),
        "-b", "pipeline",     # backend pipeline (leve, sem VLM)
        "-m", metodo,         # auto | txt | ocr
        # Diário Oficial NÃO tem fórmulas matemáticas -> desligamos o reconhecimento
        # de fórmulas. Isso evita baixar o modelo MFR (unimernet, ~810MB) que travava
        # na rede da SEFAZ. Tabelas ficam ligadas (úteis p/ licitações/concursos).
        "-f", "true" if formula else "false",
        "-t", "true" if tabela else "false",
    ]

    # No MinerU 3.x o device é definido por variável de ambiente, não por flag.
    env = os.environ.copy()
    env["MINERU_DEVICE_MODE"] = device   # cpu | cuda

    # Rede corporativa (SEFAZ): o proxy intercepta até o localhost e derruba o
    # serviço interno do MinerU (erro "403 Forbidden" em 127.0.0.1). Isentamos o
    # localhost do proxy para o MinerU falar consigo mesmo.
    sem_proxy = "127.0.0.1,localhost,::1"
    env["NO_PROXY"] = sem_proxy
    env["no_proxy"] = sem_proxy

    print(">> Rodando MinerU:\n   " + " ".join(cmd) + f"\n   (MINERU_DEVICE_MODE={device})\n")
    try:
        subprocess.run(cmd, check=True, env=env)
    except FileNotFoundError:
        sys.exit(
            "[ERRO] Comando 'mineru' não encontrado.\n"
            "       Instale com:  pip install -r requirements.txt"
        )
    except subprocess.CalledProcessError as e:
        sys.exit(f"[ERRO] MinerU falhou (código {e.returncode}).")

    # O MinerU cria: saida/<nome_do_pdf>/<metodo>/<nome_do_pdf>.md
    mds = list(saida.rglob("*.md"))
    if not mds:
        sys.exit("[ERRO] Nenhum .md gerado — verifique a saída do MinerU acima.")

    md = max(mds, key=lambda p: p.stat().st_mtime)
    return md


def main() -> None:
    ap = argparse.ArgumentParser(description="PoC: extrair Diário Oficial com MinerU")
    ap.add_argument("pdf", nargs="?", default=None,
                    help="Caminho de um PDF específico (opcional). Sem isto, usa o mais recente da --pasta")
    ap.add_argument("--pasta", default=PASTA_PADRAO,
                    help="Pasta do OneDrive com os Diários (padrão: pasta do job)")
    ap.add_argument("--saida", default="./saida", help="Pasta de saída (padrão: ./saida)")
    ap.add_argument("--metodo", default="txt", choices=["auto", "txt", "ocr"])
    ap.add_argument("--formula", action="store_true", help="Ativa reconhecimento de fórmulas (desligado por padrão; Diário não tem)")
    ap.add_argument("--sem-tabela", action="store_true", help="Desativa reconhecimento de tabelas")
    args = ap.parse_args()

    # Sem PDF explícito -> pega o Diário mais recente da pasta.
    pdf = Path(args.pdf) if args.pdf else pdf_mais_recente(Path(args.pasta))

    md = extrair(pdf, Path(args.saida), metodo=args.metodo,
                 formula=args.formula, tabela=not args.sem_tabela)

    conteudo = md.read_text(encoding="utf-8", errors="replace")
    print("\n" + "=" * 70)
    print(f"OK! Markdown gerado em: {md}")
    print(f"Tamanho: {len(conteudo):,} caracteres")
    print("=" * 70)
    print("\n--- PRÉVIA (primeiras 2000 chars) ---\n")
    print(conteudo[:2000])
    print("\n--- fim da prévia ---")


if __name__ == "__main__":
    main()
