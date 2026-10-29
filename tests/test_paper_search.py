"""Unit tests for the local paper_search tool: per-source parsing (offline, against
captured sample payloads), merge/dedup/filter/rank, and model-text formatting.
"""
from __future__ import annotations

import json

import pytest

from app import paper_search as ps

# ── Sample payloads (trimmed real-shape responses; no network in tests) ──────

ARXIV_ATOM = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/1706.03762v5</id>
    <published>2017-06-12T00:00:00Z</published>
    <title>Attention Is All You Need</title>
    <summary>The dominant sequence transduction models...</summary>
    <author><name>Ashish Vaswani</name></author>
    <author><name>Noam Shazeer</name></author>
    <link href="http://arxiv.org/abs/1706.03762v5" rel="alternate" type="text/html"/>
    <link href="http://arxiv.org/pdf/1706.03762v5" rel="related" type="application/pdf" title="pdf"/>
  </entry>
</feed>"""

S2_JSON = {
    "data": [
        {
            "title": "Attention Is All You Need",
            "abstract": "We propose the Transformer...",
            "year": 2017,
            "venue": "NeurIPS",
            "authors": [{"name": "Ashish Vaswani"}, {"name": "Noam Shazeer"}],
            "citationCount": 100000,
            "externalIds": {"DOI": "10.5555/abc", "ArXiv": "1706.03762"},
            "openAccessPdf": {"url": "https://example.com/paper.pdf"},
            "url": "https://www.semanticscholar.org/paper/abc",
        }
    ]
}

OPENALEX_JSON = {
    "results": [
        {
            "display_name": "Deep Residual Learning for Image Recognition",
            "publication_year": 2016,
            "cited_by_count": 200000,
            "doi": "https://doi.org/10.1109/cvpr.2016.90",
            "primary_location": {
                "source": {"display_name": "CVPR"},
                "landing_page_url": "https://example.com/resnet",
                "pdf_url": "https://example.com/resnet.pdf",
            },
            "authorships": [
                {"author": {"display_name": "Kaiming He"}},
                {"author": {"display_name": "Xiangyu Zhang"}},
            ],
            "abstract_inverted_index": {"Deeper": [0], "networks": [1], "are": [2], "better": [3]},
        }
    ]
}

DBLP_JSON = {
    "result": {
        "hits": {
            "hit": [
                {
                    "info": {
                        "title": "BERT: Pre-training of Deep Bidirectional Transformers.",
                        "authors": {"author": [{"text": "Jacob Devlin"}, {"text": "Ming-Wei Chang"}]},
                        "venue": "NAACL",
                        "year": "2019",
                        "ee": "https://doi.org/10.18653/v1/n19-1423",
                        "doi": "10.18653/v1/n19-1423",
                    }
                }
            ]
        }
    }
}

OPENREVIEW_JSON = {
    "notes": [
        {
            "id": "abc123",
            "content": {
                "title": {"value": "A Great ICLR Paper"},
                "authors": {"value": ["Jane Doe", "John Roe"]},
                "abstract": {"value": "We show that..."},
                "venue": {"value": "ICLR 2024"},
                "pdf": {"value": "/pdf/abc123.pdf"},
            },
        }
    ]
}


# ── Per-source parsing ───────────────────────────────────────────────────────


def test_arxiv_parse(monkeypatch):
    monkeypatch.setattr(ps, "_http_get", lambda url, headers=None: ARXIV_ATOM.encode())
    out = ps._search_arxiv("transformer", 5)
    assert len(out) == 1
    rec = out[0]
    assert rec["title"] == "Attention Is All You Need"
    assert rec["authors"] == ["Ashish Vaswani", "Noam Shazeer"]
    assert rec["year"] == 2017
    assert rec["pdf_url"] == "http://arxiv.org/pdf/1706.03762v5"
    assert rec["arxiv_id"] == "1706.03762v5"
    assert rec["source"] == "arxiv"


def test_semantic_scholar_parse(monkeypatch):
    monkeypatch.setattr(ps, "_http_get", lambda url, headers=None: json.dumps(S2_JSON).encode())
    out = ps._search_semantic_scholar("transformer", 5)
    rec = out[0]
    assert rec["venue"] == "NeurIPS"
    assert rec["citation_count"] == 100000
    assert rec["doi"] == "10.5555/abc"
    assert rec["arxiv_id"] == "1706.03762"
    assert rec["pdf_url"] == "https://example.com/paper.pdf"


def test_openalex_parse_and_abstract_reconstruction(monkeypatch):
    monkeypatch.setattr(ps, "_http_get", lambda url, headers=None: json.dumps(OPENALEX_JSON).encode())
    out = ps._search_openalex("resnet", 5)
    rec = out[0]
    assert rec["venue"] == "CVPR"
    assert rec["year"] == 2016
    assert rec["citation_count"] == 200000
    assert rec["doi"] == "10.1109/cvpr.2016.90"
    assert rec["abstract"] == "Deeper networks are better"


def test_dblp_parse(monkeypatch):
    monkeypatch.setattr(ps, "_http_get", lambda url, headers=None: json.dumps(DBLP_JSON).encode())
    out = ps._search_dblp("bert", 5)
    rec = out[0]
    assert rec["venue"] == "NAACL"
    assert rec["authors"] == ["Jacob Devlin", "Ming-Wei Chang"]
    assert rec["year"] == 2019


def test_openreview_parse(monkeypatch):
    monkeypatch.setattr(ps, "_http_get", lambda url, headers=None: json.dumps(OPENREVIEW_JSON).encode())
    out = ps._search_openreview("iclr", 5)
    rec = out[0]
    assert rec["title"] == "A Great ICLR Paper"
    assert rec["venue"] == "ICLR 2024"
    assert rec["url"] == "https://openreview.net/forum?id=abc123"
    assert rec["pdf_url"] == "https://openreview.net/pdf/abc123.pdf"


# ── Merge / dedup / filter / rank ──────────────────────────────────────────


def test_merge_dedups_by_arxiv_id_and_merges_fields():
    arxiv_rec = ps._norm_record(
        title="Attention Is All You Need", year=2017, source="arxiv", arxiv_id="1706.03762",
        pdf_url="http://arxiv.org/pdf/1706.03762", abstract="",
    )
    s2_rec = ps._norm_record(
        title="Attention is all you need!", year=2017, source="semantic_scholar",
        arxiv_id="1706.03762", citation_count=100000, abstract="We propose the Transformer.",
    )
    merged = ps._merge([arxiv_rec, s2_rec])
    assert len(merged) == 1
    rec = merged[0]
    assert rec["citation_count"] == 100000          # kept the larger
    assert rec["abstract"] == "We propose the Transformer."  # filled the blank
    assert set(rec["sources"]) == {"arxiv", "semantic_scholar"}


def test_merge_dedups_by_title_when_no_ids():
    a = ps._norm_record(title="Deep Learning", source="openalex")
    b = ps._norm_record(title="deep   learning", source="dblp")
    assert len(ps._merge([a, b])) == 1


def test_filters_year_and_venue():
    old = ps._norm_record(title="Old", year=2010, venue="NeurIPS", source="dblp")
    new = ps._norm_record(title="New", year=2023, venue="ICLR", source="dblp")
    assert ps._passes_filters(new, 2020, None, None)
    assert not ps._passes_filters(old, 2020, None, None)
    assert ps._passes_filters(new, None, None, "iclr")
    assert not ps._passes_filters(new, None, None, "emnlp")


def test_paper_search_orchestration_merges_and_records_errors(monkeypatch):
    def fake_arxiv(q, n):
        return [ps._norm_record(title="Shared Paper", source="arxiv", arxiv_id="x1", year=2022)]

    def fake_s2(q, n):
        return [ps._norm_record(title="Shared Paper", source="semantic_scholar", arxiv_id="x1",
                                year=2022, citation_count=50)]

    def boom(q, n):
        raise ValueError("rate limited")

    monkeypatch.setattr(ps, "_ADAPTERS", {"arxiv": fake_arxiv, "semantic_scholar": fake_s2, "dblp": boom})
    result = ps.paper_search("anything", sources=["arxiv", "semantic_scholar", "dblp"])
    assert len(result["results"]) == 1
    assert result["results"][0]["citation_count"] == 50
    assert "dblp" in result["errors"]
    # Internal dedup fields are stripped from the public payload.
    assert "doi" not in result["results"][0]
    assert "arxiv_id" not in result["results"][0]


def test_paper_search_empty_query_short_circuits():
    result = ps.paper_search("   ")
    assert result["results"] == []
    assert "query" in result["errors"]


def test_format_results_for_model_is_compact():
    payload = {
        "query": "transformer",
        "results": [
            {
                "title": "Attention Is All You Need", "authors": ["A", "B", "C", "D"], "year": 2017,
                "venue": "NeurIPS", "abstract": "x" * 1000, "url": "http://u", "pdf_url": "http://p",
                "citation_count": 100000, "source": "semantic_scholar", "sources": ["semantic_scholar", "arxiv"],
            }
        ],
        "errors": {},
    }
    text = ps.format_results_for_model(payload)
    assert "Attention Is All You Need (2017)" in text
    assert "A, B, C et al." in text
    assert "cites: 100000" in text
    assert "…" in text  # abstract truncated


def test_format_results_for_model_no_results():
    text = ps.format_results_for_model({"query": "zzz", "results": [], "errors": {"arxiv": "down"}})
    assert "No papers found" in text
    assert "Sources unavailable" in text
