# Implantação no OpenShift

O pipeline do DOERJ rodando no cluster, como CronJob. Não substitui
[../../IMPLANTACAO.md](../../IMPLANTACAO.md) — aquele descreve a implantação em
servidor Windows, que é o que está em produção hoje. **Não desligue a tarefa da
estação antes da verificação final** (o motivo está no fim desta página).

## O que tem aqui

| Arquivo | Papel |
|---|---|
| [`../../Containerfile`](../../Containerfile) | A imagem, na raiz do repo. É o que faltava para a esteira passar do primeiro step. |
| [`pvc.yaml`](pvc.yaml) | O disco: índice FTS5, PDFs, relatórios e os recibos de e-mail. |
| [`configmap.yaml`](configmap.yaml) | As ~25 variáveis não secretas. |
| [`secret.example.yaml`](secret.example.yaml) | Molde das 7 secretas. O Secret real nunca vai para o git. |
| [`cronjob.yaml`](cronjob.yaml) | O agendamento, no lugar do `schtasks`. |

## Ordem de aplicação

```bash
oc project <namespace>

# 1) O disco primeiro: o CronJob não sobe sem ele.
oc apply -f deploy/openshift/pvc.yaml

# 2) Configuração pública.
oc apply -f deploy/openshift/configmap.yaml

# 3) Segredos - NÃO por arquivo. Ver o cabeçalho de secret.example.yaml.
oc create secret generic ia0001-ioerj-rag-diario-secret \
  --from-literal=ANTHROPIC_API_KEY='...' \
  --from-literal=ORACLE_USER='...' \
  --from-literal=ORACLE_PASSWORD='...' \
  --from-literal=ORACLE_DSN='...' \
  --from-literal=AUTORIZADOR_TOKEN='...' \
  --from-literal=EMAIL_API_URL='...' \
  --from-literal=AZURE_FOUNDRY_ENDPOINT=''

# 4) O agendamento.
oc apply -f deploy/openshift/cronjob.yaml
```

## Verificação, em ordem

Cada passo vale por si; não pule para o seguinte com o anterior falhando.

**1. O build passa e a imagem não é absurda.**

```bash
podman build -t doerj:teste -f Containerfile .
podman images doerj:teste          # esperado: 3,5-4 GB
```

Quase todo esse peso é `torch` + os 413 MB de modelos do MinerU. Se passar muito
de 4 GB, o mais provável é que o torch tenha vindo com CUDA — confira se o
`--index-url` do índice CPU foi realmente usado (`podman run --rm doerj:teste pip
list | grep -i nvidia` não deve devolver nada).

**2. Os imports pesados sobem.** Pega de uma vez o `libGL`, o torch e o Chromium,
sem precisar de rede nem de segredo:

```bash
podman run --rm doerj:teste python -c \
  "import mineru, torch, oracledb, fitz, playwright; print('ok', torch.__version__)"
```

**3. Os 14 checks, dentro do container e com as variáveis reais.** É o melhor
smoke test que este projeto tem — prova TLS pelo proxy, Oracle, modelos, Chromium
e escrita nas pastas (o que valida o PVC de graça):

```bash
oc run verificar --rm -it --restart=Never \
  --image=<imagem> \
  --overrides='{"spec":{"containers":[{"name":"verificar","image":"<imagem>",
    "command":["python","deploy/verificar.py"],
    "envFrom":[{"configMapRef":{"name":"ia0001-ioerj-rag-diario-config"}},
               {"secretRef":{"name":"ia0001-ioerj-rag-diario-secret"}}],
    "volumeMounts":[{"name":"dados","mountPath":"/dados"}]}],
    "volumes":[{"name":"dados","persistentVolumeClaim":
      {"claimName":"ia0001-ioerj-rag-diario-dados"}}]}}'
```

**4. Uma execução de verdade, com o e-mail travado.** Com
`EMAIL_ENVIO_ATIVO: "false"` (o padrão do ConfigMap) o pipeline roda inteiro e
grava `relatorios/email_<data>.json` em vez de chamar a API — dá para conferir
destinatários e assunto sem risco de mandar boletim duplicado para a área, já que
a estação continua enviando o dela.

```bash
oc create job --from=cronjob/ia0001-ioerj-rag-diario doerj-teste-1
oc logs -f job/doerj-teste-1
```

**5. Rode uma segunda vez.** Tudo deve ser pulado pelas travas de idempotência,
sem uma única chamada de IA. É a mesma prova que o `IMPLANTACAO.md` já exige na
estação, e aqui ela vale dobrado: é o que confirma que o PVC está de fato
persistindo entre execuções.

```bash
oc create job --from=cronjob/ia0001-ioerj-rag-diario doerj-teste-2
oc logs -f job/doerj-teste-2      # esperado: "ja indexado", "ja gravada", "ja existe"
```

**6. Só então** vire `EMAIL_ENVIO_ATIVO` para `"true"` e desabilite a tarefa da
estação:

```powershell
schtasks /Change /TN "DOERJ_Pipeline" /DISABLE
```

Não pule o passo 6 nem inverta a ordem. **As duas gravam nas mesmas tabelas, e a
gravação é DELETE da edição + INSERT** — rodando juntas, uma apaga o resultado da
outra. É a mesma advertência do passo 7 do `IMPLANTACAO.md`, e a razão de o
CronJob ter `concurrencyPolicy: Forbid`.

## Migrar o índice atual

Opcional, e só depois que o volume existir. Evita reprocessar o histórico já
extraído (são ~77 MB de índice e ~33 MB de saída do MinerU na estação):

```bash
oc rsync ./data/index/  <pod>:/dados/index/
oc rsync ./relatorios/  <pod>:/dados/relatorios/
```

## Variantes de comando

A imagem serve para mais do que o pipeline. Basta trocar o `command:` do job:

| Para | `command` |
|---|---|
| Pipeline (padrão) | `["python", "src/run_pipeline.py", "--download"]` |
| Diagnóstico | `["python", "deploy/verificar.py"]` |
| Reenviar um boletim | `["python", "src/enviar_email.py", "--date", "2026-08-27", "--force"]` |
| Reprocessar uma edição | `["python", "src/run_pipeline.py", "--force"]` (gasta IA) |

## O que veio do Containerfile da app de FastAPI

A imagem foi construída em cima do padrão que a SEFAZ já usa em produção noutro
projeto Python. O que foi aproveitado e o que foi descartado, e por quê:

| Daquele Containerfile | Aqui |
|---|---|
| Base `.../svcocpsefaz/python:3.10` do Quay interno | **Aproveitado o registry, trocada a tag para 3.11** (ver abaixo) |
| `apt-get` para instalar dependências | Aproveitado — é o que confirma que a base é Debian, e não UBI |
| `libaio1` + Oracle Instant Client (~250 MB) | **Descartado.** Aquela app usa `oracledb` em modo *thick*; esta usa *thin*, Python puro ([`src/oracle_db.py`](../../src/oracle_db.py)) — nada nativo a instalar |
| `EXPOSE 8080` + `uvicorn main:app` | **Descartado.** Aquilo é um servidor; este workload é um CronJob em lote |
| Sem `USER`, sem tratamento de permissão | **Não copiado.** Aquela app não escreve em disco; esta escreve o tempo todo, e o OpenShift dá um UID aleatório |
| `#!/bin/bash` na primeira linha | Não copiado — num Containerfile é só um comentário inócuo |

O ganho concreto de a base ser **Debian**: `playwright install --with-deps
chromium` funciona (ele só conhece Debian/Ubuntu). Numa base UBI seria preciso
listar à mão os quinze pacotes que o Chromium headless exige.

## Perguntas em aberto para a infraestrutura

Quatro coisas não dá para descobrir de fora, e cada uma pode travar o build:

1. **Existe tag `python:3.11` (ou maior) no `svcocpsefaz`?** É a mais urgente.
   A app de FastAPI usa `python:3.10`, e **3.10 não serve aqui**: `numpy==2.4.6`
   e `pandas==3.0.3`, pinados no `requirements.lock.txt`, declaram
   `requires-python >=3.11` e não publicam wheel `cp310` — o pip tentaria
   compilar numpy do zero e o build morreria. O Containerfile tem uma checagem
   que falha logo no começo com essa mensagem, em vez de deixar quebrar 200
   linhas adiante. Se não houver a tag, a alternativa é repontar as 123 versões
   do lock, o que joga fora o ambiente homologado.

2. **O template da esteira procura `Containerfile` ou `Dockerfile`?** Já sabemos
   que a esteira é **GitLab CI** e que o `.gitlab-ci.yml` do projeto (que vive na
   branch `main`) não faz mais do que incluir o template central:

   ```yaml
   include:
     - project: sefaz/subtic/suinfra/time-plataforma/gitlab-pipeline
       file: .pipeline-openshift-python.yaml
   ```

   O `Containerfile` foi posto na **raiz do repo** com esse nome — o mesmo que a
   app de FastAPI usa, então é a aposta certa. Falta só confirmar com o time de
   plataforma, já que o template não é legível a partir deste projeto.

   **Atenção ao fluxo de branch:** a esteira roda a partir da `main`, e o
   desenvolvimento acontece na `first` (o histórico é uma sequência de
   *Merge branch 'first' into 'main'*). Enquanto o `Containerfile` estiver só na
   `first`, a esteira continua sem enxergá-lo — **é preciso abrir o merge
   request**.

3. **O build alcança o índice CPU do PyTorch e o ModelScope?** Bom sinal: o
   Containerfile da app de FastAPI baixa de `archive.ubuntu.com` e
   `download.oracle.com` durante o build, então há saída para fora. Falta
   confirmar estes dois:
   - `https://download.pytorch.org/whl/cpu` — sem ele o `torch` vem na variante
     CUDA e a imagem passa de 6 GB, num projeto que é CPU-only. Há um
     `--build-arg TORCH_INDEX=` no Containerfile para apontar um proxy interno.
   - ModelScope — de onde saem os 413 MB de modelos do MinerU (passo 5 do build).

4. **O egress do namespace alcança o quê?** O pod precisa de:
   - `portal.ioerj.com.br` e `www.ioerj.com.br` (HTTPS) — o download do D.O.;
   - o Oracle na **1521**. Atenção: o DSN é um **SCAN de RAC**, então o driver
     pode ser redirecionado para outros nós — a regra não pode ser para um IP só;
   - o autorizador e a API de e-mail da SEFAZ (HTTPS);
   - o endpoint da IA (Anthropic ou Azure/Foundry).

## Três sintomas enganosos

Falhas que aparecem no log como outra coisa:

| No log você vê | Quase sempre é |
|---|---|
| O download falha com `Target closed` / `Target crashed` | `/dev/shm` pequeno. O Chromium usa memória compartilhada e o padrão de um container é 64 MB. O CronJob já monta um tmpfs de 256 Mi — se você reescrever o manifesto, não perca esse volume. O pipeline sobe o Chromium **duas vezes**: passo 1/7 (`download_diario`) e passo 3/7 (`ler_cadernos`). |
| `Server disconnected without sending a response` no MinerU | Falta de RAM na fatia. Baixe `MINERU_SLICE` no ConfigMap **antes** de subir `limits.memory`. |
| A rodada termina "COMPLETO", mas faltam itens no boletim | Pode ser bloco descartado por estouro de limite. Compare a contagem da 002A com a da estação antes de confiar numa rodada do cluster. |

## Sobre o certificado do proxy

O projeto confia no proxy da SEFAZ via `truststore`, injetado por um
`sitecustomize.py` que o build grava no site-packages — precisa ser ali, e não no
código, porque tem de valer também para o binário `mineru`, que roda como
subprocesso e nunca importa o `config.py`. Em Linux o `truststore` lê o trust
store do sistema — e como a base da SEFAZ é **Debian**, o caminho é
`/etc/ssl/certs`, não o `/etc/pki/ca-trust` do RHEL.

Se o passo 3 da verificação falhar no check de TLS, injete o CA bundle do cluster:

```yaml
# ConfigMap que o próprio OpenShift preenche
apiVersion: v1
kind: ConfigMap
metadata:
  name: ca-corporativo
  labels:
    config.openshift.io/inject-trusted-cabundle: "true"
```

e monte no pod:

```yaml
volumeMounts:
  - name: ca
    # Caminho de base DEBIAN. Numa base RHEL/UBI seria
    # /etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem
    mountPath: /etc/ssl/certs/ca-certificates.crt
    subPath: ca-bundle.crt
    readOnly: true
volumes:
  - name: ca
    configMap:
      name: ca-corporativo
```

Cuidado ao montar sobre `ca-certificates.crt`: isso **substitui** o bundle da
imagem pelo do cluster, em vez de somar. Se o CA bundle do OpenShift não incluir
as raízes públicas, as chamadas para a Anthropic e para o portal do IOERJ passam
a falhar. A alternativa segura é montar o CA em `/usr/local/share/ca-certificates/`
e rodar `update-ca-certificates` na inicialização, que soma em vez de trocar.

## E o site Flask?

Fica de fora por enquanto, e é o que mantém o desenho simples. `src/app.py` é
ferramenta local de consulta, roda no servidor de desenvolvimento do Werkzeug e
está fixo em `127.0.0.1:5001`. Levá-lo ao cluster exige três coisas que o pipeline
não precisa: bind em `0.0.0.0` com porta configurável, um servidor WSGI de
verdade, e um `Service` + `Route`.

**É aí — e só aí — que o nome começado com número voltaria a incomodar.** Nome de
`Service` é validado como *DNS-1035 label* (`[a-z]([-a-z0-9]*[a-z0-9])?`), que
exige começar por **letra**; `Deployment`, `CronJob`, `ConfigMap`, `Secret`,
`Route` e o `Application` do Argo usam *DNS-1123*, que aceita dígito inicial. Ou
seja: o Argo nunca foi o problema — o `Service` é que seria.

Na prática a questão está encerrada: **o projeto já foi renomeado no GitLab** de
`0001ia-ioerj-rag-diario-inteligente` para `ia0001-ioerj-rag-diario-inteligente`.
É a mesma convenção que o projeto já adotava no Oracle
(`IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001A`), pelo mesmo motivo — nome começado
por dígito dá trabalho. Os manifestos aqui usam `ia0001-ioerj-rag-diario`, que
combina com isso.
