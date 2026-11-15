from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import re
from typing import Any
from ddgs import DDGS
import httpx

from assistant.integrations.web_search import fetch_webpage
from assistant.tools import ToolRegistry

MARKETPLACE_DOMAINS: dict[str, str] = {
    "wildberries": "site:wildberries.ru/catalog",
    "ozon": "site:ozon.ru/product",
    "yandex_market": "site:market.yandex.ru",
    "megamarket": "site:megamarket.ru/catalog",
    "avito": "site:avito.ru",
    "dns": "site:dns-shop.ru/product",
}

PRICE_RE = re.compile(
    r"(?<!\d)(\d{1,3}(?:[\s\u00a0]\d{3})+|\d{3,7})\s*(?:₽|руб\.?|р\.)",
    re.IGNORECASE,
)
RATING_RE = re.compile(r"(?:★|⭐|рейтинг[:\s]*|оценка[:\s]*)([1-5](?:[.,]\d)?)", re.IGNORECASE)
WB_ID_RE = re.compile(r"wildberries\.ru/catalog/(\d+)")


def _extract_price_rub(text: str) -> int | None:
    for m in PRICE_RE.finditer(text):
        digits = re.sub(r"[\s\u00a0]", "", m.group(1))
        try:
            val = int(digits)
            if 50 <= val <= 20_000_000:
                return val
        except ValueError:
            continue
    return None


def _extract_rating(text: str) -> float | None:
    m = RATING_RE.search(text)
    if not m:
        return None
    try:
        val = float(m.group(1).replace(",", "."))
        return val if 1.0 <= val <= 5.0 else None
    except ValueError:
        return None


def _search_wb_api(query: str, limit: int) -> list[dict[str, Any]]:
    url = "https://search.wb.ru/exactmatch/ru/common/v9/search"
    params = {
        "ab_testing": "false",
        "appType": 1,
        "curr": "rub",
        "dest": -1257786,
        "query": query,
        "resultset": "catalog",
        "sort": "popular",
        "spp": 30,
    }
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
    with httpx.Client(headers=headers, timeout=4.5) as client:
        resp = client.get(url, params=params)
        resp.raise_for_status()
        products = resp.json().get("data", {}).get("products", [])

    items: list[dict[str, Any]] = []
    for p in products[:limit]:
        pid = p.get("id")
        # В WB API цена бывает в sizes[0].price.product (в копейках) или salePriceU
        price_kopecks = None
        sizes = p.get("sizes") or []
        if sizes and isinstance(sizes[0], dict):
            price_kopecks = (sizes[0].get("price") or {}).get("product")
        if price_kopecks is None:
            price_kopecks = p.get("salePriceU") or p.get("priceU")
        price_rub = int(price_kopecks // 100) if price_kopecks else None

        brand = p.get("brand", "")
        name = p.get("name", "")
        title = f"{brand} — {name}".strip(" —")
        items.append(
            {
                "marketplace": "wildberries",
                "title": title,
                "price_rub": price_rub,
                "rating": p.get("reviewRating") or p.get("rating"),
                "reviews_count": p.get("feedbacks"),
                "url": f"https://www.wildberries.ru/catalog/{pid}/detail.aspx",
                "snippet": f"Бренд: {brand}; отзывов: {p.get('feedbacks', 0)}",
                "source": "wb_api",
            }
        )
    return items


def _search_via_ddgs(
        marketplace: str,
        query: str,
        limit: int,
) -> list[dict[str, Any]]:
    domain_filter = MARKETPLACE_DOMAINS[marketplace]
    full_query = f"{domain_filter} {query} цена"
    with DDGS(timeout=6) as ddgs:
        raw = list(ddgs.text(full_query, region="ru-ru", max_results=limit))

    items: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for r in raw:
        url = r.get("href", "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        title = r.get("title", "")
        snippet = r.get("body", "")
        combined = f"{title} {snippet}"
        items.append(
            {
                "marketplace": marketplace,
                "title": title,
                "price_rub": _extract_price_rub(combined),
                "rating": _extract_rating(combined),
                "reviews_count": None,
                "url": url,
                "snippet": snippet[:280],
                "source": "web_index",
            }
        )
    return items


def _search_single_marketplace(
        marketplace: str,
        query: str,
        limit: int,
) -> list[dict[str, Any]]:
    if marketplace == "wildberries":
        try:
            wb_items = _search_wb_api(query, limit)
            if wb_items:
                return wb_items
        except Exception:
            pass
    return _search_via_ddgs(marketplace, query, limit)


def marketplace_search(
        query: str,
        marketplaces: list[str] | None = None,
        min_price: int | None = None,
        max_price: int | None = None,
        sort_by: str = "relevance",
        limit_per_marketplace: int = 4,
) -> dict[str, Any]:
    targets = [
        m.lower().strip()
        for m in (marketplaces or ["wildberries", "ozon", "yandex_market"])
        if m.lower().strip() in MARKETPLACE_DOMAINS
    ]
    if not targets:
        return {
            "error": f"Укажите хотя бы один маркетплейс из: {list(MARKETPLACE_DOMAINS.keys())}"
        }

    limit = max(1, min(8, int(limit_per_marketplace)))
    all_items: list[dict[str, Any]] = []
    errors: dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(targets)) as pool:
        futures = {
            pool.submit(_search_single_marketplace, mp, query, limit): mp
            for mp in targets
        }
        for fut in as_completed(futures):
            mp = futures[fut]
            try:
                all_items.extend(fut.result())
            except Exception as exc:
                errors[mp] = f"{type(exc).__name__}: {exc}"

    # Фильтрация по цене (если цена распарсилась)
    filtered: list[dict[str, Any]] = []
    for item in all_items:
        p = item.get("price_rub")
        if p is not None:
            if min_price is not None and p < min_price:
                continue
            if max_price is not None and p > max_price:
                continue
        filtered.append(item)

    if sort_by == "price_asc":
        filtered.sort(
            key=lambda x: (x["price_rub"] is None, x["price_rub"] or 0)
        )
    elif sort_by == "price_desc":
        filtered.sort(
            key=lambda x: (x["price_rub"] is None, -(x["price_rub"] or 0))
        )

    return {
        "query": query,
        "marketplaces_searched": targets,
        "total_found": len(filtered),
        "items": filtered,
        "errors": errors or None,
    }


def marketplace_inspect_product(url: str) -> dict[str, Any]:
    wb_match = WB_ID_RE.search(url)
    if wb_match:
        nm_id = wb_match.group(1)
        try:
            api_url = "https://card.wb.ru/cards/v2/detail"
            params = {"appType": 1, "curr": "rub", "dest": -1257786, "nm": nm_id}
            with httpx.Client(timeout=5.0) as client:
                resp = client.get(api_url, params=params)
                resp.raise_for_status()
                prods = resp.json().get("data", {}).get("products", [])
                if prods:
                    p = prods[0]
                    sizes = p.get("sizes") or []
                    price_kop = (sizes[0].get("price") or {}).get("product") if sizes else None
                    return {
                        "marketplace": "wildberries",
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "brand": p.get("brand"),
                        "price_rub": int(price_kop // 100) if price_kop else None,
                        "rating": p.get("reviewRating") or p.get("rating"),
                        "feedbacks": p.get("feedbacks"),
                        "total_quantity": p.get("totalQuantity"),
                        "supplier": p.get("supplier"),
                        "url": url,
                    }
        except Exception:
            pass

    page = fetch_webpage(url, max_chars=4000)
    return {
        "url": page["url"],
        "detected_price_rub": _extract_price_rub(page["content"]),
        "content_excerpt": page["content"][:2500],
    }


def register_marketplace_tools(registry: ToolRegistry) -> None:
    registry.register(
        name="marketplace_search",
        description=(
            "Параллельно ищет товары на нескольких маркетплейсах (wildberries, ozon, yandex_market, "
            "megamarket, avito, dns) с извлечением цен, рейтингов и сортировкой."
        ),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Название или характеристики товара."},
                "marketplaces": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": list(MARKETPLACE_DOMAINS.keys()),
                    },
                    "description": "Список площадок (по умолчанию wildberries, ozon, yandex_market).",
                },
                "min_price": {"type": "integer", "description": "Минимальная цена в рублях (опционально)."},
                "max_price": {"type": "integer", "description": "Максимальная цена в рублях (опционально)."},
                "sort_by": {
                    "type": "string",
                    "enum": ["relevance", "price_asc", "price_desc"],
                    "description": "Сортировка результатов.",
                },
                "limit_per_marketplace": {
                    "type": "integer",
                    "description": "Сколько позиций брать с каждой площадки (1–8, по умолчанию 4).",
                },
            },
            "required": ["query"],
        },
        handler=marketplace_search,
    )

    registry.register(
        name="marketplace_inspect_product",
        description="Получает подробности о конкретном товаре по URL карточки (цена, рейтинг, остатки).",
        parameters={
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "URL товара на маркетплейсе."}
            },
            "required": ["url"],
        },
        handler=marketplace_inspect_product,
    )
