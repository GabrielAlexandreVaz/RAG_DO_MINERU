# RAG do Diário Oficial do RJ — com MinerU

Busca e consulta do **DOERJ** (Diário Oficial do Estado do RJ) por IA, com front-end web.
Estrutura inspirada no projeto **RAG_DO**, mas o texto vem do **MinerU** (extração limpa).

- **Fonte:** `DOERJ_AAAA-MM-DD.pdf` (PDF com texto), baixados do portal do IOERJ pelo próprio
  pipeline (`src/download_diario.py`, Playwright) para a pasta `DOERJ_DOWNLOADS_DIR`.
- **Hardware:** CPU (sem GPU).
- **Diferença para o RAG_DO:** a IA lê o **texto limpo extraído pelo MinerU**
  (não as imagens das páginas) → mais barato e preciso. A busca é por texto (FTS5/BM25).

## Como funciona  

```
PDF do dia (OneDrive)
  → MinerU (pipeline CPU/txt)  → texto limpo por página (content_list.json)
  → índice FTS5 (SQLite, BM25) → busca por palavra-chave, com escopo por data
  → Claude (Opus 4.8) lê os trechos → resposta em português, citando data e página
Front-end web (Flask) mostra a resposta + miniaturas das páginas p/ conferência.
```

## Estrutura

```
src/
  config.py        # configuração central (.env, caminhos, modelo)
  extrair.py       # MinerU: PDF -> texto (content_list.json)   [lento em CPU]
  index_build.py   # texto do MinerU -> índice FTS5             [rápido]
  search.py        # busca BM25 com escopo por data
  reader.py        # Claude responde a partir do TEXTO (cita data/página)
  ask.py           # CLI de perguntas
  app.py           # servidor web (Flask)
web/
  index.html       # front-end (página única)
  assets/          # brasão
data/index/        # índice FTS5 (gerado)
saida/             # saída do MinerU (gerada)
```

## Instalação (uma vez)

```cmd
deploy\instalar.bat
```

Cria o `.venv`, instala as versões exatas do `requirements.lock.txt`, configura o `truststore`
(proxy da SEFAZ) e baixa o Chromium e os modelos do MinerU **dentro do projeto**. Depois copie o
[.env.example](.env.example) para `.env` e preencha a chave da IA e o Oracle.

Para conferir se ficou tudo certo:

```cmd
deploy\verificar.bat
```

Implantação em servidor: veja [IMPLANTACAO.md](IMPLANTACAO.md).

## Uso

```bash
# 1) Extrair o Diário do dia (MinerU) — lento na 1ª vez (baixa modelos)
python src/extrair.py

# 2) Indexar o texto extraído (rápido)
python src/index_build.py

# 3a) Perguntar pelo terminal
python src/ask.py "Quais atos da Sefaz saíram hoje?"

# 3b) Ou abrir o front-end web
python src/app.py            # http://127.0.0.1:5001
#   (ou dê duplo-clique em web.bat)
```

`reindex.bat` extrai (MinerU) e reindexa tudo — bom para uma Tarefa Agendada diária.

## Saídas do monitor: Excel e boletim HTML

A leitura do dia (passo 5 do pipeline) produz **dois arquivos com os mesmos itens**, em
`relatorios/`:

| Arquivo | Para quê |
|---|---|
| `monitoramento_<data>.xlsx` | conferência: uma aba por seção, todas as colunas |
| `boletim_<data>.html` | leitura: o formato do e-mail `[DOERJ] Monitoramento SEFAZ` |

O boletim (`src/boletim.py`) **não gasta IA**: é só outro formato dos itens que o monitor já
extraiu. Sai junto com o Excel e, numa rodada parcial, vira `boletim_<data>_PARCIAL.html`, com o
aviso em vermelho no topo — a mesma regra do Excel.

Para refazer o boletim de uma edição já processada (lendo a 002A, sem chamar a IA):

```bash
python src/boletim.py                     # última edição gravada
python src/boletim.py --date 2026-08-14
```

O cabeçalho (número da edição, governador, secretário de Fazenda) é lido do texto da página 1 —
o que o MinerU não trouxer naquele dia simplesmente não aparece na linha, em vez de sair
inventado.

Não confundir com `src/resumo_executivo.py`, que gera outro documento a partir da mesma 002A: lá
as séries homogêneas são consolidadas e cada item vem com a matriz de rastreabilidade, para a
validação da área. O boletim é a leitura item a item.

### CPF sai mascarado

O que vai no relatório é transcrição do D.O., e o D.O. publica CPF por extenso (quadros de
licença-prêmio, listas de intimação). Nas saídas — Excel, boletim, export da 002A e resumo
executivo — o número sai como `097.XXX.XXX-65`: ficam os 3 primeiros dígitos e os 2 do
verificador, o suficiente para conferir na publicação. O mascaramento é na SAÍDA
(`monitor_estruturado.mascarar_cpf`), não na coleta: a 002A guarda a transcrição fiel, e por isso
uma edição antiga regerada também sai mascarada.

## Edição extra

Em alguns dias o IOERJ publica uma segunda edição. Ela **não é uma data nova**: sai na mesma
página de seleção do portal como um caderno A MAIS —
`Parte I (Poder Executivo) EDIÇÃO EXTRA` — e traz o número do dia com sufixo (`Nº 142-A`).

Como toda a idempotência do pipeline tem a data como chave, antes ela se perdia em silêncio: o
download via o PDF do dia no disco e saía por cache, o monitor via o Excel do dia e saía antes da
IA. O passo 3 (`ler_cadernos`) agora enumera os links da página de seleção e indexa o caderno
extra com rótulo próprio; o passo 6 (`monitor_estruturado.py --extra`) lê **só** esse caderno — o
diário do dia não é relido — e produz saída à parte:

| Arquivo | Observação |
|---|---|
| `monitoramento_<data>_EXTRA.xlsx` | trava de reprocessamento: sai mesmo sem item relevante |
| `boletim_<data>_EXTRA.html` | só sai se houver item — é ele que dispara o e-mail |

Na 002A as duas publicações dividem a mesma `DATA_EDICAO` e são separadas pela **faixa de ID**:
edição normal em `AAAAMMDD0001..4999`, extra em `AAAAMMDD5000..9999`. Por isso o `DELETE` de
`salvar_monitoramento` apaga só a faixa da publicação que está sendo gravada — apagar por data
levaria a outra junto.

Não é todo dia que tem edição extra. Nos dias em que não tem, o passo 6 sai em menos de um
segundo, sem chamar a IA e sem gerar arquivo.

## Boletim por e-mail

O passo 7 (`src/enviar_email.py`) manda o boletim HTML **no corpo** da mensagem, de
`EMAIL_REMETENTE` para `EMAIL_DESTINATARIOS` (ver `.env.example`). Duas travas, porque o job roda
de hora em hora: só envia se existir o boletim canônico (rodada PARCIAL não envia) e só uma vez
por edição, marcada por `relatorios/email_enviado_<data>.txt`.

**Não é SMTP.** O envio é um `POST` na API corporativa de e-mail — a mesma que os sistemas Java
chamam pelo `EmailClient` — com `Authorization: Bearer <token>` e o corpo no formato do
`EmailRequestDTO`:

```json
{"to": "...", "from": "...", "subject": "...", "corpo": "<html>…</html>", "sistema": "RAG_DOERJ"}
```

Três consequências do contrato:

- o DTO tem **um** destinatário, sem cópia e sem `Reply-To`: a lista do `.env` (destinatários +
  cópia) vira **uma requisição por endereço**. Se só parte receber, o recibo sai como
  `email_enviado_<data>_PARCIAL.txt` com quem já recebeu, e a rodada seguinte tenta **só os que
  faltaram** — ninguém recebe o mesmo boletim duas vezes;
- sem `Reply-To`, a resposta que o rodapé pede volta para o `EMAIL_REMETENTE`;
- fora do ambiente `prd` (`EMAIL_AMBIENTE`), só sai e-mail para domínio interno — a mesma trava do
  `EmailClient`, repetida aqui para o bloqueio aparecer no log em vez de virar um 4xx opaco.

**São dois tokens, e confundi-los custa uma tarde.** O `AUTORIZADOR_TOKEN` do `.env` (claims
`aplicacao: AUTORIZADOR-SERVICE`, `tipoToken: AUTH`) **não** é aceito pela API de e-mail: ele é a
credencial de entrada do autorizador. A cada envio o código faz o que o `AutorizadorClient` faz no
Java —

```
POST <autorizador>/api/v1/usuario/autenticar
Authorization: Bearer <AUTORIZADOR_TOKEN>     (sem corpo)
-> CredencialDTO {token, dataHoraExpiracao, ...}   <- este, tipoToken: ACCESS, é o do envio
```

— e guarda a credencial até a expiração. Mandar o token de entrada direto no `/email/enviar` dá
**403 com corpo vazio, idêntico ao de uma requisição sem header nenhum** — é o sintoma a
reconhecer. Se chegar 400 ou 500, ao contrário, a autenticação passou e o problema é o corpo.

`AUTORIZADOR_AMBIENTE` (`beta` | `prd`) manda em duas coisas, como o `autorizador.ambiente` do
Java: qual autorizador vale e se a trava de domínio está ligada.

Com `EMAIL_ENVIO_ATIVO=false` — o padrão — nada é enviado: as requisições são montadas e gravadas
em `relatorios/email_<data>.json` (com o token mascarado) para conferência; o boletim em si abre no
navegador, em `relatorios/boletim_<data>.html`.

```bash
python src/enviar_email.py --dry-run          # só grava o .json
python src/enviar_email.py --date 2026-08-19 --extra
python src/enviar_email.py --force            # reenvia (ignora o marcador)
python src/enviar_email.py --para eu@fazenda.rj.gov.br   # teste em um endereço só
```

## Palavras-chave dos atos de pessoal

`src/atos_pessoal.py` procura no D.O. os atos de pessoal e grava na **001A**. O que ele procura vem
da tabela **`IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B`** (`PALAVRA_CHAVE`, `DATA_INI`, `DATA_FIM`):

```sql
-- passar a acompanhar um novo tipo de ato
INSERT INTO COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B" (ID, PALAVRA_CHAVE, DATA_INI)
VALUES (3, 'designar', TRUNC(SYSDATE));

-- parar de acompanhar
UPDATE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B"
   SET DATA_FIM = TRUNC(SYSDATE) WHERE PALAVRA_CHAVE = 'designar';
```

Escreva o verbo no infinitivo. O job tira o "r" final e busca por prefixo, para pegar todas as
formas: `exonerar` → `exonera*` (acha "exonerar", "exoneração", "exonerado"). Buscar a palavra
literal perderia atos. O rótulo gravado na coluna `RESPOSTA` da 001A é derivado do verbo
(`exonerar` → `Exoneração`, `designar` → `Designação`); verbos fora do padrão -ar estão no mapa
`ROTULOS_ESPECIAIS` do próprio arquivo.

Se a regra de prefixo não servir (ex.: `remover` não alcança "remoção"), dá para escrever o padrão
FTS5 direto na coluna — quando o valor tem `*` ou espaço, ele é usado como está:
`remov* OR remoc*`. Nesse caso o rótulo sai do 1º termo, então confira se ficou legível.

Vale quem está vigente na data da edição, igual aos nomes monitorados. Se o banco não responder, o
job usa a lista de reserva `PALAVRAS_PADRAO` (`nomear`, `exonerar`) e avisa no log.

## Pré-filtro do monitor (quais páginas vão para a IA)

`src/monitor_estruturado.py` não manda a edição inteira para a IA — só as páginas que citam algum
termo de interesse. Esses termos vêm da tabela **`IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B`**
(`PALAVRA_CHAVE`, `DATA_INI`, `DATA_FIM`), 35 termos hoje:

```sql
-- passar a considerar mais um assunto
INSERT INTO COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B" (ID, PALAVRA_CHAVE, DATA_INI)
VALUES (36, 'precatorio', TRUNC(SYSDATE));

-- parar de considerar
UPDATE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B"
   SET DATA_FIM = TRUNC(SYSDATE) WHERE PALAVRA_CHAVE = 'precatorio';
```

Escreva **sem acento e em minúsculas** (o índice é normalizado). O termo casa por prefixo a partir
do início de uma palavra: `tributa` pega "tributação" e "tributário", mas `icms` não casa dentro de
outro token. Frases funcionam (`ponto facultativo`).

Cuidado com o efeito no custo e na cobertura: **incluir** termo = mais páginas vão para a IA (mais
recall, mais caro); **retirar** = mais barato, com risco de perder ato. Um termo muito genérico
(ex.: `estado`) faria quase toda página passar. Se o banco não responder, o job usa a lista de
reserva `_KW_RELEVANCIA` do código e avisa no log.

## Nomes monitorados (seção 3 do monitor)

`src/monitor_estruturado.py` varre os 5 cadernos procurando as pessoas monitoradas. A lista vem
da tabela Oracle **`IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N`** (`NOME`, `FUNCAO`, `DATA_INI`,
`DATA_FIM`) — não é preciso mexer em arquivo nem reiniciar nada, a próxima edição já usa a lista
nova:

```sql
-- incluir um monitorado
INSERT INTO COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N" (ID, NOME, FUNCAO, DATA_INI)
VALUES (19, 'Fulano de Tal', 'Subsecretario de Alguma Coisa', TRUNC(SYSDATE));

-- retirar um monitorado (não apague a linha: preserva o histórico)
UPDATE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N"
   SET DATA_FIM = TRUNC(SYSDATE) WHERE NOME = 'Fulano de Tal';
```

Vale quem está **vigente na data da edição** (`DATA_INI <= edição` e `DATA_FIM` nula ou `>= edição`),
então reprocessar uma edição antiga usa a lista que valia naquele dia. A `FUNCAO` aparece na coluna
*Cargo* do relatório. Se o banco não responder, o job cai para o [monitorados.txt](monitorados.txt)
(cópia de segurança) e avisa no log.

## Notas da rede SEFAZ

O ambiente corporativo tem proxy com inspeção TLS. Isso já está tratado:
- `truststore` (no venv) faz o Python confiar no certificado da SEFAZ;
- `NO_PROXY` isenta o localhost para o serviço interno do MinerU.
