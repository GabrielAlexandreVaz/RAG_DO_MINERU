# ==========================================================================
#  Containerfile · imagem do pipeline do DOERJ para o OpenShift
#  --------------------------------------------------------------------------
#  Equivale ao que deploy\instalar.bat faz na estacao Windows, so que para Linux
#  e assado numa imagem: venv + Chromium do Playwright + os modelos do MinerU +
#  o sitecustomize.py do truststore.
#
#  BASE: a imagem interna da SEFAZ, o mesmo registry que a app de FastAPI ja usa
#  em producao. E uma imagem Debian (aquele Containerfile instala com apt-get),
#  entao aqui tambem se usa apt-get - e nao dnf.
#
#  A TAG E 3.12-slim, e a escolha nao e livre: das quatro tags publicadas no
#  svcocpsefaz/python, so essa serve. A janela util e Python >=3.11,<3.14, e ela
#  vem de duas restricoes opostas:
#    - piso  : numpy==2.4.6 e pandas==3.0.3 (pinados no lock) exigem >=3.11 e
#              nao publicam wheel cp310  -> descarta 3.9-slim-buster e 3.10;
#    - teto  : mineru==3.4.2 exige >=3.10,<3.14                -> descarta 3.14.2-bookworm.
#  Sobra 3.12-slim. Por ser slim, a imagem vem sem compilador e sem as
#  bibliotecas do Chromium - os dois `apt-get install` abaixo cuidam disso.
#
#  Build em dois estagios: o 'builder' precisa de compilador e das ferramentas
#  de download; o estagio final so precisa das bibliotecas de execucao.
#
#      podman build -t doerj:teste -f Containerfile .
#      podman run --rm doerj:teste python -c "import mineru, torch, oracledb; print('ok')"
#
#  A configuracao NAO vem de .env aqui: vem de variaveis de ambiente
#  (ConfigMap + Secret). Ver deploy/openshift/.
# ==========================================================================

ARG BASE_IMAGE=registry-quay-openshift-operators.apps.ocp.sefnet.rj/svcocpsefaz/python:3.12-slim

# --------------------------------------------------------------------------
#  Estagio 1: builder
# --------------------------------------------------------------------------
FROM ${BASE_IMAGE} AS builder
ENV APP_VERSION="0.0.1"

# Onde tudo fica. Sao os MESMOS caminhos do estagio final, de proposito: o
# mineru.json gravado aqui guarda o caminho ABSOLUTO dos modelos, entao mudar a
# pasta entre os estagios quebraria a extracao em tempo de execucao.
ENV VENV=/opt/venv \
    PLAYWRIGHT_BROWSERS_PATH=/opt/navegadores \
    MODELSCOPE_CACHE=/opt/modelos \
    MINERU_TOOLS_CONFIG_JSON=/opt/mineru.json \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive

# So o que o BUILD precisa. As bibliotecas do Chromium NAO entram aqui: quem as
# instala e o proprio `playwright install --with-deps` mais abaixo.
#
# NAO copiamos daquele projeto o libaio nem o Oracle Instant Client: aquela app
# usa o oracledb em modo THICK, que precisa da biblioteca nativa. Esta usa modo
# THIN, Python puro (src/oracle_db.py) - sao ~250 MB de imagem e um download
# externo a menos.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

RUN python -m venv "$VENV"
ENV PATH="/opt/venv/bin:$PATH"

WORKDIR /build
COPY requirements.lock.txt /build/

# O lock foi gerado no Windows e tem finais de linha CRLF. O pip tolera, mas os
# dois `grep` abaixo carregariam o \r para dentro dos arquivos derivados, e um
# `torch==2.12.1\r` e um specifier invalido. Normaliza uma vez, aqui.
RUN tr -d '\r' < requirements.lock.txt > /tmp/lock.txt \
    && printf '[lock] %s pacotes\n' "$(grep -c '==' /tmp/lock.txt)"

# Falha cedo, e com mensagem clara, se a tag da imagem base cair fora da janela
# util - em vez de deixar o pip morrer 200 linhas adiante. O teto e tao real
# quanto o piso: mineru==3.4.2 nao suporta 3.14.
RUN python -c "import sys; v=sys.version_info; sys.exit(0) if (3,11) <= v < (3,14) else sys.exit('ERRO: imagem base tem Python ' + sys.version.split()[0] + '. A janela util e >=3.11,<3.14 - piso do numpy/pandas do lock, teto do mineru. Use --build-arg BASE_IMAGE=<...>/python:3.12-slim')" \
    && python -c "import sys; print('[ok] Python', sys.version.split()[0])"

# --- 1) torch CPU-only, ANTES de tudo -------------------------------------
# O requirements.lock.txt foi gerado no Windows, onde `torch` do PyPI ja e
# CPU-only. No Linux o MESMO pin arrasta as wheels nvidia-* (CUDA) e a imagem
# passa de 6 GB - para um projeto declaradamente CPU (MINERU_DEVICE_MODE=cpu,
# src/config.py). Instalando primeiro pelo indice CPU do PyTorch, o pip ja
# encontra a versao pinada satisfeita quando processar o resto do lock.
#
# Em rede fechada: este e o indice que precisa existir como repositorio proxy no
# Nexus. Sobrescreva com --build-arg TORCH_INDEX=<url do proxy>.
ARG TORCH_INDEX=https://download.pytorch.org/whl/cpu
RUN pip install --upgrade pip \
    && grep -E '^(torch|torchvision)==' /tmp/lock.txt > /tmp/torch.txt \
    && cat /tmp/torch.txt \
    && pip install --index-url "$TORCH_INDEX" -r /tmp/torch.txt

# --- 2) o resto do lock ----------------------------------------------------
# Fora torch/torchvision (ja instalados acima, na variante CPU) e
# win32-setctime, que e dependencia Windows-only do loguru e nao tem o que fazer
# aqui. As outras 120 versoes ficam exatamente como no ambiente homologado.
RUN grep -vE '^(torch|torchvision)==|^win32[-_]setctime==' /tmp/lock.txt > /tmp/req.txt \
    && pip install -r /tmp/req.txt

# Trava de sanidade: se alguma coisa reintroduziu o torch CUDA, e melhor o build
# falhar aqui do que descobrir por uma imagem de 6 GB no registry.
RUN if pip list 2>/dev/null | grep -qi '^nvidia-'; then \
        echo "ERRO: wheels CUDA (nvidia-*) instaladas - o TORCH_INDEX nao valeu"; \
        pip list | grep -i '^nvidia-'; \
        exit 1; \
    fi; \
    python -c "import torch; print('[ok] torch', torch.__version__, '| cuda:', torch.cuda.is_available())"

# --- 3) truststore para TODO processo do venv ------------------------------
# Igual ao que deploy\instalar.bat escreve na estacao. Precisa estar no
# site-packages, e nao no codigo do projeto, porque tem de valer tambem para o
# binario `mineru`, que roda como subprocesso (src/extrair.py) e nunca importa o
# nosso config.py. Em Linux o truststore le o trust store do sistema
# (/etc/ssl/certs numa base Debian) - e de la que sai a confianca no certificado
# do proxy da SEFAZ.
RUN printf 'import truststore\ntruststore.inject_into_ssl()\n' \
        > "$(python -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])')/sitecustomize.py" \
    && python -c "import ssl, truststore; assert ssl.SSLContext is truststore.SSLContext; print('[ok] truststore injetado')"

# --- 4) Chromium do Playwright --------------------------------------------
# So o Chromium: e o unico navegador que src/download_diario.py usa (em dois
# fluxos - o download da Parte I e a leitura dos cadernos leves).
#
# Aqui `--with-deps` FUNCIONA, porque a base da SEFAZ e Debian - ele so conhece
# Debian/Ubuntu. Numa base UBI/RHEL seria preciso listar os pacotes a mao.
RUN playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*

# --- 5) modelos do MinerU (~413 MB) ---------------------------------------
# Assados na imagem de proposito. Num container efemero, deixar para o runtime
# significaria baixar 413 MB a cada execucao do CronJob.
# Requer egress para o modelscope DURANTE O BUILD.
RUN python -m mineru.cli.models_download -s modelscope -m pipeline \
    && test -s "$MINERU_TOOLS_CONFIG_JSON" \
    && cat "$MINERU_TOOLS_CONFIG_JSON"


# --------------------------------------------------------------------------
#  Estagio 2: runtime
# --------------------------------------------------------------------------
FROM ${BASE_IMAGE}

LABEL name="ioerj-rag-diario-inteligente" \
      summary="Pipeline de leitura do Diario Oficial do RJ (MinerU + RAG)" \
      description="Le a edicao do dia do DOERJ, extrai o texto com MinerU, indexa em SQLite FTS5, grava os atos no Oracle e envia o boletim de monitoramento."

ENV DEBIAN_FRONTEND=noninteractive

# So bibliotecas de EXECUCAO - sem compilador.
#   libgl1 + libglib2.0-0 : opencv-python (dependencia do MinerU) importa libGL.so.1
#   o restante            : o que o Chromium headless precisa para subir
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
        libnss3 libnspr4 libatk1.0-0 libatk-bridge2.0-0 libcups2 libdrm2 \
        libxkbcommon0 libxcomposite1 libxdamage1 libxfixes3 libxrandr2 \
        libgbm1 libasound2 libpango-1.0-0 libcairo2 libatspi2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv        /opt/venv
COPY --from=builder /opt/navegadores /opt/navegadores
COPY --from=builder /opt/modelos     /opt/modelos
COPY --from=builder /opt/mineru.json /opt/mineru.json

# ROOT do projeto (src/config.py o deriva de __file__: a pasta ACIMA de src/).
WORKDIR /app
COPY src/            /app/src/
COPY web/            /app/web/
COPY deploy/verificar.py /app/deploy/
COPY monitorados.txt requirements.lock.txt /app/

ENV VENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PLAYWRIGHT_BROWSERS_PATH=/opt/navegadores \
    MODELSCOPE_CACHE=/opt/modelos \
    MINERU_TOOLS_CONFIG_JSON=/opt/mineru.json \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    HOME=/app \
    TZ=America/Sao_Paulo

# As cinco pastas de dados apontam para /dados, que e o PVC. Todas sao
# sobrescriviveis pelo ConfigMap; estes valores sao so o padrao sensato da
# imagem, para ela tambem rodar com um `podman run -v` simples.
ENV DOERJ_DOWNLOADS_DIR=/dados/downloads \
    DOERJ_SAIDA_DIR=/dados/saida \
    DOERJ_INDEX_DIR=/dados/index \
    DOERJ_RELATORIOS_DIR=/dados/relatorios \
    DOERJ_SCREENSHOT_DIR=/dados/screenshots

# O OpenShift roda o container com um UID ALEATORIO do grupo 0 (SCC
# restricted-v2), nao com o UID do USER abaixo. Por isso tudo que precisa ser
# gravavel vai para o grupo 0 com permissao de dono (`g=u`) - o padrao que a Red
# Hat documenta para "arbitrary user IDs". A app de FastAPI da SEFAZ nao precisa
# disso porque nao escreve em disco; esta escreve o tempo todo.
RUN mkdir -p /dados/downloads /dados/saida /dados/index /dados/relatorios \
             /dados/screenshots /app/logs \
    && (useradd -r -u 1001 -g 0 -d /app -s /usr/sbin/nologin doerj || true) \
    && chgrp -R 0 /app /dados /opt/modelos /opt/navegadores /opt/mineru.json \
    && chmod -R g=u /app /dados /opt/modelos /opt/navegadores /opt/mineru.json

USER 1001

# O pipeline em lote e o uso normal da imagem. Sem o run_pipeline.bat e sem
# redirecionar para logs\pipeline.log: no cluster o log e o stdout, coletado
# pelo `oc logs` - o que tambem torna desnecessaria a rotacao de log que existia
# para contornar o lock de arquivo do Windows (src/limpar.py).
#
# NAO ha EXPOSE nem uvicorn aqui, ao contrario do Containerfile da app de
# FastAPI: este workload e um CronJob em lote, nao um servidor. Ele roda, le a
# edicao do dia e morre.
#
# O CronJob pode sobrescrever com `command:` para rodar outra coisa:
#   python deploy/verificar.py       -> os 14 checks de sanidade
#   python src/enviar_email.py ...   -> reenvio manual de boletim
#   python src/app.py                -> o site Flask (ver deploy/openshift/README.md)
CMD ["python", "src/run_pipeline.py", "--download"]
