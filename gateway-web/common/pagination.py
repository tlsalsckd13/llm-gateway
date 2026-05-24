from dataclasses import dataclass


@dataclass(frozen=True)
class Page:
    page: int
    per_page: int
    offset: int


def page_from_query(query, default_per_page: int = 20, max_per_page: int = 100) -> Page:
    try:
        page = int(query.get("page", "1"))
    except ValueError:
        page = 1
    try:
        per_page = int(query.get("per_page", str(default_per_page)))
    except ValueError:
        per_page = default_per_page
    page = max(page, 1)
    per_page = max(1, min(per_page, max_per_page))
    return Page(page=page, per_page=per_page, offset=(page - 1) * per_page)


def envelope(items, total: int, page: Page) -> dict:
    return {
        "items": items,
        "page": page.page,
        "per_page": page.per_page,
        "total": total,
        "has_next": page.offset + len(items) < total,
        "has_prev": page.page > 1,
    }
