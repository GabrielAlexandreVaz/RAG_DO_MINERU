-- ==========================================================================
--  schema.sql · As 5 tabelas do projeto no Oracle
--  --------------------------------------------------------------------------
--  Extraido do ambiente atual (COE_IA @ sef01d) com DBMS_METADATA.GET_DDL.
--  Rode isto SO se o banco de producao for outro; no banco atual as tabelas ja
--  existem. Depois rode o seed_config.sql para popular as 3 de configuracao.
--
--  Troque COE_IA pelo schema de destino, se for diferente, e ajuste o
--  ORACLE_SCHEMA no .env para combinar.
--
--  Duas familias de tabela:
--    *A / *N  -> dados e cadastro que o job LE ou ESCREVE
--    *B       -> configuracao (o que procurar), lida a cada execucao
-- ==========================================================================

-- --------------------------------------------------------------------------
-- 001A · Atos de pessoal extraidos pela IA (escrita: atos_pessoal.py)
--        Idempotente por EDICAO + RESPOSTA (DELETE + INSERT a cada execucao).
-- --------------------------------------------------------------------------
CREATE TABLE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001A" (
    ID            NUMBER(19,0),
    RESPOSTA      VARCHAR2(150),    -- rotulo do tema: 'Exoneracao', 'Nomeacao'...
    TIPO          VARCHAR2(150),
    NUMERO        VARCHAR2(300),
    DATA_ATO      DATE,
    ORGAO         VARCHAR2(1000),
    PESSOA        VARCHAR2(500),
    ID_FUNCIONAL  VARCHAR2(40),     -- matricula/IF do servidor
    CARGO         VARCHAR2(2000),
    OBJETO        VARCHAR2(4000),
    PROCESSO      VARCHAR2(100),
    PAGINA        VARCHAR2(8),
    EDICAO        DATE,             -- data da edicao do D.O.
    DT_CARGA      TIMESTAMP(6) DEFAULT SYSTIMESTAMP
);

-- --------------------------------------------------------------------------
-- 001B · Palavras-chave dos atos de pessoal (leitura: atos_pessoal.py)
--        Define O QUE procurar. Incluir = INSERT; parar = UPDATE com DATA_FIM.
--        Escreva o verbo no infinitivo ('exonerar'); o job deriva a busca
--        ('exonera*') e o rotulo ('Exoneracao').
-- --------------------------------------------------------------------------
CREATE TABLE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001B" (
    ID             NUMBER,
    PALAVRA_CHAVE  VARCHAR2(200),
    DATA_INI       DATE,            -- vigente a partir de (nulo = sempre)
    DATA_FIM       DATE             -- vigente ate (nulo = ainda vigente)
);

-- --------------------------------------------------------------------------
-- 002A · Monitoramento estruturado, 8 secoes (escrita: monitor_estruturado.py)
--        Idempotente por DATA_EDICAO. TIPO = a categoria do item.
-- --------------------------------------------------------------------------
CREATE TABLE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002A" (
    ID           NUMBER(19,0),
    TIPO_ATO     VARCHAR2(120),
    NUMERO_ANO   VARCHAR2(56),
    ORGAO        VARCHAR2(128),
    PESSOA       VARCHAR2(360),
    CARGO        VARCHAR2(104),
    PROCESSO     VARCHAR2(200),
    VIGENCIA     VARCHAR2(56),
    RESUMO       VARCHAR2(704),
    CADERNO      VARCHAR2(48),
    DATA_EDICAO  DATE,
    DATA_ATO     DATE,
    PRAZO        DATE,
    PAGINA       NUMBER,
    DT_CARGA     TIMESTAMP(6),
    TIPO         VARCHAR2(32)       -- PRAZO_CRITICO, MOVIMENTACAO_PESSOAL, ...
                                    -- 32 e nao 26: EXPEDIENTE_PONTO_FACULTATIVO
                                    -- tem 28 e era truncado (some da secao 5).
);

-- --------------------------------------------------------------------------
-- 002B · Pre-filtro do monitor (leitura: monitor_estruturado.py)
--        Decide QUAIS paginas vao para a IA - e o corte de custo do job.
--        Termos sem acento e em minusculas; casam por prefixo a partir do
--        inicio de uma palavra ('tributa' pega 'tributacao').
-- --------------------------------------------------------------------------
CREATE TABLE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002B" (
    ID             NUMBER,
    PALAVRA_CHAVE  VARCHAR2(200),
    DATA_INI       DATE,
    DATA_FIM       DATE
);

-- --------------------------------------------------------------------------
-- 002N · Nomes monitorados (leitura: monitor_estruturado.py, secao 3)
--        A varredura e deterministica (nao usa IA). FUNCAO vai para a coluna
--        CARGO do relatorio.
-- --------------------------------------------------------------------------
CREATE TABLE COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002N" (
    ID        NUMBER,
    NOME      VARCHAR2(200),
    FUNCAO    VARCHAR2(200),
    DATA_INI  DATE,
    DATA_FIM  DATE
);

-- Indices sugeridos: o job filtra sempre pela data da edicao.
CREATE INDEX COE_IA.IX_DOERJ_001A_EDICAO
    ON COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_001A" (EDICAO, RESPOSTA);
CREATE INDEX COE_IA.IX_DOERJ_002A_EDICAO
    ON COE_IA."IA0001_IOERJ_RAG_DIARIO_INTELIGENTE_002A" (DATA_EDICAO);
