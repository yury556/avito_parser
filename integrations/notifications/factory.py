from dto import AvitoConfig
from integrations.notifications.base import Notifier
from integrations.notifications.composite import NullNotifier, CompositeNotifier
from integrations.notifications.telegram import TelegramNotifier


def build_notifier(config: AvitoConfig) -> Notifier:
    notifiers = []

    if config.tg_token:
        for _chat_id in config.tg_chat_id:
            notifiers.append(TelegramNotifier(bot_token=config.tg_token,
                                              chat_id=_chat_id,
                                              proxy=config.proxy_notifier,
                                              only_text=config.tg_only_text
                                              ))

    if notifiers:
        return CompositeNotifier(notifiers)

    return NullNotifier()  # если уведомления вообще не нужны
