#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tibia 2 - Launcher (programa SEPARADO do jogo).

Confere a integridade dos arquivos do cliente contra o manifesto do servidor
(o mesmo updater.php que o cliente ja usava), baixa o que mudou e abre o
otclient.exe. O LOGIN acontece no proprio cliente, depois de aberto.

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
import queue
import ctypes
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

from PIL import Image, ImageDraw, ImageFont, ImageTk

# ---------------------------------------------------------------------------
# Config. Producao por padrao; para testar local use as envs (test-local.bat).
# LINKS: placeholders - o dono troca depois.
# ---------------------------------------------------------------------------
UPDATER_URL = os.environ.get("PANGEIA_UPDATER_URL", "https://tibia2ot.com/updater.php")
NEWS_URL = os.environ.get("PANGEIA_NEWS_URL", "https://tibia2ot.com/noticias.php")
# Info do proprio launcher (crc + url do exe) para o auto-update.
LAUNCHER_INFO_URL = os.environ.get("PANGEIA_LAUNCHER_INFO",
                                   "https://tibia2ot.com/templates/tibia2/launcher.json")
CLIENT_EXE = "otclient.exe"
HTTP_TIMEOUT = 30
LINKS = {
    "discord": "https://discord.gg/TZW5pzWkSE",
    "instagram": "https://instagram.com/",
    "site": "https://tibia2ot.com/",
    "youtube": "https://youtube.com/",
    "coins": "https://tibia2ot.com/",  # loja de T2 Coins - trocar
}

W, H = 960, 560
GOLD = (232, 184, 62); GOLDL = (255, 217, 106); GOLDD = (169, 130, 42)
TEXT = (243, 238, 220); GROUND = (8, 13, 18); PANEL = (10, 18, 30)
GOLD_HEX = "#e8c886"; GOLDL_HEX = "#ffd96a"; TEXT_HEX = "#f3eedc"
DIM_HEX = "#7c7768"; PANEL_HEX = "#0a1220"

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


def pil_font(sz, serif=True):
    try:
        return ImageFont.truetype(asset("martel.ttf") if serif else "C:/Windows/Fonts/segoeuib.ttf", sz)
    except Exception:
        return ImageFont.load_default()


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
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def http_get_json(url):
    with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r:
        return json.loads(r.read().decode("utf-8"))


def rel_to_local(root, rel):
    return os.path.join(root, *rel.lstrip("/").split("/"))


# ---------------------------------------------------------------------------
# Fundo composto (tudo que e ESTATICO: arte, moldura, paineis, escudo, titulo,
# cabecalho NOTICIAS, badges sociais, trilho da barra, moldura do botao).
# ---------------------------------------------------------------------------

# geometria (compartilhada entre o bake e as regioes de clique)
SCR = (18, 62, 332, 410)                 # screenshot: x, y, w, h
NEWS = (364, 62, 486, 410)               # painel noticias
NEWS_BODY = (380, 120, 454, 344)         # area da Text de noticias: x,y,w,h
SIDEBAR_CX = W - 44
SIDEBAR_Y0, SIDEBAR_STEP, SIDEBAR_R = 62, 84, 20
SOCIAL_ORDER = [("discord", "Discord"), ("instagram", "Instagram"),
                ("site", "Site"), ("youtube", "Youtube"), ("coins", "T2 Coins")]
PROG = (210, H - 64, 448, 28)            # trilho mais alto p/ caber o % GRANDE dentro
# Detalhes (contagem, MB/s, tempo) numa linha CENTRALIZADA logo abaixo da barra;
# o % fica GRANDE dentro da propria barra.
STATUS_XY = (210 + 448 // 2, H - 30)
PLAY = (W - 236, H - 78, 206, 56)        # botao: x, y, w, h
CLOSE_C = (W - 32, 25, 11)               # cx, cy, r
MIN_C = (W - 62, 25, 11)


def build_static_bg():
    img = Image.new("RGBA", (W, H), GROUND + (255,))
    d = ImageDraw.Draw(img)
    # arte de fundo (cover) escurecida
    art = Image.open(asset("background.webp")).convert("RGBA")
    ar = art.width / art.height; tr = W / H
    nh, nw = (H, int(H * ar)) if ar > tr else (int(W / ar), W)
    art = art.resize((nw, nh)).crop(((nw - W) // 2, (nh - H) // 2, (nw - W) // 2 + W, (nh - H) // 2 + H))
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0)); od = ImageDraw.Draw(ov)
    for y in range(H):
        a = int(150 + 95 * (y / H)); od.line([(0, y), (W, y)], fill=(8, 14, 22, min(242, a)))
    img.alpha_composite(Image.alpha_composite(art.convert("RGBA"), ov))
    d.rectangle([0, 0, W - 1, H - 1], outline=GOLD, width=2)
    d.rectangle([0, 0, W, 50], fill=(6, 11, 17, 242))

    shield = Image.open(asset("shield.png")).convert("RGBA")
    s1 = shield.copy(); s1.thumbnail((40, 40)); img.alpha_composite(s1, (14, 5))
    d.text((58, 13), "TIBIA 2", font=pil_font(22), fill=GOLDL)

    # min / close
    for (cx, cy, r), ch in [(MIN_C, "-"), (CLOSE_C, "x")]:
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(18, 40, 64, 255), outline=GOLD)
        d.text((cx - 4, cy - 9), ch, font=pil_font(13, False), fill=GOLDL)

    # screenshot esquerda
    sx, sy, sw, sh = SCR
    scr = art.crop((30, 60, 30 + sw, 60 + sh)); img.paste(scr.convert("RGB"), (sx, sy))
    d.rounded_rectangle([sx - 1, sy - 1, sx + sw, sy + sh], radius=10, outline=GOLD, width=2)

    # painel noticias (corpo OPACO, pra a Text por cima blendar)
    nx, ny, nw2, nh2 = NEWS
    d.rounded_rectangle([nx, ny, nx + nw2, ny + nh2], radius=10, fill=PANEL_HEX, outline=GOLDD, width=1)
    d.rounded_rectangle([nx + 6, ny + 6, nx + nw2 - 6, ny + 42], radius=7, fill=GOLD)
    hw = d.textbbox((0, 0), "NOTICIAS", font=pil_font(17))[2]
    d.text((nx + nw2 // 2 - hw / 2, ny + 13), "NOTICIAS", font=pil_font(17), fill=(20, 20, 26))

    # sidebar social (visual; clique e binding)
    y = SIDEBAR_Y0
    for icon, label in SOCIAL_ORDER:
        cx, r = SIDEBAR_CX, SIDEBAR_R
        d.ellipse([cx - r, y - 1, cx + r, y - 1 + 2 * r], fill=(10, 16, 26, 255), outline=GOLD, width=2)
        ic = Image.open(os.path.join(SOCIAL, icon + ".png")).convert("RGBA"); ic.thumbnail((23, 23))
        img.alpha_composite(ic, (cx - ic.width // 2, y - 1 + r - ic.height // 2))
        lw = d.textbbox((0, 0), label, font=pil_font(11, False))[2]
        d.text((cx - lw / 2, y + 40), label, font=pil_font(11, False),
               fill=GOLDL if icon == "coins" else TEXT)
        y += SIDEBAR_STEP

    # escudo rodape
    s2 = shield.copy(); s2.thumbnail((72, 72)); img.alpha_composite(s2, (16, H - 84))

    # trilho da barra
    px, py, pw, ph = PROG
    d.rounded_rectangle([px, py, px + pw, py + ph], radius=ph // 2, fill=(10, 18, 28), outline=GOLDD, width=1)

    # moldura do botao ABRIR JOGO (texto e canvas, pra mudar de cor)
    bx, by, bw, bh = PLAY
    d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=bh // 2, fill=(14, 22, 34), outline=GOLD, width=3)

    return img


class Launcher:
    def __init__(self, tk_root):
        self.tk = tk_root
        self.root = client_root()
        self.q = queue.Queue()
        self.ready = False
        self.launched = False
        self._drag = None
        self._build_ui()
        self.tk.after(80, self._drain)
        threading.Thread(target=self._work, daemon=True).start()

    # ---- UI -------------------------------------------------------------
    def _build_ui(self):
        self.tk.overrideredirect(True)
        sw, sh = self.tk.winfo_screenwidth(), self.tk.winfo_screenheight()
        self.tk.geometry(f"{W}x{H}+{(sw - W) // 2}+{(sh - H) // 3}")
        try:
            self.tk.iconphoto(True, ImageTk.PhotoImage(Image.open(asset("shield.png"))))
        except Exception:
            pass

        self.canvas = tk.Canvas(self.tk, width=W, height=H, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.bg = ImageTk.PhotoImage(build_static_bg())
        self.canvas.create_image(0, 0, anchor="nw", image=self.bg)

        # barra de progresso (preenchimento)
        px, py, pw, ph = PROG
        self.prog_fill = self.canvas.create_rectangle(px + 2, py + 2, px + 2, py + ph - 2,
                                                       fill=GOLDL_HEX, outline="")
        # % GRANDE dentro da barra. Contorno claro (copias deslocadas) + nucleo
        # escuro: le sobre o dourado (nucleo escuro) E sobre o trilho escuro
        # (contorno claro). O nucleo (0,0) entra por ultimo p/ ficar por cima.
        # Fonte com DIGITOS: a Martel do tema e um subset sem numeros/"%", entao o
        # % e os detalhes (que sao numericos) usam Segoe UI.
        NUMF = "Segoe UI"
        cx, cy = px + pw // 2, py + ph // 2
        self.pct_items = []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                if (dx, dy) != (0, 0):        # contorno escuro (8 vizinhos)
                    self.pct_items.append(
                        self.canvas.create_text(cx + dx, cy + dy, text="",
                                                fill="#120c06", font=(NUMF, 15, "bold")))
        # nucleo claro por cima: le sobre o trilho escuro; o contorno escuro dá
        # contraste quando o % fica sobre o dourado.
        self.pct_items.append(
            self.canvas.create_text(cx, cy, text="", fill="#fff4d8",
                                    font=(NUMF, 15, "bold")))
        self.status = self.canvas.create_text(*STATUS_XY, text="Verificando arquivos...",
                                              anchor="n", fill=TEXT_HEX, font=(NUMF, 10, "bold"))
        bx, by, bw, bh = PLAY
        self.play_txt = self.canvas.create_text(bx + bw // 2, by + bh // 2, text="ABRIR JOGO",
                                                fill=DIM_HEX, font=(FONT_FAMILY, 15, "bold"))

        # noticias (Text sobre o corpo opaco do painel)
        nbx, nby, nbw, nbh = NEWS_BODY
        self.news = tk.Text(self.tk, bg=PANEL_HEX, fg=TEXT_HEX, relief="flat", wrap="word",
                            font=("Segoe UI", 9), padx=6, pady=4, highlightthickness=0,
                            cursor="arrow")
        self.news.place(x=nbx, y=nby, width=nbw, height=nbh)
        self.news.tag_configure("date", foreground=GOLDL_HEX, font=("Segoe UI", 10, "bold"))
        self.news.insert("1.0", "Carregando novidades...")
        self.news.configure(state="disabled")

        # interacao
        self.canvas.bind("<Button-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_move)

        # Janela sem borda (overrideredirect) NAO cria botao na barra de tarefas.
        # Forca o botao com WS_EX_APPWINDOW e reexibe pra aplicar.
        self.tk.after(30, self._taskbar_button)

    def _taskbar_button(self):
        try:
            GWL_EXSTYLE, WS_EX_APPWINDOW, WS_EX_TOOLWINDOW = -20, 0x40000, 0x80
            u = ctypes.windll.user32
            self.tk.update_idletasks()
            hwnd = u.GetParent(self.tk.winfo_id()) or self.tk.winfo_id()
            st = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, (st & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW)
            self.tk.withdraw()
            self.tk.after(10, self.tk.deiconify)
        except Exception:
            pass

    # ---- regioes clicaveis ---------------------------------------------
    def _hit(self, x, y):
        for (cx, cy, r), act in [(CLOSE_C, "close"), (MIN_C, "min")]:
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                return act
        bx, by, bw, bh = PLAY
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return "play"
        yb = SIDEBAR_Y0
        for icon, _ in SOCIAL_ORDER:
            cx, r = SIDEBAR_CX, SIDEBAR_R
            cy = yb - 1 + r
            if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                return "link:" + icon
            yb += SIDEBAR_STEP
        return None

    def _on_move(self, e):
        self.canvas.config(cursor="hand2" if self._hit(e.x, e.y) else "arrow")

    def _on_press(self, e):
        self._pressed = self._hit(e.x, e.y)
        if not self._pressed and e.y <= 50:      # arrastar pela top bar
            self._drag = (e.x_root - self.tk.winfo_x(), e.y_root - self.tk.winfo_y())
        else:
            self._drag = None

    def _on_drag(self, e):
        if self._drag:
            self.tk.geometry(f"+{e.x_root - self._drag[0]}+{e.y_root - self._drag[1]}")

    def _on_release(self, e):
        self._drag = None
        act = self._hit(e.x, e.y)
        if act and act == getattr(self, "_pressed", None):
            if act == "close":
                self.tk.destroy()
            elif act == "min":
                self.tk.overrideredirect(False); self.tk.iconify()
            elif act == "play":
                self._play()
            elif act.startswith("link:"):
                self._open(LINKS.get(act[5:], "https://tibia2ot.com/"))

    def _open(self, url):
        try:
            webbrowser.open(url)
        except Exception:
            pass

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
            self.q.put(("status", "Nao foi possivel checar atualizacoes."))
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
            if i % 1000 == 0:
                self.q.put(("progress", 100.0 * i / total))

        # 2) Hash SO do que o cache nao cobriu, em PARALELO (sobrepoe o custo por
        #    arquivo). Quem passa vira entrada de cache; quem falha, vai baixar.
        if to_hash:
            self.q.put(("status", "Conferindo %d arquivos..." % len(to_hash)))
            hp = {"n": 0}

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
                    hp["n"] += 1
                    k = hp["n"]
                if k % 200 == 0:
                    self.q.put(("progress", 100.0 * k / len(to_hash)))

            with ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(confere, to_hash))

        if not to_update:
            self._save_cache(cache)
            self.q.put(("progress", 100)); self.q.put(("status", "Cliente atualizado. Boa cacada!"))
            self.q.put(("ready", "ABRIR JOGO")); return

        # Download PARALELO: primeiro install sao 20k+ arquivos; sequencial seria
        # lento demais (latencia por requisicao). 8 ao mesmo tempo esconde isso.
        # Cada arquivo baixado ja entra no cache (sem rehash na proxima abertura).
        n = len(to_update)
        state = {"done": 0, "err": None, "bytes": 0}
        t0 = time.time()

        def baixa(item):
            rel, want = item
            if state["err"]:
                return
            sz = 0
            try:
                self._download_one(base, rel)
                st = os.stat(rel_to_local(self.root, rel))
                sz = st.st_size
                with clock:
                    cache[rel] = [st.st_size, st.st_mtime_ns, want]
            except Exception:
                state["err"] = rel.lstrip('/')
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

        with ThreadPoolExecutor(max_workers=8) as ex:
            list(ex.map(baixa, to_update))

        if state["err"]:
            self._save_cache(cache)
            self.q.put(("status", "Falha ao baixar %s" % state["err"]))
            self.q.put(("ready", "TENTAR DE NOVO")); return
        self._save_cache(cache)
        self.q.put(("progress", 100)); self.q.put(("status", "Cliente pronto. Boa cacada!"))
        self.q.put(("ready", "ABRIR JOGO"))

    def _download_one(self, base, rel):
        pr = urllib.parse.urlparse(base)
        path = pr.path.rstrip("/") + "/" + urllib.parse.quote(rel.lstrip("/"))
        local = rel_to_local(self.root, rel)
        os.makedirs(os.path.dirname(local), exist_ok=True)
        tmp = local + ".part"
        # Conexao persistente por thread + 1 reconexao. O Apache fecha a conexao a
        # cada ~100 requisicoes (MaxKeepAliveRequests); quando isso acontece a
        # proxima request falha, entao reabrimos e tentamos de novo.
        for tentativa in (1, 2):
            conn = getattr(_conns, "c", None)
            if conn is None:
                conn = _conns.c = http.client.HTTPSConnection(pr.netloc, timeout=HTTP_TIMEOUT)
            try:
                conn.request("GET", path)
                r = conn.getresponse()
                if r.status != 200:
                    r.read()
                    raise IOError("HTTP %d em %s" % (r.status, rel))
                with open(tmp, "wb") as f:
                    for chunk in iter(lambda: r.read(1 << 20), b""):
                        f.write(chunk)
                os.replace(tmp, local)
                return
            except (http.client.HTTPException, OSError):
                try:
                    conn.close()
                except Exception:
                    pass
                _conns.c = None
                if tentativa == 2:
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

    # ---- ponte thread -> UI --------------------------------------------
    def _drain(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "status":
                    self.canvas.itemconfig(self.status, text=val)
                elif kind == "progress":
                    self._set_progress(val)
                elif kind == "dlstat":
                    dn, n_, spd, eta = val
                    self.canvas.itemconfig(
                        self.status,
                        text="%d/%d       ·       %.1f MB/s       ·       %s"
                             % (dn, n_, spd, self._fmt_eta(eta)))
                elif kind == "news":
                    self._show_news(val)
                elif kind == "ready":
                    self.ready = True
                    self._set_progress(100)
                    self.canvas.itemconfig(self.play_txt, fill=GOLDL_HEX, text=val)
        except queue.Empty:
            pass
        self.tk.after(80, self._drain)

    def _set_progress(self, pct):
        px, py, pw, ph = PROG
        x = px + 2 + int((pw - 4) * max(0, min(100, pct)) / 100.0)
        self.canvas.coords(self.prog_fill, px + 2, py + 2, x, py + ph - 2)
        self._set_pct("%d%%" % int(pct))

    def _set_pct(self, text):
        for it in self.pct_items:
            self.canvas.itemconfig(it, text=text)

    @staticmethod
    def _fmt_eta(s):
        if s >= 90:
            return "faltam ~%d min" % round(s / 60.0)
        if s >= 3:
            return "faltam ~%ds" % int(s)
        return "quase la"

    def _show_news(self, lista):
        self.news.configure(state="normal")
        self.news.delete("1.0", "end")
        if not lista:
            self.news.insert("end", "Sem novidades no momento.")
        else:
            for it in lista:
                if it.get("data"):
                    self.news.insert("end", it["data"] + "\n", "date")
                if it.get("titulo"):
                    self.news.insert("end", it["titulo"] + "\n", "date")
                if it.get("texto"):
                    self.news.insert("end", it["texto"] + "\n\n")
        self.news.configure(state="disabled")

    # ---- jogar ----------------------------------------------------------
    def _play(self):
        if self.launched or not self.ready:
            return
        exe = os.path.join(self.root, CLIENT_EXE)
        if not os.path.exists(exe):
            self.canvas.itemconfig(self.status, text="otclient.exe nao encontrado.")
            return
        self.launched = True
        try:
            # marcador: o client so abre se veio do launcher (bloqueia abrir o
            # otclient.exe direto). O client confere os.getenv('TIBIA2_LAUNCHER').
            env = os.environ.copy()
            env["TIBIA2_LAUNCHER"] = "1"
            subprocess.Popen([exe], cwd=self.root, env=env)
        except Exception:
            self.launched = False
            self.canvas.itemconfig(self.status, text="Nao foi possivel abrir o jogo.")
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


def self_update():
    """Se o proprio Tibia 2.exe estiver desatualizado (crc != o do servidor em
    launcher.json), baixa o novo, troca e relanca. Retorna True se relancou (o
    chamador deve sair). Nao da pra sobrescrever um exe em uso, entao usamos o
    'rename dance': renomeia o exe em execucao (permitido no Windows) e poe o novo
    no lugar. Tolerante a falha: qualquer erro -> segue com a versao atual."""
    if not getattr(sys, "frozen", False):
        return False
    exe = sys.executable
    d = os.path.dirname(exe)
    old = os.path.join(d, "Tibia 2.old.exe")
    new = os.path.join(d, "Tibia 2.new.exe")
    for junk in (old, new):                    # limpa sobras de update anterior
        try:
            if os.path.exists(junk):
                os.remove(junk)
        except OSError:
            pass
    try:
        info = http_get_json(LAUNCHER_INFO_URL)
        want = (info or {}).get("crc")
        url = (info or {}).get("url")
    except Exception:
        return False
    if not want or not url or crc32_of(exe) == want:
        return False                           # sem info ou ja atualizado
    try:
        with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT) as r, open(new, "wb") as f:
            for chunk in iter(lambda: r.read(1 << 20), b""):
                f.write(chunk)
    except Exception:
        try:
            os.remove(new)
        except OSError:
            pass
        return False
    if crc32_of(new) != want:                  # download corrompido -> ignora
        try:
            os.remove(new)
        except OSError:
            pass
        return False
    try:
        os.rename(exe, old)                     # renomear exe EM USO: ok no Windows
        os.rename(new, exe)
    except OSError:
        try:                                    # desfaz se der ruim no meio
            if not os.path.exists(exe) and os.path.exists(old):
                os.rename(old, exe)
        except OSError:
            pass
        try:
            if os.path.exists(new):
                os.remove(new)
        except OSError:
            pass
        return False
    release_mutex()                             # libera antes de relancar
    try:
        subprocess.Popen([exe], cwd=d, env=os.environ.copy())
    except Exception:
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
