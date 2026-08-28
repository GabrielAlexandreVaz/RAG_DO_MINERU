# Implantação em servidor (Windows)

Guia para subir o pipeline do DOERJ num servidor, sem depender da máquina de desenvolvimento.
Só o **pipeline em lote** vai para produção; o site Flask (`src/app.py`) continua sendo ferramenta
local de consulta.

> **Há um segundo caminho.** Para rodar em container no OpenShift — CronJob no lugar da Tarefa
> Agendada — veja [deploy/openshift/README.md](deploy/openshift/README.md). Este guia continua
> valendo: é o que está em produção hoje, e a migração só termina quando a tarefa daqui for
> desligada (pelo motivo da seção [Voltar atrás](#voltar-atrás): as duas gravam nas mesmas tabelas).

## O que o servidor precisa ter

| Requisito | Por quê |
|---|---|
| Windows com Python **3.10+** no PATH | o projeto roda em 3.11 hoje |
| ~3 GB livres em disco | venv (~1,5 GB) + modelos (413 MB) + Chromium + ~2 GB/ano de dados |
| 8 GB de RAM | o MinerU processa em fatias; `MINERU_SLICE` controla o pico |
| Acesso ao portal do IOERJ | `https://portal.ioerj.com.br` e `www.ioerj.com.br` |
| Acesso ao endpoint da IA | Azure Foundry ou API da Anthropic |
| Acesso ao Oracle | porta 1521 do DSN configurado |
| Acesso a pypi e modelscope | só na instalação, para baixar dependências e modelos |

Tudo passa pelo proxy da SEFAZ, que faz inspeção TLS — o `instalar.bat` já trata isso (ver
"O detalhe do TLS" no fim).

## Passo a passo

### 1. Clonar o projeto

```cmd
git clone https://gitlab.fazenda.rj.gov.br/sefaz/subtic/susist/arquitetura/ia-centro-de-excel-ncia/01_lab/01_engenharia_ia/0001ia-ioerj-rag-diario-inteligente.git DOERJ
cd DOERJ
```

Evite pastas com espaço ou acento no caminho.

### 2. Montar o ambiente

```cmd
deploy\instalar.bat
```

Demora bastante na primeira vez (baixa ~413 MB de modelos). Ao final você terá `.venv\`,
`modelos\`, `navegadores\` e `mineru.json` — **todos dentro do projeto**, de propósito: assim a
tarefa agendada funciona com qualquer conta do Windows, sem depender do perfil de quem instalou.

### 3. Configurar

```cmd
copy .env.example .env
notepad .env
```

Mínimo a preencher: `ANTHROPIC_API_KEY`, `AZURE_FOUNDRY_ENDPOINT` (se usar Foundry), `RAG_MODEL`,
`MONITOR_MODEL`, `ORACLE_USER`, `ORACLE_PASSWORD`, `ORACLE_DSN`, `ORACLE_SCHEMA`.

As pastas podem ficar **em branco** — cada uma cai numa subpasta do projeto. Preencha só se quiser
apontar para outro disco ou share.

### 4. Conferir antes de agendar

```cmd
deploy\verificar.bat
```

Confere 12 itens (Python, TLS, bibliotecas, Chromium, modelos, `.env`, escrita nas pastas, Oracle,
índice) e devolve código 1 se algo essencial falhar. **Não agende nada enquanto não estiver tudo
`[ok]`.**

### 5. Primeira execução manual

```cmd
.venv\Scripts\python.exe src\run_pipeline.py --download
```

Leva vários minutos (MinerU + IA). No log procure `(fonte: Oracle (001B))` e
`(fonte: Oracle (002B))` — se aparecer "lista de reserva", o banco não respondeu e o job rodou com
a configuração embutida no código.

Rode **uma segunda vez**: tudo deve ser pulado, sem chamar a IA. É a prova de que as travas de
idempotência funcionam neste ambiente.

### 6. Agendar

```cmd
deploy\instalar_tarefa.bat                    :: roda como a conta atual
deploy\instalar_tarefa.bat SEF-RJ\svc_doerj   :: roda como conta de serviço (pede senha)
```

Cria a tarefa `DOERJ_Pipeline`: seg-sex, 08:05, repetindo a cada 60 min por 10 horas. O D.O. não tem
hora fixa; repetir de hora em hora custa quase nada porque cada etapa tem trava de idempotência.

### 7. Desligar a máquina antiga

```cmd
schtasks /Change /TN "DOERJ_Pipeline" /DISABLE
```

**Não pule este passo.** As duas máquinas gravam nas mesmas tabelas, e a gravação é `DELETE` da
edição + `INSERT` — rodando juntas, uma apaga o resultado da outra.

## Se o banco de produção for outro

```sql
@deploy\schema.sql        -- cria as 5 tabelas
@deploy\seed_config.sql   -- popula 001B (2 palavras), 002B (35 termos), 002N (19 nomes)
```

Ajuste o schema nos dois arquivos se não for `COE_IA`, e o `ORACLE_SCHEMA` no `.env` para combinar.
As tabelas de dados (001A/002A) nascem vazias e são preenchidas pelo pipeline.

## Operação no dia a dia

```cmd
type logs\pipeline.log                        :: o que aconteceu
schtasks /Run /TN "DOERJ_Pipeline"            :: rodar agora
deploy\verificar.bat                          :: diagnóstico quando falhar
.venv\Scripts\python.exe src\limpar.py --simular   :: o que a retenção apagaria
```

Mudanças de configuração são feitas **no banco**, sem tocar no servidor: palavras-chave dos atos na
001B, termos do pré-filtro na 002B, nomes monitorados na 002N. A próxima edição já usa a lista nova.
Ver [README.md](README.md).

## Voltar atrás

O pipeline não altera nada fora das próprias pastas e das tabelas do Oracle. Para reverter:

1. `schtasks /Change /TN "DOERJ_Pipeline" /DISABLE` no servidor;
2. reabilitar a tarefa na máquina antiga;
3. se preciso, apagar as linhas gravadas a mais: `DELETE FROM ... WHERE EDICAO = DATE 'AAAA-MM-DD'`
   (o pipeline regrava tudo na próxima execução com `--force`).

## O detalhe do TLS (não remova)

O `instalar.bat` escreve um `sitecustomize.py` de duas linhas dentro do `.venv`:

```python
import truststore
truststore.inject_into_ssl()
```

É o que faz o Python confiar no certificado do proxy da SEFAZ. Precisa estar aí, e não no código do
projeto, porque tem de valer para **todo** processo do venv — inclusive o `mineru.exe`, que roda
como subprocesso e não importa o nosso `config.py`.

Sem ele, o sintoma é confuso: o MinerU não baixa modelo e a chamada da IA falha em certificado, sem
mensagem óbvia. Se recriar o venv na mão, rode o `instalar.bat` de novo ou recrie o arquivo.

## O mesmo pipeline em container (OpenShift)

O guia completo está em [deploy/openshift/README.md](deploy/openshift/README.md). O resumo das
diferenças, para quem conhece a implantação acima:

| Aqui (Windows) | No OpenShift |
|---|---|
| `deploy\instalar.bat` monta o `.venv`, o Chromium e os modelos | [`Containerfile`](Containerfile) faz o mesmo, no build, e assa tudo na imagem |
| `.env` na raiz | ConfigMap + Secret (o código lê de `os.getenv` dos dois jeitos, sem mudança) |
| `deploy\instalar_tarefa.bat` (`schtasks`) | `CronJob` com `concurrencyPolicy: Forbid` |
| Pastas do projeto / share do OneDrive | um PVC `ReadWriteOnce` montado em `/dados` |
| `logs\pipeline.log` com rotação a 5 MB | stdout, coletado por `oc logs` |
| `deploy\verificar.bat` | o mesmo `verificar.py`, rodando dentro do pod |

O `sitecustomize.py` do truststore continua sendo essencial, pelo motivo descrito na seção acima — o
`Containerfile` o recria no build, e em Linux ele passa a ler o `/etc/ssl/certs` do sistema em vez
do cofre de certificados do Windows.
