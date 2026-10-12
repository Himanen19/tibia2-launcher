#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tibia 2 - Launcher (programa SEPARADO do jogo).

Confere a integridade dos arquivos do cliente contra o manifesto do servidor
(o mesmo updater.php que o cliente ja usava), baixa o que mudou e abre o
"Tibia 2 Client.exe" (o binario do jogo). O LOGIN acontece no proprio cliente,
depois de aberto.

Por que separado, e nao dentro do cliente: o updater embutido rodava ANTES do
loadModules do otclient, onde o loop de render/eventos ainda nao esta de pe - a
janela nao pintava e os timers/HTTP nao disparavam. Sendo um programa a parte, o
launcher tem o proprio loop (tkinter) e so lanca o jogo no fim.

CRC: crc32 em hex MINUSCULO e SEM zeros a esquerda (arquivo vazio = "0"), igual
ao ltrim(hash('crc32b')) do updater.php - zlib.crc32(dados) casa exato.
"""

import os
import io
import sys
import json
import time
import zlib
import base64
import hashlib
import queue
import ctypes
import platform
import threading
import subprocess
import webbrowser
import http.client
import urllib.request
import urllib.parse
import tkinter as tk
from concurrent.futures import ThreadPoolExecutor

# Uma conexao HTTPS persistente POR THREAD (keep-alive). Sem isto, cada arquivo
# abria um TCP+TLS novo - com 20k arquivos ate Sao Paulo, o handshake dominava
# (medido: ~18x mais lento que reusar a conexao).
_conns = threading.local()

from PIL import Image, ImageDraw, ImageFont, ImageTk, ImageFilter

# ---------------------------------------------------------------------------
# Config. Producao por padrao; para testar local use as envs (test-local.bat).
# LINKS: placeholders - o dono troca depois.
# ---------------------------------------------------------------------------
UPDATER_URL = os.environ.get("PANGEIA_UPDATER_URL", "https://tibia2ot.com/updater.php")

# Downloads simultaneos do cliente (ver _download_one / baixa). O que pesa e a
# latencia por pedido (~15,7 mil arquivos, a maioria pequena), nao a banda: mais
# conexoes persistentes = mais arquivos por segundo. Medido em 02/10/2026 contra a
# producao, borda fria, 240 arquivos: 16 conexoes = 279 arquivos/s, 24 = 394.
# Eram 8, depois 16.
DOWNLOAD_WORKERS = 32
NEWS_URL = os.environ.get("PANGEIA_NEWS_URL", "https://tibia2ot.com/noticias.php")
# Relato de falha (anonimo, agregado por mensagem+versao no servidor - tabela
# client_errors). Sem isto uma falha de download so ficava no PC do jogador e
# ninguem sabia que "faltou arquivo". A Cloudflare ja isenta esta rota.
REPORT_URL = os.environ.get("PANGEIA_REPORT_URL", "https://tibia2ot.com/client-error-report")
# Info do proprio launcher (crc + url do exe) para o auto-update.
LAUNCHER_INFO_URL = os.environ.get("PANGEIA_LAUNCHER_INFO",
                                   "https://tibia2ot.com/templates/tibia2/launcher.json")
CLIENT_EXE = "Tibia 2 Client.exe"
# Copias com o nome do rebrand OTibion (29/09, REVERTIDO em 30/09), publicadas ate
# 01/10 e que ficaram paradas no disco de quem atualizou naquela janela (o updater
# nao apaga nada). faxina_heranca_otibion() as remove.
EXES_HERANCA_OTIBION = ("OTibion Client.exe", "OTibion.exe")
# Versao DESTE launcher. O auto-update compara com o "version" do launcher.json
# publicado: diferente => baixa a pasta nova e troca (ver self_update). No onedir
# esse constante e o unico jeito barato de o launcher saber que esta velho, ja que
# o codigo vive espalhado em _internal\ e nao mais num unico .exe com um CRC. O
# deploy LE este valor daqui (nunca digita no JSON) - ver deploy-cliente.ps1.
# BUMP a cada launcher que for publicado, senao o auto-update nao dispara.
LAUNCHER_VERSION = "2026-10-11.1"

# ASSINATURA DAS ATUALIZACOES (auditoria 2026-10-01, segunda rodada; decisao do
# dono 2026-10-03). O CRC do manifesto so prova que o arquivo chegou inteiro, nao
# de quem veio: quem escrevesse na arvore do VPS ou mexesse na borda entregava um
# exe ou um launcher.zip qualquer a todo jogador. O release (no PC do dono) assina
# com Ed25519 um manifesto com o SHA-256 de cada arquivo; aqui so mora a chave
# PUBLICA. Download que nao bate com a assinatura e recusado - inclusive o do
# proprio launcher. Ver scripts/deploy/assinatura.py.
CHAVE_PUBLICA_RELEASE = "92f26fc4f11cf279b33e5119a3817875341e42e956669c7ad75b75eaa80e30ba"
ASSINATURA_URL = os.environ.get("PANGEIA_ASSINATURA_URL",
                                "https://tibia2ot.com/templates/tibia2/assinatura.json")
HTTP_TIMEOUT = 30
# TODA requisicao do launcher tem de mandar este User-Agent.
#
# O Cloudflare entrou na frente do site e a protecao de bot dele RECUSA o UA
# padrao do urllib ("Python-urllib/3.x") com 403. Como o launcher nao mandava UA
# nenhum, TODAS as chamadas passaram a levar 403 de uma vez: manifesto, noticias,
# download de arquivo e o auto-update do proprio launcher. Em quem instalava do
# site o efeito era "otclient.exe nao encontrado" ao clicar em ABRIR JOGO - a
# pasta so tem o launcher, e o download que traria o cliente nunca acontecia.
#
# O UA e identificavel de proposito (e nao um Chrome falso): assim da para
# reconhecer o launcher no log da borda e criar regra para ele se um dia a
# protecao apertar. Medido: com este UA o updater.php responde 200.
# Leva a VERSAO e o NOME com que o launcher foi aberto: o log de acesso do Caddy
# (lido pelo painel do /admin) mostra quantos launchers de cada versao existem.
# Continua comecando com "Tibia2-Launcher/", que e o que as regras e os logs
# reconhecem.
_EXE_ABERTO = os.path.basename(sys.executable) if getattr(sys, "frozen", False) else "dev"
USER_AGENT = "Tibia2-Launcher/%s (%s; +https://tibia2ot.com)" % (
    LAUNCHER_VERSION, _EXE_ABERTO if _EXE_ABERTO.isascii() else "exe")

# ---------------------------------------------------------------------------
# CONTEUDO DINAMICO (a UI le do servidor pra quase nunca precisar rebuildar o
# launcher). Todos os endpoints sao TOLERANTES A FALHA: se nao existirem/derem
# erro, a secao correspondente fica vazia e o launcher segue.
#   - NEWS_URL        : {noticias:[{data,titulo,texto,imagem?,link?,tag?,destaque?}]}
#   - PARCEIROS_URL   : lives ao vivo da Twitch (via Helix no servidor):
#                       [{login,display,viewers,title,thumb?}] ordem embaralhada
#   - YOUTUBE_URL     : [{canal,titulo,dur,url,thumb?}]
#   - ONLINE_URL      : {total:int, mundos:[{nome,tipo,online:int,up:bool}]}
#   - METRICA_URL     : POST {rede,canal} -> conta cliques por parceiro
# Ainda NAO existem no servidor; ver docs/deploy-online.md (a construir).
# ---------------------------------------------------------------------------
DESTAQUES_URL = os.environ.get("PANGEIA_DESTAQUES_URL", "https://tibia2ot.com/destaques")
PARCEIROS_URL = os.environ.get("PANGEIA_PARCEIROS_URL", "https://tibia2ot.com/parceiros-live")
YOUTUBE_URL   = os.environ.get("PANGEIA_YOUTUBE_URL",   "https://tibia2ot.com/youtube")
ONLINE_URL    = os.environ.get("PANGEIA_ONLINE_URL",    "https://tibia2ot.com/status")
METRICA_URL   = os.environ.get("PANGEIA_METRICA_URL",   "https://tibia2ot.com/metrica-clique")


# ---------------------------------------------------------------------------
# DIARIO DE ERROS
#
# O launcher nao registrava nada. Quando algo falhava, a unica pista era uma
# frase de uma linha na janela ("Nao foi possivel checar atualizacoes"), e ela
# nao distingue rede caida, TLS recusado, disco cheio, 404 no servidor nem
# antivirus apagando o arquivo. Diagnosticar virava adivinhacao por telefone -
# quem esta na frente do problema so consegue repetir a frase.
#
# Agora cada falha escreve UMA linha em tibia2-launcher.log, ao lado do exe, com
# o tipo da excecao, a mensagem e o que estava sendo feito. Basta o jogador
# mandar o arquivo.
#
# Cuidados de proposito:
#   - nunca levanta excecao (um erro AO REGISTRAR erro nao pode derrubar nada);
#   - trunca em ~64 KB, para nao crescer sem fim na maquina de ninguem;
#   - so tipo e mensagem, nada de dado pessoal.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# CONFIANCA TLS PROPRIA
#
# O Python no Windows valida HTTPS contra o repositorio de certificados do
# SISTEMA. Numa maquina com o repositorio velho, a cadeia da Let's Encrypt cai
# na raiz cruzada DST Root CA X3, que EXPIROU em 30/09/2021 - e o erro que
# aparece e "certificate has expired", apontando para o nosso certificado, que
# esta perfeitamente valido. Aconteceu de verdade em 2026-09-03: certificado do
# servidor emitido no mesmo dia, valido por 90 dias, e um jogador sem conseguir
# nem checar atualizacao.
#
# Levar a nossa propria lista de raizes (certifi) tira o launcher da dependencia
# do que a Microsoft atualizou ou nao naquela maquina.
#
# NAO resolve o outro caso do mesmo erro: relogio do computador errado. Contra
# esse nao ha o que fazer do nosso lado - por isso a dica no log.
# ---------------------------------------------------------------------------
def _contexto_ssl():
    try:
        import ssl
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return None


_SSL_CTX = _contexto_ssl()


def log_erro(contexto, exc=None, extra=""):
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(sys.executable)
        else:
            base = os.path.dirname(os.path.abspath(__file__))
        caminho = os.path.join(base, "tibia2-launcher.log")
        try:
            if os.path.getsize(caminho) > 64 * 1024:
                os.remove(caminho)
        except OSError:
            pass
        detalhe = ""
        if exc is not None:
            detalhe = " | %s: %s" % (type(exc).__name__, exc)
            # "certificate has expired" quase nunca e o certificado do servidor:
            # e o relogio da maquina fora de hora, ou uma raiz vencida no
            # repositorio do Windows. Dizer isso na hora poupa a rodada de
            # diagnostico em que todo mundo olha para o servidor.
            if "CERTIFICATE_VERIFY_FAILED" in str(exc) or "certificate" in str(exc).lower():
                detalhe += " | DICA: confira a DATA E HORA do computador e as atualizacoes do Windows"
        with open(caminho, "a", encoding="utf-8", errors="replace") as f:
            f.write("%s  %s%s%s\n" % (
                time.strftime("%Y-%m-%d %H:%M:%S"), contexto, detalhe,
                (" | " + extra) if extra else ""))
    except Exception:
        pass
LINKS = {
    "twitch": "https://www.twitch.tv/himanentv",
    "discord": "https://discord.gg/fd3gS47sFg",
    "instagram": "https://www.instagram.com/tibia2ot/",
    "site": "https://tibia2ot.com/",
    "youtube": "https://www.youtube.com/@HimanenTV",
    "coins": "https://tibia2ot.com/loja",  # loja de T2 Coins
}

W, H = 1000, 668
# Paleta IGUAL ao site/jogo (templates/tibia2/basic.css): escuro + ouro (recompensa:
# titulos, frisos, CTA) + azul (interacao). Tuplas RGB para o PIL.
GROUND = (8, 13, 18); GROUND2 = (5, 10, 14)
SURFA = (16, 25, 35); SURFB = (11, 18, 25); SURF3 = (22, 33, 46)
LINE = (40, 53, 68); LINE2 = (52, 70, 92)
GOLD = (232, 184, 62); GOLDL = (255, 217, 106); GOLDD = (156, 122, 43)
BLUE = (22, 119, 200); BLUEB = (39, 167, 255); BLUE_DEEP = (18, 63, 110)
TEXT = (243, 238, 220); MUTED = (154, 166, 184); MUTED2 = (99, 112, 134)
LIVE = (229, 72, 77); EVT = (232, 147, 62); LOJA = (154, 124, 230); YTRED = (255, 0, 51)
GOOD = (57, 192, 122)
TEXT_HEX = "#f3eedc"
SS = 2  # supersampling: desenha tudo em 2x e reduz no fim (texto/bordas/pontos nitidos)

HERE = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(HERE, "assets")
SOCIAL = os.path.join(ASSETS, "social")


def asset(*p):
    return os.path.join(ASSETS, *p)


def client_root():
    override = os.environ.get("PANGEIA_CLIENT_ROOT")  # so para testes
    if override:
        return override
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return HERE


# ---- fonte martel (do tema do site) registrada p/ o tkinter usar ----------
FONT_FAMILY = "Martel"


def load_martel():
    global FONT_FAMILY
    try:
        path = asset("martel.ttf")
        if os.path.exists(path):
            FR_PRIVATE = 0x10
            ctypes.windll.gdi32.AddFontResourceExW(ctypes.c_wchar_p(path), FR_PRIVATE, 0)
    except Exception:
        FONT_FAMILY = "Georgia"


_FONT_CACHE = {}


def pil_font(sz, kind="ui"):
    """Fontes IGUAIS ao site (empacotadas em assets/fonts/): 'disp' = Cinzel Bold
    (wordmark, titulos, section headers, CTA), 'uib' = Inter SemiBold (rotulos,
    numeros, titulos de item), 'ui' = Inter Regular (corpo). Fallback: Segoe UI."""
    key = (sz, kind)
    f = _FONT_CACHE.get(key)
    if f is None:
        px = int(sz * SS)  # supersampling: fonte renderizada em 2x
        try:
            if kind == "disp":
                f = ImageFont.truetype(asset("fonts", "Cinzel.ttf"), px)
                _set_var(f, "Black" if sz >= 15 else "Bold")  # mais grosso nos titulos grandes
            else:
                f = ImageFont.truetype(asset("fonts", "Inter.ttf"), px)
                _set_var(f, "Semi Bold" if kind == "uib" else "Regular")
        except Exception:
            try:
                sysf = "C:/Windows/Fonts/segoeuib.ttf" if kind in ("disp", "uib") else "C:/Windows/Fonts/segoeui.ttf"
                f = ImageFont.truetype(sysf, px)
            except Exception:
                f = ImageFont.load_default()
        _FONT_CACHE[key] = f
    return f


def _set_var(font, name):
    """Aplica a variacao (peso) de uma fonte variavel, tolerante ao nome exato
    ('Semi Bold' vs 'SemiBold') e a ausencia de suporte."""
    try:
        avail = [n.decode() if isinstance(n, bytes) else n for n in font.get_variation_names()]
        for cand in (name, name.replace(" ", ""), name.replace("Semi Bold", "SemiBold")):
            if cand in avail:
                font.set_variation_by_name(cand)
                return
        if avail:
            font.set_variation_by_name(avail[0])
    except Exception:
        pass


def crc32_of(path):
    try:
        crc = 0
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                crc = zlib.crc32(chunk, crc)
        return format(crc & 0xFFFFFFFF, "x")
    except OSError:
        return None


def http_post_json(url, payload):
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                          "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def reportar_falha(mensagem):
    """Manda UMA mensagem agregada ao servidor (client_errors). Melhor esforco:
    nunca levanta, nunca atrasa o jogador mais que 8s. version = so os digitos
    de LAUNCHER_VERSION (a coluna e inteira); a versao completa vai no texto."""
    try:
        corpo = json.dumps({
            "message": ("[launcher %s] %s" % (LAUNCHER_VERSION, mensagem))[:1900],
            "level": "launcher",
            "version": int("".join(ch for ch in LAUNCHER_VERSION if ch.isdigit())[:9] or 0),
            "player": "",
            "os": platform.platform()[:60],
        }).encode("utf-8")
        req = urllib.request.Request(REPORT_URL, data=corpo, headers={
            "Content-Type": "application/json", "User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=8, context=_SSL_CTX) as r:
            r.read()
    except Exception:
        pass


def http_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_bytes(url):
    """Bytes crus (miniatura de live). Mesmo TLS/UA/timeout do resto."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as r:
        return r.read()


# ---------------------------------------------------------------------------
# Ed25519 (so VERIFICAR; RFC 8032, implementacao de referencia em Python puro -
# nenhuma biblioteca a mais no pacote). Conferida no scripts/deploy/assinatura.py
# contra o vetor da RFC e contra a biblioteca cryptography.
_EP = 2 ** 255 - 19
_EL = 2 ** 252 + 27742317777372353535851937790883648493
_ED = -121665 * pow(121666, _EP - 2, _EP) % _EP
_EI = pow(2, (_EP - 1) // 4, _EP)


def _ed_x(y, sinal):
    if y >= _EP:
        return None
    x2 = (y * y - 1) * pow(_ED * y * y + 1, _EP - 2, _EP)
    if x2 % _EP == 0:
        return None if sinal else 0
    x = pow(x2, (_EP + 3) // 8, _EP)
    if (x * x - x2) % _EP != 0:
        x = x * _EI % _EP
    if (x * x - x2) % _EP != 0:
        return None
    if (x & 1) != sinal:
        x = _EP - x
    return x


_EGY = 4 * pow(5, _EP - 2, _EP) % _EP
_EGX = _ed_x(_EGY, 0)
_EG = (_EGX, _EGY, 1, _EGX * _EGY % _EP)


def _ed_soma(a, b):
    A = (a[1] - a[0]) * (b[1] - b[0]) % _EP
    B = (a[1] + a[0]) * (b[1] + b[0]) % _EP
    C = 2 * a[3] * b[3] * _ED % _EP
    D = 2 * a[2] * b[2] % _EP
    E, F, G, H = B - A, D - C, D + C, B + A
    return (E * F % _EP, G * H % _EP, F * G % _EP, E * H % _EP)


def _ed_mult(k, ponto):
    q = (0, 1, 1, 0)
    while k > 0:
        if k & 1:
            q = _ed_soma(q, ponto)
        ponto = _ed_soma(ponto, ponto)
        k >>= 1
    return q


def _ed_ponto(b32):
    if len(b32) != 32:
        return None
    y = int.from_bytes(b32, "little")
    sinal = y >> 255
    y &= (1 << 255) - 1
    x = _ed_x(y, sinal)
    if x is None:
        return None
    return (x, y, 1, x * y % _EP)


def ed25519_verificar(publica, mensagem, assinatura):
    if len(publica) != 32 or len(assinatura) != 64:
        return False
    A = _ed_ponto(publica)
    R = _ed_ponto(assinatura[:32])
    if A is None or R is None:
        return False
    sv = int.from_bytes(assinatura[32:], "little")
    if sv >= _EL:
        return False
    h = int.from_bytes(hashlib.sha512(assinatura[:32] + publica + mensagem).digest(), "little") % _EL
    a = _ed_mult(sv, _EG)
    b = _ed_soma(R, _ed_mult(h, A))
    return (a[0] * b[2] - b[0] * a[2]) % _EP == 0 and (a[1] * b[2] - b[1] * a[2]) % _EP == 0


def carregar_assinatura():
    """Baixa o envelope assinado do release e confere com a chave publica.
    Devolve o manifesto (dict) ou None - None quer dizer "nao confio em nada que
    vier do servidor agora". Manifesto e assinatura vem no MESMO arquivo (nunca um
    par desencontrado); o `?t=` fura cache de borda."""
    try:
        bruto = http_get_bytes(ASSINATURA_URL + "?t=" + str(int(time.time())))
    except Exception as e:
        log_erro("baixar assinatura do release", e, ASSINATURA_URL)
        return None
    try:
        env = json.loads(bruto.decode("ascii"))
        dados = base64.b64decode(env["manifesto"], validate=True)
        sig = base64.b64decode(env["sig"], validate=True)
    except Exception as e:
        log_erro("assinatura do release ilegivel", e, ASSINATURA_URL)
        return None
    if not ed25519_verificar(bytes.fromhex(CHAVE_PUBLICA_RELEASE), dados, sig):
        log_erro("assinatura do release INVALIDA", None, ASSINATURA_URL)
        reportar_falha("assinatura do release invalida (%d bytes)" % len(bruto))
        return None
    try:
        man = json.loads(dados.decode("ascii"))
    except Exception as e:
        log_erro("manifesto assinado ilegivel", e, ASSINATURA_URL)
        return None
    if not isinstance(man, dict) or not isinstance(man.get("cliente"), dict):
        return None
    return man


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel_to_local(root, rel):
    return os.path.join(root, *rel.lstrip("/").split("/"))


# ---------------------------------------------------------------------------
# GEOMETRIA (layout novo, ver mockup). Coordenadas absolutas p/ W=1000, H=668.
# As regioes clicaveis sao construidas em runtime pelo render() (self._regions).
# ---------------------------------------------------------------------------
PAD = 16
BAR_H = 72                                 # barra de titulo
WB = 42                                    # botao de janela (quadrado)
BTN_CLOSE = (W - PAD - WB, 15, WB, WB)
BTN_MIN = (W - PAD - 2 * WB - 9, 15, WB, WB)
BTN_LANG = (W - PAD - 3 * WB - 18, 15, WB, WB)   # botao de idioma (mostra PT/EN/ES)
PILL_W = 200
PILL = (BTN_LANG[0] - 9 - PILL_W, 15, PILL_W, WB)   # x,y,w,h da pill de online
DROP_W = 272                               # dropdown de mundos (altura dinamica)

ROW1_Y = BAR_H + PAD                        # 88
ROW1_H = 264
NEWS_W = 330
HERO = (PAD, ROW1_Y, W - 2 * PAD - NEWS_W - PAD, ROW1_H)      # 16,88,622,264
NEWS = (HERO[0] + HERO[2] + PAD, ROW1_Y, NEWS_W, ROW1_H)      # 654,88,330,264
ROW2_Y = ROW1_Y + ROW1_H + PAD             # 368
ROW2_H = 210
COMM = (PAD, ROW2_Y, W - 2 * PAD, ROW2_H)  # 16,368,968,210
FOOT_Y = ROW2_Y + ROW2_H + PAD             # 594
FOOT_H = H - FOOT_Y                         # 74

CTA_W, CTA_H = 206, 60
CTA = (W - PAD - CTA_W, FOOT_Y + (FOOT_H - CTA_H) // 2, CTA_W, CTA_H)
SB = 44                                     # botao social (quadrado)
SOC_GAP = 9
SOC_Y = FOOT_Y + (FOOT_H - SB) // 2
SOCIALS = [("twitch", "Twitch"), ("discord", "Discord"),
           ("instagram", "Instagram"), ("youtube", "YouTube"), ("site", "Site")]

# FUNDO: a mesma arte da tela de login do cliente (assets/background.jpg, 16:9, sem o
# escudo - a barra de titulo ja tem o dela). Aparece duas vezes: desfocada e escurecida
# atras da janela inteira, com os paineis em vidro escuro por cima, e nitida no cartao
# de destaque. Sem o arquivo (ou se ele nao abrir) a janela volta ao degrade liso de
# antes - o launcher nunca deixa de abrir por causa de imagem.
FUNDO_ARQ = "background.jpg"
FUNDO_ESCURO = 0.36                         # quanto do fundo desfocado vira GROUND2
FUNDO_DESFOQUE = 6                          # raio, medido na janela em 1x
HERO_ARTE_TOPO = 0.160                      # o recorte do destaque comeca a esta fracao da altura da arte
VIDRO_BARRA, VIDRO_PAINEL, VIDRO_RODAPE = 165, 188, 190   # alfa (0-255) do vidro de cada faixa

CARD_ROT_MS = 10000                         # lives + youtube trocam JUNTOS (timer unico)
CAROUSEL_MS = 30000                         # carrossel de destaques


# ---- idiomas --------------------------------------------------------------
# O botao de idioma (canto sup. dir.) cicla PT -> EN -> ES e re-renderiza a UI.
# A traducao e por STRING-FONTE (a chave e o proprio texto em portugues): o pt e
# a identidade e so en/es tem tabela. Textos vindos do servidor (noticias,
# destaques) NAO sao traduzidos aqui - so a "moldura" (rotulos fixos + status
# parados + o botao ENTRAR + a contagem regressiva).
LANGS = ["pt", "en", "es"]
I18N = {
    "en": {
        "LAUNCHER OFICIAL": "OFFICIAL LAUNCHER",
        "NOTÍCIAS": "NEWS",
        "STREAMERS PARCEIROS": "PARTNER STREAMERS",
        "SERVIDORES": "SERVERS",
        "%d mundos · ": "%d worlds · ",
        "%d ao vivo": "%d live",
        "AO VIVO": "LIVE",
        "%s jogadores": "%s players",
        "Total": "Total",
        "carregando...": "loading...",
        "nenhuma live agora": "no live streams",
        "nenhum video": "no videos",
        "Bem-vindo ao Tibia 2": "Welcome to Tibia 2",
        "Clique em Entrar no jogo quando o cliente terminar de atualizar.":
            "Click Enter Game once the client finishes updating.",
        "ENTRAR NO JOGO": "ENTER GAME",
        # status (traduzidos na hora de desenhar, por string-fonte)
        "Verificando arquivos...": "Checking files...",
        "Verificando launcher...": "Checking launcher...",
        "Atualizando launcher...": "Updating launcher...",
        "Consultando servidor de atualizacao...": "Contacting update server...",
        "Nao foi possivel checar atualizacoes. Veja tibia2-launcher.log":
            "Could not check for updates. See tibia2-launcher.log",
        "Atualizacao indisponivel - jogando com o que ha.":
            "Update unavailable - playing with what's here.",
        "Cliente pronto. Boa caçada!": "Client ready. Happy hunting!",
        "Refazendo arquivos que falharam...": "Retrying files that failed...",
        "Alguns arquivos nao baixaram. Clique em TENTAR DE NOVO.":
            "Some files did not download. Click TRY AGAIN.",
        "TENTAR DE NOVO": "TRY AGAIN",
    },
    "es": {
        "LAUNCHER OFICIAL": "LANZADOR OFICIAL",
        "NOTÍCIAS": "NOTICIAS",
        "STREAMERS PARCEIROS": "STREAMERS SOCIOS",
        "SERVIDORES": "SERVIDORES",
        "%d mundos · ": "%d mundos · ",
        "%d ao vivo": "%d en vivo",
        "AO VIVO": "EN VIVO",
        "%s jogadores": "%s jugadores",
        "Total": "Total",
        "carregando...": "cargando...",
        "nenhuma live agora": "sin transmisiones",
        "nenhum video": "sin videos",
        "Bem-vindo ao Tibia 2": "Bienvenido a Tibia 2",
        "Clique em Entrar no jogo quando o cliente terminar de atualizar.":
            "Haz clic en Entrar al juego cuando el cliente termine de actualizar.",
        "ENTRAR NO JOGO": "ENTRAR AL JUEGO",
        "Verificando arquivos...": "Verificando archivos...",
        "Verificando launcher...": "Verificando lanzador...",
        "Atualizando launcher...": "Actualizando lanzador...",
        "Consultando servidor de atualizacao...": "Consultando servidor de actualizacion...",
        "Nao foi possivel checar atualizacoes. Veja tibia2-launcher.log":
            "No se pudo buscar actualizaciones. Ver tibia2-launcher.log",
        "Atualizacao indisponivel - jogando com o que ha.":
            "Actualizacion no disponible - jugando con lo que hay.",
        "Cliente pronto. Boa caçada!": "Cliente listo. ¡Buena cacería!",
        "Refazendo arquivos que falharam...": "Reintentando archivos que fallaron...",
        "Alguns arquivos nao baixaram. Clique em TENTAR DE NOVO.":
            "Algunos archivos no se descargaron. Haz clic en REINTENTAR.",
        "TENTAR DE NOVO": "REINTENTAR",
    },
}


def tr(lang, s):
    """Traduz s para o idioma (pt = identidade). Fallback: devolve s."""
    if lang == "pt":
        return s
    return I18N.get(lang, {}).get(s, s)


# ---- supersampling: desenha em 2x com coordenadas LOGICAS ------------------
# O codigo de desenho segue usando coordenadas 1x; estes proxies multiplicam por
# SS na hora de tocar o pixel, e no fim o render reduz de 2x -> 1x (LANCZOS).
def _sc(v):
    if isinstance(v, (int, float)):
        return v * SS
    if isinstance(v, (list, tuple)) and v:
        if isinstance(v[0], (list, tuple)):
            return [(p[0] * SS, p[1] * SS) for p in v]
        return [c * SS for c in v]
    return v


class SImg:
    """Envelope de Image que escala a POSICAO de paste/alpha_composite por SS."""
    __slots__ = ("img",)

    def __init__(self, img):
        self.img = img

    def alpha_composite(self, other, dest=(0, 0)):
        o = other.img if isinstance(other, SImg) else other
        self.img.alpha_composite(o, (int(dest[0] * SS), int(dest[1] * SS)))

    def paste(self, other, box=None, mask=None):
        o = other.img if isinstance(other, SImg) else other
        m = mask.img if isinstance(mask, SImg) else mask
        if box is None:
            self.img.paste(o, mask=m)
        else:
            self.img.paste(o, (int(box[0] * SS), int(box[1] * SS)), m)


class ScaledDraw:
    """Proxy de ImageDraw que escala coordenadas/width/radius por SS. As fontes ja
    vem em 2x (pil_font); textbbox/textlength devolvem a medida em 1x (/SS) para o
    layout continuar raciocinando em coordenadas logicas."""
    __slots__ = ("d",)

    def __init__(self, d):
        self.d = d

    def text(self, xy, *a, **k):
        self.d.text((xy[0] * SS, xy[1] * SS), *a, **k)

    def line(self, xy, **k):
        if k.get("width"):
            k["width"] = int(k["width"] * SS)
        self.d.line(_sc(xy), **k)

    def rectangle(self, xy, **k):
        if k.get("width"):
            k["width"] = int(k["width"] * SS)
        self.d.rectangle(_sc(xy), **k)

    def rounded_rectangle(self, xy, radius=0, **k):
        if k.get("width"):
            k["width"] = int(k["width"] * SS)
        self.d.rounded_rectangle(_sc(xy), radius=radius * SS, **k)

    def ellipse(self, xy, **k):
        if k.get("width"):
            k["width"] = int(k["width"] * SS)
        self.d.ellipse(_sc(xy), **k)

    def polygon(self, xy, **k):
        self.d.polygon(_sc(xy), **k)

    def textbbox(self, xy, *a, **k):
        # As fontes vem em 2x; devolve a caixa em coordenadas LOGICAS (/SS) para
        # que todo calculo de layout via _tw() fique em 1x sem tocar nas chamadas.
        b = self.d.textbbox((xy[0] * SS, xy[1] * SS), *a, **k)
        return (b[0] / SS, b[1] / SS, b[2] / SS, b[3] / SS)

    def textlength(self, *a, **k):
        return self.d.textlength(*a, **k) / SS


def sdraw(x):
    return ScaledDraw(ImageDraw.Draw(x.img if isinstance(x, SImg) else x))


# ---- helpers de desenho ---------------------------------------------------
def vgrad(w, h, top, bot):
    """Gradiente vertical de 2 cores (RGB)."""
    w = max(1, int(w)); h = max(1, int(h))
    img = Image.new("RGB", (w, h)); d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bot[i] - top[i]) * t) for i in range(3)))
    return img


def rgrad_card(w, h, top, bot, radius=9):
    """Card RGBA (gradiente + cantos) em 2x, embrulhado em SImg. w/h/radius LOGICOS."""
    w = max(1, int(w * SS)); h = max(1, int(h * SS)); radius = int(radius * SS)
    base = vgrad(w, h, top, bot).convert("RGBA")
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, w - 1, h - 1], radius=radius, fill=255)
    base.putalpha(m)
    return SImg(base)


def _tw(d, s, f):
    b = d.textbbox((0, 0), s, font=f); return b[2] - b[0]


def ell(d, s, f, maxw):
    if _tw(d, s, f) <= maxw:
        return s
    while s and _tw(d, s + "…", f) > maxw:
        s = s[:-1]
    return (s + "…") if s else s


def wrap2(d, s, f, maxw):
    """Quebra em ate 2 linhas; corta a 2a com reticencias."""
    lines, cur = [], ""
    for wd in s.split():
        t = (cur + " " + wd).strip()
        if _tw(d, t, f) <= maxw or not cur:
            cur = t
        else:
            lines.append(cur); cur = wd
            if len(lines) == 2:
                break
    if len(lines) < 2 and cur:
        lines.append(cur)
    if len(lines) == 2:
        lines[1] = ell(d, lines[1], f, maxw)
    return lines[:2]


def tw_ls(d, text, font, ls):
    """Largura de um texto com letter-spacing (coordenadas logicas; _tw ja vem /SS)."""
    if not text:
        return 0
    return int(sum(_tw(d, ch, font) + ls for ch in text) - ls)


def text_ls(d, xy, text, font, fill, ls):
    """Desenha texto com letter-spacing (o PIL nao tem nativo). E o que faz o
    Cinzel em maiuscula parecer o do site em vez de espremido. Coordenadas
    LOGICAS: _tw ja vem em 1x e o d (ScaledDraw) escala a posicao."""
    x, y = xy
    for ch in text:
        d.text((x, y), ch, font=font, fill=fill)
        x += _tw(d, ch, font) + ls


def scrim(w, h, radius=0):
    """Degrade transparente->escuro (legenda sobre imagem) em 2x -> SImg. w/h LOGICOS."""
    w = int(w * SS); h = int(h * SS)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0)); d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        a = int(245 * (t ** 1.4))
        d.line([(0, y), (w, y)], fill=(4, 7, 13, a))
    return SImg(img)


_CD_FMT = {  # (dias+horas, horas+min) por idioma
    "pt": ("Faltam %dd %dh", "Faltam %dh %dmin"),
    "en": ("%dd %dh left", "%dh %dmin left"),
    "es": ("Faltan %dd %dh", "Faltan %dh %dmin"),
}


def countdown_txt(iso, lang="pt"):
    """'Faltam Xd Yh' ate a data ISO (ex.: 2026-12-17T20:00:00-03:00), ou None se
    invalida/ja passou. Granularidade dias+horas: o carrossel re-renderiza a cada
    30s, entao nao vale um relogio de segundos. Traduzido pelo idioma da moldura."""
    if not iso:
        return None
    try:
        from datetime import datetime, timezone
        target = datetime.fromisoformat(iso)
        if target.tzinfo is None:
            target = target.replace(tzinfo=timezone.utc)
        secs = int((target - datetime.now(target.tzinfo)).total_seconds())
        if secs <= 0:
            return None
        d, h, m = secs // 86400, (secs % 86400) // 3600, (secs % 3600) // 60
        fd, fh = _CD_FMT.get(lang, _CD_FMT["pt"])
        return (fd % (d, h)) if d > 0 else (fh % (h, m))
    except Exception:
        return None


def _octa(x0, y0, x1, y1, ch):
    return [(x0 + ch, y0), (x1 - ch, y0), (x1, y0 + ch), (x1, y1 - ch),
            (x1 - ch, y1), (x0 + ch, y1), (x0, y1 - ch), (x0, y0 + ch)]


def build_cta_frames(w, h, label="ENTRAR NO JOGO", n=24, ch=10):
    """Quadros do botao ENTRAR NO JOGO: octogono chanfrado com miolo AZUL e a
    borda CONICA (ouro<->azul) GIRANDO, igual ao card do contador do site
    (.t2-cd). Pre-renderiza n quadros; o loop da UI so cicla entre eles (barato).
    Retorna (frames, pad) - componha cada frame em (CTA_x - pad, CTA_y - pad)."""
    pad = 8
    W2, H2 = w + 2 * pad, h + 2 * pad
    # fonte LOCAL 1x (pil_font agora vem em 2x p/ o supersampling do resto; o CTA
    # e uma camada 1x separada, entao precisa da fonte no tamanho real).
    try:
        lblf = ImageFont.truetype(asset("fonts", "Cinzel.ttf"), 16)
        _set_var(lblf, "Black")
    except Exception:
        try:
            lblf = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 16)
        except Exception:
            lblf = ImageFont.load_default()

    # disco conico (uma vez): fatias ouro->azul->ouro em volta dos 360
    D = int((W2 ** 2 + H2 ** 2) ** 0.5) + 4
    conic = Image.new("RGBA", (D, D), (0, 0, 0, 0)); cd = ImageDraw.Draw(conic)
    stops = [(0, GOLD), (70, GOLDL), (150, GOLDD), (210, BLUEB),
             (270, GOLDD), (330, GOLDL), (360, GOLD)]

    def col_at(a):
        a %= 360
        for i in range(len(stops) - 1):
            a0, c0 = stops[i]; a1, c1 = stops[i + 1]
            if a0 <= a <= a1:
                t = (a - a0) / max(1, (a1 - a0))
                return tuple(int(c0[k] + (c1[k] - c0[k]) * t) for k in range(3))
        return stops[-1][1]

    for a in range(0, 360, 3):
        cd.pieslice([0, 0, D - 1, D - 1], a, a + 4, fill=col_at(a + 1.5))

    # mascara do anel (borda do octogono): externo menos interno
    ring = Image.new("L", (W2, H2), 0); rd = ImageDraw.Draw(ring)
    rd.polygon(_octa(pad, pad, pad + w, pad + h, ch), fill=255)
    rd.polygon(_octa(pad + 3, pad + 3, pad + w - 3, pad + h - 3, ch), fill=0)

    core_grad = vgrad(w - 6, h - 6, (43, 111, 174), (11, 43, 77))
    core_mask = Image.new("L", (W2, H2), 0)
    ImageDraw.Draw(core_mask).polygon(_octa(pad + 3, pad + 3, pad + w - 3, pad + h - 3, ch), fill=255)

    shadow = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).polygon(_octa(pad, pad + 3, pad + w, pad + h + 3, ch), fill=(0, 0, 0, 150))
    shadow = shadow.filter(ImageFilter.GaussianBlur(5))

    cx = D / 2.0
    frames = []
    for fi in range(n):
        out = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
        out.alpha_composite(shadow)
        base = Image.new("RGBA", (W2, H2), (0, 0, 0, 0))
        ImageDraw.Draw(base).polygon(_octa(pad, pad, pad + w, pad + h, ch), fill=(1, 4, 7, 255))
        out.alpha_composite(base)
        rot = conic.rotate(fi * 360.0 / n, resample=Image.BILINEAR)
        left, top = int(cx - W2 / 2), int(cx - H2 / 2)
        out.paste(rot.crop((left, top, left + W2, top + H2)), (0, 0), ring)
        cimg = Image.new("RGBA", (W2, H2), (0, 0, 0, 0)); cimg.paste(core_grad, (pad + 3, pad + 3))
        out.paste(cimg, (0, 0), core_mask)
        od = ImageDraw.Draw(out)
        od.line([(pad + 10, pad + 4), (pad + w - 10, pad + 4)], fill=(210, 228, 245, 110))
        ls = 2  # letter-spacing local (1x; nao usar tw_ls/text_ls que assumem 2x)
        lbl = label
        tw = int(sum(_tw(od, c, lblf) + ls for c in lbl) - ls)
        tx = W2 // 2 - tw // 2
        for c in lbl:
            od.text((tx, H2 // 2 - 10), c, font=lblf, fill=(255, 246, 220, 255))
            tx += _tw(od, c, lblf) + ls
        frames.append(out)
    return frames, pad


class Launcher:
    def __init__(self, tk_root):
        self.tk = tk_root
        self.root = client_root()
        self.q = queue.Queue()
        self.ready = False
        self.launched = False
        self._drag = None
        self._pressed = None
        self._regions = []            # [(x0,y0,x1,y1,acao)] construidas pelo render()
        self._thumbs = {}             # url -> Image RGB da miniatura (ou False se falhou)
        self._thumb_inflight = set()  # urls baixando agora (nao duplica o pedido)
        self._dirty = True
        self.lang = self._load_lang()  # idioma da moldura (botao de idioma); persiste
        self.st = {
            "status": "Verificando arquivos...",
            "progress": 0.0,
            "news": None,             # None=carregando, []=vazio, list=itens
            "destaques": None,        # carrossel (curado); fallback: news destaque
            "streams": None,
            "youtube": None,
            "online": None,
            "carousel": 0,
            "tw_page": 0,
            "yt_page": 0,
            "dropdown": False,
            "modal": None,            # dict {tag,data,titulo,texto}
        }
        self._build_ui()
        self.tk.after(80, self._drain)
        self.tk.after(CAROUSEL_MS, self._tick_carousel)
        self.tk.after(CARD_ROT_MS, self._tick_cards)
        threading.Thread(target=self._work, daemon=True).start()
        threading.Thread(target=self._fetch_content, daemon=True).start()

    # ---- UI base --------------------------------------------------------
    def _build_ui(self):
        self.tk.overrideredirect(True)
        sw, sh = self.tk.winfo_screenwidth(), self.tk.winfo_screenheight()
        self.tk.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 3}")
        try:
            self.tk.iconphoto(True, ImageTk.PhotoImage(Image.open(asset("shield.png"))))
        except Exception:
            pass
        self.canvas = tk.Canvas(self.tk, width=W, height=H, highlightthickness=0, bd=0, bg="#080d12")
        self.canvas.pack(fill="both", expand=True)
        self.base_item = self.canvas.create_image(0, 0, anchor="nw")
        self._imgtk = None
        try:
            self._shield = Image.open(asset("shield.png")).convert("RGBA")
        except Exception:
            self._shield = None
        self._social_icons = self._load_social_icons()
        self._carregar_fundo()
        # botao ENTRAR NO JOGO: borda conica girando, camada POR CIMA do base
        try:
            self._cta_frames, self._cta_pad = build_cta_frames(
                CTA_W, CTA_H, self.t("ENTRAR NO JOGO"))
        except Exception:
            self._cta_frames, self._cta_pad = [], 0
        self._ctatk = [ImageTk.PhotoImage(f) for f in self._cta_frames]
        self._cta_i = 0
        self.cta_item = self.canvas.create_image(
            CTA[0] - self._cta_pad, CTA[1] - self._cta_pad, anchor="nw",
            image=(self._ctatk[0] if self._ctatk else None))
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_move)
        self.tk.bind("<Escape>", lambda e: self._close_overlays())
        # Rolagem do texto da noticia aberta (modal): roda do mouse e teclado.
        self.canvas.bind("<MouseWheel>", lambda e: self._rolar_modal(-60 if e.delta > 0 else 60))
        self.tk.bind("<Up>", lambda e: self._rolar_modal(-40))
        self.tk.bind("<Down>", lambda e: self._rolar_modal(40))
        self.tk.bind("<Prior>", lambda e: self._rolar_modal(-200))
        self.tk.bind("<Next>", lambda e: self._rolar_modal(200))
        self.render()
        if self._ctatk:
            self.tk.after(70, self._tick_cta)
        self.tk.after(30, self._taskbar_button)

    def _load_social_icons(self):
        icons = {}
        for key, _ in SOCIALS:
            try:
                im = Image.open(os.path.join(SOCIAL, key + ".png")).convert("RGBA")
                im.thumbnail((24 * SS, 24 * SS), Image.LANCZOS); icons[key] = im
            except Exception:
                icons[key] = None
        return icons

    def _carregar_fundo(self):
        """Monta UMA vez, em 2x, as duas camadas da arte: self._fundo (janela inteira,
        desfocada e escurecida) e self._hero_arte (recorte nitido do cartao de destaque,
        ja com os cantos arredondados). Qualquer falha deixa as duas em None e o
        _compose cai no degrade liso."""
        self._fundo = self._hero_arte = None
        try:
            arte = Image.open(asset(FUNDO_ARQ)).convert("RGB")
            aw, ah = arte.size
            # janela: cobre (corta a sobra dos lados), desfoca pequeno e amplia - o
            # desfoque em 1/4 do tamanho custa uma fracao e o resultado e o mesmo
            fw, fh = W * SS, H * SS
            esc = max(fw / aw, fh / ah)
            cw, ch = min(aw, round(fw / esc)), min(ah, round(fh / esc))
            cx, cy = (aw - cw) // 2, (ah - ch) // 2
            peq = arte.crop((cx, cy, cx + cw, cy + ch)).resize((W // 2, H // 2), Image.LANCZOS)
            peq = peq.filter(ImageFilter.GaussianBlur(FUNDO_DESFOQUE / 2.0))
            fundo = peq.resize((fw, fh), Image.BICUBIC)
            fundo = Image.blend(fundo, Image.new("RGB", (fw, fh), GROUND2), FUNDO_ESCURO)
            # destaque: a largura toda da arte, na proporcao do cartao
            hw, hh = HERO[2] * SS, HERO[3] * SS
            rh = min(ah, round(aw * hh / hw))
            top = max(0, min(ah - rh, round(ah * HERO_ARTE_TOPO)))
            hero = arte.crop((0, top, aw, top + rh)).resize((hw, hh), Image.LANCZOS).convert("RGBA")
            m = Image.new("L", (hw, hh), 0)
            ImageDraw.Draw(m).rounded_rectangle([0, 0, hw - 1, hh - 1], radius=12 * SS, fill=255)
            hero.putalpha(m)
            self._fundo, self._hero_arte = fundo.convert("RGBA"), hero
        except Exception:
            self._fundo = self._hero_arte = None

    def _vidro(self, img, x, y, w, h, cor, alfa, r=0):
        """Retangulo translucido (vidro escuro) por cima do fundo. O ImageDraw num RGBA
        SUBSTITUI o pixel em vez de misturar, por isso vai por alpha_composite."""
        pw, ph = max(1, int(w * SS)), max(1, int(h * SS))
        placa = Image.new("RGBA", (pw, ph), tuple(cor) + (alfa,))
        if r:
            m = Image.new("L", (pw, ph), 0)
            ImageDraw.Draw(m).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=int(r * SS), fill=alfa)
            placa.putalpha(m)
        img.alpha_composite(placa, (x, y))

    # ---- idioma ---------------------------------------------------------
    def t(self, s):
        """Traduz um rotulo da moldura para o idioma atual (pt = identidade)."""
        return tr(self.lang, s)

    def _lang_file(self):
        return os.path.join(self.root, "launcher_lang.txt")

    def _load_lang(self):
        try:
            v = open(self._lang_file(), encoding="utf-8").read().strip()
            if v in LANGS:
                return v
        except Exception:
            pass
        return "pt"

    def _cycle_lang(self):
        self.lang = LANGS[(LANGS.index(self.lang) + 1) % len(LANGS)]
        try:
            with open(self._lang_file(), "w", encoding="utf-8") as f:
                f.write(self.lang)
        except Exception:
            pass
        # o rotulo do botao ENTRAR e "assado" nos quadros -> reconstroi
        self._rebuild_cta()
        self.render()

    def _rebuild_cta(self):
        try:
            self._cta_frames, self._cta_pad = build_cta_frames(
                CTA_W, CTA_H, self.t("ENTRAR NO JOGO"))
            self._ctatk = [ImageTk.PhotoImage(f) for f in self._cta_frames]
            self._cta_i = 0
            if hasattr(self, "cta_item"):
                self.canvas.itemconfigure(self.cta_item, image=self._ctatk[0])
        except Exception:
            pass

    def _taskbar_button(self):
        try:
            GWL_EXSTYLE, WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = -20, 0x40000, 0x80
            u = ctypes.windll.user32
            self.tk.update_idletasks()
            hwnd = u.GetParent(self.tk.winfo_id()) or self.tk.winfo_id()
            stl = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, (stl & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
            self.tk.withdraw()
            self.tk.after(10, self.tk.deiconify)
        except Exception:
            pass

    @staticmethod
    def _fmtnum(n):
        try:
            return "{:,}".format(int(n)).replace(",", ".")
        except Exception:
            return str(n)

    def _reg(self, x, y, w, h, act):
        self._regions.append((int(x), int(y), int(x + w), int(y + h), act))

    # ---- COMPOSE (desenha tudo em 2x com coords logicas; retorna PIL 1x) --
    def _compose(self):
        """Renderiza a janela inteira em 2x (SS) e reduz pra WxH (LANCZOS).
        Testavel sem tk. Preenche self._regions (coords LOGICAS p/ hit-test)."""
        self._regions = []
        fundo = getattr(self, "_fundo", None)
        base = fundo.copy() if fundo is not None else vgrad(W * SS, H * SS, (13, 24, 38), GROUND2).convert("RGBA")
        img = SImg(base)
        d = sdraw(img)
        d.line([(0, 0), (W, 0)], fill=GOLD, width=2)
        self._draw_titlebar(img, d)
        self._draw_hero(img, d)
        self._draw_news(img, d)
        self._draw_community(img, d)
        self._draw_footer(img, d)
        if self.st["dropdown"]:
            self._draw_dropdown(img, d)
        if self.st["modal"]:
            self._draw_modal(img, d)
        return base.resize((W, H), Image.LANCZOS)

    def render(self):
        self._dirty = False
        self._imgtk = ImageTk.PhotoImage(self._compose())
        self.canvas.itemconfigure(self.base_item, image=self._imgtk)
        # a camada do CTA some quando ha modal (senao gira por cima dele)
        if hasattr(self, "cta_item"):
            self.canvas.itemconfigure(self.cta_item, state=("hidden" if self.st["modal"] else "normal"))
            self.canvas.tag_raise(self.cta_item)

    def _panel(self, img, d, x, y, w, h, fill=SURFA, r=12):
        if getattr(self, "_fundo", None) is not None:
            self._vidro(img, x, y, w, h, fill, VIDRO_PAINEL, r)
            d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=r, outline=GOLDD)
            return
        d.rounded_rectangle([x, y, x + w - 1, y + h - 1], radius=r, fill=fill, outline=GOLDD)

    def _wbtn(self, img, d, rect, kind):
        x, y, w, h = rect
        d.rounded_rectangle([x, y, x + w, y + h], radius=9, fill=(11, 20, 33), outline=LINE2)
        cx, cy = x + w // 2, y + h // 2
        if kind == "close":
            d.line([(cx - 6, cy - 6), (cx + 6, cy + 6)], fill=MUTED, width=2)
            d.line([(cx + 6, cy - 6), (cx - 6, cy + 6)], fill=MUTED, width=2)
        elif kind == "min":
            d.line([(cx - 7, cy), (cx + 7, cy)], fill=MUTED, width=2)
        else:  # lang (idioma): globinho + o codigo do idioma atual (PT/EN/ES)
            gy = y + 14; r = 7
            d.ellipse([cx - r, gy - r, cx + r, gy + r], outline=MUTED, width=1)
            d.ellipse([cx - r // 2 - 1, gy - r, cx + r // 2 + 1, gy + r], outline=MUTED, width=1)
            d.line([(cx - r, gy), (cx + r, gy)], fill=MUTED, width=1)
            f = pil_font(11, "uib"); code = self.lang.upper()
            tw = _tw(d, code, f)
            d.text((cx - tw // 2, y + h - 16), code, font=f, fill=GOLDL)
        self._reg(x, y, w, h, kind)

    def _draw_titlebar(self, img, d):
        if getattr(self, "_fundo", None) is not None:
            self._vidro(img, 0, 1, W, BAR_H - 1, (9, 20, 31), VIDRO_BARRA)
        else:
            d.rectangle([0, 1, W, BAR_H], fill=(14, 33, 47))
        d.line([(0, BAR_H), (W, BAR_H)], fill=LINE)
        if self._shield:
            s = self._shield.copy(); s.thumbnail((46 * SS, 46 * SS)); img.alpha_composite(s, (PAD, 13))
        text_ls(d, (PAD + 58, 15), "TIBIA 2", pil_font(26, "disp"), GOLDL, 3)
        text_ls(d, (PAD + 60, 49), self.t("LAUNCHER OFICIAL") + "   ·   V" + LAUNCHER_VERSION,
                pil_font(10, "ui"), MUTED2, 2)
        # botoes de janela
        self._wbtn(img, d, BTN_CLOSE, "close")
        self._wbtn(img, d, BTN_MIN, "min")
        self._wbtn(img, d, BTN_LANG, "lang")
        # pill de online
        px, py, pw, ph = PILL
        d.rounded_rectangle([px, py, px + pw, py + ph], radius=9, fill=(11, 21, 36), outline=LINE2)
        online = self.st["online"] or {}
        mundos = online.get("mundos") or []
        total = online.get("total")
        d.ellipse([px + 13, py + ph // 2 - 4, px + 21, py + ph // 2 + 4], fill=GOOD)
        fx = px + 30
        left = (self.t("%d mundos · ") % len(mundos)) if mundos else ""
        f = pil_font(12, "ui")
        if left:
            d.text((fx, py + ph // 2 - 8), left, font=f, fill=TEXT); fx += _tw(d, left, f)
        num = ("%s online" % self._fmtnum(total)) if total is not None else "online"
        d.text((fx, py + ph // 2 - 8), num, font=pil_font(12, "uib"), fill=GOLDL)
        # caret
        cxr = px + pw - 16
        if self.st["dropdown"]:
            d.polygon([(cxr, py + ph // 2 + 2), (cxr + 8, py + ph // 2 + 2), (cxr + 4, py + ph // 2 - 3)], fill=GOLDL)
        else:
            d.polygon([(cxr, py + ph // 2 - 2), (cxr + 8, py + ph // 2 - 2), (cxr + 4, py + ph // 2 + 3)], fill=MUTED2)
        self._reg(px, py, pw, ph, "dropdown")

    # ---- carrossel ------------------------------------------------------
    def _carousel_slides(self):
        dest = self.st.get("destaques") or []          # curado (endpoint /destaques)
        if not dest:
            news = self.st["news"] or []
            dest = [n for n in news if n.get("destaque")] or news[:3]
        if not dest:
            dest = [{"tag": "", "titulo": self.t("Bem-vindo ao Tibia 2"),
                     "texto": self.t("Clique em Entrar no jogo quando o cliente terminar de atualizar.")}]
        return dest

    def _draw_hero(self, img, d):
        hx, hy, hw, hh = HERO
        slides = self._carousel_slides()
        ci = self.st["carousel"] % len(slides)
        sl = slides[ci]
        tag = (sl.get("tag") or "").lower()
        pal = {"evento": ((16, 34, 55), (11, 24, 50)), "loja": ((28, 20, 56), (13, 16, 48)),
               "update": ((16, 30, 47), (11, 22, 38))}
        top, bot = pal.get(tag, ((16, 33, 54), (11, 23, 48)))
        arte = getattr(self, "_hero_arte", None)
        card = SImg(arte.copy()) if arte is not None else rgrad_card(hw, hh, top, bot, 12)
        cd = sdraw(card)
        # scrim esquerda + base (legibilidade do texto) - veil em 2x. Com a arte por
        # baixo o veu da esquerda e mais leve e mais curto: escurece so onde o texto
        # mora e deixa o resto da cena a mostra.
        forca, alcance = (150, 0.60) if arte is not None else (215, 0.72)
        vw, vh = hw * SS, hh * SS
        veil = Image.new("RGBA", (vw, vh), (0, 0, 0, 0)); vd = ImageDraw.Draw(veil)
        for xx in range(vw):  # passo 1 = gradiente continuo (passo 2 virava listras)
            a = int(forca * max(0.0, 1 - xx / (vw * alcance)))
            vd.line([(xx, 0), (xx, vh)], fill=(6, 10, 18, a))
        card.alpha_composite(veil, (0, 0))
        card.alpha_composite(scrim(hw, int(hh * 0.55)), (0, hh - int(hh * 0.55)))
        if arte is not None:      # veu e scrim pintam os cantos; devolve o recorte arredondado
            card.img.putalpha(arte.getchannel("A"))
        # badge + titulo + texto
        badgecol = {"evento": EVT, "loja": LOJA, "update": BLUE}.get(tag, BLUE)
        btxt = (sl.get("tag") or "Destaque").upper()
        self._badge_solid(cd, 22, hh - 118, btxt, badgecol)
        cd.text((22, hh - 92), ell(cd, sl.get("titulo", ""), pil_font(22, "disp"), hw - 60),
                font=pil_font(22, "disp"), fill=(255, 255, 255))
        cdn = countdown_txt(sl.get("lancamento", ""), self.lang)
        if cdn:
            cd.text((22, hh - 58), cdn, font=pil_font(20, "disp"), fill=GOLDL)
            cd.text((22, hh - 30), ell(cd, sl.get("texto", ""), pil_font(12, "ui"), int(hw * 0.62)),
                    font=pil_font(12, "ui"), fill=(201, 211, 226))
        else:
            for i, ln in enumerate(wrap2(cd, sl.get("texto", ""), pil_font(13, "ui"), int(hw * 0.62))):
                cd.text((22, hh - 52 + i * 18), ln, font=pil_font(13, "ui"), fill=(201, 211, 226))
        img.alpha_composite(card, (hx, hy))
        d.rounded_rectangle([hx, hy, hx + hw - 1, hy + hh - 1], radius=12, outline=GOLDD)
        self._reg(hx, hy, hw, hh, "carousel:%d" % ci)
        if len(slides) > 1:
            for ax, act, ch in [(hx + 14, "cprev", "‹"), (hx + hw - 50, "cnext", "›")]:
                ay = hy + hh // 2 - 18
                d.ellipse([ax, ay, ax + 36, ay + 36], fill=(6, 12, 21), outline=GOLDD)
                fa = pil_font(20, "uib"); tw = _tw(d, ch, fa)
                d.text((ax + 18 - tw // 2, ay + 6), ch, font=fa, fill=GOLDL)
                self._reg(ax, ay, 36, 36, act)
            dx = hx + hw - 16 - (len(slides) * 12); dyy = hy + hh - 18
            for p in range(len(slides)):
                if p == ci:
                    d.rounded_rectangle([dx, dyy, dx + 18, dyy + 7], radius=3, fill=GOLD); dx += 24
                else:
                    d.ellipse([dx, dyy, dx + 7, dyy + 7], fill=(90, 100, 118)); dx += 12

    def _badge_solid(self, cd, x, y, text, col, txtcol=(20, 16, 8)):
        f = pil_font(11, "disp"); ls = 1; tw = tw_ls(cd, text, f, ls)
        cd.rounded_rectangle([x, y, x + tw + 20, y + 20], radius=5, fill=col)
        text_ls(cd, (x + 10, y + 4), text, f, txtcol, ls)

    # ---- noticias -------------------------------------------------------
    def _draw_news(self, img, d):
        nx, ny, nw, nh = NEWS
        self._panel(img, d, nx, ny, nw, nh)
        text_ls(d, (nx + 14, ny + 15), self.t("NOTÍCIAS"), pil_font(14, "disp"), GOLD, 2)
        d.line([(nx, ny + 45), (nx + nw, ny + 45)], fill=LINE)
        news = self.st["news"]
        y = ny + 54
        if news is None or not news:
            msg = "Carregando novidades..." if news is None else "Sem novidades no momento."
            d.text((nx + 16, y), msg, font=pil_font(12, "ui"), fill=MUTED)
            return
        for i, it in enumerate(news):
            ih = 66
            if y + ih > ny + nh - 6:
                break
            tag = (it.get("tag") or "update").lower()
            tcol = {"update": BLUE, "evento": EVT, "loja": LOJA}.get(tag, BLUE)
            tf = pil_font(9, "uib"); tt = (it.get("tag") or "Update").upper()
            d.rounded_rectangle([nx + 14, y + 4, nx + 14 + _tw(d, tt, tf) + 14, y + 20], radius=4, fill=tcol)
            d.text((nx + 21, y + 7), tt, font=tf, fill=(240, 246, 255))
            data = it.get("data") or ""
            d.text((nx + 14 + _tw(d, tt, tf) + 24, y + 7), data, font=pil_font(11, "ui"), fill=MUTED2)
            tit = it.get("titulo") or ""
            ty = y + 26
            for ln in wrap2(d, tit, pil_font(14, "uib"), nw - 28):
                d.text((nx + 14, ty), ln, font=pil_font(14, "uib"), fill=TEXT); ty += 18
            self._reg(nx + 6, y, nw - 12, ih, "news:%d" % i)
            y += ih

    # ---- comunidade (Twitch ao vivo + YouTube; sem lives -> YT ate 4) ----
    def _draw_community(self, img, d):
        cx, cy, cw, ch = COMM
        self._panel(img, d, cx, cy, cw, ch)
        streams = self.st["streams"] or []
        youtube = self.st["youtube"] or []
        cpad = 14; zgap = 14; head = 26
        inx = cx + cpad; iny = cy + 13; inw = cw - 2 * cpad
        bottom = cy + ch - 12
        if streams:
            zw = (inw - zgap) // 2
            twp = max(1, (len(streams) + 1) // 2)
            self._zone_header(img, d, inx, iny, zw, self.t("STREAMERS PARCEIROS"), "live",
                              (self.t("%d ao vivo") % len(streams)), self.st["tw_page"], twp, GOLD)
            self._cards_row(img, d, inx, iny + head, zw, bottom, streams, self.st["tw_page"], 2, "tw")
            dvx = inx + zw + zgap // 2
            d.line([(dvx, iny), (dvx, bottom)], fill=LINE)
            ytx = inx + zw + zgap
            ytp = max(1, (len(youtube) + 1) // 2)
            self._zone_header(img, d, ytx, iny, zw, "YOUTUBE", "yt", "", self.st["yt_page"], ytp, YTRED)
            self._cards_row(img, d, ytx, iny + head, zw, bottom, youtube, self.st["yt_page"], 2, "yt")
        else:
            ytp = max(1, (len(youtube) + 3) // 4)
            self._zone_header(img, d, inx, iny, inw, "YOUTUBE", "yt", "", self.st["yt_page"], ytp, YTRED)
            self._cards_row(img, d, inx, iny + head, inw, bottom, youtube, self.st["yt_page"], 4, "yt")

    def _zone_header(self, img, d, x, y, w, title, mark, right, page, pages, dotcol):
        tx = x
        if mark == "live":
            d.ellipse([x, y + 3, x + 9, y + 12], fill=LIVE); tx = x + 15
        elif mark == "yt":
            ic = self._social_icons.get("youtube")
            if ic is not None:
                yt = ic.copy(); yt.thumbnail((18 * SS, 18 * SS), Image.LANCZOS)
                img.alpha_composite(yt, (x, y))
                tx = x + yt.width // SS + 8
            else:
                d.rounded_rectangle([x, y + 1, x + 20, y + 15], radius=4, fill=YTRED)
                d.polygon([(x + 8, y + 4), (x + 8, y + 12), (x + 15, y + 8)], fill=(255, 255, 255)); tx = x + 28
        text_ls(d, (tx, y + 3), title, pil_font(13, "disp"), GOLD, 2)
        rx = x + w
        if right:
            rf = pil_font(11, "ui"); rw = _tw(d, right, rf)
            d.text((x + w - rw, y + 3), right, font=rf, fill=MUTED2); rx = x + w - rw - 12
        if pages > 1:
            dx = rx - (pages * 12)
            for p in range(pages):
                if p == page:
                    d.rounded_rectangle([dx, y + 5, dx + 16, y + 11], radius=3, fill=dotcol); dx += 22
                else:
                    d.ellipse([dx, y + 5, dx + 6, y + 11], fill=(70, 80, 96)); dx += 12

    def _cards_row(self, img, d, x, y, w, ybot, items, page, ncols, kind):
        ch = ybot - y
        cw = (w - (ncols - 1) * 12) // ncols
        loading = items is None
        items = items or []
        for c in range(ncols):
            cx = x + c * (cw + 12)
            idx = page * ncols + c
            if idx < len(items):
                img.alpha_composite(self._poster(cw, ch, items[idx], kind), (cx, y))
                self._reg(cx, y, cw, ch, "%s:%d" % (kind, idx))
            else:
                ph = rgrad_card(cw, ch, SURF3, (13, 21, 31), 9)
                pd = sdraw(ph)
                pd.rounded_rectangle([0, 0, cw - 1, ch - 1], radius=9, outline=LINE)
                msg = (self.t("carregando...") if loading else
                       (self.t("nenhuma live agora") if kind == "tw" else self.t("nenhum video")))
                f = pil_font(11, "ui"); tw = _tw(pd, msg, f)
                pd.text((cw // 2 - tw // 2, ch // 2 - 8), msg, font=f, fill=MUTED2)
                img.alpha_composite(ph, (cx, y))

    # ---- miniatura de live (baixada async; cover-fit no card) -----------
    def _thumb(self, url, w, h):
        """SImg 2x cover-fit com cantos arredondados, ou None ate a imagem chegar.
        Dispara o download UMA vez; a chegada re-renderiza (fila -> _drain)."""
        if not url:
            return None
        src = self._thumbs.get(url)
        if src is None:              # ainda nao pedimos -> pede e mostra o gradiente
            self._want_thumb(url)
            return None
        if src is False:             # falhou -> nao insiste, card fica no gradiente
            return None
        pw, ph = max(1, int(w * SS)), max(1, int(h * SS))
        sw, sh = src.size
        scale = max(pw / sw, ph / sh)             # cover: preenche o card, corta sobra
        rw, rh = max(1, round(sw * scale)), max(1, round(sh * scale))
        im = src.resize((rw, rh), Image.LANCZOS)
        left, top = (rw - pw) // 2, (rh - ph) // 2
        im = im.crop((left, top, left + pw, top + ph)).convert("RGBA")
        m = Image.new("L", (pw, ph), 0)
        ImageDraw.Draw(m).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=int(9 * SS), fill=255)
        im.putalpha(m)
        return SImg(im)

    def _want_thumb(self, url):
        if not url or url in self._thumbs or url in self._thumb_inflight:
            return
        self._thumb_inflight.add(url)
        threading.Thread(target=self._fetch_thumb, args=(url,), daemon=True).start()

    def _fetch_thumb(self, url):
        img = False
        try:
            im = Image.open(io.BytesIO(http_get_bytes(url))); im.load()
            img = im.convert("RGB")
        except Exception:
            img = False              # rede/decode falhou -> marca pra nao reagendar
        self.q.put(("thumb", (url, img)))   # aplicado no thread da UI (ver _drain)

    def _poster(self, w, h, it, kind):
        if kind == "tw":
            card = rgrad_card(w, h, (22, 50, 79), (14, 32, 56), 9)
        else:
            card = rgrad_card(w, h, (58, 22, 32), (26, 13, 20), 9)
        # fundo = miniatura (twitch: frame da live; youtube: thumb do video),
        # cover-fit. O gradiente fica so ate a imagem baixar. No YT o botao de
        # play (desenhado abaixo) cai POR CIMA -> cara de card do YouTube.
        th = self._thumb(it.get("thumb"), w, h)
        if th is not None:
            card.alpha_composite(th, (0, 0))
        cd = sdraw(card)
        card.alpha_composite(scrim(w, int(h * 0.62)), (0, h - int(h * 0.62)))
        cd.rounded_rectangle([0, 0, w - 1, h - 1], radius=9, outline=LINE2)
        pad = 10
        if kind == "tw":
            self._badge_dark(cd, 8, 8, self.t("AO VIVO"), dot=LIVE)
            v = it.get("viewers")
            if v is not None:
                self._badge_dark_r(cd, w - 8, 8, self._fmtnum(v), dot=LIVE)
            if it.get("boost"):
                # canal turbinado (parceiros-live, boost.php): quem assiste ganha
                # Drops Coin mais rapido -> selo dourado + borda dourada no card
                self._badge_gold(cd, 8, 30, "DROPS %dx" % int(it.get("boost_mult") or 2))
                cd.rounded_rectangle([0, 0, w - 1, h - 1], radius=9, outline=GOLD, width=2)
            who = it.get("display") or it.get("login") or "canal"
            title = it.get("title") or ""
            meta = who; avcol = GOLD; date = ""
        else:
            pr = 18; pcx, pcy = w // 2, int(h * 0.40)
            cd.ellipse([pcx - pr, pcy - pr, pcx + pr, pcy + pr], fill=YTRED)
            cd.polygon([(pcx - 5, pcy - 8), (pcx - 5, pcy + 8), (pcx + 9, pcy)], fill=(255, 255, 255))
            dur = it.get("dur")
            if dur:
                self._badge_dark_r(cd, w - 6, 6, str(dur))
            who = it.get("canal") or "canal"
            title = it.get("titulo") or ""
            date = it.get("data") or ""
            meta = who + (" · " + date if date else ""); avcol = YTRED
        lines = wrap2(cd, title, pil_font(12, "uib"), w - 2 * pad)
        blk = len(lines) * 16 + 16
        ty = h - pad - blk + 2
        for ln in lines:
            cd.text((pad, ty), ln, font=pil_font(12, "uib"), fill=(238, 242, 248)); ty += 16
        cd.ellipse([pad, ty + 2, pad + 12, ty + 14], fill=avcol)
        cd.text((pad + 17, ty + 1), ell(cd, meta, pil_font(11, "ui"), w - 2 * pad - 18),
                font=pil_font(11, "ui"), fill=(205, 215, 230))
        return card

    def _badge_dark(self, cd, x, y, text, dot=None):
        f = pil_font(10, "uib"); tw = _tw(cd, text, f)
        bw = tw + (16 if dot else 0) + 12
        cd.rounded_rectangle([x, y, x + bw, y + 18], radius=5, fill=(6, 10, 16))
        tx = x + 6
        if dot:
            cd.ellipse([tx, y + 7, tx + 5, y + 12], fill=dot); tx += 10
        cd.text((tx, y + 4), text, font=f, fill=(255, 255, 255))

    def _badge_gold(self, cd, x, y, text):
        f = pil_font(10, "uib"); tw = _tw(cd, text, f)
        cd.rounded_rectangle([x, y, x + tw + 12, y + 18], radius=5, fill=GOLD)
        cd.text((x + 6, y + 4), text, font=f, fill=(20, 14, 4))

    def _badge_dark_r(self, cd, xr, y, text, dot=None):
        f = pil_font(10, "uib"); tw = _tw(cd, text, f)
        bw = tw + (16 if dot else 0) + 12
        self._badge_dark(cd, xr - bw, y, text, dot)

    # ---- icones sociais desenhados (uniformes, cor de cada rede) --------
    _SOC_COL = {"twitch": (183, 145, 255), "discord": (124, 143, 232),
                "instagram": (236, 132, 190), "youtube": (255, 70, 80), "site": (255, 217, 106)}

    def _social_glyph(self, d, key, x, y, s):
        col = self._SOC_COL.get(key, MUTED)
        bg = (11, 21, 36)
        cx, cy = x + s / 2.0, y + s / 2.0
        S = 22.0
        ox, oy = cx - S / 2.0, cy - S / 2.0

        def P(px, py):
            return (ox + px / 24.0 * S, oy + py / 24.0 * S)

        if key == "twitch":
            outer = [(4, 2), (3, 6), (3, 19), (7, 19), (7, 22), (10, 22),
                     (13, 19), (17, 19), (22, 14), (22, 2)]
            d.polygon([P(*p) for p in outer], fill=col)
            for bx in (10, 15):
                d.rectangle([P(bx, 7), P(bx + 2, 12)], fill=bg)
        elif key == "youtube":
            d.rounded_rectangle([P(2, 5), P(22, 19)], radius=4, fill=col)
            d.polygon([P(10, 8), P(10, 16), P(16, 12)], fill=(255, 255, 255))
        elif key == "instagram":
            d.rounded_rectangle([P(3, 3), P(21, 21)], radius=5, outline=col, width=2)
            d.ellipse([P(8, 8), P(16, 16)], outline=col, width=2)
            d.ellipse([P(16.6, 6), P(18.4, 7.8)], fill=col)
        elif key == "discord":
            d.rounded_rectangle([P(4, 6), P(20, 17)], radius=6, fill=col)
            d.polygon([P(7, 16), P(9, 20), P(10, 16)], fill=col)
            d.polygon([P(14, 16), P(15, 20), P(17, 16)], fill=col)
            d.ellipse([P(8, 10), P(11, 14)], fill=bg)
            d.ellipse([P(13, 10), P(16, 14)], fill=bg)
        else:  # site (globo)
            d.ellipse([P(3, 3), P(21, 21)], outline=col, width=2)
            d.ellipse([P(9, 3), P(15, 21)], outline=col, width=2)
            d.line([P(3, 12), P(21, 12)], fill=col, width=2)

    # ---- rodape ---------------------------------------------------------
    def _draw_footer(self, img, d):
        if getattr(self, "_fundo", None) is not None:
            self._vidro(img, 0, FOOT_Y, W, FOOT_H, (9, 18, 28), VIDRO_RODAPE)
        else:
            d.rectangle([0, FOOT_Y, W, H], fill=(14, 26, 40))
        d.line([(0, FOOT_Y), (W, FOOT_Y)], fill=LINE)
        x = PAD
        for key, label in SOCIALS:
            d.rounded_rectangle([x, SOC_Y, x + SB, SOC_Y + SB], radius=9, fill=(11, 21, 36), outline=LINE2)
            ic = self._social_icons.get(key)
            if ic is not None:
                img.alpha_composite(ic, (x + (SB - ic.width // SS) // 2, SOC_Y + (SB - ic.height // SS) // 2))
            else:
                self._social_glyph(d, key, x, SOC_Y, SB)
            self._reg(x, SOC_Y, SB, SB, "soc:" + key)
            x += SB + SOC_GAP
        # barra de progresso + status (entre socials e o botao)
        px = x + 8
        pr = CTA[0] - 16
        pw = max(40, pr - px)
        d.text((px, FOOT_Y + 13), ell(d, self.t(self.st["status"]), pil_font(11, "ui"), pw),
               font=pil_font(11, "ui"), fill=MUTED)
        ty = FOOT_Y + 38; th = 9
        d.rounded_rectangle([px, ty, px + pw, ty + th], radius=4, fill=(11, 20, 32), outline=LINE2)
        frac = max(0.0, min(1.0, self.st["progress"] / 100.0))
        fw = int((pw - 2) * frac)
        if fw >= 4:
            img.alpha_composite(rgrad_card(fw, th - 2, GOLDL, GOLDD, (th - 2) // 2), (px + 1, ty + 1))
        self._reg(CTA[0], CTA[1], CTA_W, CTA_H, "play")

    # ---- dropdown de mundos --------------------------------------------
    def _draw_dropdown(self, img, d):
        px, py, pw, ph = PILL
        online = self.st["online"] or {}
        mundos = online.get("mundos") or [
            {"nome": "Open", "tipo": "Open PvP", "online": 0, "up": True},
            {"nome": "Hardcore", "tipo": "Hardcore PvP", "online": 0, "up": True},
            {"nome": "Non-PvP", "tipo": "Optional PvP", "online": 0, "up": True},
            {"nome": "Evento", "tipo": "Arena", "online": 0, "up": True},
        ]
        rowh = 46
        dh = 34 + len(mundos) * rowh + 34
        dx = px + pw - DROP_W; dy = py + ph + 8
        self._reg(0, 0, W, H, "dropclose")             # clique fora fecha
        d.rounded_rectangle([dx, dy, dx + DROP_W, dy + dh], radius=10, fill=(16, 28, 40), outline=GOLDD)
        text_ls(d, (dx + 14, dy + 12), self.t("SERVIDORES"), pil_font(11, "disp"), GOLD, 2)
        d.line([(dx, dy + 33), (dx + DROP_W, dy + 33)], fill=LINE)
        y = dy + 34
        for m in mundos:
            up = m.get("up", True)
            d.ellipse([dx + 14, y + rowh // 2 - 5, dx + 24, y + rowh // 2 + 5],
                      fill=(GOOD if up else (90, 101, 119)))
            d.text((dx + 36, y + 9), str(m.get("nome", "")), font=pil_font(13, "uib"), fill=TEXT)
            d.text((dx + 36, y + 27), str(m.get("tipo", "")), font=pil_font(10, "ui"), fill=MUTED2)
            if up:
                nn = self._fmtnum(m.get("online", 0))
                nf = pil_font(15, "uib"); nw = _tw(d, nn, nf)
                d.text((dx + DROP_W - 16 - nw, y + 10), nn, font=nf, fill=GOLDL)
                sf = pil_font(9, "ui"); sw = _tw(d, "online", sf)
                d.text((dx + DROP_W - 16 - sw, y + 30), "online", font=sf, fill=MUTED2)
            else:
                of = pil_font(11, "ui"); ow = _tw(d, "offline", of)
                d.text((dx + DROP_W - 16 - ow, y + 16), "offline", font=of, fill=MUTED2)
            d.line([(dx + 12, y + rowh), (dx + DROP_W - 12, y + rowh)], fill=(28, 38, 52))
            y += rowh
        total = online.get("total")
        if total is None:
            total = sum(int(m.get("online", 0)) for m in mundos if m.get("up", True))
        d.text((dx + 14, y + 10), self.t("Total"), font=pil_font(11, "ui"), fill=MUTED)
        tf = pil_font(12, "uib"); tt = self.t("%s jogadores") % self._fmtnum(total)
        d.text((dx + DROP_W - 14 - _tw(d, tt, tf), y + 9), tt, font=tf, fill=GOLDL)
        # a caixa em si (por cima do dropclose) nao fecha ao clicar
        self._reg(dx, dy, DROP_W, dh, "dropkeep")

    # ---- modal de detalhe ----------------------------------------------
    def _draw_modal(self, img, d):
        mv = self.st["modal"]
        img.alpha_composite(Image.new("RGBA", (W * SS, H * SS), (4, 7, 12, 205)), (0, 0))
        self._reg(0, 0, W, H, "mclose")
        mw, mh = 560, 460
        mx, my = (W - mw) // 2, (H - mh) // 2
        d.rounded_rectangle([mx, my, mx + mw, my + mh], radius=13, fill=(16, 28, 40), outline=GOLDD)
        img.alpha_composite(rgrad_card(mw - 2, 6, GOLDL, GOLDD, 3), (mx + 1, my + 1))
        # botao fechar
        bx = mx + mw - 44; by = my + 16
        d.rounded_rectangle([bx, by, bx + 30, by + 30], radius=8, fill=(11, 20, 33), outline=LINE2)
        d.line([(bx + 10, by + 10), (bx + 20, by + 20)], fill=MUTED, width=2)
        d.line([(bx + 20, by + 10), (bx + 10, by + 20)], fill=MUTED, width=2)
        self._reg(bx, by, 30, 30, "mclose")
        # tag + data
        tag = (mv.get("tag") or "Update"); data = mv.get("data") or ""
        tcol = {"update": BLUE, "evento": EVT, "loja": LOJA}.get(tag.lower(), BLUE)
        tf = pil_font(9, "uib")
        d.rounded_rectangle([mx + 22, my + 22, mx + 22 + _tw(d, tag.upper(), tf) + 14, my + 38], radius=4, fill=tcol)
        d.text((mx + 29, my + 25), tag.upper(), font=tf, fill=(240, 246, 255))
        d.text((mx + 22 + _tw(d, tag.upper(), tf) + 24, my + 25), data, font=pil_font(11, "ui"), fill=MUTED2)
        # titulo
        ty = my + 46
        for ln in wrap2(d, mv.get("titulo", ""), pil_font(19, "disp"), mw - 90):
            d.text((mx + 22, ty), ln, font=pil_font(19, "disp"), fill=GOLDL); ty += 26
        # corpo: monta TODAS as linhas e desenha so a janela visivel. Antes o
        # texto era cortado no fim da caixa, sem rolagem - patch note comprida
        # sumia pela metade. Rola pela roda do mouse e pelas setas/PageUp/PageDown
        # (ver _rolar_modal).
        ty += 6
        bodyf = pil_font(13, "ui")
        linhas, off = [], 0
        for para in (mv.get("texto") or "").split("\n"):
            if not para.strip():
                off += 8; continue
            for ln in self._wrap_n(d, para, bodyf, mw - 56):
                linhas.append((off, ln)); off += 20
        topo, fundo = ty, my + mh - 20
        vista = max(1, fundo - topo)
        maxs = max(0, off - vista)
        mv["max_scroll"] = maxs
        rol = min(max(0, int(mv.get("scroll", 0))), maxs)
        mv["scroll"] = rol
        for o, ln in linhas:
            y = topo + o - rol
            if y < topo or y + 18 > fundo:
                continue
            d.text((mx + 22, y), ln, font=bodyf, fill=(211, 218, 231))
        if maxs > 0:
            sx = mx + mw - 16
            d.rounded_rectangle([sx, topo, sx + 5, fundo], radius=2, fill=(11, 20, 33))
            th = max(28, int(vista * vista / off))
            sy = topo + int((vista - th) * rol / maxs)
            d.rounded_rectangle([sx, sy, sx + 5, sy + th], radius=2, fill=GOLDD)

    def _rolar_modal(self, passo):
        mv = self.st.get("modal")
        if not mv or not mv.get("max_scroll"):
            return
        novo = min(max(0, int(mv.get("scroll", 0)) + passo), mv["max_scroll"])
        if novo != mv.get("scroll", 0):
            mv["scroll"] = novo
            self.render()

    @staticmethod
    def _wrap_n(d, s, f, maxw):
        out, cur = [], ""
        for wd in s.split():
            t = (cur + " " + wd).strip()
            if _tw(d, t, f) <= maxw or not cur:
                cur = t
            else:
                out.append(cur); cur = wd
        if cur:
            out.append(cur)
        return out

    # ---- timers de animacao --------------------------------------------
    def _tick_cta(self):
        if self._ctatk and not self.st["modal"]:
            self._cta_i = (self._cta_i + 1) % len(self._ctatk)
            self.canvas.itemconfigure(self.cta_item, image=self._ctatk[self._cta_i])
        self.tk.after(70, self._tick_cta)

    def _tick_carousel(self):
        n = len(self._carousel_slides())
        if n > 1:
            self.st["carousel"] = (self.st["carousel"] + 1) % n
            self.render()
        self.tk.after(CAROUSEL_MS, self._tick_carousel)

    def _tick_cards(self):
        tw = self.st["streams"] or []
        yt = self.st["youtube"] or []
        ncols = 2 if tw else 4
        changed = False
        if len(tw) > 2:
            self.st["tw_page"] = (self.st["tw_page"] + 1) % max(1, (len(tw) + 1) // 2); changed = True
        if len(yt) > ncols:
            self.st["yt_page"] = (self.st["yt_page"] + 1) % max(1, (len(yt) + ncols - 1) // ncols); changed = True
        if changed:
            self.render()
        self.tk.after(CARD_ROT_MS, self._tick_cards)

    # ---- interacao ------------------------------------------------------
    def _hit(self, x, y):
        act = None
        for (x0, y0, x1, y1, a) in self._regions:   # o ULTIMO (topo) vence
            if x0 <= x <= x1 and y0 <= y <= y1:
                act = a
        return act

    def _on_move(self, e):
        a = self._hit(e.x, e.y)
        cur = "hand2" if (a and a not in ("dropkeep",)) else "arrow"
        self.canvas.config(cursor=cur)

    def _on_press(self, e):
        self._pressed = self._hit(e.x, e.y)
        # arrastar pela barra de titulo (fora de qualquer regiao)
        if self._pressed is None and e.y <= BAR_H:
            self._drag = (e.x_root - self.tk.winfo_x(), e.y_root - self.tk.winfo_y())
        else:
            self._drag = None

    def _on_drag(self, e):
        if self._drag:
            self.tk.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _on_release(self, e):
        self._drag = None
        act = self._hit(e.x, e.y)
        if not act or act != self._pressed:
            return
        st = self.st
        if act == "close":
            self.tk.destroy()
        elif act == "min":
            self.tk.overrideredirect(False); self.tk.iconify()
        elif act == "lang":
            self._cycle_lang()
        elif act == "play":
            self._play()
        elif act == "dropdown":
            st["dropdown"] = not st["dropdown"]; self.render()
        elif act in ("dropclose",):
            st["dropdown"] = False; self.render()
        elif act == "dropkeep":
            pass
        elif act in ("mclose",):
            st["modal"] = None; self.render()
        elif act == "cprev" or act == "cnext":
            n = len(self._carousel_slides())
            st["carousel"] = (st["carousel"] + (1 if act == "cnext" else -1)) % n
            self.render()
        elif act.startswith("carousel:"):
            i = int(act.split(":")[1]); sl = self._carousel_slides()[i]
            self._open_modal(sl)
        elif act.startswith("news:"):
            i = int(act.split(":")[1]); news = st["news"] or []
            if i < len(news):
                self._open_modal(news[i])
        elif act.startswith("tw:"):
            i = int(act.split(":")[1]); it = (st["streams"] or [])[i:i + 1]
            if it:
                it = it[0]; login = it.get("login") or ""
                self.track("twitch", login or (it.get("display") or "?"))
                self._open(it.get("url") or ("https://www.twitch.tv/" + login))
        elif act.startswith("yt:"):
            i = int(act.split(":")[1]); it = (st["youtube"] or [])[i:i + 1]
            if it:
                it = it[0]
                self.track("youtube", it.get("canal") or "?")
                self._open(it.get("url") or "https://www.youtube.com/@HimanenTV")
        elif act.startswith("soc:"):
            key = act.split(":")[1]
            if key == "twitch":
                self.track("twitch", "social")
            self._open(LINKS.get(key, "https://tibia2ot.com/"))

    def _close_overlays(self):
        if self.st["modal"] or self.st["dropdown"]:
            self.st["modal"] = None; self.st["dropdown"] = False; self.render()

    def _open_modal(self, item):
        self.st["dropdown"] = False
        self.st["modal"] = {"tag": item.get("tag") or "Update", "data": item.get("data") or "",
                            "titulo": item.get("titulo") or "", "texto": item.get("texto") or ""}
        self.render()

    def _open(self, url):
        try:
            if url:
                webbrowser.open(url)
        except Exception:
            pass

    # ---- medidor de cliques (Twitch/YouTube) ---------------------------
    def track(self, rede, canal):
        def _post():
            try:
                http_post_json(METRICA_URL, {"rede": rede, "canal": canal})
            except Exception:
                pass
        threading.Thread(target=_post, daemon=True).start()

    # ---- conteudo dinamico (streams/youtube/online) --------------------
    def _fetch_content(self):
        for url, key, empty in ((DESTAQUES_URL, "destaques", []),
                                (PARCEIROS_URL, "streams", []),
                                (YOUTUBE_URL, "youtube", []),
                                (ONLINE_URL, "online", {})):
            try:
                data = http_get_json(url)
            except Exception:
                data = empty
            self.q.put((key, data))

    # ---- worker (thread) ------------------------------------------------
    def _work(self):
        # Auto-update do PROPRIO launcher, antes de tudo. Se trocar, relanca e sai.
        self.q.put(("status", "Verificando launcher..."))
        try:
            if self_update():
                self.q.put(("status", "Atualizando launcher..."))
                os._exit(0)
        except Exception:
            pass
        # Ja na versao atual: o atalho "OTibion" (rebrand revertido) volta a "Tibia 2"
        # e as copias paradas com aquele nome saem da pasta.
        migrar_atalho_desktop()
        faxina_heranca_otibion(self.root)

        try:
            data = http_get_json(NEWS_URL)
            self.q.put(("news", data.get("noticias", []) if isinstance(data, dict) else []))
        except Exception:
            self.q.put(("news", None))

        try:
            self.q.put(("status", "Consultando servidor de atualizacao..."))
            manifest = http_post_json(UPDATER_URL, {"version": "launcher", "build": "1",
                                                    "os": "windows", "platform": 1, "args": {}})
        except Exception as e:
            log_erro("checar atualizacoes", e, UPDATER_URL)
            reportar_falha("manifesto do updater indisponivel: %s: %s" % (type(e).__name__, e))
            self.q.put(("status", "Nao foi possivel checar atualizacoes. Veja tibia2-launcher.log"))
            self.q.put(("ready", "ABRIR JOGO"))
            return

        if not isinstance(manifest, dict) or manifest.get("error"):
            self.q.put(("status", "Atualizacao indisponivel - jogando com o que ha."))
            self.q.put(("ready", "ABRIR JOGO"))
            return

        base = manifest.get("url") or ""
        files = manifest.get("files") or {}
        cache = self._load_cache()          # {rel: [tamanho, mtime_ns, crc]} da ultima vez
        clock = threading.Lock()
        self.q.put(("status", "Verificando arquivos (%d)..." % len(files)))

        # 1) Passada rapida: so um stat por arquivo. Se tamanho+mtime batem com o
        #    cache e o crc guardado e o do manifesto, o arquivo esta OK sem reler
        #    os bytes. So entra na fila de HASH quem o cache nao cobre; quem falta
        #    vai direto pra baixar. Isso derruba a reverificacao de ~164s (reler
        #    943 MB) para um stat de cada arquivo (~1-2s) quando nada mudou.
        to_update, to_hash = [], []
        total = max(1, len(files))
        for i, (rel, want) in enumerate(files.items()):
            local = rel_to_local(self.root, rel)
            try:
                st = os.stat(local)
            except OSError:
                to_update.append((rel, want)); continue
            c = cache.get(rel)
            if c and c[0] == st.st_size and c[1] == st.st_mtime_ns and c[2] == want:
                continue
            to_hash.append((rel, want))
            # A passada de stat e rapida (~1-2s); nao move a barra pra nao zipar
            # ate 100 e reiniciar quando o hash comecar.

        # 2) Hash SO do que o cache nao cobriu, em PARALELO (sobrepoe o custo por
        #    arquivo). Quem passa vira entrada de cache; quem falha, vai baixar.
        #    Mesmo feedback do download: % na barra + contagem + tempo estimado.
        if to_hash:
            nh = len(to_hash)
            vf = {"n": 0}
            tv = time.time()

            def confere(item):
                rel, want = item
                local = rel_to_local(self.root, rel)
                if crc32_of(local) == want:
                    try:
                        st = os.stat(local)
                        with clock:
                            cache[rel] = [st.st_size, st.st_mtime_ns, want]
                    except OSError:
                        pass
                else:
                    with clock:
                        to_update.append((rel, want))
                with clock:
                    vf["n"] += 1
                    k = vf["n"]
                if k % 15 == 0 or k == nh:
                    self.q.put(("progress", 100.0 * k / nh))
                    el = max(0.001, time.time() - tv)
                    eta = (el / k) * (nh - k) if k else 0
                    self.q.put(("vfstat", (k, nh, eta)))

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(confere, to_hash))

        if not to_update:
            self._save_cache(cache)
            self.q.put(("progress", 100)); self.q.put(("status", "Cliente atualizado. Boa caçada!"))
            self.q.put(("ready", "ABRIR JOGO")); return

        # So baixa o que a ASSINATURA do release conhece (ver CHAVE_PUBLICA_RELEASE):
        # sem assinatura valida nao baixa nada - melhor nao atualizar do que
        # instalar um arquivo que nao veio do release.
        self.q.put(("status", "Conferindo a assinatura da atualizacao..."))
        assinada = carregar_assinatura()
        if assinada is None:
            self.q.put(("status", "Nao foi possivel conferir a assinatura da atualizacao. Tente de novo em instantes."))
            self.q.put(("ready", "TENTAR DE NOVO")); return
        self._assinados = assinada["cliente"]

        # Download PARALELO: primeiro install sao ~15k arquivos, metade com menos
        # de 16 KB - o custo e a LATENCIA por requisicao, nao a banda. 16 ao mesmo
        # tempo (eram 8) esconde isso; o servidor agora e o Caddy (estatico em Go)
        # e, com o ?v=<crc>, a borda do Cloudflare - aguenta a concorrencia.
        # Cada arquivo baixado ja entra no cache (sem rehash na proxima abertura).
        #
        # NAO PARA NO PRIMEIRO ERRO (2026-09-28). Antes, um arquivo que falhasse
        # parava todo o resto e o jogador ficava com a instalacao pela metade ate
        # clicar em TENTAR DE NOVO - "faltou arquivo". Agora baixa tudo, junta o
        # que falhou e refaz ate 2 vezes com o MANIFESTO RECARREGADO: o servidor
        # responde 404 a um ?v=<crc> que nao e mais o do arquivo (troca de versao
        # no meio do download), e o manifesto novo traz o crc certo. So o que
        # sobrar depois disso vira TENTAR DE NOVO - e e relatado ao servidor.
        clock_err = threading.Lock()

        def rodada(itens, base_url):
            n = len(itens)
            state = {"done": 0, "bytes": 0}
            falhas = []
            t0 = time.time()

            def baixa(item):
                rel, want = item
                sz = 0
                try:
                    self._download_one(base_url, rel, want)
                    st = os.stat(rel_to_local(self.root, rel))
                    sz = st.st_size
                    with clock:
                        cache[rel] = [st.st_size, st.st_mtime_ns, want]
                except Exception as e:
                    with clock_err:
                        falhas.append((rel, want, "%s: %s" % (type(e).__name__, e)))
                with clock:
                    state["done"] += 1
                    state["bytes"] += sz
                    dn = state["done"]; by = state["bytes"]
                self.q.put(("progress", 100.0 * dn / n))
                # velocidade + tempo estimado: media movel simples desde o inicio.
                # ETA por contagem de arquivos (o manifesto nao traz tamanho); com
                # download paralelo a taxa de arquivos/s e estavel, entao a estimativa
                # converge rapido.
                if dn % 15 == 0 or dn == n:
                    el = max(0.001, time.time() - t0)
                    spd = by / el / 1e6                          # MB/s
                    eta = (el / dn) * (n - dn) if dn else 0      # segundos restantes
                    self.q.put(("dlstat", (dn, n, spd, eta)))

            with ThreadPoolExecutor(max_workers=DOWNLOAD_WORKERS) as ex:
                list(ex.map(baixa, itens))
            return falhas

        falhas = rodada(to_update, base)
        for _refazer in range(2):
            if not falhas:
                break
            self.q.put(("status", "Refazendo arquivos que falharam..."))
            time.sleep(2 + 3 * _refazer)
            try:
                novo = http_post_json(UPDATER_URL, {"version": "launcher", "build": "1",
                                                    "os": "windows", "platform": 1, "args": {}})
                base = novo.get("url") or base
                files = novo.get("files") or files
            except Exception as e:
                log_erro("recarregar manifesto", e, UPDATER_URL)
            # Release novo no meio do caminho traz assinatura nova junto.
            nova_assinatura = carregar_assinatura()
            if nova_assinatura is not None:
                self._assinados = nova_assinatura["cliente"]
            # So o que falhou; com o crc do manifesto NOVO. Arquivo que saiu do
            # manifesto nao precisa mais ser baixado.
            refazer = [(rel, files[rel]) for rel, _w, _m in falhas if rel in files]
            falhas = rodada(refazer, base) if refazer else []

        if falhas:
            self._save_cache(cache)
            resumo = "; ".join("%s (%s)" % (rel, motivo) for rel, _w, motivo in falhas[:12])
            reportar_falha("download falhou em %d arquivo(s) de %d: %s" % (len(falhas), len(to_update), resumo))
            self.q.put(("status", "Alguns arquivos nao baixaram. Clique em TENTAR DE NOVO."))
            self.q.put(("ready", "TENTAR DE NOVO")); return
        self._save_cache(cache)
        self.q.put(("progress", 100)); self.q.put(("status", "Cliente pronto. Boa caçada!"))
        self.q.put(("ready", "ABRIR JOGO"))

    def _download_one(self, base, rel, want=None):
        pr = urllib.parse.urlparse(base)
        path = pr.path.rstrip("/") + "/" + urllib.parse.quote(rel.lstrip("/"))
        # URL VERSIONADA pelo crc do manifesto: o arquivo muda, a URL muda. O
        # servidor da cache de 1 ano a esse pedido, o Cloudflare serve da borda
        # (Sao Paulo) e nenhuma atualizacao fica presa a um cache velho.
        if want:
            path += "?v=" + urllib.parse.quote(str(want))
        local = rel_to_local(self.root, rel)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        tmp = local + ".part"
        # Conexao persistente por thread + 1 reconexao. O Apache fecha a conexao a
        # cada ~100 requisicoes (MaxKeepAliveRequests); quando isso acontece a
        # proxima request falha, entao reabrimos e tentamos de novo.
        for tentativa in (1, 2):
            conn = getattr(_conns, "c", None)
            if conn is None:
                conn = _conns.c = http.client.HTTPSConnection(
                    pr.netloc, timeout=HTTP_TIMEOUT, context=_SSL_CTX)
            try:
                conn.request("GET", path, headers={"User-Agent": USER_AGENT})
                r = conn.getresponse()
                if r.status != 200:
                    r.read()
                    raise IOError("HTTP %d em %s" % (r.status, rel))
                crc = 0
                sha = hashlib.sha256()
                with open(tmp, "wb") as f:
                    for chunk in iter(lambda: r.read(1 << 20), b""):
                        f.write(chunk)
                        crc = zlib.crc32(chunk, crc)
                        sha.update(chunk)
                # CONFERE o que chegou contra o manifesto. Antes o arquivo baixado
                # entrava no cache com o crc ESPERADO sem conferir - um cache
                # velho no caminho (CDN, proxy) deixava sprite antigo instalado
                # e dado como certo para sempre.
                if want and format(crc & 0xFFFFFFFF, "x") != want:
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise IOError("crc diferente do manifesto em %s" % rel)
                # E o que decide: o SHA-256 que o release ASSINOU. Arquivo fora da
                # assinatura, ou com outro conteudo, nao entra.
                assinados = getattr(self, "_assinados", None)
                if assinados is None or assinados.get(rel) != sha.hexdigest():
                    try:
                        os.remove(tmp)
                    except OSError:
                        pass
                    raise IOError("arquivo nao confere com a assinatura do release: %s" % rel)
                os.replace(tmp, local)
                return
            except (http.client.HTTPException, OSError) as e:
                try:
                    conn.close()
                except Exception:
                    pass
                _conns.c = None
                if tentativa == 2:
                    log_erro("baixar arquivo", e, base + path)
                    raise

    # ---- cache de integridade (pula rehash de arquivo inalterado) -------
    def _cache_file(self):
        return os.path.join(self.root, ".tibia2_cache.json")

    def _load_cache(self):
        try:
            with open(self._cache_file(), "r", encoding="utf-8") as f:
                d = json.load(f)
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save_cache(self, cache):
        try:
            tmp = self._cache_file() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(cache, f)
            os.replace(tmp, self._cache_file())
        except Exception:
            pass

    # ---- ponte thread -> UI (coalesce: re-renderiza 1x por ciclo) ------
    def _drain(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                st = self.st
                if kind == "status":
                    st["status"] = val
                elif kind == "progress":
                    st["progress"] = val
                elif kind == "dlstat":
                    dn, n_, spd, eta = val
                    st["status"] = ("%d/%d  ·  %.1f MB/s  ·  %s"
                                    % (dn, n_, spd, self._fmt_eta(eta)))
                elif kind == "vfstat":
                    k, nh, eta = val
                    st["status"] = "Conferindo %d/%d  ·  %s" % (k, nh, self._fmt_eta(eta))
                elif kind == "news":
                    st["news"] = val if isinstance(val, list) else []
                elif kind in ("streams", "youtube", "destaques"):
                    st[kind] = val if isinstance(val, list) else []
                elif kind == "thumb":
                    url, im = val
                    self._thumbs[url] = im            # Image RGB ou False (falhou)
                    self._thumb_inflight.discard(url)
                elif kind == "online":
                    st["online"] = val if isinstance(val, dict) else {}
                elif kind == "ready":
                    self.ready = True
                    st["progress"] = 100
                self._dirty = True
        except queue.Empty:
            pass
        if self._dirty:
            self.render()
        self.tk.after(80, self._drain)

    @staticmethod
    def _fmt_eta(s):
        if s >= 90:
            return "faltam ~%d min" % round(s / 60.0)
        if s >= 3:
            return "faltam ~%ds" % int(s)
        return "quase la"

    # ---- jogar ----------------------------------------------------------
    def _play(self):
        if self.launched or not self.ready:
            return
        exe = os.path.join(self.root, CLIENT_EXE)
        if not os.path.exists(exe):
            self.st["status"] = CLIENT_EXE + " nao encontrado."
            self.render()
            return
        self.launched = True
        try:
            # marcador: o client so abre se veio do launcher (bloqueia abrir o
            # binario do jogo direto). O client confere os.getenv('TIBIA2_LAUNCHER').
            env = os.environ.copy()
            env["TIBIA2_LAUNCHER"] = "1"
            subprocess.Popen([exe], cwd=self.root, env=env)
        except Exception:
            self.launched = False
            self.st["status"] = "Nao foi possivel abrir o jogo."
            self.render()
            return
        self.tk.destroy()


_mutex_handle = None


def release_mutex():
    """Libera o mutex de instancia unica (usado antes de relancar no auto-update,
    senao a nova instancia acha o mutex e sai achando que ja tem outra aberta)."""
    global _mutex_handle
    try:
        if _mutex_handle:
            ctypes.windll.kernel32.CloseHandle(_mutex_handle)
    except Exception:
        pass
    _mutex_handle = None


# ---------------------------------------------------------------------------
# SWAPPER DO AUTO-UPDATE (onedir) - versao ROBUSTA
#
# No onedir o launcher e uma PASTA (Tibia 2.exe + _internal\). Nao da pra
# sobrescrever _internal\*.dll com o launcher aberto - DLL carregada fica
# travada. Entao o auto-update baixa a pasta nova, escreve este .bat e SAI; o
# .bat espera o launcher fechar e troca _internal\ + o exe. So mexe nesses dois -
# NUNCA nos ~940 MB do cliente, que ficam soltos na mesma pasta.
#
# POR QUE ROBUSTO (a versao antiga loopava): ela fazia
# `move "%NEW%\_internal" "_internal"` - rename atomico da pasta RECEM extraida.
# Se o antivirus estava varrendo uma DLL nova (a openblas do numpy tem ~20 MB), o
# rename falhava de uma vez, o swap revertia e relancava o exe VELHO, que via de
# novo "estou velho" e repetia: abre-e-fecha sem fim. Agora, em 3 camadas:
#   1. STAGE por robocopy /R (REINTENTA cada arquivo travado) -> _internal.stage;
#   2. COMMIT so entao, por renomeacoes LOCAIS (rapidas, do que ja e nosso), cada
#      uma com retry e em ordem que deixa sempre um par consistente se falhar;
#   3. o teto de tentativas TOTAL fica no marcador em self_update (quebra-loop):
#      apos MAX_UPDATE_ATTEMPTS o launcher desiste e roda a versao atual.
# Cada passo loga em tibia2-launcher.log - o swap deixou de ser silencioso.
#
# Detalhes que tem motivo:
#   - ping -n em vez de timeout: timeout le do console, e este .bat roda
#     destacado SEM console (timeout falharia na hora).
#   - a espera pelo fim do launcher tem TETO (~40s): se o processo travar, o
#     swapper aborta sem mexer, em vez de esperar pra sempre.
#   - o .bat se AUTO-LOCALIZA por %~dp0 (APP e o pai de _launcher_update\), em vez
#     de embutir o caminho da instalacao. O caminho passa pelo Desktop do usuario,
#     que em pt-BR costuma ter acento (C:\Users\Jose...) - embutir isso num .bat
#     escrito em ASCII corromperia o caminho e o swap falharia. Assim o .bat e
#     ASCII puro e o caminho (acentuado ou nao) so aparece em runtime, resolvido
#     pelo proprio cmd.
# ---------------------------------------------------------------------------
_SWAPPER_BAT = r"""@echo off
setlocal enableextensions enabledelayedexpansion
set "EXE=__EXE__"
set "UPD=%~dp0"
for %%I in ("%UPD%..") do set "APP=%%~fI"
set "NEW=%UPD%new"
set "LOG=%APP%\tibia2-launcher.log"
cd /d "%APP%"
>>"%LOG%" echo %date% %time%  swapper: iniciado

rem 1) Espera o launcher fechar. Teto ~40s (80 x ~0.5s): sem console o timeout
rem    falha, entao contamos ciclos de ping. Estourou -> aborta sem mexer em nada.
set /a _w=0
:waitloop
tasklist /fi "imagename eq %EXE%" 2>nul | find /i "%EXE%" >nul
if errorlevel 1 goto closed
set /a _w+=1
if !_w! gtr 80 (
    >>"%LOG%" echo %date% %time%  swapper: launcher nao fechou em 40s, abortando
    goto done
)
ping -n 2 127.0.0.1 >nul
goto waitloop
:closed

rem 2) STAGE: copia o _internal novo para _internal.stage. robocopy REINTENTA
rem    arquivo travado (AV varrendo DLL recem-extraida) - a raiz do loop antigo,
rem    onde o "move" da pasta falhava de uma vez. /R:20 /W:1 = ate 20 tentativas
rem    por arquivo, 1s entre elas. Exit >=8 = falha real.
if exist "%APP%\_internal.stage" rmdir /s /q "%APP%\_internal.stage"
robocopy "%NEW%\_internal" "%APP%\_internal.stage" /E /R:20 /W:1 /NFL /NDL /NJH /NJS /NP >nul
if %ERRORLEVEL% GEQ 8 (
    >>"%LOG%" echo %date% %time%  swapper: robocopy stage falhou err=%ERRORLEVEL%
    if exist "%APP%\_internal.stage" rmdir /s /q "%APP%\_internal.stage"
    goto relaunch
)

rem 3) Sanity do que vamos instalar.
if not exist "%NEW%\%EXE%" goto sanity_fail
if not exist "%APP%\_internal.stage\python313.dll" goto sanity_fail

rem 4) COMMIT: renomeacoes LOCAIS (rapidas, do que ja e nosso), cada uma com
rem    retry, em ordem que deixa sempre um par (_internal, exe) CONSISTENTE se
rem    algo falhar no meio - ver os rollbacks nos labels commit_fail_*.
if exist "%APP%\_internal.old" rmdir /s /q "%APP%\_internal.old"
call :ren_retry "_internal" "_internal.old"
if errorlevel 1 goto commit_fail_a
call :ren_retry "_internal.stage" "_internal"
if errorlevel 1 goto commit_fail_b
call :copy_retry "%NEW%\%EXE%" "%APP%\%EXE%"
if errorlevel 1 goto commit_fail_c
rem 4b) Os OUTROS nomes do launcher (rebrand: "Tibia 2.exe" e "OTibion.exe" sao o
rem     MESMO programa; o codigo mora DENTRO do exe). Todos vao junto, senao o que
rem     nao foi aberto fica com codigo velho sobre o _internal novo. Best-effort:
rem     o par (_internal, %EXE%) ja e consistente, falhar aqui nao reverte nada.
for %%F in ("%NEW%\*.exe") do if /i not "%%~nxF"=="%EXE%" copy /y "%%~fF" "%APP%\%%~nxF" >nul 2>&1

rem 5) Sucesso. Loga e relanca. NAO apaga %UPD% aqui: este .bat roda de DENTRO
rem    dele - um rmdir do proprio diretorio corta a leitura do batch no meio, e o
rem    relaunch abaixo nem rodaria (launcher trocaria mas nao reabriria). Quem
rem    limpa _launcher_update e o self_update na proxima abertura (faxina). So o
rem    _internal.old, que fica FORA de %UPD%, da pra remover aqui com seguranca.
>>"%LOG%" echo %date% %time%  swapper: update aplicado com sucesso
if exist "%APP%\_internal.old" rmdir /s /q "%APP%\_internal.old"
goto relaunch

:sanity_fail
>>"%LOG%" echo %date% %time%  swapper: pacote novo incompleto, abortando
if exist "%APP%\_internal.stage" rmdir /s /q "%APP%\_internal.stage"
goto relaunch

:commit_fail_a
rem nao tirou o _internal velho do caminho: nada mudou, versao atual intacta.
>>"%LOG%" echo %date% %time%  swapper: _internal travado, mantendo versao atual
if exist "%APP%\_internal.stage" rmdir /s /q "%APP%\_internal.stage"
goto relaunch

:commit_fail_b
rem _internal virou _internal.old mas o novo nao entrou: restaura o velho.
>>"%LOG%" echo %date% %time%  swapper: novo _internal nao entrou, revertendo
call :ren_retry "_internal.old" "_internal"
if exist "%APP%\_internal.stage" rmdir /s /q "%APP%\_internal.stage"
goto relaunch

:commit_fail_c
rem _internal ja e o novo mas o exe nao trocou: reverte pro par VELHO consistente.
>>"%LOG%" echo %date% %time%  swapper: exe travado, revertendo o par
call :ren_retry "_internal" "_internal.stage"
call :ren_retry "_internal.old" "_internal"
if exist "%APP%\_internal.stage" rmdir /s /q "%APP%\_internal.stage"
goto relaunch

:relaunch
start "" "%APP%\%EXE%"
:done
endlocal
exit /b

:ren_retry
rem %1=origem %2=destino ; ok = a origem sumiu. Ate 8 tentativas (lock transitorio).
set /a _r=0
:ren_loop
ren %1 %2 >nul 2>&1
if not exist %1 exit /b 0
set /a _r+=1
if !_r! gtr 8 exit /b 1
ping -n 2 127.0.0.1 >nul
goto ren_loop

:copy_retry
set /a _c=0
:copy_loop
copy /y %1 %2 >nul 2>&1
if not errorlevel 1 exit /b 0
set /a _c+=1
if !_c! gtr 8 exit /b 1
ping -n 2 127.0.0.1 >nul
goto copy_loop
"""


def _rmtree_quieto(path):
    """Remove arquivo ou pasta sem nunca levantar (faxina best-effort)."""
    try:
        import shutil
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
        elif os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# Quantas vezes tentar TROCAR para a MESMA versao antes de desistir e rodar a
# atual. O quebra-loop: sem isto, um swap que falha sempre (lock, permissao,
# disco cheio) faz o exe velho reabrir, ver "estou velho" e repetir pra sempre -
# o launcher "fecha ao abrir". Com o marcador abaixo, apos N falhas o launcher
# fica na versao atual (que funciona) ate o servidor publicar uma versao NOVA.
MAX_UPDATE_ATTEMPTS = 3


def _marker_path(app):
    return os.path.join(app, "_update_pending.json")


def _read_marker(app):
    """(versao-alvo, tentativas) do marcador; ('', 0) se ausente/ilegivel."""
    try:
        with open(_marker_path(app), "r", encoding="ascii") as f:
            d = json.load(f)
        return str(d.get("target") or ""), int(d.get("attempts") or 0)
    except Exception:
        return "", 0


def _write_marker(app, target, attempts):
    try:
        with open(_marker_path(app), "w", encoding="ascii") as f:
            json.dump({"target": target, "attempts": attempts}, f)
    except Exception:
        pass


def _clear_marker(app):
    _rmtree_quieto(_marker_path(app))


def _pasta_desktop():
    """Area de trabalho do usuario pela API (SHGetKnownFolderPath) - acerta o Desktop
    redirecionado para o OneDrive, que o caminho montado a mao (~\\Desktop) erra."""
    try:
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("d1", wintypes.DWORD), ("d2", wintypes.WORD),
                        ("d3", wintypes.WORD), ("d4", ctypes.c_ubyte * 8)]
        # FOLDERID_Desktop = {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}
        fid = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                   (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        p = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None, ctypes.byref(p)) != 0:
            return None
        try:
            return p.value
        finally:
            ctypes.windll.ole32.CoTaskMemFree(p)
    except Exception:
        return None


def migrar_atalho_desktop():
    """O rebrand OTibion (29/09, REVERTIDO em 30/09) renomeou o atalho da area de
    trabalho de "Tibia 2" para "OTibion" (launchers 2026-09-30.2 a .6), e o
    instalador daquela fase criou o atalho ja como "OTibion". Aqui ele volta a se
    chamar "Tibia 2".

    RENOMEIA o .lnk em vez de recria-lo: recriar exige o shell do Windows (COM ou
    PowerShell), e um exe PyInstaller disparando PowerShell e o padrao que antivirus
    acusa. So mexe se o .lnk apontar para um exe nosso ("Tibia 2.exe" ou
    "OTibion.exe" - os dois sao o mesmo launcher) e se ainda nao existir um
    "Tibia 2.lnk". Idempotente e silencioso."""
    if not getattr(sys, "frozen", False):
        return
    try:
        desk = _pasta_desktop()
        if not desk:
            return
        velho = os.path.join(desk, "OTibion.lnk")
        novo = os.path.join(desk, "Tibia 2.lnk")
        if not os.path.isfile(velho) or os.path.exists(novo):
            return
        with open(velho, "rb") as f:
            dados = f.read(256 * 1024)
        nosso = any(a.encode("utf-16-le") in dados or a.encode("ascii") in dados
                    for a in ("Tibia 2.exe", "OTibion.exe"))
        if not nosso:
            return                             # atalho de outra coisa: nao e nosso
        os.rename(velho, novo)
        log_erro("atalho migrado", None, "OTibion.lnk -> Tibia 2.lnk em " + desk)
    except Exception as e:
        log_erro("migrar atalho", e)


def faxina_heranca_otibion(root):
    """Apaga "OTibion Client.exe" e "OTibion.exe" da pasta do jogo. Os dois foram
    publicados de 29/09 a 01/10 (rebrand revertido) e, como o updater nunca apaga
    arquivo, ficaram PARADOS no disco de quem atualizou naquela janela - um exe de
    jogo velho que os launchers 2026-09-30.2 a .6 abririam em vez do atual.

    So age se o "Tibia 2 Client.exe" existir (a pasta e mesmo do jogo) e nunca apaga
    o exe com que este launcher foi aberto (instalacao daquela fase aberta como
    "OTibion.exe"). Arquivo em uso (jogo aberto) so falha calado e sai na proxima."""
    if not getattr(sys, "frozen", False):
        return
    try:
        if not os.path.isfile(os.path.join(root, CLIENT_EXE)):
            return
        aberto = os.path.normcase(os.path.abspath(sys.executable))
        for nome in EXES_HERANCA_OTIBION:
            cam = os.path.join(root, nome)
            if os.path.isfile(cam) and os.path.normcase(os.path.abspath(cam)) != aberto:
                try:
                    os.remove(cam)
                    log_erro("heranca OTibion removida", None, cam)
                except OSError:
                    pass
    except Exception as e:
        log_erro("faxina heranca OTibion", e)


def self_update():
    """Se este launcher (onedir) estiver desatualizado (version != a do servidor
    em launcher.json), baixa a PASTA nova (zip), dispara o swapper .bat e sai.
    Retorna True se disparou a troca (o chamador deve sair). Tolerante a falha:
    qualquer erro -> segue com a versao atual (o launcher tem de abrir mesmo sem
    conseguir se atualizar)."""
    if not getattr(sys, "frozen", False):
        return False
    exe = sys.executable
    app = os.path.dirname(exe)
    exe_name = os.path.basename(exe)           # "Tibia 2.exe"
    upd = os.path.join(app, "_launcher_update")
    # Faxina de restos do swap anterior (roda sempre, mesmo ja atualizado): a
    # pasta de update e as pastas de trabalho do swapper (.old/.stage). O swapper
    # roda DEPOIS que saimos, entao a limpeza fica pra proxima abertura.
    _rmtree_quieto(upd)
    _rmtree_quieto(os.path.join(app, "_internal.old"))
    _rmtree_quieto(os.path.join(app, "_internal.stage"))
    try:
        info = http_get_json(LAUNCHER_INFO_URL)
    except Exception as e:
        # ANTES isso era mudo, e um 403 nesta URL passou despercebido por muito
        # tempo: a auto-atualizacao estava morta e nada nunca reclamou.
        log_erro("auto-update do launcher", e, LAUNCHER_INFO_URL)
        return False
    remote_ver = (info or {}).get("version")
    url = (info or {}).get("url")
    # A chave e "zip_crc", NAO "crc", de proposito. O launcher onefile ANTIGO fazia
    # `want = info.get("crc")` e baixava a `url` COMO SE FOSSE o exe novo. Se o
    # launcher.json onedir trouxesse "crc", todo onefile la fora baixaria o ZIP,
    # renomearia pra "Tibia 2.exe" e tentaria executar um zip -> launcher tijolo.
    # Com a chave renomeada o onefile antigo ve want=None e nao faz nada (seguro);
    # ele migra pra onedir reinstalando. Ver docs/deploy-online.md.
    want_crc = (info or {}).get("zip_crc")
    if not remote_ver or not url:
        _clear_marker(app)
        return False                           # servidor sem info de launcher
    if remote_ver == LAUNCHER_VERSION:
        # Estamos na versao do servidor: atualizado (ou o swap anterior deu certo).
        # Zera o marcador de tentativas - o proximo alvo comeca do zero.
        _clear_marker(app)
        return False
    # Desatualizados. QUEBRA-LOOP: se ja tentamos trocar para ESTA versao
    # MAX_UPDATE_ATTEMPTS vezes e continuamos velhos, o swap esta falhando de forma
    # persistente. Paramos de tentar e rodamos a versao ATUAL (que abre) - melhor
    # um launcher velho funcionando que um abre-e-fecha. O marcador so vale para
    # ESTE alvo; quando o servidor publicar outra versao, tentamos de novo.
    mtarget, mattempts = _read_marker(app)
    attempts = mattempts if mtarget == remote_ver else 0
    if attempts >= MAX_UPDATE_ATTEMPTS:
        log_erro("auto-update desistiu (quebra-loop)", None,
                 "alvo %s apos %d tentativas; seguindo na %s"
                 % (remote_ver, attempts, LAUNCHER_VERSION))
        return False
    # Baixa o zip da pasta nova.
    try:
        os.makedirs(upd, exist_ok=True)
        zpath = os.path.join(upd, "launcher.zip")
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=_SSL_CTX) as r, \
                open(zpath, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
    except Exception as e:
        log_erro("baixar launcher (zip)", e, url)
        _rmtree_quieto(upd)
        return False
    if want_crc and crc32_of(zpath) != want_crc:   # download corrompido -> ignora
        log_erro("launcher zip corrompido", None, "crc esperado " + str(want_crc))
        _rmtree_quieto(upd)
        return False
    # O launcher novo so entra se o release ASSINOU este zip (ver
    # CHAVE_PUBLICA_RELEASE): sem isto, quem trocasse o zip no servidor rodaria
    # o que quisesse na maquina de todo jogador no proximo auto-update.
    assinada = carregar_assinatura()
    lanc = (assinada or {}).get("launcher") or {}
    if not assinada or lanc.get("version") != remote_ver or lanc.get("zip_sha256") != sha256_of(zpath):
        log_erro("launcher zip sem assinatura valida", None,
                 "versao %s; assinado %s" % (remote_ver, lanc.get("version")))
        _rmtree_quieto(upd)
        return False
    # Extrai para _launcher_update\new (o zip tem exe + _internal na RAIZ).
    newdir = os.path.join(upd, "new")
    try:
        import zipfile
        with zipfile.ZipFile(zpath) as z:
            z.extractall(newdir)
    except Exception as e:
        log_erro("extrair launcher (zip)", e, zpath)
        _rmtree_quieto(upd)
        return False
    # Aberto por outro nome (instalacao da fase OTibion aberta como "OTibion.exe", que
    # o zip nao traz mais desde 01/10): sem o proprio nome no zip o launcher desistiria
    # da atualizacao para sempre. Todos os exes do zip sao o MESMO programa, entao um
    # deles vira a copia com o nosso nome e o atalho continua abrindo.
    alvo_exe = os.path.join(newdir, exe_name)
    if not os.path.isfile(alvo_exe):
        for fonte in ("OTibion.exe", "Tibia 2.exe"):
            if os.path.isfile(os.path.join(newdir, fonte)):
                try:
                    import shutil
                    shutil.copy2(os.path.join(newdir, fonte), alvo_exe)
                except Exception as e:
                    log_erro("copiar exe do zip para o nome aberto", e, fonte + " -> " + exe_name)
                break
    if not os.path.isfile(os.path.join(newdir, exe_name)) or \
       not os.path.isdir(os.path.join(newdir, "_internal")):
        log_erro("launcher zip sem exe/_internal", None, newdir)
        _rmtree_quieto(upd)
        return False
    # Conta ESTA tentativa ANTES de disparar o swapper. Se o swap falhar, a
    # proxima abertura le attempts+1 e, no teto, aciona o quebra-loop. Se der
    # certo, a versao nova abre, ve remote_ver == LAUNCHER_VERSION e zera tudo.
    _write_marker(app, remote_ver, attempts + 1)
    # Escreve o swapper (caminhos baked) e dispara destacado.
    bat = os.path.join(upd, "apply.bat")
    try:
        # O swapper troca o exe PELO NOME com que este launcher foi aberto ("Tibia 2.exe"
        # ou "OTibion.exe"). Era fixo "Tibia 2.exe": aberto pelo nome novo, o waitloop
        # nem esperaria o processo certo fechar. O .bat e ASCII, entao um nome com
        # acento (jogador renomeou o exe) cai no padrao em vez de corromper o script.
        nome_bat = exe_name if exe_name.isascii() and '"' not in exe_name else "Tibia 2.exe"
        with open(bat, "w", encoding="ascii", errors="replace") as f:
            f.write(_SWAPPER_BAT.replace("__EXE__", nome_bat))
    except Exception as e:
        log_erro("escrever swapper", e, bat)
        _rmtree_quieto(upd)
        return False
    release_mutex()                            # libera o mutex antes de relancar
    try:
        # CREATE_NO_WINDOW, e NAO DETACHED_PROCESS. Com DETACHED o cmd fica SEM
        # console; ai cada console-app que ele chama (tasklist, robocopy, ping)
        # ALOCA o proprio console -> JANELAS DE CMD PISCANDO na cara do jogador, e
        # de quebra o `tasklist | find` do waitloop nem retorna direito (o swapper
        # travava). CREATE_NO_WINDOW da um console OCULTO que os filhos herdam:
        # nenhuma janela aparece e os console-apps funcionam normalmente. O startupinfo
        # com SW_HIDE reforca (nada some, nada pisca). O processo sobrevive ao
        # os._exit do launcher (nao esta preso a nenhum console dele).
        CREATE_NO_WINDOW = 0x08000000
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                     # SW_HIDE
        subprocess.Popen(["cmd", "/c", bat], creationflags=CREATE_NO_WINDOW,
                         startupinfo=si, close_fds=True)
    except Exception as e:
        log_erro("disparar swapper", e, bat)
        return False
    return True


def single_instance_or_focus():
    """True = esta e a unica instancia (segue). False = ja havia outra: foca a
    janela dela e o chamador deve sair. Assim, se o otclient for aberto direto e
    mandar abrir o launcher que ja esta aberto, a instancia existente e focada em
    vez de abrir uma segunda."""
    global _mutex_handle
    try:
        k = ctypes.windll.kernel32
        _mutex_handle = k.CreateMutexW(None, False, "Tibia2LauncherSingleton")
        if k.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            u = ctypes.windll.user32
            hwnd = u.FindWindowW(None, "Tibia 2")
            if hwnd:
                u.ShowWindow(hwnd, 9)          # SW_RESTORE
                u.SetForegroundWindow(hwnd)
            return False
        return True
    except Exception:
        return True


def main():
    # DPI awareness ANTES do Tk: sem isto, numa tela com escala (125/150%) o
    # Windows estica a imagem do canvas e o texto sai BORRADO. Per-monitor v2 se
    # der; senao system-DPI. Tem de vir antes de qualquer janela.
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    load_martel()
    if not single_instance_or_focus():
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Tibia2.Launcher")
    except Exception:
        pass
    root = tk.Tk()
    root.title("Tibia 2")
    Launcher(root)
    root.mainloop()


if __name__ == "__main__":
    main()
