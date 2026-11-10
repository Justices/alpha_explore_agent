"""Complete paginated BRAIN catalogs before field admission."""
from urllib.parse import urljoin, urlsplit


class PaginationError(RuntimeError):
    pass


def collect_pages(first_page: dict, fetch_next, base_url: str,
                  max_pages: int = 100, next_url_for_offset=None) -> dict:
    if not isinstance(first_page, dict) or not isinstance(first_page.get("results"), list):
        raise PaginationError("catalog page has no readable results")
    origin = urlsplit(base_url)
    rows = []
    page = first_page
    seen = set()
    for _ in range(max_pages):
        part = page.get("results")
        if not isinstance(part, list):
            raise PaginationError("catalog page has unreadable results")
        rows.extend(part)
        next_url = page.get("next")
        count = first_page.get("count")
        if not next_url and isinstance(count, int) and len(rows) < count:
            if not part:
                raise PaginationError("catalog page is empty before declared count")
            if next_url_for_offset is not None:
                next_url = next_url_for_offset(len(rows))
        if not next_url:
            if count is not None and (not isinstance(count, int) or count != len(rows)):
                raise PaginationError("catalog count does not match fetched rows")
            return {**first_page, "results": rows, "count": len(rows),
                    "next": None, "complete": True}
        next_url = urljoin(base_url.rstrip("/") + "/", next_url)
        target = urlsplit(next_url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            raise PaginationError("catalog next page points outside platform origin")
        if next_url in seen:
            raise PaginationError("catalog pagination loop")
        seen.add(next_url)
        page = fetch_next(next_url)
        if not isinstance(page, dict):
            raise PaginationError("catalog next page unreadable")
    raise PaginationError("catalog pagination limit reached")
