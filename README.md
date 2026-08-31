# Tibia 2 Launcher

Launcher (updater) do cliente do servidor **Tibia 2**. É um programa **separado**
do jogo: ele confere a integridade dos arquivos do cliente contra o manifesto do
servidor, baixa o que mudou e então abre o `otclient.exe`. **O login acontece
dentro do próprio cliente** — o launcher não toca em conta/senha.

O jogador baixa só o launcher (~20 MB). Na primeira execução, com a pasta vazia,
o launcher baixa o cliente inteiro (em paralelo) e abre o jogo. Nas próximas, só
baixa o que mudou.

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
pyinstaller --onefile --noconsole --name "Tibia 2" --icon "assets/shield.ico" --add-data "assets;assets" launcher.py
```

Sai `dist/Tibia 2.exe`. O CI (`.github/workflows/build.yml`) faz o mesmo a cada
tag `v*` e publica o `.exe` como artefato.

## Dependências

- **Runtime:** [Pillow](https://python-pillow.org/) (composição da UI). O resto é
  biblioteca padrão (`tkinter`, `ctypes`, `urllib`, ...).
- **Build:** [PyInstaller](https://pyinstaller.org/).

## Licença

Código sob **MIT** (ver [LICENSE](LICENSE)). A marca "Tibia 2", o escudo e o logo
são artwork do projeto e **não** entram na MIT. Fonte e ícones de terceiros
mantêm suas licenças — ver [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
