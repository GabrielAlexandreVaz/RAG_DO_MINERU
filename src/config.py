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
import re                          # separar listas do .env (destinatários de e-mail)
from pathlib import Path           # manipular caminhos de arquivo de forma segura

from dotenv import load_dotenv     # lê o arquivo .env e joga no ambiente

# O que conta como "sim" numa chave booleana do .env (quem preenche escreve em
# português tanto quanto em inglês).
_SIM = {"1", "true", "yes", "sim"}

# ROOT = a pasta raiz do projeto (uma acima de /src). Path(__file__) é este
# arquivo; .resolve() vira caminho absoluto; .parent.parent sobe dois níveis
# (src -> raiz).
ROOT = Path(__file__).resolve().parent.parent

# Carrega o .env da raiz -> as variáveis ficam disponíveis via os.getenv().
load_dotenv(ROOT / ".env")

# --- Runtime autocontido (servidor) -----------------------------------------
# Por padrão, o MinerU guarda a configuração em ~/mineru.json, os modelos em
# ~/.cache/modelscope e o Playwright o navegador em ~/AppData. Isso amarra o job
# ao PERFIL da conta do Windows — num servidor, a tarefa agendada pode rodar com
# outro usuário e nada é encontrado.
#
# Se o deploy\instalar.bat tiver colocado essas coisas DENTRO do projeto,
# apontamos as ferramentas para cá. Como usamos setdefault, uma variável já
# definida no ambiente ou no .env continua mandando; e se as pastas não existirem
# (instalação antiga, com tudo no perfil), nada muda.
for _var, _alvo in (
    ("MINERU_TOOLS_CONFIG_JSON", ROOT / "mineru.json"),   # config do MinerU
    ("MODELSCOPE_CACHE", ROOT / "modelos"),               # os ~413 MB de modelos
    ("PLAYWRIGHT_BROWSERS_PATH", ROOT / "navegadores"),   # o Chromium do download
):
    if _alvo.exists():
        os.environ.setdefault(_var, str(_alvo))

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
        # 001B: palavras-chave dos atos de pessoal (o que procurar no D.O.).
        "table_palavras": os.getenv(
            "ORACLE_TABLE_PALAVRAS", "IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B").strip(),
        # 002B: palavras do pre-filtro do monitor (quais paginas vao para a IA).
        "table_filtro": os.getenv(
            "ORACLE_TABLE_FILTRO", "IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B").strip(),
        # 002N: cadastro dos nomes monitorados (fonte da verdade da varredura de nomes).
        "table_monitorados": os.getenv(
            "ORACLE_TABLE_MONITORADOS", "IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N").strip(),
        "schema": os.getenv("ORACLE_SCHEMA", "").strip(),  # ex.: COE_IA (vazio = schema do usuário)
    }


def _lista_emails(valor):
    """'a@x; b@y , c@z' -> ['a@x','b@y','c@z'] (aceita ponto-e-vírgula e vírgula).

    O ponto-e-vírgula entra porque é o que o Outlook usa: quem preencher o .env
    copiando do campo "Para" do Outlook cola com ';' e não perceberia o erro."""
    return [e.strip() for e in re.split(r"[;,]", valor or "") if e.strip()]


# As duas constantes do AutorizadorClient (o `autorizador.ambiente` escolhe).
AUTORIZADOR_URLS = {
    "beta": "https://beta-autorizador-service.fazenda.rj.gov.br/autorizador",
    "prd": "https://autorizador-service.fazenda.rj.gov.br/autorizador",
}
AUTORIZADOR_ENDPOINT = "/api/v1/usuario/autenticar"


def _autorizador_url(ambiente):
    """URL de autenticação do autorizador ('' se o ambiente for desconhecido).

    O AutorizadorClient levanta IllegalArgumentException num ambiente que não
    seja beta/prd. Aqui devolvemos vazio e quem for usar é que dá o recado:
    config não é lugar de derrubar nada — o passo 7 avisa e o pipeline segue."""
    base = AUTORIZADOR_URLS.get(str(ambiente).lower(), "")
    return base + AUTORIZADOR_ENDPOINT if base else ""


def email_settings():
    """Relê as configurações de e-mail do .env AGORA (override) e devolve um dict.

    O envio NÃO é mais SMTP: o boletim vai num POST para a API corporativa de
    e-mail (a mesma que os sistemas Java usam via `EmailClient`), com
    `Authorization: Bearer <token>` e o corpo no formato do `EmailRequestDTO`
    ({to, from, subject, corpo, sistema}).

    Mesma razão de oracle_settings() para reler o arquivo: a URL e o token podem
    ser preenchidos depois, sem reiniciar nada — o job agendado relê a cada
    execução.

    `ativo` é a chave-mestra: com ela em false, o envio MONTA a requisição e a
    grava em disco para conferência em vez de chamar a API. É o que permite o
    passo entrar em produção antes de existir credencial."""
    load_dotenv(ROOT / ".env", override=True)
    # UMA variável para as duas coisas, como o `autorizador.ambiente` do Java:
    # escolhe a URL do autorizador E libera (só em 'prd') o domínio externo.
    ambiente = (os.getenv("AUTORIZADOR_AMBIENTE", "")
                or os.getenv("EMAIL_AMBIENTE", "") or "beta").strip()
    return {
        "ativo": os.getenv("EMAIL_ENVIO_ATIVO", "false").strip().lower() in _SIM,
        # --- API de e-mail (o `apps.email` do EmailClientProperties) ---
        "url": os.getenv("EMAIL_API_URL", "").strip(),
        # A credencial de ENTRADA do autorizador (o `token` do
        # EmailClientProperties / o `autorizador.token` do YAML). O nome antigo
        # EMAIL_API_TOKEN continua valendo para não quebrar .env já preenchido.
        "token": (os.getenv("AUTORIZADOR_TOKEN", "")
                  or os.getenv("EMAIL_API_TOKEN", "")).strip(),
        "timeout": int(os.getenv("EMAIL_API_TIMEOUT_S", "30").strip() or 30),
        # Vai no campo `sistema` do DTO — é como a API identifica quem chamou
        # (equivale ao ${spring.application.name} do EmailService).
        "sistema": os.getenv("EMAIL_SISTEMA", "RAG_DOERJ").strip(),
        # --- Autorizador (espelha AutorizadorClient) ---
        # O EMAIL_API_TOKEN acima NÃO é aceito pela API de e-mail: ele é a
        # credencial de entrada do autorizador, que a troca por um token de
        # verdade. A URL sai do ambiente (as duas constantes do AutorizadorClient);
        # preencher AUTORIZADOR_URL só é preciso se esse endereço mudar.
        "autorizador_url": (os.getenv("AUTORIZADOR_URL", "").strip()
                            or _autorizador_url(ambiente)),
        # --- Trava de domínio (o validarDominio do EmailClient) ---
        # Fora de 'prd', só pode sair e-mail para domínio interno. O padrão é
        # 'beta' de propósito: numa máquina sem o .env ajustado, o comportamento
        # seguro é o restritivo.
        "ambiente": ambiente,
        "dominios_internos": [d.strip().lower() for d in re.split(
            r"[;,]", os.getenv("EMAIL_DOMINIOS_INTERNOS",
                               "fazenda.rj.gov.br,redhat.com")) if d.strip()],
        # --- Quem envia, quem recebe ---
        "remetente": os.getenv("EMAIL_REMETENTE", "").strip(),
        "destinatarios": _lista_emails(os.getenv("EMAIL_DESTINATARIOS", "")),
        "copia": _lista_emails(os.getenv("EMAIL_COPIA", "")),
        "assunto_prefixo": os.getenv("EMAIL_ASSUNTO_PREFIXO",
                                     "[DOERJ] Monitoramento SEFAZ").strip(),
    }


def nomes_monitorados():
    """Lê a lista de nomes de monitorados.txt (um por linha; # = comentário).

    FALLBACK: a fonte da verdade dos nomes é a tabela Oracle 002N
    (oracle_db.listar_monitorados). Este arquivo só é usado quando o banco não
    responde, para o relatório do dia não sair sem a seção de nomes monitorados."""
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

# --- Pastas de dados --------------------------------------------------------
# TODAS relativas ao projeto por padrão, e TODAS sobrescrevíveis pelo .env. Assim
# o projeto roda em qualquer máquina sem configurar nada (o default cai dentro do
# próprio diretório), e no servidor cada pasta pode ir para outro disco/share.
def _pasta(var, padrao):
    """Pasta vinda do .env; se vazia, `padrao` (relativo à raiz do projeto)."""
    valor = os.getenv(var, "").strip()
    return Path(valor) if valor else (ROOT / padrao)


# Pasta onde ficam os PDFs do DOERJ — de onde o MinerU lê e onde o download grava.
DOWNLOADS_DIR = _pasta("DOERJ_DOWNLOADS_DIR", "downloads")

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

# --- Edição EXTRA -----------------------------------------------------------
# Em alguns dias o IOERJ publica uma edição EXTRA. Ela NÃO é uma data nova: sai na
# MESMA página de seleção, como um link A MAIS. Verificado na edição de 07/08/2026,
# em que o portal listou seis cadernos, o segundo sendo
# "Parte I (Poder Executivo) EDIÇÃO EXTRA".
#
# Toda a cadeia de travas do pipeline tem a DATA como chave, então, sem isto, a
# extra é perdida em silêncio: o download vê o PDF do dia no disco e sai por cache,
# e o monitor vê o Excel do dia e sai antes da IA. Foi o que aconteceu em 07/08 —
# aquela edição teve de ser baixada e indexada à mão.
#
# O padrão abaixo é sobrescrivível pelo .env de propósito: se um dia o portal mudar
# a redação do link, ajusta-se a chave e pronto — e, enquanto isso, o downloader
# REGISTRA NO LOG o texto de todos os links da página, inclusive os que não
# reconheceu, para que a variante nova apareça em vez de sumir.
EXTRA_ATIVO = os.getenv("DOERJ_EXTRA_ATIVO", "true").strip().lower() in _SIM
EXTRA_MATCH = os.getenv("DOERJ_EXTRA_MATCH", r"edi[cç][ãa]o\s*extra|suplement|especial").strip()
# Sufixo do rótulo do caderno extra no índice e na coluna CADERNO da 002A. Sem
# acento porque é assim que a linha de 07/08/2026 já está gravada no índice.
EXTRA_CADERNO_SUFIXO = "EDICAO EXTRA"
DOWNLOAD_HEADLESS = os.getenv("DOWNLOAD_HEADLESS", "true").strip().lower() in _SIM
DOWNLOAD_TZ = os.getenv("DOWNLOAD_TZ", "America/Sao_Paulo").strip()  # fuso da data da edição
NAV_TIMEOUT_MS = int(os.getenv("NAV_TIMEOUT_MS", "60000"))          # timeout de navegação
SCREENSHOT_DIR = _pasta("DOERJ_SCREENSHOT_DIR", "screenshots")      # prints de falha do download

# Pasta onde o MinerU grava o resultado da extração (Markdown + content_list.json).
# Cresce ~6 MB por edição; ver limpar.py (retenção).
SAIDA_DIR = _pasta("DOERJ_SAIDA_DIR", "saida")

# Índice de busca por TEXTO. Usamos SQLite com a extensão FTS5 (full-text search),
# que já vem embutida no Python — não precisa instalar banco de dados nenhum.
INDEX_DIR = _pasta("DOERJ_INDEX_DIR", "data/index")
DB_PATH = INDEX_DIR / "doerj_fts.db"     # o arquivo do índice (gerado pelo index_build)

# Relatórios do monitor estruturado (monitoramento_AAAA-MM-DD.xlsx).
RELATORIOS_DIR = _pasta("DOERJ_RELATORIOS_DIR", "relatorios")

# Dias que a saída do MinerU fica em disco antes de ser apagada por limpar.py.
# O texto já vive no índice FTS5; saida/ só serve para reindexar. 0 = nunca apaga.
RETENCAO_DIAS = int(os.getenv("DOERJ_RETENCAO_DIAS", "30"))

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
