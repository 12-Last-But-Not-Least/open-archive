"""Institution name suggestions from the public Research Organization Registry."""

import re

import httpx


ROR_ORGANIZATIONS = "https://api.ror.org/v2/organizations"
# Treat a person's typing as text, not Elasticsearch query syntax.
QUERY_SPECIAL = re.compile(r'([+\-=|&><!(){}\[\]^"~*?:\\/])')


async def search_institutions(query):
    words = QUERY_SPECIAL.sub(r"\\\1", query).split()
    # Prefix-match the unfinished last term while retaining completed terms.
    # Only the application, never the text field, supplies the wildcard.
    ror_query = " AND ".join(f'"{word}"' for word in words[:-1])
    last = words[-1] if words[-1] not in {"AND", "OR", "NOT"} else words[-1].lower()
    ror_query += (" AND " if ror_query else "") + last + ("*" if last[-1].isalnum() else "")
    async with httpx.AsyncClient(timeout=3, follow_redirects=False, trust_env=False) as client:
        response = await client.get(ROR_ORGANIZATIONS, params={"query": ror_query})
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ValueError("Unexpected institution response")
    names, seen = [], set()
    for item in data["items"][:20]:
        if not isinstance(item, dict) or not isinstance(item.get("names"), list):
            continue
        for name in item["names"]:
            if not isinstance(name, dict) or not isinstance(name.get("types"), list) or "ror_display" not in name["types"]:
                continue
            value = name.get("value")
            if isinstance(value, str) and 2 <= len(value.strip()) <= 200 and value.strip().casefold() not in seen:
                value = value.strip()
                names.append(value)
                seen.add(value.casefold())
            break
    # If present in ROR's result set, prioritize the same starting text.
    names.sort(key=lambda name: not name.casefold().startswith(query.casefold()))
    return names[:8]
