"""
Клиент для запросов парсера (curl_cffi) с обходом антибота Авито.

Вместо новой сессии на каждый запрос используется одна долгоживущая сессия
с фиксированным Firefox-профилем — это условие прохождения QRATOR/firewall
верификации. При защитных ответах (439 PoW / 403 / 429) вызывается
handle_firewall_response из firewall_client (портировано из avito-antibot-client).
"""
import os
import time
from curl_cffi import requests
from loguru import logger

from avito_parser.cookies.base import CookiesProvider
from avito_parser.proxies.proxy import Proxy
from avito_parser.firewall_client.main import (
    DOCUMENT_REQUEST_HEADERS,
    HTTP_IMPERSONATE_PROFILE,
    handle_firewall_response,
)


class HttpClient:
    def __init__(
        self,
        proxy: Proxy,
        cookies: CookiesProvider | None = None,
        timeout: int = 20,
        max_retries: int = 5,
        retry_delay: int = 5,
        block_threshold: int = 3,
    ):
        self.proxy = proxy
        self.cookies = cookies
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self.block_threshold = block_threshold

        self._block_attempts = 0
        self._session: requests.Session | None = None

    def _build_session(self) -> requests.Session:
        session = requests.Session(impersonate=HTTP_IMPERSONATE_PROFILE)
        session.headers.update(DOCUMENT_REQUEST_HEADERS)

        proxy = self.proxy.get_httpx_proxy()
        if proxy:
            session.proxies = {
                "http": proxy,
                "https": proxy,
            }

        return session

    def _ensure_session(self) -> requests.Session:
        if self._session is None:
            self._session = self._build_session()
            self._warmup_session(self._session)
        return self._session

    def _warmup_session(self, session: requests.Session) -> None:
        """Прогрев: GET каталога с правильными заголовками — сессия получает
        валидные куки Авито (как в генераторе), что снижает 439-трения
        на рабочих запросах. Не критичен при неудаче."""
        try:
            warm_url = os.environ.get(
                "AVITO_WARMUP_URL",
                "https://www.avito.ru/sankt-peterburg/tovary_dlya_kompyutera",
            )
            r = session.get(warm_url, timeout=self.timeout, allow_redirects=True)
            if self.cookies:
                self.cookies.update(r)
            logger.info(
                f"Прогрев сессии: HTTP {r.status_code}, "
                f"куки в jar: {len(session.cookies.jar)}"
            )
        except Exception as e:
            logger.warning(f"Прогрев не удался (не критично): {e}")

    def request(self, method: str, url: str, **kwargs):
        last_exc = None

        for attempt in range(1, self.max_retries + 1):
            try:
                client = self._ensure_session()

                # Куки НЕ подставляем с диска: застрявший pow_challenge в
                # own_cookies.json вызывает 439 на каждый запрос. Сессия
                # сама накапливает cookie-jar из ответов (Set-Cookie).

                response = client.request(
                    method,
                    url,
                    timeout=self.timeout,
                    allow_redirects=True,
                    **kwargs,
                )

                # === обновление cookies из ответа ===
                if self.cookies:
                    self.cookies.update(response)

                # === обработка защитных ответов (firewall) ===
                if response.status_code in (401, 403, 429, 439):
                    self._block_attempts += 1

                    logger.warning(
                        f"Запрос заблокирован ({response.status_code}), "
                        f"попытка {self._block_attempts}"
                    )

                    if self._block_attempts >= self.block_threshold:
                        logger.warning("Достигнут лимит блокировок, запускается обработка")
                        if self.cookies:
                            self.cookies.handle_block()
                        self.proxy.handle_block()
                        self._block_attempts = 0

                    # Попытка пройти верификацию firewall (PoW / GeeTest)
                    stage_cleared = None
                    try:
                        stage_cleared = handle_firewall_response(
                            client, response, context=f"parser:{url[:80]}"
                        )
                    except RuntimeError as fw_err:
                        logger.warning(f"Firewall-обработка не прошла: {fw_err}")
                        if "проблема с IP" in str(fw_err):
                            # IP-бан: быстрый ретрай только продлевает бан —
                            # ждём долго (5 минут) между попытками.
                            logger.warning("IP забанен Авито; пауза 300с")
                            time.sleep(300)
                            continue

                    if stage_cleared is not None:
                        logger.info(
                            f"Firewall-верификация пройдена ({type(stage_cleared).__name__}); "
                            f"повторяю запрос"
                        )
                    # Rate-лимит канала (429/439): мгновенные ретраи жгут квоту.
                    # Пауза растёт с числом подряд идущих блокировок.
                    time.sleep(min(60, 10 + 10 * self._block_attempts))
                    continue

                # === успех ===
                response.raise_for_status()
                self._block_attempts = 0
                return response

            except requests.RequestsError as e:
                last_exc = e
                logger.warning(f"Request error (attempt {attempt}): {e}")
                # Транспортная ошибка — пересоздаём сессию
                self._session = None
                time.sleep(self.retry_delay)

        raise RuntimeError("HTTP запросы были неуспешными") from last_exc
