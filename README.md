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
