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
  6) EDICAO EXTRA -> monitor_estruturado.py --extra. As vezes o IOERJ publica uma
     segunda edicao no mesmo dia; ela aparece como um caderno A MAIS na pagina de
     selecao e o passo 3 ja a indexa. Aqui ela e lida SOZINHA (o diario do dia nao
     e relido), com saida *_EXTRA propria. Nos dias sem edicao extra - a maioria -
     este passo sai na hora, sem chamar a IA.
  7) Boletins por e-mail -> enviar_email.py (o do dia e, se houver, o da extra).
     Manda o HTML no CORPO da mensagem pela API corporativa de e-mail (um POST
     por destinatario), 1x por edicao (marcador em relatorios\) e so se a rodada
     foi COMPLETA. Tambem NAO derruba o pipeline. Enquanto EMAIL_ENVIO_ATIVO=false,
     grava relatorios\email_<data>.json em vez de chamar a API.
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
    """Roda as 7 etapas em sequência, cada uma num subprocesso próprio.

    Subprocesso em vez de import para isolar falhas: cada etapa tem seu código de
    saída, e um travamento do MinerU não leva junto o restante. Download, e-mail e
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
        print("\n===== 1/7 Download do D.O. (Playwright) =====", flush=True)
        if subprocess.run([str(rag_py), str(src / "download_diario.py")]).returncode != 0:
            print("[ATENCAO] o download falhou; seguindo com o que ja estiver baixado.",
                  flush=True)

    # 1) MinerU + indice da Parte I (edicao mais recente). Idempotente e barato.
    _run("2/7 MinerU + indice Parte I (index_build --latest)",
         [rag_py, src / "index_build.py", "--latest"])

    # 2) Cadernos leves (IB/II/IV/V): le em memoria e indexa (sem salvar PDF).
    #    So roda se o Playwright estiver instalado; senao, avisa e segue.
    if args.skip_cadernos:
        print("[skip] passo 3/7 (cadernos leves) pulado por --skip-cadernos.", flush=True)
    else:
        _run("3/7 Cadernos leves IB/II/IV/V (ler_cadernos)", [rag_py, src / "ler_cadernos.py"])

    # 3) Atos de pessoal -> Oracle (tabela 001A).
    _run("4/7 Atos de pessoal -> Oracle (001A)",
         [rag_py, src / "atos_pessoal.py", "--tema", args.tema] + forca)

    # 4) Monitor estruturado (8 temas) -> Excel + boletim HTML + Oracle (002A). Usa o MONITOR_MODEL
    #    (Haiku, mais barato). Idempotente: pula se o Excel canonico do dia ja existe.
    _run("5/7 Monitor 8 temas -> Excel + boletim HTML + Oracle (002A)",
         [rag_py, src / "monitor_estruturado.py"] + forca)

    # 5) EDICAO EXTRA. Nem todo dia tem uma; nos dias normais este passo sai em
    #    menos de um segundo, ANTES de qualquer chamada de IA (nao ha caderno de
    #    edicao extra indexado na data). Quando tem, le SO o caderno novo — o
    #    diario do dia nao e relido — e gera o *_EXTRA proprio.
    _run("6/7 Edicao extra (le so o caderno novo, se houver)",
         [rag_py, src / "monitor_estruturado.py", "--extra"] + forca)

    # 6) Boletins por e-mail (o do dia e, se houver, o da edicao extra). NAO usa
    #    _run, pela mesma razao do download e da limpeza: uma falha da API de
    #    e-mail nao pode marcar como fracassada uma execucao que ja gravou tudo
    #    no Oracle.
    #    Cada envio tem trava propria (so rodada COMPLETA, so uma vez por edicao),
    #    entao rodar de hora em hora nao gera e-mail repetido.
    print("\n===== 7/7 Boletins por e-mail =====", flush=True)
    for rotulo, extra in (("edicao do dia", []), ("edicao extra", ["--extra"])):
        if subprocess.run([str(rag_py), str(src / "enviar_email.py")]
                          + extra + forca).returncode != 0:
            print(f"[ATENCAO] o envio do boletim ({rotulo}) falhou; o pipeline em si "
                  "terminou bem.", flush=True)

    # 6) Retencao de disco. NAO usa _run: falha de limpeza nao pode marcar como
    #    fracassada uma execucao que ja gravou tudo no Oracle.
    print("\n===== Limpeza (retencao de disco) =====", flush=True)
    if subprocess.run([str(rag_py), str(src / "limpar.py")]).returncode != 0:
        print("[ATENCAO] a limpeza falhou; o pipeline em si terminou bem.", flush=True)

    print("\n[done] pipeline completo com sucesso.", flush=True)


if __name__ == "__main__":
    main()
