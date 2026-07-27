"""
============================================================================
config.py · Configuração central do projeto
----------------------------------------------------------------------------
Este arquivo é o "painel de controle": todos os outros módulos (extrair,
index_build, search, reader, app...) importam daqui os caminhos, o modelo e as
opções. Assim, para mudar algo (a pasta dos PDFs, o modelo da IA, o DPI...),
você mexe só AQUI, num lugar só.

As configurações vêm do arquivo .env (na raiz do projeto). O que não estiver
no .env usa o valor-padrão definido abaixo (o 2º argumento de os.getenv).
============================================================================
"""
import os                          # ler variáveis de ambiente (do sistema / .env)
from pathlib import Path           # manipular caminhos de arquivo de forma segura

from dotenv import load_dotenv     # lê o arquivo .env e joga no ambiente

# ROOT = a pasta raiz do projeto (uma acima de /src). Path(__file__) é este
# arquivo; .resolve() vira caminho absoluto; .parent.parent sobe dois níveis
# (src -> raiz).
ROOT = Path(__file__).resolve().parent.parent

# Carrega o .env da raiz -> as variáveis ficam disponíveis via os.getenv().
load_dotenv(ROOT / ".env")

# Chave da API da Anthropic (Claude). Sem ela, a BUSCA funciona, mas a
# RESPOSTA da IA não. O .strip() remove espaços acidentais nas pontas.
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()


def current_api_key():
    """Relê a chave direto do .env, AGORA (override=True força reler o arquivo).

    Por que isso existe: o servidor web carrega o .env só uma vez, ao iniciar.
    Se você trocar a chave depois, o servidor continuaria com a antiga em memória
    (o clássico erro 401 'invalid x-api-key'). Relendo a cada pergunta, a chave
    nova passa a valer na hora, sem precisar reiniciar o servidor."""
    load_dotenv(ROOT / ".env", override=True)
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def get_client():
    """Cria o cliente da IA lendo o .env AGORA (chave/endpoint valem na hora).

    - Se AZURE_FOUNDRY_ENDPOINT estiver no .env  -> usa o Claude na Azure /
      Microsoft Foundry (anthropic.AnthropicFoundry). O deployment vai em RAG_MODEL.
    - Senão -> usa a Anthropic direta (anthropic.Anthropic)."""
    load_dotenv(ROOT / ".env", override=True)
    key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    endpoint = os.getenv("AZURE_FOUNDRY_ENDPOINT", "").strip()
    import anthropic
    if endpoint:
        return anthropic.AnthropicFoundry(base_url=endpoint, api_key=key)
    return anthropic.Anthropic(api_key=key)


def oracle_settings():
    """Relê as credenciais do Oracle do .env AGORA (override) e devolve um dict.

    Mesma ideia de current_api_key(): reler o .env na hora evita usar valores
    antigos em cache se você editar o arquivo com o servidor/job já em memória.
    Usado por oracle_db.py (job de atos de pessoal)."""
    load_dotenv(ROOT / ".env", override=True)
    return {
        "user": os.getenv("ORACLE_USER", "").strip(),
        "password": os.getenv("ORACLE_PASSWORD", ""),   # senha: NÃO dar strip (pode ter espaço)
        "dsn": os.getenv("ORACLE_DSN", "").strip(),      # ex.: host:1521/SERVICE_NAME
        "table": os.getenv("ORACLE_TABLE", "DOERJ_ATOS_PESSOAL").strip(),        # 001A: atos de pessoal
        "table_monitor": os.getenv("ORACLE_TABLE_MONITOR", "").strip(),          # 002A: monitor (8 temas)
        "schema": os.getenv("ORACLE_SCHEMA", "").strip(),  # ex.: COE_IA (vazio = schema do usuário)
    }


def nomes_monitorados():
    """Lê a lista de nomes monitorados de monitorados.txt (um por linha; # = comentário).

    Usada pela varredura de nomes no DOERJ (monitor_estruturado)."""
    f = ROOT / "monitorados.txt"
    if not f.exists():
        return []
    nomes = []
    for ln in f.read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if ln and not ln.startswith("#"):
            nomes.append(ln)
    return nomes


# Modelo (ou, na Azure, o NOME DO DEPLOYMENT) que redige a resposta.
MODEL = os.getenv("RAG_MODEL", "claude-opus-4-8").strip()

# Modelo (deployment) do JOB de monitoramento estruturado. É extração em massa
# (muitas páginas por edição) -> vale um modelo mais barato (ex.: Sonnet 4.6).
# Vazio -> usa o MODEL (Opus). O site (/api/ask) sempre usa o MODEL.
MONITOR_MODEL = os.getenv("MONITOR_MODEL", "").strip() or MODEL

# Pasta onde ficam os PDFs do DOERJ (a mesma que o seu job salva no OneDrive).
DOWNLOADS_DIR = Path(
    os.getenv(
        "DOERJ_DOWNLOADS_DIR",
        r"C:\Users\Gsilva11\OneDrive - SEFAZ-RJ\DIRETORIO_AGENTE_DO_A01",
    )
)

# Pasta onde o job diário salva o Excel das exonerações (no OneDrive).
EXONERACOES_DIR = Path(
    os.getenv(
        "DOERJ_EXONERACOES_DIR",
        r"C:\Users\Gsilva11\OneDrive - SEFAZ-RJ\Exonerações",
    )
)

# --- Download do D.O. (Playwright dirige o portal do IOERJ) ------------------
# O portal não expõe URL estável do PDF; dirigimos um navegador pelo fluxo
# (última edição -> caderno) e interceptamos a resposta do PDF (mostra_pdf.php).
PORTAL_URL = os.getenv("PORTAL_URL", "https://portal.ioerj.com.br/").strip()
ULTIMA_EDICAO_URL = os.getenv(
    "ULTIMA_EDICAO_URL",
    "https://www.ioerj.com.br/portal/modules/conteudoonline/do_ultima_edicao.php",
).strip()
CADERNO_ALVO = os.getenv("CADERNO_ALVO", "Poder Executivo").strip()  # Parte I

# Os 5 cadernos do DOERJ. 'match' = texto do link na pagina de selecao (do
# _seleciona_edicao); 'estrategia': "completa" (Parte I -> salva + MinerU) ou
# "leve" (demais -> lidas em memoria com PyMuPDF, texto indexado, PDF descartado).
CADERNOS = [
    {"chave": "parte_1",  "nome": "Parte I (Poder Executivo)",     "match": "Poder Executivo",   "ativo": True, "estrategia": "completa"},
    {"chave": "parte_1b", "nome": "Parte IB (Tribunal de Contas)", "match": "Tribunal de Contas", "ativo": True, "estrategia": "leve"},
    {"chave": "parte_2",  "nome": "Parte II (Poder Legislativo)",  "match": "Poder Legislativo", "ativo": True, "estrategia": "leve"},
    {"chave": "parte_4",  "nome": "Parte IV (Municipalidades)",    "match": "Municipalidades",   "ativo": True, "estrategia": "leve"},
    {"chave": "parte_5",  "nome": "Parte V (Publicações a Pedido)", "match": "Publica",          "ativo": True, "estrategia": "leve"},
]

# Nome do caderno da Parte I (usado para rotular no indice as paginas do MinerU).
CADERNO_PARTE_I = "Parte I (Poder Executivo)"
DOWNLOAD_HEADLESS = os.getenv("DOWNLOAD_HEADLESS", "true").strip().lower() in {"1", "true", "yes", "sim"}
DOWNLOAD_TZ = os.getenv("DOWNLOAD_TZ", "America/Sao_Paulo").strip()  # fuso da data da edição
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "60000"))          # timeout de navegação
SCREENSHOT_DIR = ROOT / "screenshots"                               # prints de falha do download

# Pasta onde o MinerU grava o resultado da extração (Markdown + content_list.json).
SAIDA_DIR = ROOT / "saida"

# Índice de busca por TEXTO. Usamos SQLite com a extensão FTS5 (full-text search),
# que já vem embutida no Python — não precisa instalar banco de dados nenhum.
INDEX_DIR = ROOT / "data" / "index"
DB_PATH = INDEX_DIR / "doerj_fts.db"     # o arquivo do índice (gerado pelo index_build)

# DPI (resolução) das miniaturas de página renderizadas no front-end. Maior =
# imagem mais nítida, porém mais pesada.
RENDER_DPI = int(os.getenv("RENDER_DPI", "150"))

# --- Opções do MinerU (etapa de extração) -----------------------------------
MINERU_DEVICE = os.getenv("MINERU_DEVICE_MODE", "cpu")   # cpu | cuda (GPU)
MINERU_METHOD = os.getenv("MINERU_METHOD", "txt")        # auto | txt | ocr
MINERU_FORMULA = os.getenv("MINERU_FORMULA", "false")    # Diário não tem fórmulas -> desligado
MINERU_TABLE = os.getenv("MINERU_TABLE", "true")         # reconhecer tabelas
# Fatia (nº de páginas por rodada do MinerU). Edições grandes em uma rodada só
# podem estourar a memória e derrubar o worker; processar em fatias mantém o uso
# de RAM sob controle. 30 é seguro para CPU.
MINERU_SLICE = int(os.getenv("MINERU_SLICE", "30"))
# O proxy da SEFAZ intercepta até o tráfego de localhost; isentamos o loopback
# para o serviço interno do MinerU conseguir falar consigo mesmo.
NO_PROXY_HOSTS = "127.0.0.1,localhost,::1"
