"""FunPay messaging adapter (task spec, sections 8, 10, 18).

Wraps ``cardinal.send_message`` and adds:

* ``send_once`` - idempotent, anti-spam sending keyed by an arbitrary string
  (persisted in the DB, so it survives restarts - section 10);
* ``notify_admin`` - pushes a message to the FPC Telegram admin(s) (section 18).

Kept import-clean: no FunPayCardinal import at module load. ``cardinal`` and its
Telegram bot are duck-typed, so the service layer and tests can inject fakes.
"""

from __future__ import annotations

from ..utils.logger import get_logger

log = get_logger("messenger")


class FunPayMessenger:
    def __init__(self, cardinal, config, repo):
        self.cardinal = cardinal
        self.cfg = config
        self.repo = repo

    def send(self, chat_id, text: str) -> bool:
        if not chat_id:
            log.warning("send: no chat_id, message dropped")
            return False
        try:
            self.cardinal.send_message(chat_id, text)
            return True
        except Exception:
            log.exception("Failed to send message to chat %s", chat_id)
            return False

    def send_once(self, key: str, chat_id, text: str) -> bool:
        """Send only if ``key`` was never used before. Returns True if sent."""
        first_time = self.repo.mark_event_processed(f"sent:{key}")
        if not first_time:
            return False
        return self.send(chat_id, text)

    def notify_admin(self, text: str) -> None:
        tg = getattr(self.cardinal, "telegram", None)
        bot = getattr(tg, "bot", None)
        if bot is None:
            log.info("[ADMIN] %s", text)
            return
        for admin_id in self.cfg.admin_ids:
            try:
                bot.send_message(admin_id, text)
            except Exception:
                log.exception("Failed to notify admin %s", admin_id)
