#!/bin/zsh
# Prepara a Base de Leads nesta máquina (macOS, Apple Silicon). Pode rodar de novo sem problema.
set -e
cd "$(dirname "$0")"
VERSAO=1.21.0
ARQ=google_maps_scraper-$VERSAO-darwin-arm64

# 1. scraper gosom/google-maps-scraper, conferindo o checksum
if [ ! -x bin/gmaps ]; then
  mkdir -p bin
  URL=https://github.com/gosom/google-maps-scraper/releases/download/v$VERSAO
  echo "Baixando o scraper v$VERSAO..."
  curl -fsSL -o bin/$ARQ $URL/$ARQ
  curl -fsSL -o bin/checksums.txt $URL/checksums.txt
  (cd bin && shasum -a 256 -c checksums.txt --ignore-missing)
  mv bin/$ARQ bin/gmaps && chmod +x bin/gmaps && xattr -c bin/gmaps
fi

# 2. o Chromium que o scraper baixa trava ao rolar a lista do Maps; usamos o Google Chrome do Mac
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
if [ -x "$CHROME" ] && [ ! -d browsers ]; then
  CACHE=~/Library/Caches/ms-playwright
  D=browsers/chromium_headless_shell-1248
  mkdir -p $D/chrome-headless-shell-mac-arm64
  touch $D/INSTALLATION_COMPLETE $D/DEPENDENCIES_VALIDATED
  for x in chromium-1248 ffmpeg-1013; do [ -e $CACHE/$x ] && ln -s $CACHE/$x browsers/$x; done
  cat > $D/chrome-headless-shell-mac-arm64/chrome-headless-shell <<'W'
#!/bin/sh
# O chrome-headless-shell 156 (Playwright 1248) quebra (SIGTRAP no decodificador de PNG) ao rolar o Google Maps.
# Usamos o Google Chrome instalado no Mac, em modo sem tela, com perfil temporário criado pelo Playwright.
exec "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" "$@"
W
  chmod +x $D/chrome-headless-shell-mac-arm64/chrome-headless-shell
elif [ ! -x "$CHROME" ]; then
  echo "Aviso: Google Chrome não encontrado. Buscas em 'Normal' e 'Máximo' podem falhar."
fi

# 3. dependências Python
uv sync -q
echo "Pronto. Rode ./iniciar.sh"
