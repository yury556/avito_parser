import json
import random
import re
import time
from datetime import datetime, timedelta

from bs4 import BeautifulSoup
from loguru import logger
from pydantic import ValidationError

from avito_parser.headers import HEADERS
from avito_parser.storages.sqlite import SQLiteDBHandler
from avito_parser.settings import Proxy, AvitoConfig
from avito_parser.ads_filters.filters import AdsFilter
from avito_parser.logging import log_config
from avito_parser.config import load_avito_config
from avito_parser.entities import ItemsResponse, Item
from avito_parser.cookies.factory import build_cookies_provider
from avito_parser.sheets.factory import build_result_storage
from avito_parser.fetchers.client import HttpClient
from avito_parser.proxies.factory import build_proxy
from avito_parser.api.params import build_api_params
from avito_parser.version import VERSION

DEBUG_MODE = False

logger.add("logs/app.log", rotation="5 MB", retention="5 days", level="DEBUG")


class AvitoParse:
    def __init__(
            self,
            config: AvitoConfig,
            stop_event=None
    ):
        self.config = config
        self.proxy = build_proxy(self.config)
        self.cookies_provider = build_cookies_provider(config=config)
        self.db_handler = SQLiteDBHandler()
        self.result_storage = None
        self.stop_event = stop_event
        self.headers = HEADERS
        self.good_request_count = 0
        self.bad_request_count = 0
        self.http = HttpClient(
            proxy=self.proxy,
            cookies=self.cookies_provider,
            timeout=config.timeout,
            max_retries=self.config.max_count_of_retry,
            retry_delay=config.retry_delay,
            block_threshold=config.block_threshold
        )
        self.ads_filter = AdsFilter(config=config, is_viewed_fn=self.is_viewed)
        log_config(config=self.config, version=VERSION)


    def get_proxy_obj(self) -> Proxy | None:
        if all([self.config.proxy_string, self.config.proxy_change_url]):
            return Proxy(
                proxy_string=self.config.proxy_string,
                change_ip_link=self.config.proxy_change_url
            )
        logger.info("Работаем без прокси")
        return None

    def fetch_data(self, url: str) -> str | None:
        if self.stop_event and self.stop_event.is_set():
            return None

        try:
            response = self.http.request("GET", url)
            self.good_request_count += 1
            return response.text

        except Exception as err:
            self.bad_request_count += 1
            logger.warning(f"Ошибка при запросе {url}: {err}")
            return None

    def fetch_api_data(self, base_params: dict, page: int, context: str):
        params = base_params.copy()

        params.update({
            'p': str(page),
            'context': context,
            'updateListOnly': 'true',
        })

        response = self.http.request(
            "GET",
            "https://www.avito.ru/web/1/js/items",
            params=params
        )

        return response.json()

    def parse(self):
        if not self.config.one_file_for_link:
            self.result_storage = build_result_storage(config=self.config)

        all_ads = []

        for _index, url in enumerate(self.config.urls):

            if self.config.one_file_for_link:
                self.result_storage = build_result_storage(
                    config=self.config,
                    link_index=_index
                )
            ads_in_link = []
            api_params = None
            context = None

            for i in range(0, self.config.count):
                logger.info(f"page={i + 1}")
                if self.stop_event and self.stop_event.is_set():
                    return
                if DEBUG_MODE:
                    html_code = open("debug.txt", "r", encoding="utf-8").read()
                else:
                    if i == 0:
                        html_code = self.fetch_data(url=url)
                    else:
                        if api_params and context:
                            json_data = self.fetch_api_data(api_params, page=i + 1, context=context)
                        else:
                            logger.info("Т.к. 1-я страница была неудачной - дальше смотреть не можем")
                            break

                if not html_code:
                    logger.warning(
                        f"Не удалось получить HTML для {url}, пробую заново через {self.config.pause_between_links} сек.")
                    time.sleep(self.config.pause_between_links)
                    continue

                data_from_page = self.find_json_on_page(html_code=html_code)

                if i == 0:
                    search_core = data_from_page.get("searchCore") or {}
                    context = data_from_page.get("context")

                    api_params = build_api_params(search_core)

                try:
                    if i == 0:
                        catalog = data_from_page.get("catalog") or {}
                    else:
                        catalog = json_data.get("catalog") or json_data.get("result", {}).get("catalog") or {}

                    ads_models = ItemsResponse(**catalog)
                except ValidationError as err:
                    logger.error(f"При валидации объявлений произошла ошибка: {err}")
                    continue

                ads = self._clean_null_ads(ads=ads_models.items)

                logger.info(f"Объявлений перед чисткой {len(ads)}")

                ads = self._add_seller_to_ads(ads=ads)

                ads = self._add_promotion_to_ads(ads=ads)

                if not ads:
                    logger.info("Объявления закончились, заканчиваю работу с данной ссылкой")
                    break

                filter_ads = self.filter_ads(ads=ads)

                filter_ads = self.parse_views(ads=filter_ads)

                if filter_ads:
                    self.__save_viewed(ads=filter_ads)
                    ads_in_link.extend(filter_ads)

                logger.info(f"Пауза {self.config.pause_between_links} сек.")
                time.sleep(self.config.pause_between_links)

            if ads_in_link:
                logger.info(f"Сохраняю {len(ads_in_link)} объявлений")
                self.result_storage.save(ads_in_link)
                all_ads.extend(ads_in_link)
                if self.config.save_json:
                    self._save_ads_to_json(ads_in_link)
            else:
                logger.info("Сохранять нечего")

        logger.info(f"Хорошие запросы: {self.good_request_count}шт, плохие: {self.bad_request_count}шт")

        if self.config.one_time_start:
            self.stop_event = True

        return all_ads

    @staticmethod
    def _item_to_dict(item: Item) -> dict:
        return {
            "avito_id": item.id if isinstance(item.id, int) else None,
            "title": item.title or "",
            "price_rub": item.priceDetailed.value if item.priceDetailed else None,
            "url": f"https://www.avito.ru{item.urlPath}" if item.urlPath else None,
            "location": item.location.name if item.location else None,
            "seller": item.sellerId if item.sellerId else None,
            "description": item.description or None,
            "is_reserved": item.isReserved if item.isReserved is not None else False,
            "is_promotion": item.isPromotion if item.isPromotion is not None else False,
            "total_views": item.total_views,
            "today_views": item.today_views,
        }

    def _save_ads_to_json(self, ads: list[Item]) -> None:
        import json as json_lib
        output_path = self.config.output_dir / "ads.json"
        output_path.parent.mkdir(exist_ok=True)
        existing = []
        if output_path.exists():
            existing = json_lib.loads(output_path.read_text(encoding="utf-8"))
        existing.extend([self._item_to_dict(ad) for ad in ads])
        output_path.write_text(
            json_lib.dumps(existing, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        logger.info(f"JSON сохранён: {output_path} ({len(existing)} объявлений)")

    @staticmethod
    def _clean_null_ads(ads: list[Item]) -> list[Item]:
        return [ad for ad in ads if ad.id]

    @staticmethod
    def find_json_on_page(html_code, data_type: str = "mime") -> dict:
        import html as html_lib
        html_code = BeautifulSoup(html_code, "html.parser")
        try:
            for _script in html_code.select('script'):

                script_type = _script.get('type')

                if data_type == 'mime':
                    for script in html_code.select('script'):
                        if script.get('type') == 'mime/invalid' and script.get('data-mfe-state') == 'true' and 'sandbox' not in script.text:
                            data = json.loads(html_lib.unescape(script.text))
                            if data.get('i18n', {}).get('hasMessages'):
                                return data.get('loaderData', {}).get("data", {})

        except Exception as err:
            logger.error(f"Ошибка при поиске информации на странице: {err}")
        logger.warning("not found json")
        return {}


    def filter_ads(self, ads: list[Item]) -> list[Item]:
        return self.ads_filter.apply(ads)

    def _add_seller_to_ads(self, ads: list[Item]) -> list[Item]:
        for ad in ads:
            if seller_id := self._extract_seller_slug(data=ad):
                ad.sellerId = seller_id
        return ads

    @staticmethod
    def _add_promotion_to_ads(ads: list[Item]) -> list[Item]:
        for ad in ads:
            ad.isPromotion = any(
                v.get("title") == "Продвинуто"
                for step in (ad.iva or {}).get("DateInfoStep", [])
                for v in step.payload.get("vas", [])
            )
        return ads

    def parse_views(self, ads: list[Item]) -> list[Item]:
        if not self.config.parse_views:
            return ads

        logger.info("Начинаю парсинг просмотров и описаний")

        for ad in ads:
            try:
                html_code_full_page = self.fetch_data(url=f"https://www.avito.ru{ad.urlPath}")
                if not html_code_full_page:
                    continue
                ad.total_views, ad.today_views = self._extract_views(html=html_code_full_page)
                full_description = self._extract_description(html=html_code_full_page)
                if full_description:
                    ad.description = full_description
                delay = random.uniform(0.1, 0.9)
                time.sleep(delay)
            except Exception as err:
                logger.warning(f"Ошибка при парсинге {ad.urlPath}: {err}")
                continue

        return ads

    @staticmethod
    def _extract_description(html: str) -> str | None:
        soup = BeautifulSoup(html, "html.parser")

        desc_block = soup.select_one('[data-marker="item-view/item-description"]')
        if desc_block:
            text = desc_block.get_text(separator=" ", strip=True)
            if text:
                return text

        import html as html_lib
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                data = json.loads(html_lib.unescape(script.text))
                if isinstance(data, dict) and data.get("description"):
                    return data["description"]
                if isinstance(data, list):
                    for entry in data:
                        if isinstance(entry, dict) and entry.get("description"):
                            return entry["description"]
            except Exception:
                continue

        for script in soup.select('script'):
            if script.get('type') == 'mime/invalid' and script.get('data-mfe-state') == 'true':
                try:
                    data = json.loads(html_lib.unescape(script.text))
                    item_data = data.get("loaderData", {}).get("data", {})
                    item_view = item_data.get("item", {})
                    desc = item_view.get("description") or item_view.get("fullDescription")
                    if desc:
                        return desc
                except Exception:
                    continue

        return None

    @staticmethod
    def _extract_views(html: str) -> tuple:
        soup = BeautifulSoup(html, "html.parser")

        def extract_digits(element):
            return int(''.join(filter(str.isdigit, element.get_text()))) if element else None

        total = extract_digits(soup.select_one('[data-marker="item-view/total-views"]'))
        today = extract_digits(soup.select_one('[data-marker="item-view/today-views"]'))

        return total, today

    @staticmethod
    def _extract_seller_slug(data):
        match = re.search(r"/brands/([^/?#]+)", str(data))
        if match:
            return match.group(1)
        return None

    def is_viewed(self, ad: Item) -> bool:
        """Проверяет, смотрели мы это или нет"""
        return self.db_handler.record_exists(record_id=ad.id, price=ad.priceDetailed.value)

    @staticmethod
    def _is_recent(timestamp_ms: int, max_age_seconds: int) -> bool:
        now = datetime.utcnow()
        published_time = datetime.utcfromtimestamp(timestamp_ms / 1000)
        return (now - published_time) <= timedelta(seconds=max_age_seconds)

    def __save_viewed(self, ads: list[Item]) -> None:
        """Сохраняет просмотренные объявления"""
        try:
            self.db_handler.add_record_from_page(ads=ads)
        except Exception as err:
            logger.info(f"При сохранении в БД ошибка {err}")


if __name__ == "__main__":
    try:
        config = load_avito_config("config.toml")
    except Exception as err:
        logger.error(f"Ошибка загрузки конфига: {err}")
        exit(1)

    while True:
        try:
            parser = AvitoParse(config)
            parser.parse()
            if config.one_time_start:
                logger.info("Парсинг завершен т.к. включён one_time_start в настройках")
                break
            logger.info(f"Парсинг завершен. Пауза {config.pause_general} сек")
            time.sleep(config.pause_general)
        except Exception as err:
            logger.exception(err)
            logger.error(f"Произошла ошибка {err}. Будет повторный запуск через 30 сек.")
            time.sleep(30)
