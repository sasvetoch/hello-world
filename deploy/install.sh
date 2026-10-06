#!/usr/bin/env bash
# Установка и обновление цифрового двойника на сервере с Ubuntu или Debian.
#
#   curl -fsSL https://raw.githubusercontent.com/sasvetoch/hello-world/claude/telegram-digital-twin-bot-xzcrax/deploy/install.sh -o install.sh
#   sudo bash install.sh
#
# Повторный запуск обновляет код и перезапускает бота; ключи и данные сохраняются.
set -euo pipefail

REPO="${REPO:-https://github.com/sasvetoch/hello-world.git}"
BRANCH="${BRANCH:-claude/telegram-digital-twin-bot-xzcrax}"
APP_USER="twin"
APP_DIR="/opt/twin"
SERVICE="twin"

say()  { printf '\n\033[1;34m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
TTY="${TTY:-/dev/tty}"  # ответы читаются с терминала, даже если скрипт пришёл через конвейер
ask()  { local var; read -r -p "$1" var <"$TTY"; printf '%s' "$var"; }
ask_secret() { local var; read -r -s -p "$1" var <"$TTY"; echo >&2; printf '%s' "$var"; }

if [[ $EUID -ne 0 ]]; then
  echo "Запустите через sudo: sudo bash install.sh"
  exit 1
fi

say "1/5 Устанавливаю системные пакеты"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git python3 python3-venv curl >/dev/null

if ! id "$APP_USER" &>/dev/null; then
  useradd --system --create-home --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

say "2/5 Скачиваю код"
if [[ -d "$APP_DIR/app/.git" ]]; then
  sudo -u "$APP_USER" git -C "$APP_DIR/app" fetch -q origin "$BRANCH"
  sudo -u "$APP_USER" git -C "$APP_DIR/app" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
  sudo -u "$APP_USER" git clone -q --branch "$BRANCH" "$REPO" "$APP_DIR/app"
fi
cd "$APP_DIR/app"

say "3/5 Устанавливаю зависимости"
sudo -u "$APP_USER" python3 -m venv .venv
sudo -u "$APP_USER" .venv/bin/pip install -q --upgrade pip
sudo -u "$APP_USER" .venv/bin/pip install -q -r requirements.txt

say "4/5 Настройки"
if [[ ! -f .env ]]; then
  echo "Сейчас нужно ввести три значения. Токен и ключ при вводе не отображаются — это нормально."
  while true; do
    TOKEN=$(ask_secret "Токен бота от @BotFather: ")
    if curl -fsS "https://api.telegram.org/bot${TOKEN}/getMe" | grep -q '"ok":true'; then
      echo "Токен подходит."
      break
    fi
    warn "Telegram не принял токен. Скопируйте его из @BotFather целиком и попробуйте ещё раз."
  done
  while true; do
    OWNER_ID=$(ask "Ваш числовой id (покажет @userinfobot): ")
    [[ "$OWNER_ID" =~ ^[0-9]+$ ]] && break
    warn "Нужны только цифры, например 123456789."
  done
  while true; do
    API_KEY=$(ask_secret "Ключ Anthropic (начинается с sk-ant-): ")
    CODE=$(curl -s -o /dev/null -w '%{http_code}' https://api.anthropic.com/v1/models \
      -H "x-api-key: ${API_KEY}" -H "anthropic-version: 2023-06-01" || true)
    case "$CODE" in
      200) echo "Ключ подходит."; break ;;
      401) warn "Anthropic не принял ключ. Проверьте, что скопировали его целиком." ;;
      403) warn "Anthropic отказал в доступе с этого сервера. Скорее всего, сервер находится в стране,"
           warn "где API недоступен. Нужен сервер, например, в Германии, Нидерландах или Финляндии."
           exit 1 ;;
      *)   warn "Не удалось проверить ключ (код ${CODE}). Сохраняю как есть."; break ;;
    esac
  done
  umask 077
  cat > .env <<EOF
TG_BOT_TOKEN=${TOKEN}
TG_OWNER_ID=${OWNER_ID}
ANTHROPIC_API_KEY=${API_KEY}
EOF
  chown "$APP_USER:$APP_USER" .env
  chmod 600 .env
  umask 022
else
  echo "Файл .env уже есть — оставляю."
fi

if [[ ! -f config.yaml ]]; then
  OWNER_NAME=$(ask "Как вас зовут (так двойник будет говорить о вас, например «Светлана»): ")
  sed -e "s/^owner_name: .*/owner_name: ${OWNER_NAME//\//\\/}/" \
      -e 's/^stranger_reply: .*/stranger_reply: "Привет! Я цифровой двойник. Сейчас уточню, можно ли нам пообщаться."/' \
      config.example.yaml > config.yaml
  chown "$APP_USER:$APP_USER" config.yaml
fi
[[ -f persona.md ]] || sudo -u "$APP_USER" cp persona.example.md persona.md
[[ -d knowledge ]] || sudo -u "$APP_USER" cp -r knowledge.example knowledge

say "5/5 Запускаю бота"
cat > "/etc/systemd/system/${SERVICE}.service" <<EOF
[Unit]
Description=Telegram digital twin bot
After=network-online.target
Wants=network-online.target

[Service]
User=${APP_USER}
WorkingDirectory=${APP_DIR}/app
ExecStart=${APP_DIR}/app/.venv/bin/python -m twin
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable -q "$SERVICE"
systemctl restart "$SERVICE"
sleep 5

if systemctl is-active -q "$SERVICE"; then
  say "Готово! Бот работает."
  echo "Откройте чат с ботом в Telegram и нажмите «Старт» — он пришлёт список команд."
  echo
  echo "Полезные команды на сервере:"
  echo "  sudo journalctl -u ${SERVICE} -f     — смотреть журнал (выход: Ctrl+C)"
  echo "  sudo systemctl restart ${SERVICE}    — перезапустить"
  echo "  sudo bash install.sh            — обновить до последней версии"
else
  warn "Бот не запустился. Последние строки журнала:"
  journalctl -u "$SERVICE" -n 30 --no-pager
  exit 1
fi
