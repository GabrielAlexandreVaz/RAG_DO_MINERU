# RAG do Diário Oficial do RJ (DOERJ) com MinerU — Resumo do Projeto

## 1. O que é

Uma ferramenta interna que lê o **Diário Oficial do Estado do RJ (DOERJ)** todos os dias,
transforma o PDF em **texto limpo e pesquisável**, e permite:

- **Consultar por IA** (site local): perguntas em linguagem natural → resposta **estruturada**
  (tabela de atos) com citação de data e página, e **destaque** do trecho na imagem da página.
- **Baixar** a resposta em **Excel, JSON ou PDF**.
- **Job automático** que extrai os **atos de pessoal do dia** (nomeações, exonerações) e grava numa
  tabela Oracle, além do **monitoramento estruturado** em 8 seções (Excel + Oracle).

Tudo roda **localmente**, em **CPU** (sem GPU), na rede corporativa da SEFAZ.

---

## 2. Como funciona (o fluxo)

```
[seg-sex, de hora em hora a partir das 08:05]  run_pipeline.bat
        |
   1) baixa o PDF do dia do portal do IOERJ (Playwright)  ->  DOERJ_DOWNLOADS_DIR
        |
   2) MinerU extrai o texto limpo da Parte I (em fatias)  ->  saida/<edição>/txt/*_content_list.json
      e indexa por página no banco de busca (SQLite FTS5) ->  data/index/doerj_fts.db
        |
   3) Partes IB/II/IV/V lidas em memória (PyMuPDF) e indexadas (sem salvar PDF)
        |
   4) Atos de pessoal: palavras-chave da 001B -> Claude estrutura -> Oracle 001A
        |
   5) Monitor 8 temas: pré-filtro da 002B + nomes da 002N -> Claude -> Oracle 002A + Excel
        |
   +) Limpeza: apaga saida/ com mais de 30 dias e rotaciona o log

Site (Flask, http://127.0.0.1:5001), à parte: busca BM25 -> Claude -> tabela + downloads
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
| `config.py` | Configuração central (.env, caminhos, modelo, relê a chave) |
| `run_pipeline.py` | **Orquestra** as 5 etapas do job diário, em subprocessos |
| `download_diario.py` | Baixa o D.O. do portal do IOERJ (Playwright) |
| `extrair.py` | MinerU: PDF → texto (`content_list.json`), **em fatias** para não estourar a memória |
| `index_build.py` | Texto do MinerU → índice FTS5 (`--latest`, `--extract`, `--force`) |
| `ler_cadernos.py` | Partes IB/II/IV/V lidas em memória (PyMuPDF) e indexadas |
| `search.py` | Busca BM25, datas disponíveis, texto de uma página, `pages_matching` (todas as páginas de um tema) |
| `reader.py` | Chama o Claude e devolve a resposta **estruturada** (resumo + itens) |
| `atos_pessoal.py` | **Job**: atos de pessoal → Oracle 001A (palavras-chave da 001B) |
| `monitor_estruturado.py` | **Job**: monitor 8 seções → Oracle 002A + Excel (002B e 002N) |
| `oracle_db.py` | Leitura das tabelas de configuração e gravação nas de dados |
| `limpar.py` | Retenção de disco: apaga `saida/` antiga e rotaciona o log |
| `export.py` | Gera **Excel / PDF / JSON** (só a tabela, sem cabeçalho de pergunta) |
| `render.py` | Renderiza a página do PDF em imagem, com **marca-texto** alinhado |
| `highlight.py` | Prepara os termos a destacar |
| `app.py` | Servidor web (Flask): `/api/ask`, `/api/export`, `/api/page`, `/api/dates` |
| `ask.py` | Versão de terminal (perguntar sem abrir o site) |

### Frontend
- `web/index.html` — página única (busca, atalhos, resposta estruturada, botões de download,
  miniaturas das páginas com destaque). `web/assets/` = brasão.

### Automação e apoio
| Arquivo | Papel |
|---|---|
| `run_pipeline.bat` | **O job**: o que a tarefa `DOERJ_Pipeline` dispara |
| `deploy/instalar.bat` | Monta o ambiente do zero (venv, truststore, Chromium, modelos) |
| `deploy/verificar.py` | Diagnóstico: confere 12 itens antes de agendar |
| `deploy/instalar_tarefa.bat` | Cria a tarefa agendada no servidor |
| `deploy/schema.sql`, `deploy/seed_config.sql` | Recriar as 5 tabelas noutro banco |
| `atualizar_dia.bat` | Legado: extrai + indexa a edição do dia (tarefa desativada) |
| `web.bat` | Sobe o site local |
| `reindex.bat` | Reindexa tudo manualmente |
| `requirements.txt`, `README.md`, `IMPLANTACAO.md`, `.env` | Dependências, guias e configurações/chave |
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
# Rodar o job completo na mão (sem esperar o horário)
run_pipeline.bat
schtasks /Run /TN "DOERJ_Pipeline"

# Diagnóstico quando algo falhar
deploy\verificar.bat
type logs\pipeline.log

# Etapas isoladas
.venv\Scripts\python.exe src\download_diario.py                 # só baixar
.venv\Scripts\python.exe src\index_build.py --latest            # só indexar
.venv\Scripts\python.exe src\atos_pessoal.py --tema exonerar    # só um tema no Oracle
.venv\Scripts\python.exe src\monitor_estruturado.py --force     # refazer o monitor do dia

# Ligar o site (consulta local, fora do pipeline)
web.bat                      # -> http://127.0.0.1:5001
.venv\Scripts\python.exe src\ask.py "quais decretos sairam hoje?"
```
