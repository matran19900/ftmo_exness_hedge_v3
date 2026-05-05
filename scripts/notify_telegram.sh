#!/usr/bin/env bash
# Send a message to Telegram, optionally with a report file attached.
# Usage:
#   notify_telegram.sh "caption text"                          # text-only
#   notify_telegram.sh "caption text" /path/to/report.md       # text + file
#
# Caption is shown in notification preview (max 1024 chars for sendDocument).
# File has no practical size limit (Telegram allows up to 50MB).

set -euo pipefail

ENV_FILE="$HOME/.config/hedger-sandbox/telegram.env"

if [ ! -f "$ENV_FILE" ]; then
  echo "[notify_telegram] no env file at $ENV_FILE, skipping" >&2
  exit 0
fi

# shellcheck disable=SC1090
source "$ENV_FILE"

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
  echo "[notify_telegram] missing token or chat_id, skipping" >&2
  exit 0
fi

MESSAGE="${1:-(no message)}"
REPORT_FILE="${2:-}"

API_BASE="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"

send_text_only() {
  local text="$1"
  # Truncate if Telegram 4096 char limit risk
  if [ ${#text} -gt 3900 ]; then
    text="${text:0:3800}

... (truncated, $((${#text} - 3800)) chars omitted)"
  fi

  curl -s -o /dev/null -w "[notify_telegram] sendMessage HTTP %{http_code}\n" \
    "${API_BASE}/sendMessage" \
    -d "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=${text}"
}

send_with_file() {
  local caption="$1"
  local file="$2"

  # Caption max 1024 chars for sendDocument
  if [ ${#caption} -gt 1000 ]; then
    caption="${caption:0:980}... (truncated)"
  fi

  curl -s -o /dev/null -w "[notify_telegram] sendDocument HTTP %{http_code}\n" \
    "${API_BASE}/sendDocument" \
    -F "chat_id=${TELEGRAM_CHAT_ID}" \
    -F "caption=${caption}" \
    -F "document=@${file}"
}

# Decide path
if [ -n "$REPORT_FILE" ]; then
  if [ -f "$REPORT_FILE" ] && [ -r "$REPORT_FILE" ]; then
    send_with_file "$MESSAGE" "$REPORT_FILE"
  else
    echo "[notify_telegram] report file not found or unreadable: $REPORT_FILE" >&2
    send_text_only "$MESSAGE

⚠️ Report file missing: $REPORT_FILE"
  fi
else
  send_text_only "$MESSAGE"
fi