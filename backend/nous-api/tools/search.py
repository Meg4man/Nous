"""
nous-api — search tool
Web search via local SearXNG instance.

SearXNG is a self-hosted meta-search engine. No search query ever leaves
the user's network — SearXNG fans out to configured engines and returns
aggregated results.

The agent uses this for any factual query that isn't in memory.
"""

import json
import os

import httpx
import structlog
from langchain_core.tools import tool

log = structlog.get_logger("nous.tools.search")

SEARXNG_HOST = os.getenv("SEARXNG_HOST", "http://127.0.0.1:8888")

# Hard cap on results returned to the agent to keep context window manageable
MAX_RESULTS = 5


@tool
async def searxng_search(
    query: str,
    categories: str = "general",
    language: str = "auto",
) -> str:
    """
    Search the web using the local SearXNG instance.
    Use this for factual questions, current events, or anything not in memory.

    Args:
        query: Search query string
        categories: Search category — one of: general, news, images, science, it, files, social_media
        language: Language code (e.g. "en", "nl") or "auto" to detect from query

    Returns:
        JSON string with up to 5 search results (title, url, snippet).
    """
    # Validate category against known SearXNG categories
    VALID_CATEGORIES = {
        "general", "news", "images", "science", "it",
        "files", "social_media", "map", "music", "videos",
    }
    if categories not in VALID_CATEGORIES:
        categories = "general"

    params = {
        "q": query,
        "format": "json",
        "categories": categories,
        "pageno": 1,
    }
    if language != "auto":
        params["language"] = language

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{SEARXNG_HOST}/search",
                params=params,
                timeout=15.0,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()

        results_raw = data.get("results", [])[:MAX_RESULTS]

        # Trim to what the agent actually needs — no tracking URLs, no raw HTML
        results = [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "snippet": r.get("content", ""),
                "engine": r.get("engine", ""),
                "score": round(r.get("score", 0.0), 3),
            }
            for r in results_raw
        ]

        log.info("search.completed", query_len=len(query), result_count=len(results), category=categories)
        return json.dumps({"query": query, "results": results, "count": len(results)})

    except httpx.HTTPError as e:
        log.error("search.failed", error=str(e))
        return json.dumps({"error": f"SearXNG error: {e}", "results": []})
    except Exception as e:
        log.error("search.unexpected_error", error=str(e))
        return json.dumps({"error": str(e), "results": []})
