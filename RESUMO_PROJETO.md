# RAG do Diário Oficial do RJ (DOERJ) com MinerU — Resumo do Projeto

## 1. O que é

Uma ferramenta interna que lê o **Diário Oficial do Estado do RJ (DOERJ)** todos os dias,
transforma o PDF em **texto limpo e pesquisável**, e permite:

- **Consultar por IA** (site local): perguntas em linguagem natural → resposta **estruturada**
  (tabela de atos) com citação de data e página, e **destaque** do trecho na imagem da página.
- **Baixar** a resposta em **Excel, JSON ou PDF**.
- **Job automático** que extrai as **exonerações do dia** e salva um Excel numa pasta do OneDrive.

Tudo roda **localmente**, em **CPU** (sem GPU), na rede corporativa da SEFAZ.

---

## 2. Como funciona (o fluxo)

```
Job externo baixa o PDF do dia (~08:00)  ->  C:\...\OneDrive - SEFAZ-RJ\DIRETORIO_AGENTE_DO_A01
        |
[08:05] MinerU extrai o texto limpo (em fatias de 30 páginas)  ->  saida/<edição>/txt/*_content_list.json
        |
        indexa o texto por página no banco de busca (SQLite FTS5)  ->  data/index/doerj_fts.db
        |
        +--> Site (Flask, http://127.0.0.1:5001): busca BM25 -> Claude estrutura a resposta -> tabela + downloads
        |
[08:30 seg-sex] Job das exonerações: busca todas as páginas com "exoner*" -> Claude estrutura -> Excel no OneDrive
```

- **MinerU** = extrai o texto respeitando a ordem de leitura das colunas e as tabelas (o grande
  diferencial: o Diário é multi-coluna).
- **FTS5/BM25** = busca por palavra-chave rápida, com escopo por data (edição).
- **Claude** (`claude-sonnet-4-6`, configurável) = lê o texto e devolve **JSON estruturado**
  (structured outputs): um resumo + uma lista de atos com campos fixos.

---

## 3. Componentes (arquivos)

### Código (`src/`)
| Arquivo | Papel |
|---|---|
| `config.py` | Configuração central (.env, caminhos, modelo, pasta das exonerações, relê a chave) |
| `extrair.py` | MinerU: PDF → texto (`content_list.json`), **em fatias** para não estourar a memória |
| `index_build.py` | Texto do MinerU → índice FTS5 (`--latest`, `--extract`, `--force`) |
| `search.py` | Busca BM25, datas disponíveis, texto de uma página, `pages_matching` (todas as páginas de um tema) |
| `reader.py` | Chama o Claude e devolve a resposta **estruturada** (resumo + itens) |
| `export.py` | Gera **Excel / PDF / JSON** (só a tabela, sem cabeçalho de pergunta) |
| `render.py` | Renderiza a página do PDF em imagem, com **marca-texto** alinhado |
| `highlight.py` | Prepara os termos a destacar |
| `app.py` | Servidor web (Flask): `/api/ask`, `/api/export`, `/api/page`, `/api/dates` |
| `ask.py` | Versão de terminal (perguntar sem abrir o site) |
| `exonerar.py` | **Job**: extrai as exonerações do dia e salva o Excel |

### Frontend
- `web/index.html` — página única (busca, atalhos, resposta estruturada, botões de download,
  miniaturas das páginas com destaque). `web/assets/` = brasão.

### Automação e apoio
| Arquivo | Papel |
|---|---|
| `atualizar_dia.bat` | Job das **08:05**: extrai + indexa a edição do dia |
| `atualizar_exoneracoes.bat` | Job das **08:30 (seg-sex)**: indexa (garantia) + gera o Excel das exonerações |
| `web.bat` | Sobe o site local |
| `reindex.bat` | Reindexa tudo manualmente |
| `requirements.txt`, `README.md`, `.env` | Dependências, guia e configurações/chave |
| `data/index/doerj_fts.db` | O índice de busca (gerado) |
| `saida/` | Saída do MinerU (gerada) |

---

## 4. A tarefa agendada (Windows)

| Tarefa | Quando | O que faz |
|---|---|---|
| `DOERJ_Pipeline` | **Seg–Sex**, 08:05, repetindo a cada 60 min por 10 h | Roda o `run_pipeline.bat`: baixa o D.O., extrai com o MinerU, indexa, gera os Excel e grava no Oracle (001A e 002A) |

Repete de hora em hora porque o D.O. não tem hora fixa — pode sair às 08:00 ou às 10:00. Cada etapa
tem trava de idempotência, então as execuções seguintes do mesmo dia saem de graça (não re-chamam a
IA sobre uma edição já processada).

As tarefas antigas `DOERJ_MinerU_atualizar` e `DOERJ_Exoneracoes` foram substituídas por esta e
estão desabilitadas.

```
schtasks /Run    /TN "DOERJ_Pipeline"     # rodar agora (teste)
schtasks /Query  /TN "DOERJ_Pipeline" /V  # status / próxima execução
```
Log: `logs\pipeline.log` (rotacionado pelo `src/limpar.py` ao passar de 5 MB).

Para criar a tarefa num servidor novo: `deploy\instalar_tarefa.bat` — ver [IMPLANTACAO.md](IMPLANTACAO.md).

---

## 5. O site (http://127.0.0.1:5001)

- **Busca** por texto com **escopo por data** (padrão: edição do dia; ou escolher data; ou "todas").
- **Atalhos** prontos: Sefaz, Exonerações, Nomeações, Atos do Governador, Leis, Decretos, Resoluções.
- **Resposta estruturada**: um resumo + uma **tabela** de atos (Tipo, Número, Data, Órgão, Pessoa,
  Cargo, Objeto, Processo, Página, Edição).
- **Downloads**: botões **Excel / JSON / PDF** (gerados no servidor).
- **Conferência visual**: miniatura de cada página usada, com o termo buscado **marcado em amarelo**
  exatamente sobre o texto.
- Ligar o site: **`web.bat`** (só um por vez na porta 5001).

---

## 6. Decisões e cuidados importantes (por que está assim)

**Rede da SEFAZ (proxy + inspeção TLS)** — tratados no código:
- `NO_PROXY` isenta o localhost (o MinerU fala com um serviço interno).
- `truststore` faz o Python confiar no certificado da SEFAZ (baixar modelos e chamar a API).
- Ambiente **venv isolado** (evita conflito de `numpy` com outros projetos).

**MinerU em CPU:**
- Backend `pipeline` + método `txt` (leve); **fórmulas desligadas** (o Diário não tem; evita baixar
  um modelo de 810 MB que travava).
- Extração **em fatias de 30 páginas** — edições grandes (79+ páginas) num processo só derrubavam o
  MinerU por memória; em fatias, o uso de RAM fica controlado (com 1 retry por fatia).

**IA (Claude):**
- **Structured outputs** (JSON validado por schema) → resposta sempre no mesmo formato.
- **Sem "thinking"** nesse passo — a extração é bem definida, e o thinking poderia consumir o
  orçamento de tokens e truncar o JSON.
- A **chave da API é relida do `.env` a cada pergunta** — trocar a chave passa a valer na hora,
  sem reiniciar o servidor (resolvia o antigo erro 401 "invalid x-api-key").

**Excel:** só a tabela (sem bloco de Pergunta/Edição/Resumo no topo), com o cabeçalho **fixo** ao rolar. 

---

## 7. Requisitos para funcionar no dia a dia

- **Máquina ligada** nos horários do job (08:05 às 18:05, seg-sex). Num servidor a tarefa roda com o
  usuário deslogado; ver [IMPLANTACAO.md](IMPLANTACAO.md).
- **`ANTHROPIC_API_KEY`** válida no `.env` (a IA é chamada ~3 vezes por edição). Custo pequeno.
- Acesso ao **portal do IOERJ** (o próprio pipeline baixa o D.O. com o Playwright) e ao **Oracle**.
- As tabelas de configuração no Oracle (**001B**, **002B**, **002N**) precisam ter linha vigente; se
  o banco não responder, o job usa as listas de reserva do código e avisa no log.

---

## 8. Como operar o sistema       

```bash
# Ligar o site
web.bat                      # -> http://127.0.0.1:5001

# Rodar o job das exonerações na mão (sem esperar 08:30)
.venv\Scripts\python.exe src\exonerar.py                    # edição mais recente
.venv\Scripts\python.exe src\exonerar.py --date 2026-07-08  # uma data

# Indexar a edição do dia na mão
.venv\Scripts\python.exe src\index_build.py --latest

# Perguntar pelo terminal
.venv\Scripts\python.exe src\ask.py "quais decretos sairam hoje?"
```
