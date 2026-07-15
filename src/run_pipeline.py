"""
============================================================================
run_pipeline.py · Orquestra o pipeline diário, passo a passo
----------------------------------------------------------------------------
Roda cada etapa em sequência; SÓ avança se a anterior terminou com sucesso
(para no primeiro erro, com codigo != 0). As etapas são projetos/ambientes
DIFERENTES, então cada uma roda com o SEU proprio python (venv), via subprocess.

Etapas:
  1) Download do D.O.  -> download_diario.py (Playwright): baixa a Parte I da
     ultima edicao e salva DOERJ_AAAA-MM-DD.pdf em config.DOWNLOADS_DIR.
  2) MinerU + indice   -> index_build.py --latest extrai (MinerU) a edicao do
     dia e indexa no FTS5.
  3) Atos de pessoal   -> atos_pessoal.py --tema all grava os atos no Oracle.

Tudo roda no MESMO venv (projeto autocontido). Os passos 2 e 3 sao chamados por
subprocess so para isolar falhas (cada etapa tem seu proprio codigo de saida).

Uso:
    python src/run_pipeline.py
    python src/run_pipeline.py --tema exoneracao      # so um tema no passo 3
    python src/run_pipeline.py --skip-download        # pula o passo 1 (reprocessa)
============================================================================
"""
import argparse
import subprocess
import sys
from pathlib import Path

import config


def _run(desc, cmd, cwd=None):
    """Roda um comando e ABORTA o pipeline se ele falhar (codigo != 0)."""
    print(f"\n===== {desc} =====", flush=True)
    print(">", " ".join(str(c) for c in cmd), flush=True)
    result = subprocess.run([str(c) for c in cmd], cwd=str(cwd) if cwd else None)
    if result.returncode != 0:
        sys.exit(f"[ERRO] etapa falhou: {desc} (codigo {result.returncode}). Pipeline interrompido.")
    print(f"[ok] etapa concluida: {desc}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Pipeline diario do DOERJ (download -> MinerU -> Oracle).")
    ap.add_argument("--tema", default="all", help="Tema do passo 3 (padrao: all)")
    ap.add_argument("--skip-download", action="store_true", help="Pula o passo 1 (nao baixa)")
    args = ap.parse_args()

    rag_py = Path(sys.executable)          # o python DESTE processo = venv do RAG
    src = config.ROOT / "src"

    # 1) Download do D.O. (Playwright, mesmo venv).
    if args.skip_download:
        print("[skip] passo 1 (download) pulado por --skip-download.", flush=True)
    else:
        _run("1/3 Download do D.O. (Playwright)", [rag_py, src / "download_diario.py"])

    # 2) MinerU + indice da edicao mais recente (venv do RAG).
    _run("2/3 MinerU + indice (index_build --latest)",
         [rag_py, src / "index_build.py", "--latest"])

    # 3) Atos de pessoal -> Oracle (venv do RAG).
    _run("3/3 Atos de pessoal -> Oracle",
         [rag_py, src / "atos_pessoal.py", "--tema", args.tema])

    print("\n[done] pipeline completo com sucesso.", flush=True)


if __name__ == "__main__":
    main()
