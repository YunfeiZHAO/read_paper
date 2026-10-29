"""Local academic paper search — a key-free, stdlib-only replacement for the native
web_search tool, used as a fallback when the gateway forbids Claude's server-side
web_search tool (see app/runners/claude.py).

Queries several free scholarly APIs and merges the results into one normalized,
de-duplicated list. No third-party deps (urllib + xml.etree + json), matching the
rest of the app's minimal-dependency style. Google Scholar is intentionally absent:
it has no API and blocks bots; the sources below cover the same papers with real
metadata.

Normalized record shape (every field always present; unknowns are "" or 0):
    {title, authors, year, venue, abstract, url, pdf_url, citation_count, source}
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable, Dict, List, Optional

from .config import (
    OPENALEX_MAILTO,
    PAPER_SEARCH_MAX_RESULTS,
    PAPER_SEARCH_SOURCES,
    PAPER_SEARCH_TIMEOUT_SECONDS,
    SEMANTIC_SCHOLAR_API_KEY,
)

USER_AGENT = "read-paper/1.0 (academic paper search; +https://arxiv.org)"

# Source priority for ranking when citation counts tie or are absent. Lower = better.
SOURCE_PRIORITY = {
    "semantic_scholar": 0,
    "openalex": 1,
    "dblp": 2,
    "arxiv": 3,
    "openreview": 4,
}

ALL_SOURCES = ["arxiv", "semantic_scholar", "openalex", "dblp", "openreview"]


# ── HTTP helpers ──────────────────────────────────────────────────────────
def _http_get(url: str, headers: Optional[Dict[str, str]] = None) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **(headers or {})})
    with urllib.request.urlopen(req, timeout=PAPER_SEARCH_TIMEOUT_SECONDS) as resp:  # noqa: S310
        return resp.read()


def _http_get_json(url: str, headers: Optional[Dict[str, str]] = None) -> Any:
    return json.loads(_http_get(url, headers).decode("utf-8", errors="replace"))


def _norm_record(
    *,
    title: str,
    authors: Optional[List[str]] = None,
    year: Any = None,
    venue: str = "",
    abstract: str = "",
    url: str = "",
    pdf_url: str = "",
    citation_count: Any = 0,
    source: str = "",
    doi: str = "",
    arxiv_id: str = "",
) -> Dict[str, Any]:
    try:
        year_int = int(year) if year not in (None, "") else 0
    except (TypeError, ValueError):
        year_int = 0
    try:
        cites = int(citation_count) if citation_count not in (None, "") else 0
    except (TypeError, ValueError):
        cites = 0
    return {
        "title": (title or "").strip(),
        "authors": [a.strip() for a in (authors or []) if a and a.strip()],
        "year": year_int,
        "venue": (venue or "").strip(),
        "abstract": (abstract or "").strip(),
        "url": (url or "").strip(),
        "pdf_url": (pdf_url or "").strip(),
        "citation_count": cites,
        "source": source,
        "doi": (doi or "").strip().lower(),
        "arxiv_id": (arxiv_id or "").strip().lower(),
    }


# ── Source adapters ─────────────────────────────────────────────────────────
# Each adapter takes (query, limit) and returns a list of normalized records.
# Adapters raise on failure; the orchestrator catches per-source so one bad
# source never sinks the whole search.

_ARXIV_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _search_arxiv(query: str, limit: int) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode(
        {"search_query": f"all:{query}", "start": 0, "max_results": limit}
    )
    raw = _http_get(f"http://export.arxiv.org/api/query?{params}")
    root = ET.fromstring(raw)
    out: List[Dict[str, Any]] = []
    for entry in root.findall("atom:entry", _ARXIV_NS):
        title = (entry.findtext("atom:title", default="", namespaces=_ARXIV_NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=_ARXIV_NS) or "").strip()
        published = entry.findtext("atom:published", default="", namespaces=_ARXIV_NS) or ""
        year = published[:4] if published else ""
        authors = [
            (a.findtext("atom:name", default="", namespaces=_ARXIV_NS) or "").strip()
            for a in entry.findall("atom:author", _ARXIV_NS)
        ]
        abs_url = (entry.findtext("atom:id", default="", namespaces=_ARXIV_NS) or "").strip()
        arxiv_id = abs_url.rsplit("/", 1)[-1] if abs_url else ""
        pdf_url = ""
        for link in entry.findall("atom:link", _ARXIV_NS):
            if link.get("title") == "pdf" or link.get("type") == "application/pdf":
                pdf_url = link.get("href", "")
        out.append(
            _norm_record(
                title=title,
                authors=authors,
                year=year,
                venue="arXiv",
                abstract=summary,
                url=abs_url,
                pdf_url=pdf_url,
                source="arxiv",
                arxiv_id=arxiv_id,
            )
        )
    return out


def _search_semantic_scholar(query: str, limit: int) -> List[Dict[str, Any]]:
    fields = "title,abstract,year,venue,authors,citationCount,externalIds,openAccessPdf,url"
    params = urllib.parse.urlencode({"query": query, "limit": limit, "fields": fields})
    headers = {"x-api-key": SEMANTIC_SCHOLAR_API_KEY} if SEMANTIC_SCHOLAR_API_KEY else {}
    data = _http_get_json(
        f"https://api.semanticscholar.org/graph/v1/paper/search?{params}", headers=headers
    )
    out: List[Dict[str, Any]] = []
    for paper in data.get("data", []) or []:
        ext = paper.get("externalIds") or {}
        oa = paper.get("openAccessPdf") or {}
        out.append(
            _norm_record(
                title=paper.get("title") or "",
                authors=[a.get("name", "") for a in (paper.get("authors") or [])],
                year=paper.get("year"),
                venue=paper.get("venue") or "",
                abstract=paper.get("abstract") or "",
                url=paper.get("url") or "",
                pdf_url=oa.get("url") or "",
                citation_count=paper.get("citationCount"),
                source="semantic_scholar",
                doi=ext.get("DOI") or "",
                arxiv_id=ext.get("ArXiv") or "",
            )
        )
    return out


def _search_openalex(query: str, limit: int) -> List[Dict[str, Any]]:
    q = {"search": query, "per-page": limit}
    if OPENALEX_MAILTO:
        q["mailto"] = OPENALEX_MAILTO
    data = _http_get_json(f"https://api.openalex.org/works?{urllib.parse.urlencode(q)}")
    out: List[Dict[str, Any]] = []
    for work in data.get("results", []) or []:
        loc = work.get("primary_location") or {}
        venue = ((loc.get("source") or {}).get("display_name")) or ""
        doi = (work.get("doi") or "").replace("https://doi.org/", "")
        authors = [
            (a.get("author") or {}).get("display_name", "")
            for a in (work.get("authorships") or [])
        ]
        out.append(
            _norm_record(
                title=work.get("display_name") or "",
                authors=authors,
                year=work.get("publication_year"),
                venue=venue,
                abstract=_reconstruct_openalex_abstract(work.get("abstract_inverted_index")),
                url=work.get("doi") or (loc.get("landing_page_url") or ""),
                pdf_url=(loc.get("pdf_url") or ""),
                citation_count=work.get("cited_by_count"),
                source="openalex",
                doi=doi,
            )
        )
    return out


def _reconstruct_openalex_abstract(inverted: Optional[Dict[str, List[int]]]) -> str:
    """OpenAlex ships abstracts as an inverted index {word: [positions]}; rebuild the text."""
    if not inverted:
        return ""
    positions: List[tuple] = []
    for word, idxs in inverted.items():
        for i in idxs:
            positions.append((i, word))
    positions.sort()
    return " ".join(word for _, word in positions)


def _search_dblp(query: str, limit: int) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode({"q": query, "format": "json", "h": limit})
    data = _http_get_json(f"https://dblp.org/search/publ/api?{params}")
    hits = ((data.get("result") or {}).get("hits") or {}).get("hit") or []
    out: List[Dict[str, Any]] = []
    for hit in hits:
        info = hit.get("info") or {}
        authors_field = (info.get("authors") or {}).get("author") or []
        if isinstance(authors_field, dict):
            authors_field = [authors_field]
        authors = [
            (a.get("text") if isinstance(a, dict) else str(a)) for a in authors_field
        ]
        out.append(
            _norm_record(
                title=info.get("title") or "",
                authors=authors,
                year=info.get("year"),
                venue=info.get("venue") or "",
                url=info.get("ee") or info.get("url") or "",
                citation_count=0,
                source="dblp",
                doi=info.get("doi") or "",
            )
        )
    return out


def _search_openreview(query: str, limit: int) -> List[Dict[str, Any]]:
    params = urllib.parse.urlencode({"term": query, "limit": limit, "content": "all"})
    data = _http_get_json(f"https://api2.openreview.net/notes/search?{params}")
    out: List[Dict[str, Any]] = []
    for note in data.get("notes", []) or []:
        content = note.get("content") or {}

        def _val(key: str) -> Any:
            field = content.get(key)
            return field.get("value") if isinstance(field, dict) else field

        note_id = note.get("id") or ""
        out.append(
            _norm_record(
                title=_val("title") or "",
                authors=_val("authors") or [],
                year=str(_val("year") or "") or "",
                venue=_val("venue") or _val("venueid") or "OpenReview",
                abstract=_val("abstract") or "",
                url=f"https://openreview.net/forum?id={note_id}" if note_id else "",
                pdf_url=f"https://openreview.net{_val('pdf')}" if _val("pdf") else "",
                source="openreview",
            )
        )
    return out


_ADAPTERS: Dict[str, Callable[[str, int], List[Dict[str, Any]]]] = {
    "arxiv": _search_arxiv,
    "semantic_scholar": _search_semantic_scholar,
    "openalex": _search_openalex,
    "dblp": _search_dblp,
    "openreview": _search_openreview,
}


# ── Merge / dedup / rank ─────────────────────────────────────────────────────
def _title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (title or "").lower())


def _dedup_key(record: Dict[str, Any]) -> str:
    if record.get("doi"):
        return f"doi:{record['doi']}"
    if record.get("arxiv_id"):
        return f"arxiv:{record['arxiv_id']}"
    return f"title:{_title_key(record['title'])}"


def _merge(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse duplicates across sources, keeping the richest field values."""
    merged: Dict[str, Dict[str, Any]] = {}
    for rec in records:
        if not rec.get("title"):
            continue
        key = _dedup_key(rec)
        if key not in merged:
            merged[key] = dict(rec)
            merged[key]["sources"] = [rec["source"]]
            continue
        existing = merged[key]
        existing.setdefault("sources", [existing.get("source", "")])
        if rec["source"] not in existing["sources"]:
            existing["sources"].append(rec["source"])
        # Fill blanks and keep the larger citation count / better-ranked source.
        for field in ("abstract", "venue", "url", "pdf_url", "doi", "arxiv_id"):
            if not existing.get(field) and rec.get(field):
                existing[field] = rec[field]
        if not existing.get("authors") and rec.get("authors"):
            existing["authors"] = rec["authors"]
        if not existing.get("year") and rec.get("year"):
            existing["year"] = rec["year"]
        existing["citation_count"] = max(
            existing.get("citation_count", 0), rec.get("citation_count", 0)
        )
        if SOURCE_PRIORITY.get(rec["source"], 99) < SOURCE_PRIORITY.get(existing["source"], 99):
            existing["source"] = rec["source"]
    return list(merged.values())


def _passes_filters(
    rec: Dict[str, Any], year_from: Optional[int], year_to: Optional[int], venue: Optional[str]
) -> bool:
    if year_from and rec["year"] and rec["year"] < year_from:
        return False
    if year_to and rec["year"] and rec["year"] > year_to:
        return False
    if venue and venue.lower() not in rec.get("venue", "").lower():
        return False
    return True


def _rank_key(rec: Dict[str, Any]) -> tuple:
    # More sources agreeing, more citations, more recent, better source priority.
    return (
        -len(rec.get("sources", [rec.get("source")])),
        -rec.get("citation_count", 0),
        -rec.get("year", 0),
        SOURCE_PRIORITY.get(rec.get("source", ""), 99),
    )


# ── Public entry point ───────────────────────────────────────────────────────
def paper_search(
    query: str,
    sources: Optional[List[str]] = None,
    limit: Optional[int] = None,
    year_from: Optional[int] = None,
    year_to: Optional[int] = None,
    venue: Optional[str] = None,
) -> Dict[str, Any]:
    """Search academic papers across free scholarly APIs.

    Returns {"query", "results": [...], "errors": {source: message}}. Sources are
    queried concurrently; a failing source is recorded in "errors" but never aborts
    the search. Results are merged, de-duplicated, filtered, and ranked.
    """
    query = (query or "").strip()
    limit = limit or PAPER_SEARCH_MAX_RESULTS
    requested = [s for s in (sources or PAPER_SEARCH_SOURCES) if s in _ADAPTERS]
    if not requested:
        requested = list(_ADAPTERS)

    if not query:
        return {"query": query, "results": [], "errors": {"query": "empty query"}}

    # Over-fetch per source so merge/filter still leaves `limit` good results.
    per_source = max(limit, 10)
    collected: List[Dict[str, Any]] = []
    errors: Dict[str, str] = {}

    with ThreadPoolExecutor(max_workers=len(requested)) as pool:
        futures = {
            pool.submit(_ADAPTERS[name], query, per_source): name for name in requested
        }
        for fut in as_completed(futures):
            name = futures[fut]
            try:
                collected.extend(fut.result())
            except (urllib.error.URLError, urllib.error.HTTPError, ET.ParseError, ValueError, KeyError, TimeoutError) as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"
            except Exception as exc:  # noqa: BLE001 — never let one source crash the tool
                errors[name] = f"{type(exc).__name__}: {exc}"

    merged = _merge(collected)
    filtered = [r for r in merged if _passes_filters(r, year_from, year_to, venue)]
    filtered.sort(key=_rank_key)
    # Drop internal-only dedup fields from the public payload.
    results = []
    for rec in filtered[:limit]:
        rec.pop("doi", None)
        rec.pop("arxiv_id", None)
        results.append(rec)
    return {"query": query, "results": results, "errors": errors}


def format_results_for_model(payload: Dict[str, Any], abstract_chars: int = 400) -> str:
    """Render a paper_search payload as a compact, token-efficient text block."""
    results = payload.get("results", [])
    lines: List[str] = []
    if not results:
        lines.append(f'No papers found for "{payload.get("query", "")}".')
    else:
        lines.append(f'Found {len(results)} paper(s) for "{payload.get("query", "")}":\n')
        for i, r in enumerate(results, 1):
            authors = ", ".join(r["authors"][:3]) + (" et al." if len(r["authors"]) > 3 else "")
            srcs = ", ".join(r.get("sources", [r.get("source", "")]))
            header = f"{i}. {r['title']}"
            if r["year"]:
                header += f" ({r['year']})"
            lines.append(header)
            meta = []
            if authors:
                meta.append(authors)
            if r["venue"]:
                meta.append(r["venue"])
            meta.append(f"cites: {r['citation_count']}")
            meta.append(f"via: {srcs}")
            lines.append("   " + " | ".join(meta))
            if r["url"]:
                lines.append(f"   url: {r['url']}")
            if r["pdf_url"]:
                lines.append(f"   pdf: {r['pdf_url']}")
            if r["abstract"]:
                abstract = r["abstract"][:abstract_chars].rstrip()
                if len(r["abstract"]) > abstract_chars:
                    abstract += "…"
                lines.append(f"   abstract: {abstract}")
            lines.append("")
    errors = payload.get("errors") or {}
    if errors:
        lines.append("(Sources unavailable: " + "; ".join(f"{k}: {v}" for k, v in errors.items()) + ")")
    return "\n".join(lines).strip()
