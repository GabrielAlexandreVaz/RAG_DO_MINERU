"""
============================================================================
run_pipeline.py · Orquestra a LEITURA do D.O., passo a passo
----------------------------------------------------------------------------
Roda cada etapa em sequência; SÓ avança se a anterior terminou com sucesso
(para no primeiro erro). Cada etapa é um subprocess, para isolar falhas — cada
uma tem seu próprio código de saída.

Etapas:
  0) Download (OPCIONAL, --download) -> download_diario.py (Playwright).
     Por padrão NÃO baixa: quem baixa é a tarefa "DOERJ Downloader" (projeto
     diario-rj), que já roda de hora em hora e deixa o PDF em DOWNLOADS_DIR.
  1) MinerU + indice -> index_build.py --latest (idempotente: pula se ja indexou).
  2) Exoneracoes -> Excel  -> exonerar.py      (pula se o .xlsx do dia ja existe).
  3) Atos de pessoal -> Oracle -> atos_pessoal.py (pula se ja gravado no banco).

POR QUE ISTO EXISTE (e roda de hora em hora): o D.O. nao tem hora fixa — pode
sair as 08:00 ou as 10:00. Um job que roda 1x as 08:05 perde a edicao quando ela
atrasa (foi o que aconteceu em 16/07: o site ficou no dia anterior). Rodando de
hora em hora, a leitura acontece assim que a edicao aparece.

E O CUSTO? Cada etapa tem sua TRAVA de idempotencia, entao as execucoes seguintes
do mesmo dia saem de graca: nao re-chamam a IA sobre uma edicao ja processada.

Uso:
    python src/run_pipeline.py                    # leitura (nao baixa)
    python src/run_pipeline.py --download         # baixa tambem (Playwright)
    python src/run_pipeline.py --tema exoneracao  # so um tema no Oracle
    python src/run_pipeline.py --force            # reprocessa tudo (GASTA IA)
============================================================================
"""
import argparse
import subprocess
import sys
from pathlib import Path

import config


def _run(desc, cmd):
    """Roda um comando e ABORTA o pipeline se ele falhar (codigo != 0)."""
    print(f"\n===== {desc} =====", flush=True)
    print(">", " ".join(str(c) for c in cmd), flush=True)
    result = subprocess.run([str(c) for c in cmd])
    if result.returncode != 0:
        sys.exit(f"[ERRO] etapa falhou: {desc} (codigo {result.returncode}). Pipeline interrompido.")
    print(f"[ok] etapa concluida: {desc}", flush=True)


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Pipeline de leitura do DOERJ (MinerU -> Excel -> Oracle).")
    ap.add_argument("--tema", default="all", help="Tema dos atos no Oracle (padrao: all)")
    ap.add_argument("--download", action="store_true",
                    help="Tambem baixa o D.O. (padrao: nao; quem baixa e a tarefa DOERJ Downloader)")
    ap.add_argument("--skip-cadernos", action="store_true",
                    help="Pula a leitura das Partes IB/II/IV/V (ex.: sem Playwright instalado)")
    ap.add_argument("--force", action="store_true",
                    help="Ignora as travas e reprocessa Excel/Oracle (GASTA IA)")
    args = ap.parse_args()

    rag_py = Path(sys.executable)          # o python DESTE processo = venv do RAG
    src = config.ROOT / "src"
    forca = ["--force"] if args.force else []

    # 0) Download (opt-in). Requer: pip install playwright.
    if args.download:
        _run("0/3 Download do D.O. (Playwright)", [rag_py, src / "download_diario.py"])

    # 1) MinerU + indice da Parte I (edicao mais recente). Idempotente e barato.
    _run("1/4 MinerU + indice Parte I (index_build --latest)",
         [rag_py, src / "index_build.py", "--latest"])

    # 2) Cadernos leves (IB/II/IV/V): le em memoria e indexa (sem salvar PDF).
    #    So roda se o Playwright estiver instalado; senao, avisa e segue.
    if args.skip_cadernos:
        print("[skip] passo 2 (cadernos leves) pulado por --skip-cadernos.", flush=True)
    else:
        _run("2/4 Cadernos leves IB/II/IV/V (ler_cadernos)", [rag_py, src / "ler_cadernos.py"])

    # 3) Exoneracoes -> Excel no OneDrive.
    _run("3/4 Exoneracoes -> Excel", [rag_py, src / "exonerar.py"] + forca)

    # 4) Atos de pessoal -> Oracle.
    _run("4/4 Atos de pessoal -> Oracle",
         [rag_py, src / "atos_pessoal.py", "--tema", args.tema] + forca)

    print("\n[done] pipeline completo com sucesso.", flush=True)


if __name__ == "__main__":
    main()
