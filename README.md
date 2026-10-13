# Tibia 2 Launcher

Launcher (updater) do cliente do servidor **Tibia 2**. É um programa **separado**
do jogo: ele confere a integridade dos arquivos do cliente contra o manifesto do
servidor, baixa o que mudou e então abre o `otclient.exe`. **O login acontece
dentro do próprio cliente** — o launcher não toca em conta/senha.

O jogador baixa só o launcher (~20 MB). Na primeira execução, com a pasta vazia,
o launcher baixa o cliente inteiro (em paralelo) e abre o jogo. Nas próximas, só
baixa o que mudou.

## Download

Installer and portable build: **[Releases](https://github.com/Himanen19/tibia2-launcher/releases/latest)**.
Players normally get it from [tibia2ot.com/downloads](https://tibia2ot.com/downloads).
The files are not code-signed yet: see [Code signing](#code-signing) below.

## Por que um programa separado

O updater embutido do OTClient roda *antes* do `loadModules`, num ponto em que o
loop de render/eventos do cliente ainda não está de pé — a janela não pinta e os
timers/HTTP não disparam. Sendo um programa à parte (com seu próprio loop
`tkinter`), o launcher funciona e só lança o jogo no fim.

## Como funciona

1. Faz `POST` no `updater.php` do servidor e recebe um manifesto
   `{ url, files: { "/caminho": crc32 } }`.
2. Para cada arquivo, compara o CRC32 local com o do manifesto. CRC em hex
   minúsculo, sem zeros à esquerda (arquivo vazio = `"0"`), igual ao
   `ltrim(hash_file('crc32b'), '0')` do PHP — `zlib.crc32` casa exato.
3. Baixa em paralelo (8 simultâneos) o que estiver faltando/mudado, cada arquivo
   para um `.part` + `rename` atômico.
4. Abre o `otclient.exe` com a env `TIBIA2_LAUNCHER=1` (o cliente distribuído só
   abre pelo launcher).

Configurável por variáveis de ambiente (padrão = produção):

| Env | Padrão | Uso |
|---|---|---|
| `PANGEIA_UPDATER_URL` | `https://tibia2ot.com/updater.php` | manifesto |
| `PANGEIA_NEWS_URL` | `https://tibia2ot.com/noticias.php` | painel de notícias |
| `PANGEIA_CLIENT_ROOT` | pasta do `.exe` | onde baixar/abrir o cliente (testes) |

## Build

Requer Python 3.11+ no Windows.

```bat
pip install -r requirements.txt
python -m PyInstaller --noconfirm --distpath dist-noupx --workpath build-noupx "Tibia 2.spec"
```

Sai a pasta `dist-noupx/Tibia 2/` (`Tibia 2.exe` + `_internal/`). O build é em
**pasta** (onedir), sem UPX: o modo de um arquivo só extrai tudo para `%TEMP%` e
relança a si mesmo, o que antivírus costuma barrar. O instalador
(`Tibia 2 Setup.exe`) é feito pelo Inno Setup a partir de `installer.iss`.

O CI (`.github/workflows/build.yml`) faz o mesmo a cada tag `v*` e publica a
pasta e o instalador como artefatos. **Os binários distribuídos saem só do CI**,
nunca de uma máquina de desenvolvedor.

## Dependências

- **Runtime:** [Pillow](https://python-pillow.org/) (composição da UI). O resto é
  biblioteca padrão (`tkinter`, `ctypes`, `urllib`, ...).
- **Build:** [PyInstaller](https://pyinstaller.org/).

## Code signing

The releases are **not code-signed** at the moment, so Windows SmartScreen and some
browsers may warn about the download. The executables are built from this repository
with the steps under [Build](#build), so anyone can read the source and build the same
program.

## Privacy policy

The launcher never asks for, reads or sends account names, passwords, e-mail
addresses or any file outside its own installation folder. Login happens inside
the game client, not in the launcher.

It talks only to `tibia2ot.com`, over HTTPS:

- **Update check and download** — requests the file manifest and its Ed25519
  signature, then downloads the client files that are missing or changed. This
  is the launcher's purpose and runs on every start.
- **Launcher content** — fetches the news panel, the featured streams/videos
  and the online player count shown in the window.
- **Error report** — if an update or download fails, sends one anonymous line
  with the error message, the launcher version and the Windows version
  (for example `Windows-11-10.0.26200`). No account, character or hardware
  identifier is included.
- **Click counter** — when you click a partner stream or video card, sends the
  network and channel name of that card so partners can be credited. Nothing
  about you is included.

As with any web request, the server sees the IP address of the connection. Links
(Discord, Instagram, YouTube, Twitch, the store) open in your default browser
only when you click them.

The launcher changes nothing in the system configuration. It writes only inside
its own folder: the downloaded client, an integrity cache and a log file
(`tibia2-launcher.log`).

## Uninstall

Use **Tibia 2** in *Settings → Apps → Installed apps* (or the *Uninstall Tibia 2*
shortcut in the Start menu). The uninstaller removes the launcher, the downloaded
client and the shortcuts. If you only copied the folder, deleting it removes
everything.

## Licença

Código sob **MIT** (ver [LICENSE](LICENSE)). A marca "Tibia 2", o escudo e o logo
são artwork do projeto e **não** entram na MIT. Fonte e ícones de terceiros
mantêm suas licenças — ver [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
