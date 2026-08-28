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
        """Executa a checagem, guarda o resultado e NUNCA propaga a exceção -
        assim uma falha não impede as verificações seguintes."""
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
    """Versão do interpretador e se estamos num ambiente isolado (não no global).

    O que importa é NÃO ser o Python do sistema; ONDE fica o venv varia: na
    estação é ROOT\\.venv (deploy\\instalar.bat), no container é /opt/venv, fora
    do projeto de propósito (o código é copiado, o ambiente é assado na imagem).
    Se existir um .venv na raiz, então é ele que tem de estar rodando — senão a
    checagem passaria enquanto o job usa outro interpretador."""
    v = sys.version_info
    if v < (3, 10):
        raise RuntimeError(f"Python {v.major}.{v.minor} - o projeto pede 3.10+")
    if sys.prefix == sys.base_prefix:
        raise RuntimeError(f"rodando no Python global ({sys.prefix}) - use o venv do projeto")
    local = ROOT / ".venv"
    if local.exists() and Path(sys.prefix).resolve() != local.resolve():
        raise RuntimeError(f"existe {local}, mas este processo roda em {sys.prefix}")
    return f"{v.major}.{v.minor}.{v.micro} em {sys.prefix}"


# --- 2. truststore (proxy com inspeção TLS) --------------------------------
@checar("truststore ativo (sitecustomize)")
def _truststore():
    """O sitecustomize.py existe E o truststore trocou a classe padrão de SSL.

    Verificar só o arquivo não bastaria: ele pode existir sem ter efeito."""
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
    """Prova prática do TLS: um GET no portal do IOERJ atravessando o proxy."""
    import urllib.request
    import config
    with urllib.request.urlopen(config.PORTAL_URL, timeout=30) as r:
        return f"{config.PORTAL_URL} -> HTTP {r.status}"


# --- 3. Dependências -------------------------------------------------------
@checar("Bibliotecas importaveis")
def _libs():
    """Todas as bibliotecas do projeto importam. Lista as que faltam pelo nome do pip."""
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
    """O navegador do Playwright está instalado e o executável existe no disco."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        exe = Path(pw.chromium.executable_path)
        if not exe.exists():
            raise RuntimeError("nao instalado - rode: python -m playwright install chromium")
        return str(exe.parent.parent.name)


@checar("Modelos do MinerU")
def _mineru():
    """O mineru.json existe e o models-dir dele aponta para uma pasta que existe.

    É a falha mais comum numa máquina nova: a config aparece, mas os modelos
    ficaram noutro perfil de usuário."""
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
    """O binário do MinerU do venv (e não um global) é encontrável pelo extrair.py.

    `_mineru_bin()` devolve o caminho quando acha o executável ao lado do
    interpretador (mineru.exe no Windows, mineru no Linux) e cai no literal
    "mineru" quando não acha — ou seja, o fallback É a falha."""
    import extrair
    b = extrair._mineru_bin()
    if b == "mineru":
        raise RuntimeError(f"nao encontrado em {Path(sys.executable).parent}")
    return b


# --- 4. Configuração e pastas ---------------------------------------------
@checar("Variaveis de configuracao")
def _env():
    """As variáveis sem as quais o job não roda estão preenchidas.

    O que se cobra é o VALOR, não a origem: na estação ele vem do .env, no
    container vem do ConfigMap/Secret e não existe arquivo nenhum (`load_dotenv`
    sem .env é inócuo, e todo o config.py lê de os.getenv). Exigir o arquivo
    reprovaria um pod perfeitamente configurado."""
    import config
    origem = ".env" if (ROOT / ".env").exists() else "ambiente"
    faltando = [n for n, v in [("ANTHROPIC_API_KEY", config.current_api_key()),
                               ("RAG_MODEL", config.MODEL)] if not v]
    if faltando:
        raise RuntimeError(f"vazias (origem: {origem}): " + ", ".join(faltando))
    return f"origem={origem} | modelo={config.MODEL} | monitor={config.MONITOR_MODEL}"


@checar("Pastas de dados (existem e aceitam escrita)")
def _pastas():
    """Cada pasta de dados existe (cria se faltar) e aceita escrita de verdade.

    Testa gravando um arquivo: permissão de share só aparece na hora de escrever."""
    import config
    problemas = []
    for nome in ("DOWNLOADS_DIR", "SAIDA_DIR", "INDEX_DIR",
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
    return f"5 pastas ok (downloads={config.DOWNLOADS_DIR})"


# --- 5. Oracle -------------------------------------------------------------
@checar("Oracle: conexao e tabelas")
def _oracle():
    """Conecta no Oracle e conta as linhas das 5 tabelas.

    Um SELECT COUNT prova de uma vez a conexão, o nome qualificado e a permissão
    de leitura - os três pontos onde a configuração costuma errar."""
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
    """As 3 tabelas de configuração têm linha VIGENTE hoje.

    Tabela populada mas toda com DATA_FIM no passado faria o job cair
    silenciosamente para as listas de reserva do código."""
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


# --- 6. E-mail do boletim --------------------------------------------------
# As duas checagens são NÃO essenciais de propósito: enquanto a API de e-mail
# não estiver liberada, a máquina continua "pronta" (o passo 7 do pipeline só
# grava o .json de conferência). Quando a credencial chegar, viram [ok] sem
# nenhuma alteração de código.
@checar("E-mail: configuracao do .env", essencial=False)
def _email_cfg():
    """Remetente, destinatários e credencial preenchidos, e se o envio está ligado.

    Também avisa quando a trava de domínio do ambiente barraria a lista: em
    homologação, um destinatário externo no .env vira erro só na hora do envio —
    aqui ele aparece antes de a tarefa ser agendada."""
    import config
    from enviar_email import EmailBloqueado, validar_dominio
    cfg = config.email_settings()
    faltando = [n for n, v in [("EMAIL_REMETENTE", cfg["remetente"]),
                               ("EMAIL_DESTINATARIOS", cfg["destinatarios"]),
                               ("EMAIL_API_URL", cfg["url"]),
                               ("AUTORIZADOR_TOKEN", cfg["token"]),
                               (f"autorizador do ambiente '{cfg['ambiente']}'",
                                cfg["autorizador_url"])] if not v]
    if faltando:
        raise RuntimeError("vazias: " + ", ".join(faltando))
    try:
        for endereco in cfg["destinatarios"] + cfg["copia"]:
            validar_dominio(endereco, cfg)
    except EmailBloqueado as e:
        raise RuntimeError(str(e)) from e
    modo = "ENVIO ATIVO" if cfg["ativo"] else "so grava .json (conferencia)"
    return (f"{cfg['remetente']} -> {', '.join(cfg['destinatarios'])} | "
            f"ambiente={cfg['ambiente']} | {modo}")


@checar("E-mail: autorizador emite token e a API o aceita", essencial=False)
def _email_api():
    """Autentica no autorizador e chama a API com um POST vazio SÓ para ver como
    ela responde — sem mandar e-mail nenhum (o corpo não tem destinatário nem
    HTML).

    É o teste que separa os cinco problemas que a infraestrutura resolve em
    lugares diferentes: nome do host, firewall/proxy, certificado, credencial de
    entrada e token emitido. Um HTTP 400/500 aqui é BOA notícia: significa que a
    requisição chegou AUTENTICADA e só foi recusada pelo corpo vazio. O 403 é a
    marca do token errado — a API responde igualzinho a quem não manda header."""
    import json
    import urllib.error
    import urllib.request

    import config
    from enviar_email import obter_token
    cfg = config.email_settings()
    if not cfg["url"]:
        raise RuntimeError("EMAIL_API_URL vazia - aguardando a infraestrutura")
    req = urllib.request.Request(
        cfg["url"], data=json.dumps({}).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json; charset=utf-8",
                 "Authorization": "Bearer " + obter_token(cfg)})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            return f"{cfg['url']} respondeu HTTP {r.status}"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(f"HTTP {e.code} - token recusado "
                               "(EMAIL_API_TOKEN vencido ou sem permissao)") from e
        return f"{cfg['url']} alcancada, token aceito (HTTP {e.code} no corpo vazio)"


# --- 7. Índice -------------------------------------------------------------
@checar("Indice FTS5", essencial=False)
def _indice():
    """Quantas edições há no índice FTS5. Não é essencial: numa instalação nova
    ele ainda não existe e será criado na primeira execução do pipeline."""
    import config
    from search import list_dates
    if not config.DB_PATH.exists():
        raise RuntimeError("ainda nao existe (normal numa instalacao nova; "
                           "sera criado no 1o run_pipeline)")
    datas = list_dates()
    return f"{len(datas)} edicao(oes), mais recente {datas[0] if datas else '-'}"


def main():
    """Imprime o relatório alinhado e devolve 1 se algo ESSENCIAL falhou.

    O código de saída é o que permite ao instalar.bat parar antes de agendar."""
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
