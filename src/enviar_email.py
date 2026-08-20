"""
============================================================================
enviar_email.py · Manda o boletim do dia por e-mail (API corporativa)
----------------------------------------------------------------------------
O pipeline já produz `relatorios/boletim_<data>.html` no formato do e-mail que
a área recebe. Este módulo é o último passo: entregar esse HTML na caixa de
destino, sem ninguém copiar e colar.

  De:    robodoerj@fazenda.rj.gov.br      (EMAIL_REMETENTE)
  Para:  svc_sei_auto@fazenda.rj.gov.br   (EMAIL_DESTINATARIOS)

NÃO É MAIS SMTP. O envio é um POST na API corporativa de e-mail — a mesma que
os sistemas Java chamam pelo `EmailClient`:

    POST  <EMAIL_API_URL>
    Authorization: Bearer <token>
    {"to": "...", "from": "...", "subject": "...", "corpo": "<html>",
     "sistema": "..."}                       <- o EmailRequestDTO, campo a campo

O token do header NÃO é o do .env: são DOIS tokens. O AUTORIZADOR_TOKEN é a
credencial de entrada do autorizador, que a troca pelo token que a API de e-mail
aceita (ver `obter_token`) — exatamente o que o AutorizadorClient faz no Java.

Três consequências do contrato da API, todas visíveis no código abaixo:

  1. o DTO tem UM destinatário (`to`), sem cópia e sem Reply-To. Então a lista do
     .env (destinatários + cópia) vira UMA REQUISIÇÃO POR ENDEREÇO — cada uma
     validada e registrada por si. Como não há Reply-To, a resposta que o rodapé
     do boletim pede volta para o EMAIL_REMETENTE;
  2. o corpo é só HTML (não há parte text/plain para montar);
  3. fora do ambiente 'prd' a API do Java bloqueia domínio externo. A MESMA trava
     está aqui (`validar_dominio`), para o bloqueio aparecer no nosso log em vez
     de virar um 4xx opaco — e para uma configuração errada de homologação não
     mandar boletim para fora.

DUAS TRAVAS, porque o job roda de hora em hora (10x por dia):
  1. só envia se existir o boletim CANÔNICO (`boletim_<data>.html`, sem sufixo).
     O monitor só grava esse nome numa rodada COMPLETA — rodada parcial vira
     `boletim_<data>_PARCIAL.html`. Então a existência do canônico JÁ É o sinal
     de "rodada completa": não precisa de flag nem de estado à parte;
  2. depois de enviar, grava `relatorios/email_enviado_<data>.txt`. Enquanto ele
     existir, as outras 9 rodadas do dia não reenviam. Se SÓ PARTE dos endereços
     recebeu, o recibo sai com sufixo _PARCIAL e lista quem já recebeu: a rodada
     seguinte tenta apenas os que faltaram, em vez de mandar boletim repetido
     para quem já tinha recebido.

SEM CREDENCIAL AINDA? Com EMAIL_ENVIO_ATIVO=false (o padrão), as requisições são
MONTADAS e gravadas como `relatorios/email_<data>.json` em vez de enviadas — dá
para conferir URL, destinatários, assunto e tamanho do HTML antes de existir
token (o token sai mascarado no arquivo). O boletim em si abre no navegador:
`relatorios/boletim_<data>.html`. O marcador NÃO é gravado nesse modo: no dia em
que a credencial chegar, o envio de verdade acontece.

Uso:
    python src/enviar_email.py                     # boletim mais recente
    python src/enviar_email.py --date 2026-08-19
    python src/enviar_email.py --extra             # boletim da EDIÇÃO EXTRA
    python src/enviar_email.py --dry-run           # força só gravar o .json
    python src/enviar_email.py --force             # reenvia (ignora o marcador)
============================================================================
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import config

# Em rede corporativa com proxy de inspeção TLS (SEFAZ), confia no cofre do
# Windows — mesma linha do reader.py. Sem isto, o POST na API interna morre em
# CERTIFICATE_VERIFY_FAILED em vez de chegar ao servidor.
try:
    import truststore
    truststore.inject_into_ssl()
except Exception:  # noqa: BLE001 - fora da rede corporativa não é necessário
    pass


class EmailBloqueado(Exception):
    """Trava de domínio: o ambiente não pode mandar e-mail para este endereço.

    É separada das falhas de rede de propósito — não adianta a rodada seguinte
    tentar de novo, o que precisa mudar é a configuração."""


# Credencial em vigor: (token, quando_expira). Espelha o `Map credenciais` do
# AutorizadorClient — o mesmo processo manda uma requisição por destinatário e
# não vale reautenticar em cada uma.
_credencial = (None, datetime.min)


def configurado():
    """True se dá para enviar de verdade (URL, credencial e endereços no .env).

    Espelha oracle_db.configurado(): o job não quebra por falta de configuração,
    ele avisa e segue."""
    cfg = config.email_settings()
    return bool(cfg["url"] and cfg["token"] and cfg["autorizador_url"]
                and cfg["remetente"] and cfg["destinatarios"])


def _data_br(date_iso):
    """'2026-08-19' -> '19/08/2026'."""
    try:
        a, m, d = str(date_iso).split("-")
        return f"{d}/{m}/{a}"
    except ValueError:
        return str(date_iso or "")


def assunto_boletim(date, extra=False):
    """Assunto da mensagem. A EDIÇÃO EXTRA precisa se anunciar já no assunto:
    nos dias em que ela sai, a área recebe DOIS e-mails, e a caixa de entrada
    mostra só essa linha."""
    prefixo = config.email_settings()["assunto_prefixo"]
    marca = " - EDICAO EXTRA" if extra else ""
    return f"{prefixo}{marca} - {_data_br(date)}"


def validar_dominio(endereco, cfg):
    """Fora de 'prd', só endereço de domínio interno. Levanta EmailBloqueado.

    Tradução do `validarDominio` do EmailClient. Vale repetir a regra aqui, do
    lado de cá da rede: em homologação, um EMAIL_DESTINATARIOS apontando para
    fora é erro de configuração que precisa aparecer com nome e sobrenome no log
    — não como um HTTP 400 sem explicação."""
    if cfg["ambiente"].lower() == "prd":
        return
    dominio = str(endereco).rsplit("@", 1)[-1].lower()
    if not any(interno in dominio for interno in cfg["dominios_internos"]):
        raise EmailBloqueado(
            f"Bloqueio de seguranca: ambiente '{cfg['ambiente']}' nao pode enviar "
            f"e-mail para dominio externo. Tentativa para: {endereco}")


def obter_token(cfg, renovar=False):
    """O Bearer do header, emitido pelo AUTORIZADOR. Levanta em qualquer falha.

    É o `AutorizadorClient.getAuthenticatedToken("EMAIL")` traduzido:

        POST <autorizador>/api/v1/usuario/autenticar
        Authorization: Bearer <AUTORIZADOR_TOKEN>     <- credencial de ENTRADA
        (sem corpo)                             -> CredencialDTO, com o token bom

    O detalhe que custou uma tarde: o token do .env NÃO é aceito pela API de
    e-mail. Ele é a credencial de entrada aqui (claim `aplicacao:
    AUTORIZADOR-SERVICE`, `tipoToken: AUTH`); quem o `/email/enviar` aceita é o
    token que ESTA chamada devolve. Mandar o de entrada dá 403 de corpo vazio,
    idêntico ao de uma requisição sem header nenhum.

    O cache espelha o `Map<String, CredencialDTO> credenciais` do Java: o job
    manda uma requisição por destinatário e não vale reautenticar em cada uma."""
    global _credencial
    if not cfg["token"]:
        raise RuntimeError("sem credencial: preencha AUTORIZADOR_TOKEN no .env")
    if not cfg["autorizador_url"]:
        raise RuntimeError(
            f"ambiente '{cfg['ambiente']}' nao tem autorizador conhecido - use "
            f"{' ou '.join(config.AUTORIZADOR_URLS)} em AUTORIZADOR_AMBIENTE "
            "(ou preencha AUTORIZADOR_URL no .env)")

    token, expira = _credencial
    if token and not renovar and datetime.now() < expira:
        return token

    # Sem corpo, como o HttpEntity<>(headers) do Java. O b"" (em vez de None) é
    # o que faz o urllib mandar Content-Length: 0 num POST — sem ele, servidor
    # atrás de proxy pode recusar com 411.
    req = urllib.request.Request(
        cfg["autorizador_url"], data=b"", method="POST",
        headers={"Authorization": "Bearer " + cfg["token"],
                 "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
            dados = json.loads(r.read().decode("utf-8", "replace") or "{}")
    except urllib.error.HTTPError as e:
        detalhe = e.read().decode("utf-8", "replace")[:200]
        raise RuntimeError(f"autorizador respondeu HTTP {e.code}"
                           f"{': ' + detalhe if detalhe else ''}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"nao foi possivel falar com o autorizador "
                           f"({cfg['autorizador_url']}): {e.reason}") from e

    token = (dados.get("token") or "").strip()
    if not token:
        raise RuntimeError("autorizador nao devolveu token (status="
                           f"{dados.get('status')}; {dados.get('msgStatus')})")

    # dataHoraExpiracao vem como 'dd/MM/yyyy HH:mm' (o @JsonFormat do
    # CredencialDTO). Sem ela, ou ilegível, valemos 5 minutos: reautenticar de
    # graça é barato, usar token vencido custa uma edição sem boletim.
    try:
        expira = datetime.strptime(dados["dataHoraExpiracao"], "%d/%m/%Y %H:%M")
    except (KeyError, TypeError, ValueError):
        expira = datetime.now() + timedelta(minutes=5)
    # Margem: token que vence no meio do envio vale como vencido.
    _credencial = (token, expira - timedelta(seconds=60))
    print(f"[e-mail] autorizador: token de {dados.get('usuario') or '?'}"
          f"/{dados.get('codigoAplicacao') or '?'} valido ate "
          f"{dados.get('dataHoraExpiracao') or '?'}")
    return token


def montar_payload(assunto, corpo_html, destinatario, cfg):
    """O EmailRequestDTO, campo a campo. Nomes exatamente como o @JsonProperty."""
    return {
        "to": destinatario,
        "from": cfg["remetente"],
        "subject": assunto,
        "corpo": corpo_html,
        "sistema": cfg["sistema"],
    }


def _postar(payload, cfg):
    """POST na API de e-mail. Levanta exceção em QUALQUER falha.

    Repete UMA vez em caso de 401/403: com o autorizador ligado, o token pode ter
    expirado entre o cache e a chamada — o retry com token novo é mais barato que
    uma edição inteira sem boletim."""
    corpo = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for tentativa in (1, 2):
        req = urllib.request.Request(
            cfg["url"], data=corpo, method="POST",
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json",
                     "Authorization": "Bearer " + obter_token(cfg, renovar=tentativa == 2)})
        try:
            with urllib.request.urlopen(req, timeout=cfg["timeout"]) as r:
                return r.status
        except urllib.error.HTTPError as e:
            if e.code in (401, 403) and tentativa == 1 and cfg["autorizador_url"]:
                continue
            detalhe = e.read().decode("utf-8", "replace")[:300]
            raise RuntimeError(f"a API de e-mail respondeu HTTP {e.code}"
                               f"{': ' + detalhe if detalhe else ''}") from e
        except urllib.error.URLError as e:
            raise RuntimeError(f"nao foi possivel falar com {cfg['url']}: "
                               f"{e.reason}") from e


def _arquivo_boletim(date, extra=False):
    """Caminho do boletim CANÔNICO da edição (o de rodada completa)."""
    return config.RELATORIOS_DIR / f"boletim_{date}{'_EXTRA' if extra else ''}.html"


def _arquivo_conferencia(date, extra=False):
    """Onde o modo de conferência grava as requisições montadas."""
    return config.RELATORIOS_DIR / f"email_{date}{'_EXTRA' if extra else ''}.json"


def _marcador(date, extra=False, parcial=False):
    """Recibo do envio. Enquanto o completo existir, as rodadas seguintes não
    reenviam; o _PARCIAL lista quem JÁ recebeu, para a rodada seguinte tentar só
    os que faltaram."""
    return config.RELATORIOS_DIR / (
        f"email_enviado_{date}{'_EXTRA' if extra else ''}"
        f"{'_PARCIAL' if parcial else ''}.txt")


def _ja_entregues(date, extra=False):
    """Endereços já entregues numa rodada anterior (lidos do recibo _PARCIAL)."""
    arq = _marcador(date, extra, parcial=True)
    if not arq.exists():
        return set()
    return {ln.strip().lower() for ln in arq.read_text(encoding="utf-8").splitlines()
            if "@" in ln and not ln.startswith("#")}


def _data_da_edicao_corrente():
    """Data do boletim NORMAL mais recente em relatorios/ ('' se não houver).

    Sai do NOME do arquivo, sem tocar no índice nem no Oracle: quem manda no envio
    é justamente o que o monitor conseguiu fechar em disco.

    Vale também para o envio da edição EXTRA, e isso é o ponto: procurar "o boletim
    _EXTRA mais recente" faria o job de hoje desenterrar a última edição extra que
    existiu — no teste, um 07/08 sendo oferecido para envio em 19/08. Edição extra
    é evento raro; o que interessa é se a edição CORRENTE teve uma."""
    padrao = re.compile(r"^boletim_(\d{4}-\d{2}-\d{2})\.html$")
    datas = [m.group(1) for f in config.RELATORIOS_DIR.glob("boletim_*.html")
             if (m := padrao.match(f.name))]
    return max(datas) if datas else ""


def enviar_html(assunto, corpo_html, destinatarios=None, dry_run=False, arquivo=None):
    """Monta e entrega (uma requisição por destinatário) ou grava o .json.

    NÃO levanta por falha de UM destinatário: devolve o que entregou e o que não,
    para o chamador registrar os dois. Levanta só o que impede qualquer envio
    (configuração faltando).

    Devolve {"entregues","falhas","arquivo","bytes","assunto"}, com `falhas`
    sendo uma lista de (endereço, motivo)."""
    cfg = config.email_settings()
    if destinatarios is None:
        destinatarios = cfg["destinatarios"] + cfg["copia"]
    if not destinatarios:
        raise RuntimeError("nenhum destinatario definido (EMAIL_DESTINATARIOS no .env)")
    if not cfg["remetente"]:
        raise RuntimeError("EMAIL_REMETENTE nao definido no .env")

    payloads = [montar_payload(assunto, corpo_html, d, cfg) for d in destinatarios]
    resultado = {"entregues": [], "falhas": [], "arquivo": None,
                 "bytes": len(corpo_html or ""), "assunto": assunto}

    # Sem API configurada, ou com o envio desligado, as requisições viram arquivo.
    if dry_run or not cfg["ativo"] or not cfg["url"]:
        motivo = ("--dry-run" if dry_run else
                  "EMAIL_ENVIO_ATIVO=false" if not cfg["ativo"] else "EMAIL_API_URL vazia")
        destino = Path(arquivo) if arquivo else config.RELATORIOS_DIR / "email.json"
        destino.parent.mkdir(parents=True, exist_ok=True)
        destino.write_text(json.dumps({
            "modo": f"conferencia ({motivo}) - NADA foi enviado",
            "url": cfg["url"] or "(EMAIL_API_URL vazia)",
            "ambiente": cfg["ambiente"],
            "headers": {"Content-Type": "application/json; charset=utf-8",
                        "Authorization": "Bearer ***"},
            "requisicoes": payloads,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[e-mail] modo conferencia ({motivo}): NADA enviado, requisicoes gravadas "
              f"em {destino}")
        print(f"[e-mail]   assunto: {assunto}")
        print(f"[e-mail]   de: {cfg['remetente']} -> para: {', '.join(destinatarios)}")
        resultado["arquivo"] = destino
        return resultado

    for payload in payloads:
        endereco = payload["to"]
        try:
            validar_dominio(endereco, cfg)
            _postar(payload, cfg)
        except Exception as e:  # noqa: BLE001 - um destinatario ruim nao cancela os outros
            resultado["falhas"].append((endereco, f"{type(e).__name__}: {str(e)[:200]}"))
            continue
        resultado["entregues"].append(endereco)
        print(f"[ok] boletim enviado para {endereco} | assunto: {assunto}")
    return resultado


def enviar_boletim(date=None, extra=False, dry_run=False, force=False,
                   destinatarios=None):
    """Passo 7/7: envia o boletim da edição. NUNCA levanta — devolve True/False.

    True só quando TODOS os destinatários receberam: uma entrega parcial não pode
    ser registrada como boletim entregue. Uma falha de e-mail também não pode
    marcar como fracassada uma execução que já gravou tudo no Oracle; mas não pode
    passar despercebida, então o erro sai em bloco destacado no log."""
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass

    if not date:
        date = _data_da_edicao_corrente()
        if not date:
            print(f"[e-mail] nenhum boletim em {config.RELATORIOS_DIR} -> nada a enviar.")
            return False

    arquivo = _arquivo_boletim(date, extra)
    if not arquivo.exists():
        # Rodada parcial grava boletim_<data>_PARCIAL.html; o canônico não existe.
        parcial = arquivo.with_name(arquivo.stem + "_PARCIAL.html")
        if parcial.exists():
            print(f"[e-mail] o boletim de {date}{' (EXTRA)' if extra else ''} saiu PARCIAL "
                  "-> NAO envia (aguarda uma rodada completa).")
        elif extra:
            # O caso comum: a maioria das edições não tem extra.
            print(f"[e-mail] a edicao de {date} nao tem boletim de EDICAO EXTRA "
                  "-> nada a enviar.")
        else:
            print(f"[e-mail] {arquivo.name} nao existe -> nada a enviar.")
        return False

    marcador = _marcador(date, extra)
    if marcador.exists() and not force:
        print(f"[e-mail] boletim de {date}"
              f"{' (EXTRA)' if extra else ''} ja foi enviado -> pulando "
              f"({marcador.name}; use --force para reenviar).")
        return False

    cfg = config.email_settings()
    alvos = destinatarios if destinatarios is not None else (cfg["destinatarios"]
                                                             + cfg["copia"])
    # Rodada anterior entregou parte da lista: os que já receberam ficam de fora,
    # senão a cada hora eles ganhariam o mesmo boletim de novo.
    anteriores = set() if force else _ja_entregues(date, extra)
    if anteriores:
        pendentes = [d for d in alvos if d.lower() not in anteriores]
        print(f"[e-mail] envio anterior entregou {len(anteriores)} de {len(alvos)} "
              f"endereco(s) -> tentando so {len(pendentes)} que faltaram.")
        if not pendentes:
            # A lista do .env encolheu: todo mundo que resta já recebeu.
            marcador.write_text(f"{datetime.now():%Y-%m-%d %H:%M:%S} | entrega completa "
                                f"(todos ja constavam do recibo parcial)\n", encoding="utf-8")
            _marcador(date, extra, parcial=True).unlink(missing_ok=True)
            return True
        alvos = pendentes

    assunto = assunto_boletim(date, extra)
    try:
        res = enviar_html(assunto, arquivo.read_text(encoding="utf-8"),
                          destinatarios=alvos, dry_run=dry_run,
                          arquivo=_arquivo_conferencia(date, extra))
    except Exception as e:  # noqa: BLE001 - o pipeline não cai por causa do e-mail
        res = {"entregues": [], "falhas": [("(todos)", f"{type(e).__name__}: {str(e)[:200]}")],
               "arquivo": None, "bytes": 0, "assunto": assunto}

    # Modo de conferência: nada foi enviado e nenhum marcador é gravado — gravá-lo
    # faria o primeiro envio real (quando a credencial chegar) ser pulado.
    if res["arquivo"] is not None:
        return False

    if res["falhas"]:
        print("=" * 72, flush=True)
        print("[ATENCAO][E-MAIL] O BOLETIM NAO FOI ENVIADO PARA TODOS OS DESTINATARIOS.",
              flush=True)
        print(f"[ATENCAO][E-MAIL] edicao: {date}{' (EXTRA)' if extra else ''} | "
              f"assunto: {assunto}", flush=True)
        print(f"[ATENCAO][E-MAIL] API: {cfg['url'] or '(EMAIL_API_URL vazia)'} | "
              f"ambiente: {cfg['ambiente']}", flush=True)
        for endereco, motivo in res["falhas"]:
            print(f"[ATENCAO][E-MAIL]   {endereco}: {motivo}", flush=True)
        if res["entregues"]:
            print(f"[ATENCAO][E-MAIL] JA receberam (nao serao reenviados): "
                  f"{', '.join(res['entregues'])}", flush=True)
        print("[ATENCAO][E-MAIL] o marcador de envio completo NAO foi gravado -> a "
              "proxima rodada tentara os que faltaram.", flush=True)
        print("=" * 72, flush=True)

    entregues = sorted(anteriores | {d.lower() for d in res["entregues"]})
    if res["falhas"]:
        if res["entregues"]:
            _marcador(date, extra, parcial=True).write_text(
                f"# entrega PARCIAL de {assunto}\n"
                f"# ultima tentativa: {datetime.now():%Y-%m-%d %H:%M:%S}\n"
                + "\n".join(entregues) + "\n", encoding="utf-8")
        return False

    marcador.write_text(
        f"{datetime.now():%Y-%m-%d %H:%M:%S} | {assunto} | "
        f"{', '.join(entregues)} | {res['bytes']} bytes de HTML\n",
        encoding="utf-8")
    _marcador(date, extra, parcial=True).unlink(missing_ok=True)
    return True


def main():
    """Linha de comando do passo 7/7."""
    ap = argparse.ArgumentParser(
        description="Envia o boletim HTML do DOERJ pela API corporativa de e-mail.")
    ap.add_argument("--date", default=None, help="Edicao AAAA-MM-DD (padrao: a mais recente)")
    ap.add_argument("--extra", action="store_true",
                    help="Envia o boletim da EDICAO EXTRA (boletim_<data>_EXTRA.html)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Nao envia: so grava relatorios/email_<data>.json para conferencia")
    ap.add_argument("--force", action="store_true",
                    help="Reenvia mesmo se o marcador de envio ja existir")
    ap.add_argument("--para", default=None,
                    help="Destinatario(s) desta execucao, sobrepondo o .env (teste)")
    args = ap.parse_args()

    destinatarios = config._lista_emails(args.para) if args.para else None
    enviar_boletim(date=args.date, extra=args.extra, dry_run=args.dry_run,
                   force=args.force, destinatarios=destinatarios)
    # Sai 0 SEMPRE: quem decide se a falha derruba algo é o run_pipeline, que
    # chama este passo sem _run() (como faz com o download e a limpeza).


if __name__ == "__main__":
    main()
