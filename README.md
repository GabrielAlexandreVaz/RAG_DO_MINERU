RAG do Diário Oficial do RJ — com MinerU

Busca e consulta do DOERJ (Diário Oficial do Estado do RJ) por IA, com front-end web. Estrutura inspirada no projeto RAG_DO, mas o texto vem do MinerU (extração limpa).

Fonte: (PDF com texto), baixados do portal do IOERJ pelo próprio pipeline (, Playwright) para a pasta .DOERJ_AAAA-MM-DD.pdfsrc/download_diario.pyDOERJ_DOWNLOADS_DIR
Hardware: CPU (sem GPU).
Diferença para o RAG_DO: a IA lê o texto limpo extraído pelo MinerU (não as imagens das páginas) → mais barato e preciso. A busca é por texto (FTS5/BM25).
Como funciona
PDF do dia (OneDrive)
  → MinerU (pipeline CPU/txt)  → texto limpo por página (content_list.json)
  → índice FTS5 (SQLite, BM25) → busca por palavra-chave, com escopo por data
  → Claude (Opus 4.8) lê os trechos → resposta em português, citando data e página
Front-end web (Flask) mostra a resposta + miniaturas das páginas p/ conferência.
Estrutura
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
Instalação (uma vez)
deploy\instalar.bat
Cria o , instala as versões exatas do , configura o (proxy da SEFAZ) e baixa o Chromium e os modelos do MinerU dentro do projeto. Depois copie o .env.example para e preencha a chave da IA e o Oracle..venvrequirements.lock.txttruststore.env

Para conferir se ficou tudo certo:

deploy\verificar.bat
Implantação em servidor: veja IMPLANTACAO.md.

Uso
# 1) Extrair o Diário do dia (MinerU) — lento na 1ª vez (baixa modelos)
python src/extrair.py

# 2) Indexar o texto extraído (rápido)
python src/index_build.py

# 3a) Perguntar pelo terminal
python src/ask.py "Quais atos da Sefaz saíram hoje?"

# 3b) Ou abrir o front-end web
python src/app.py            # http://127.0.0.1:5001
#   (ou dê duplo-clique em web.bat)
reindex.bat extrai (MinerU) e reindexa tudo — bom para uma Tarefa Agendada diária.

Palavras-chave dos atos de pessoal
src/atos_pessoal.py procura no D.O. os atos de pessoal e grava na 001A. O que ele procura vem da tabela IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B (, , ):PALAVRA_CHAVEDATA_INIDATA_FIM

-- passar a acompanhar um novo tipo de ato
INSERT INTO COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B" (ID, PALAVRA_CHAVE, DATA_INI)
VALUES (3, 'designar', TRUNC(SYSDATE));

-- parar de acompanhar
UPDATE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B"
   SET DATA_FIM = TRUNC(SYSDATE) WHERE PALAVRA_CHAVE = 'designar';
Escreva o verbo no infinitivo. O job tira o "r" final e busca por prefixo, para pegar todas as formas: → (acha "exonerar", "exoneração", "exonerado"). Buscar a palavra Perderia Atos literal. O rótulo gravado na coluna da 001A é derivado do verbo ( → , → ); verbos for a do padrão -ar estão no mapa do próprio arquivo.exonerarexonera*RESPOSTAexonerarExoneraçãodesignarDesignaçãoROTULOS_ESPECIAIS

Se a regra de prefixo não servir (ex.: não alcança "remoção"), dá para escrever o padrão FTS5 direto na coluna — quando o valor tem ou espaço, ele é usado como está: . Nesse caso o rótulo sai do 1º termo, então confira se ficou legível.remover*remov* OR remoc*

Vale quem está vigente na data da edição, igual aos nomes monitorados. Se o banco não responder, o Job USA a lista de reserva (, ) e avisa no log.PALAVRAS_PADRAOnomearexonerar

Pré-filtro do monitor (quais páginas vão para a IA)
src/monitor_estruturado.py não manda a edição inteira para a IA — só as páginas que citam algum termo de interesse. Esses termos vêm da tabela IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B (, , ), 35 termos hoje:PALAVRA_CHAVEDATA_INIDATA_FIM

-- passar a considerar mais um assunto
INSERT INTO COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B" (ID, PALAVRA_CHAVE, DATA_INI)
VALUES (36, 'precatorio', TRUNC(SYSDATE));

-- parar de considerar
UPDATE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B"
   SET DATA_FIM = TRUNC(SYSDATE) WHERE PALAVRA_CHAVE = 'precatorio';
Escreva sem acento e em minúsculas (o índice é normalizado). O termo casa por prefixo a partir do início de uma palavra: pega "tributação" e "tributário", mas não casa dentro de token de outro. Frases funcionam ().tributaicmsponto facultativo

Cuidado com o efeito no custo e na cobertura: incluir termo = mais páginas vão para a IA (mais recall, mais caro); retirar = mais barato, com risco de perder ato. Um termo muito genérico (ex.: ) faria quase toda página passar. Se o banco não responder, o job usa a lista de reserva do código e avisa no log.estado_KW_RELEVANCIA

Nomes monitorados (seção 3 do monitor)
src/monitor_estruturado.py varre os 5 cadernos procurando as pessoas monitoradas. A lista vem da tabela Oracle IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N (, , , ) — não é preciso mexer em arquivo nem reiniciar nada, a próxima edição já usa a lista Nova:NOMEFUNCAODATA_INIDATA_FIM

-- incluir um monitorado
INSERT INTO COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N" (ID, NOME, FUNCAO, DATA_INI)
VALUES (19, 'Fulano de Tal', 'Subsecretario de Alguma Coisa', TRUNC(SYSDATE));

-- retirar um monitorado (não apague a linha: preserva o histórico)
UPDATE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N"
   SET DATA_FIM = TRUNC(SYSDATE) WHERE NOME = 'Fulano de Tal';
Vale quem está vigente na data da edição ( e nula ou ), então reprocessar uma edição antiga usa a lista que valia naquele dia. A aparece na coluna Cargo do relatório. Se o banco não responder, o job cai para o monitorados.txt (cópia de segurança) e avisa no log.DATA_INI <= ediçãoDATA_FIM>= ediçãoFUNCAO

Notas da rede SEFAZ
O ambiente corporativo tem proxy com inspeção TLS. Isso já está tratado:

truststore (no venv) faz o Python confiar no certificado da SEFAZ;
NO_PROXY isenta o localhost para o serviço interno do MinerU.