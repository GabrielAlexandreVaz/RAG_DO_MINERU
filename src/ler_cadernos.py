"""
============================================================================
ler_cadernos.py · Lê as Partes IB/II/IV/V do DOERJ SEM salvar PDF
----------------------------------------------------------------------------
Busca EM MEMÓRIA os cadernos "leves" (config.CADERNOS com estrategia='leve'),
extrai o texto com PyMuPDF e indexa no FTS5 com o rótulo do caderno. Nenhum PDF
é salvo em disco — por isso essas partes ficam pesquisáveis no site, mas SEM
miniatura de página (a miniatura precisa do arquivo).

A Parte I NÃO é tratada aqui (continua com o MinerU, via index_build --latest).

Idempotente: pula caderno+edição já indexado.

Uso:
    python src/ler_cadernos.py
============================================================================
"""
import sys

import config
import download_diario
import index_build


def ler():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    # Playwright é opcional: se não estiver instalado, avisa e sai SEM erro, para
    # não derrubar o pipeline (a Parte I e as demais etapas seguem funcionando).
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[cadernos] AVISO: playwright nao instalado -> nao da para ler os cadernos "
              "leves. Rode: pip install -r requirements.txt && python -m playwright install chromium")
        return

    print("[cadernos] buscando cadernos leves (IB/II/IV/V) em memoria (Playwright)...")
    con = index_build.connect()
    try:
        # Passa a checagem de idempotência ao downloader: caderno já indexado
        # NÃO é baixado de novo (economiza o navegador no job de hora em hora).
        res = download_diario.baixar_cadernos(
            estrategias=("leve",),
            pular=lambda nome, date: index_build.caderno_indexado(con, nome, date),
        )
        date = res.get("edition_date")
        cadernos = res.get("cadernos", [])
        if not date or not cadernos:
            print("[cadernos] nada a processar.")
            return

        total = 0
        for c in cadernos:
            if not c.get("bytes"):          # já indexado (pulado) ou PDF não capturado
                continue
            n = index_build.index_caderno_bytes(con, c["nome"], date, c["bytes"], chave=c["chave"])
            print(f"[ok] {c['nome']} ({date}): +{n} paginas indexadas (PDF nao salvo)")
            total += n
        print(f"[done] {total} paginas de cadernos leves indexadas para {date}.")
    finally:
        con.close()


if __name__ == "__main__":
    ler()
