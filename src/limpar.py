"""
============================================================================
limpar.py · Retenção de disco — apaga saída antiga do MinerU e rotaciona o log
----------------------------------------------------------------------------
POR QUE ISTO EXISTE: cada edição deixa ~6 MB em saida/ e ~2 MB no índice, e o
pipeline roda todo dia útil — algo como 2 GB por ano num servidor que ninguém
olha. O log também cresce sem parar (já passou de 1 MB).

O QUE PODE SER APAGADO: a pasta saida/<edição> guarda o Markdown e o
content_list.json que o MinerU produziu. Depois que o index_build leu esse JSON,
o texto vive no índice FTS5 (data/index/doerj_fts.db) — a saída só serve para
REINDEXAR sem reprocessar o PDF. Por isso ela é descartável passado algum tempo.

O QUE NÃO É APAGADO: o índice, os relatórios, os Excel e os PDFs baixados. Nada
que alguém consuma.

Uso:
    python src/limpar.py                 # aplica a retenção do .env (padrão 30 dias)
    python src/limpar.py --dias 7        # outro prazo
    python src/limpar.py --simular       # só mostra o que apagaria
============================================================================
"""
import argparse
import datetime
import re
import shutil
import sys

import config

# Nome da edição dentro de saida/: DOERJ_AAAA-MM-DD
_RE_EDICAO = re.compile(r"(\d{4})-(\d{2})-(\d{2})")

# Tamanho a partir do qual o log é rotacionado (vira .1 e recomeça).
LOG_MAX_BYTES = int(5 * 1024 * 1024)


def _tamanho(pasta):
    """Soma o tamanho dos arquivos de uma pasta (em bytes)."""
    return sum(f.stat().st_size for f in pasta.rglob("*") if f.is_file())


def _data_da_pasta(pasta):
    """Data da edição a partir do nome da pasta; None se não der para ler."""
    m = _RE_EDICAO.search(pasta.name)
    if not m:
        return None
    try:
        return datetime.date(*(int(g) for g in m.groups()))
    except ValueError:
        return None


def limpar_saida(dias, simular=False):
    """Apaga saida/<edição> mais velha que `dias`. Devolve (nº pastas, bytes)."""
    if dias <= 0:
        print("[limpar] retencao desligada (dias=0) -> saida/ preservada.")
        return 0, 0
    if not config.SAIDA_DIR.exists():
        return 0, 0

    corte = datetime.date.today() - datetime.timedelta(days=dias)
    n = total = 0
    for pasta in sorted(config.SAIDA_DIR.iterdir()):
        if not pasta.is_dir() or pasta.name.startswith("_"):   # _slices é temporária
            continue
        data = _data_da_pasta(pasta)
        if data is None:
            print(f"[limpar] {pasta.name}: sem data no nome -> mantida por seguranca.")
            continue
        if data >= corte:
            continue
        tam = _tamanho(pasta)
        print(f"[limpar] {'(simulacao) ' if simular else ''}{pasta.name} "
              f"({data}, {tam / 1e6:.1f} MB)")
        if not simular:
            shutil.rmtree(pasta, ignore_errors=True)
        n += 1
        total += tam
    return n, total


def rotacionar_logs(simular=False):
    """Renomeia para .1 os logs que passaram de LOG_MAX_BYTES. Devolve o nº rotacionado."""
    pasta = config.ROOT / "logs"
    if not pasta.exists():
        return 0
    n = 0
    for log in pasta.glob("*.log"):
        if log.stat().st_size < LOG_MAX_BYTES:
            continue
        print(f"[limpar] {'(simulacao) ' if simular else ''}rotacionando {log.name} "
              f"({log.stat().st_size / 1e6:.1f} MB)")
        if not simular:
            antigo = log.with_suffix(".log.1")
            antigo.unlink(missing_ok=True)      # guarda só a geração anterior
            log.rename(antigo)
        n += 1
    return n


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    ap = argparse.ArgumentParser(description="Retencao de disco do pipeline do DOERJ.")
    ap.add_argument("--dias", type=int, default=None,
                    help=f"Dias de saida/ a preservar (padrao: {config.RETENCAO_DIAS}, do .env)")
    ap.add_argument("--simular", action="store_true", help="So mostra o que seria apagado")
    args = ap.parse_args()

    dias = config.RETENCAO_DIAS if args.dias is None else args.dias
    print(f"[limpar] retencao: {dias} dia(s) | saida: {config.SAIDA_DIR}")
    n, bytes_ = limpar_saida(dias, args.simular)
    logs = rotacionar_logs(args.simular)
    print(f"[done] {n} pasta(s) de saida ({bytes_ / 1e6:.1f} MB) e {logs} log(s) "
          f"{'seriam tratados' if args.simular else 'tratados'}.")


if __name__ == "__main__":
    main()
