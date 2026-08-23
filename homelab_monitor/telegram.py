"""Tiny Telegram Bot API client (stdlib only)."""

import html
import json
import logging
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("telegram")


def escape(text):
    return html.escape(str(text), quote=False)


class Telegram:
    def __init__(self, token):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.offset = 0

    def call(self, method, http_timeout=35, **params):
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        request = urllib.request.Request(self.base + method, data=data)
        with urllib.request.urlopen(request, timeout=http_timeout) as response:
            payload = json.loads(response.read().decode())
        if not payload.get("ok"):
            raise RuntimeError(f"Telegram {method}: {payload.get('description')}")
        return payload["result"]

    def get_me(self):
        return self.call("getMe", http_timeout=15)

    def get_updates(self, timeout=30):
        updates = self.call(
            "getUpdates",
            http_timeout=timeout + 10,
            offset=self.offset,
            timeout=timeout,
            allowed_updates=json.dumps(["message"]),
        )
        if updates:
            self.offset = updates[-1]["update_id"] + 1
        return updates

    def send(self, chat_id, text):
        """Send HTML-formatted text, splitting messages over Telegram's 4096 limit."""
        chunks = _split(text, 4000)
        result = None
        for chunk in chunks:
            try:
                result = self.call(
                    "sendMessage",
                    http_timeout=15,
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode="HTML",
                    disable_web_page_preview="true",
                )
            except RuntimeError as error:
                # Fall back to plain text if our HTML did not parse.
                log.warning("sendMessage HTML failed (%s), retrying as plain text", error)
                result = self.call("sendMessage", http_timeout=15, chat_id=chat_id, text=chunk)
        return result

    def delete_message(self, chat_id, message_id):
        try:
            self.call("deleteMessage", http_timeout=10, chat_id=chat_id, message_id=message_id)
            return True
        except (RuntimeError, urllib.error.URLError):
            return False

    def set_commands(self, commands):
        payload = json.dumps([{"command": name, "description": desc} for name, desc in commands])
        try:
            self.call("setMyCommands", http_timeout=15, commands=payload)
        except (RuntimeError, urllib.error.URLError) as error:
            log.warning("setMyCommands failed: %s", error)


def _split(text, limit):
    if len(text) <= limit:
        return [text]
    chunks = []
    current = ""
    for line in text.split("\n"):
        if len(current) + len(line) + 1 > limit and current:
            chunks.append(current)
            current = ""
        current += line + "\n"
    if current:
        chunks.append(current)
    return chunks
