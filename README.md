# RAG do Diário Oficial do RJ — com MinerU

Busca e consulta do **DOERJ** (Diário Oficial do Estado do RJ) por IA, com front-end web.
Estrutura inspirada no projeto **RAG_DO**, mas o texto vem do **MinerU** (extração limpa).

- **Fonte:** `DOERJ_AAAA-MM-DD.pdf` (PDF com texto), salvos no OneDrive por um job.
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

```bash
python -m venv .venv
source .venv/Scripts/activate        # Git Bash no Windows
pip install -r requirements.txt
```

Depois cole sua chave em [.env](.env):
```
ANTHROPIC_API_KEY=sk-ant-...
```

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

## Notas da rede SEFAZ

O ambiente corporativo tem proxy com inspeção TLS. Isso já está tratado:
- `truststore` (no venv) faz o Python confiar no certificado da SEFAZ;
- `NO_PROXY` isenta o localhost para o serviço interno do MinerU.
#