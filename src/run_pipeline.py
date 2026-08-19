"""
============================================================================
run_pipeline.py · Orquestra a LEITURA do D.O., passo a passo
----------------------------------------------------------------------------
Roda cada etapa em sequência; SÓ avança se a anterior terminou com sucesso
(para no primeiro erro). Cada etapa é um subprocess, para isolar falhas — cada
uma tem seu próprio código de saída.

Etapas:
  1) Download (OPCIONAL, --download) -> download_diario.py (Playwright).
     O run_pipeline.bat usa --download. NÃO derruba o pipeline se falhar: o PDF
     pode já estar baixado de uma execução anterior e faltar só gravar no Oracle.
  2) MinerU + indice -> index_build.py --latest (idempotente: pula se ja indexou).
  3) Cadernos leves IB/II/IV/V -> ler_cadernos.py (indexa sem salvar PDF).
  4) Atos de pessoal -> Oracle 001A -> atos_pessoal.py (pula se ja gravado).
  5) Monitor 8 temas -> Excel + boletim HTML + Oracle 002A -> monitor_estruturado.py.
     O boletim (relatorios/boletim_<data>.html) e a MESMA extracao no formato do
     e-mail de monitoramento; sai junto com o Excel, sem chamada extra de IA.
  +) Limpeza (retenção de disco) -> limpar.py. Também não derruba o pipeline.

O Excel diário das exonerações saiu do pipeline: as exonerações já vão para a
tabela 001A no passo 4 (RESPOSTA='Exoneração'), então gerá-lo era ler as mesmas
páginas uma segunda vez com o Opus (~72 mil tokens/dia) para produzir a mesma
informação noutro formato.

POR QUE ISTO EXISTE (e roda de hora em hora): o D.O. nao tem hora fixa — pode
sair as 08:00 ou as 10:00. Um job que roda 1x as 08:05 perde a edicao quando ela
atrasa (foi o que aconteceu em 16/07: o site ficou no dia anterior). Rodando de
hora em hora, a leitura acontece assim que a edicao aparece.

E O CUSTO? Cada etapa tem sua TRAVA de idempotencia, entao as execucoes seguintes
do mesmo dia saem de graca: nao re-chamam a IA sobre uma edicao ja processada.

Uso:
    python src/run_pipeline.py                    # leitura (nao baixa)
    python src/run_pipeline.py --download         # baixa tambem (Playwright)
    python src/run_pipeline.py --tema exonerar    # so uma palavra-chave no Oracle
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
    """Roda as 5 etapas em sequência, cada uma num subprocesso próprio.

    Subprocesso em vez de import para isolar falhas: cada etapa tem seu código de
    saída, e um travamento do MinerU não leva junto o restante. Download e
    limpeza são as exceções que NÃO derrubam o pipeline — as demais param no
    primeiro erro, porque cada uma depende da anterior."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    ap = argparse.ArgumentParser(description="Pipeline de leitura do DOERJ (MinerU -> Excel -> Oracle).")
    ap.add_argument("--tema", default="all",
                    help="Palavra-chave da 001B (ex.: exonerar) ou 'all' (padrao: all)")
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
    #    NAO usa _run de proposito: uma instabilidade do portal do IOERJ nao pode
    #    impedir as etapas seguintes. O PDF pode ja estar em DOWNLOADS_DIR de uma
    #    execucao anterior (o job roda de hora em hora) e faltar so a gravacao no
    #    Oracle. Se nao houver nada para ler, o passo 1 falha com mensagem propria.
    if args.download:
        print("\n===== 1/5 Download do D.O. (Playwright) =====", flush=True)
        if subprocess.run([str(rag_py), str(src / "download_diario.py")]).returncode != 0:
            print("[ATENCAO] o download falhou; seguindo com o que ja estiver baixado.",
                  flush=True)

    # 1) MinerU + indice da Parte I (edicao mais recente). Idempotente e barato.
    _run("2/5 MinerU + indice Parte I (index_build --latest)",
         [rag_py, src / "index_build.py", "--latest"])

    # 2) Cadernos leves (IB/II/IV/V): le em memoria e indexa (sem salvar PDF).
    #    So roda se o Playwright estiver instalado; senao, avisa e segue.
    if args.skip_cadernos:
        print("[skip] passo 3/5 (cadernos leves) pulado por --skip-cadernos.", flush=True)
    else:
        _run("3/5 Cadernos leves IB/II/IV/V (ler_cadernos)", [rag_py, src / "ler_cadernos.py"])

    # 3) Atos de pessoal -> Oracle (tabela 001A).
    _run("4/5 Atos de pessoal -> Oracle (001A)",
         [rag_py, src / "atos_pessoal.py", "--tema", args.tema] + forca)

    # 4) Monitor estruturado (8 temas) -> Excel + boletim HTML + Oracle (002A). Usa o MONITOR_MODEL
    #    (Haiku, mais barato). Idempotente: pula se o Excel canonico do dia ja existe.
    _run("5/5 Monitor 8 temas -> Excel + boletim HTML + Oracle (002A)",
         [rag_py, src / "monitor_estruturado.py"] + forca)

    # 6) Retencao de disco. NAO usa _run: falha de limpeza nao pode marcar como
    #    fracassada uma execucao que ja gravou tudo no Oracle.
    print("\n===== Limpeza (retencao de disco) =====", flush=True)
    if subprocess.run([str(rag_py), str(src / "limpar.py")]).returncode != 0:
        print("[ATENCAO] a limpeza falhou; o pipeline em si terminou bem.", flush=True)

    print("\n[done] pipeline completo com sucesso.", flush=True)


if __name__ == "__main__":
    main()
