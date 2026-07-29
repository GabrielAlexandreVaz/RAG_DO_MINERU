"""
============================================================================
verificar.py · Confere se a máquina está pronta para rodar o pipeline
----------------------------------------------------------------------------
Feito para ser a PRIMEIRA coisa a rodar depois do instalar.bat, e para servir
de diagnóstico quando o job falhar no servidor. Cada verificação é isolada:
uma falha não impede as seguintes, e no fim sai um resumo.

Uso:
    .venv\\Scripts\\python.exe deploy\\verificar.py

Sai com código 1 se alguma verificação ESSENCIAL falhar (serve para o .bat
abortar antes de agendar a tarefa).
============================================================================
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

OK, FALHA, AVISO = "[ok]   ", "[FALHA]", "[aviso]"
_resultados = []


def checar(nome, essencial=True):
    """Decorador: roda a função, captura a exceção e registra o resultado."""
    def wrap(fn):
        try:
            detalhe = fn()
            _resultados.append((OK, nome, detalhe or ""))
        except Exception as e:  # noqa: BLE001 - o objetivo é justamente reportar
            _resultados.append((FALHA if essencial else AVISO, nome,
                                f"{type(e).__name__}: {str(e)[:160]}"))
        return fn
    return wrap


# --- 1. Interpretador ------------------------------------------------------
@checar("Python do venv")
def _python():
    v = sys.version_info
    if v < (3, 10):
        raise RuntimeError(f"Python {v.major}.{v.minor} - o projeto pede 3.10+")
    if not (ROOT / ".venv") .exists():
        raise RuntimeError(".venv nao encontrado na raiz do projeto")
    return f"{v.major}.{v.minor}.{v.micro} em {sys.prefix}"


# --- 2. truststore (proxy com inspeção TLS) --------------------------------
@checar("truststore ativo (sitecustomize)")
def _truststore():
    import ssl
    import truststore
    site = Path(truststore.__file__).parent.parent / "sitecustomize.py"
    if not site.exists():
        raise RuntimeError(f"sitecustomize.py ausente em {site.parent} - "
                           "rode deploy\\instalar.bat")
    # inject_into_ssl troca a classe padrão de SSLContext.
    if ssl.SSLContext is truststore.SSLContext:
        return "injetado"
    raise RuntimeError("truststore instalado mas NAO injetado no ssl")


@checar("HTTPS ate o portal do IOERJ")
def _https():
    import urllib.request
    import config
    with urllib.request.urlopen(config.PORTAL_URL, timeout=30) as r:
        return f"{config.PORTAL_URL} -> HTTP {r.status}"


# --- 3. Dependências -------------------------------------------------------
@checar("Bibliotecas importaveis")
def _libs():
    import importlib
    faltando = []
    for mod, pip in [("anthropic", "anthropic"), ("oracledb", "oracledb"),
                     ("fitz", "PyMuPDF"), ("xlsxwriter", "xlsxwriter"),
                     ("reportlab", "reportlab"), ("playwright", "playwright"),
                     ("mineru", "mineru[core]"), ("dotenv", "python-dotenv")]:
        try:
            importlib.import_module(mod)
        except ImportError:
            faltando.append(pip)
    if faltando:
        raise RuntimeError("faltam: " + ", ".join(faltando))
    return "todas presentes"


@checar("Chromium do Playwright")
def _chromium():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        exe = Path(pw.chromium.executable_path)
        if not exe.exists():
            raise RuntimeError("nao instalado - rode: python -m playwright install chromium")
        return str(exe.parent.parent.name)


@checar("Modelos do MinerU")
def _mineru():
    import json
    cfg = Path(os.getenv("MINERU_TOOLS_CONFIG_JSON", Path.home() / "mineru.json"))
    if not cfg.exists():
        raise RuntimeError(f"{cfg} nao existe - rode: python -m mineru.cli.models_download "
                           "-s modelscope -m pipeline")
    d = json.loads(cfg.read_text(encoding="utf-8"))
    modelos = Path((d.get("models-dir") or {}).get("pipeline", ""))
    if not modelos.exists():
        raise RuntimeError(f"models-dir do {cfg.name} aponta para pasta inexistente: {modelos}")
    return str(modelos)


@checar("Executavel do MinerU")
def _mineru_bin():
    import extrair
    b = extrair._mineru_bin()
    if b == "mineru" and not Path(sys.prefix, "Scripts", "mineru.exe").exists():
        raise RuntimeError("mineru.exe nao encontrado no venv")
    return b


# --- 4. Configuração e pastas ---------------------------------------------
@checar("Variaveis do .env")
def _env():
    import config
    if not (ROOT / ".env").exists():
        raise RuntimeError(".env nao existe - copie o .env.example e preencha")
    faltando = [n for n, v in [("ANTHROPIC_API_KEY", config.current_api_key()),
                               ("RAG_MODEL", config.MODEL)] if not v]
    if faltando:
        raise RuntimeError("vazias: " + ", ".join(faltando))
    return f"modelo={config.MODEL} | monitor={config.MONITOR_MODEL}"


@checar("Pastas de dados (existem e aceitam escrita)")
def _pastas():
    import config
    problemas = []
    for nome in ("DOWNLOADS_DIR", "EXONERACOES_DIR", "SAIDA_DIR", "INDEX_DIR",
                 "RELATORIOS_DIR", "SCREENSHOT_DIR"):
        p = getattr(config, nome)
        try:
            p.mkdir(parents=True, exist_ok=True)
            teste = p / ".escrita_ok"
            teste.write_text("x", encoding="utf-8")
            teste.unlink()
        except Exception as e:  # noqa: BLE001
            problemas.append(f"{nome}={p} ({type(e).__name__})")
    if problemas:
        raise RuntimeError("sem escrita em: " + "; ".join(problemas))
    return f"6 pastas ok (downloads={config.DOWNLOADS_DIR})"


# --- 5. Oracle -------------------------------------------------------------
@checar("Oracle: conexao e tabelas")
def _oracle():
    import oracle_db
    if not oracle_db.configurado():
        raise RuntimeError("ORACLE_USER/PASSWORD/DSN nao preenchidos no .env")
    con = oracle_db.get_connection()
    try:
        cur = con.cursor()
        faltando, contagem = [], {}
        for rot, alvo in [("001A", oracle_db._tabela()),
                          ("001B", oracle_db._tabela_palavras()),
                          ("002A", oracle_db._tabela_monitor()),
                          ("002B", oracle_db._tabela_filtro()),
                          ("002N", oracle_db._tabela_monitorados())]:
            try:
                cur.execute(f"SELECT COUNT(*) FROM {alvo}")
                contagem[rot] = cur.fetchone()[0]
            except Exception:  # noqa: BLE001
                faltando.append(f"{rot} ({alvo})")
        if faltando:
            raise RuntimeError("nao existem/sem permissao: " + ", ".join(faltando))
        return " | ".join(f"{k}={v}" for k, v in contagem.items())
    finally:
        con.close()


@checar("Oracle: tabelas de configuracao populadas")
def _config_oracle():
    import oracle_db
    palavras = oracle_db.listar_palavras_chave()
    filtro = oracle_db.listar_palavras_filtro()
    nomes = oracle_db.listar_monitorados()
    vazias = [n for n, v in [("001B palavras-chave", palavras), ("002B pre-filtro", filtro),
                             ("002N monitorados", nomes)] if not v]
    if vazias:
        raise RuntimeError("sem linha vigente em: " + ", ".join(vazias)
                           + " - rode deploy\\seed_config.sql")
    return f"001B={len(palavras)} | 002B={len(filtro)} | 002N={len(nomes)}"


# --- 6. Índice -------------------------------------------------------------
@checar("Indice FTS5", essencial=False)
def _indice():
    import config
    from search import list_dates
    if not config.DB_PATH.exists():
        raise RuntimeError("ainda nao existe (normal numa instalacao nova; "
                           "sera criado no 1o run_pipeline)")
    datas = list_dates()
    return f"{len(datas)} edicao(oes), mais recente {datas[0] if datas else '-'}"


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(f"\n=== Verificacao do ambiente · {ROOT} ===\n")
    largura = max(len(n) for _, n, _ in _resultados)
    for status, nome, detalhe in _resultados:
        print(f"  {status} {nome.ljust(largura)}  {detalhe}")
    falhas = [n for s, n, _ in _resultados if s == FALHA]
    print()
    if falhas:
        print(f"  {len(falhas)} verificacao(oes) ESSENCIAL(is) falharam: {', '.join(falhas)}")
        print("  Corrija antes de agendar a tarefa.\n")
        return 1
    print("  Ambiente pronto.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
