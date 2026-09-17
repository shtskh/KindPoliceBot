"""
Единый интерфейс доставки поста в канал/чат.
Это позволяет разрабатывать и тестировать весь пайплайн в Telegram
(токен получается за минуту, без верификации юрлица), а потом
подключить Max как второй канал, не переписывая пайплайн.
"""
from abc import ABC, abstractmethod


class DeliveryChannel(ABC):
    @abstractmethod
    def send_post(self, text: str, image_path: str | None = None) -> bool:
        """Отправляет готовый пост в канал. Возвращает True при успехе."""
        raise NotImplementedError
