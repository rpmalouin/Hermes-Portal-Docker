#!/usr/bin/env python3
"""Check every link a running Hermes Portal emits, in the three places it emits them.

An audit of the live deployment found 367 dead links, all one class of defect: a link
generated for a record that has no page, or with an id that was not URL-encoded.  The
renderer already had the honest rule (:func:`hermes.portal.render.row_target`); the
command palette, the favourites rail and the cron rows each carried a stale copy of the
old guess.  This script is that audit made repeatable, so the next copy is caught by a
check rather than by a browser.

Three layers, against a *running* portal:

* **(a) page links** -- every ``<a href>`` rendered on ``/``, ``/favorites``,
  ``/search?q=...`` and each domain page (the domains come from ``/index.json``).
* **(b) record ids** -- every record id in each ``/<domain>.json``, as the detail URL
  the UI emits (``urllib.parse.quote(id, safe="")``), plus the record's own ``href``
  when it has one (the palette and the row both follow it).  A record with no href
  whose id-derived URL 404s is not a broken link: the UI links a row only when the
  domain confirms a page, and renders plain text otherwise.
* **(c) palette hits** -- every hit in ``/search.json`` for the query list, followed
  exactly as the post-fix palette follows it: the hit's own ``url`` when present, and
  no request at all when it is empty.  This is the layer that catches a regression in
  the command palette.

Usage::

    python3 tools/link_check.py                     # default http://127.0.0.1:8087
    python3 tools/link_check.py http://127.0.0.1:8098
    python3 tools/link_check.py --queries cron,vault,the --limit 6
    python3 tools/link_check.py --json

The portal package needs Python 3.11+; this check runs on the host's Python 3.10, so it
imports nothing from the package and keeps its own syntax 3.10-compatible.  It only
reads: every request is a GET.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

DEFAULT_BASE = "http://127.0.0.1:8087"
#: ~15 words broad enough to reach most domains, the shape the live audit used.
DEFAULT_QUERIES = (
    "cron",
    "vault",
    "session",
    "agent",
    "memory",
    "graph",
    "error",
    "log",
    "skill",
    "plugin",
    "usage",
    "health",
    "message",
    "docker",
    "the",
)
#: The palette calls ``/search.json`` with the per-domain cap of 6.
DEFAULT_LIMIT = 6
#: A page's own links: internal hrefs only (fragments and external links are not ours).
HREF_RE = re.compile(r'href="([^"#]*)"')
TIMEOUT = 20.0
#: How many broken URLs the human-readable report shows before summarising.
MAX_SHOWN = 20

LAYER_LABELS = {
    "a": "page links",
    "b": "record ids",
    "c": "palette hits",
}


@dataclass(frozen=True)
class Ref:
    """One broken URL, and the page that emitted it."""

    layer: str
    source: str
    url: str


@dataclass
class Layer:
    """What one layer checked and what broke."""

    key: str
    checked: int = 0
    broken: list[Ref] = field(default_factory=list)
    #: Records with no href whose id-derived URL has no page -- plain-text rows.
    pageless: int = 0


def fetch(base: str, path: str) -> tuple[int, str]:
    """GET one path; status 0 means no response at all."""
    url = urllib.parse.urljoin(base, path)
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except Exception as exc:  # noqa: BLE001 - a dead server is a failing check
        return 0, f"{type(exc).__name__}: {exc}"


def get_json(base: str, path: str) -> object | None:
    """GET one JSON route, or ``None`` when it did not answer with JSON."""
    status, body = fetch(base, path)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def encoded(path: str) -> str:
    """Quote one path segment the way ``detail_url`` does (a slash stays inside)."""
    return urllib.parse.quote(str(path), safe="")


def detail_path(domain: str, record_id: object) -> str:
    """The detail URL for a record id, in the UI's encoded form."""
    return f"/{encoded(domain)}/{encoded(record_id)}"


def domain_keys(index: object) -> list[str]:
    """The domain keys ``/index.json`` lists, in order."""
    if not isinstance(index, dict):
        return []
    keys = []
    for entry in index.get("domains") or []:
        if isinstance(entry, dict) and entry.get("key"):
            keys.append(str(entry["key"]))
    return keys


def check_pages(base: str, domains: list[str], queries: list[str]) -> Layer:
    """Layer (a): every ``<a href>`` on the index, favourites and domain pages."""
    layer = Layer("a")
    pages = ["/", "/favorites"]
    pages += ["/" + encoded(domain) for domain in domains]
    pages += ["/search?q=" + urllib.parse.quote(query, safe="") for query in queries]
    for page in pages:
        status, body = fetch(base, page)
        layer.checked += 1
        if status != 200:
            # A page that will not load is the loudest broken link there is.
            layer.broken.append(Ref("a", page, page))
            continue
        for raw in sorted(set(HREF_RE.findall(body))):
            target = html.unescape(raw)
            if not target.startswith("/"):
                continue
            layer.checked += 1
            code, _body = fetch(base, target)
            if code != 200:
                layer.broken.append(Ref("a", page, target))
    return layer


def check_records(base: str, domains: list[str]) -> Layer:
    """Layer (b): every record id in ``/<domain>.json``, plus every record href."""
    layer = Layer("b")
    for domain in domains:
        route = "/" + encoded(domain) + ".json"
        payload = get_json(base, route)
        if payload is None:
            layer.broken.append(Ref("b", route, route))
            continue
        if not isinstance(payload, dict):
            continue
        for collection in payload.get("collections") or []:
            if not isinstance(collection, dict):
                continue
            for record in collection.get("records") or []:
                if not isinstance(record, dict):
                    continue
                record_id = record.get("id")
                if record_id in (None, ""):
                    continue
                href = str(record.get("href") or "")
                # The UI emits the detail URL whenever the domain confirms a page.
                # Asking over HTTP is the same question: a 404 with no href is the
                # plain-text row `row_target` already produces, not a dead link.
                layer.checked += 1
                code, _body = fetch(base, detail_path(domain, record_id))
                if code != 200 and not href:
                    layer.pageless += 1
                if href:
                    # The record carries its own target; the UI follows it as-is,
                    # so a non-answer here is a broken link whatever the id would do.
                    layer.checked += 1
                    code, _body = fetch(base, href)
                    if code != 200:
                        layer.broken.append(Ref("b", route, href))
    return layer


def search_route(query: str, limit: int) -> str:
    """The ``/search.json`` route the palette calls for one query."""
    wanted = urllib.parse.quote(query, safe="")
    return f"/search.json?limit={limit}&q={wanted}"


def check_palette(base: str, queries: list[str], limit: int) -> Layer:
    """Layer (c): every palette hit's ``url``, skipping hits with no page."""
    layer = Layer("c")
    for query in queries:
        route = search_route(query, limit)
        payload = get_json(base, route)
        if payload is None:
            layer.broken.append(Ref("c", route, route))
            continue
        if not isinstance(payload, dict):
            continue
        groups = payload.get("groups") or {}
        order = payload.get("order") or list(groups)
        for domain_key in order:
            for hit in groups.get(domain_key) or []:
                if not isinstance(hit, dict):
                    continue
                url = str(hit.get("url") or "")
                if not url:
                    # The palette renders this hit as plain text and requests
                    # nothing, so neither does the check.
                    continue
                layer.checked += 1
                code, _body = fetch(base, url)
                if code != 200:
                    layer.broken.append(Ref("c", route, url))
    return layer


def build_report(base: str, queries: list[str], limit: int) -> dict:
    """Run all three layers and return the machine-readable result."""
    index = get_json(base, "/index.json")
    domains = domain_keys(index)
    layers = [
        check_pages(base, domains, queries),
        check_records(base, domains),
        check_palette(base, queries, limit),
    ]
    broken = [ref for layer in layers for ref in layer.broken]
    return {
        "base": base,
        "ok": not broken,
        "domains": domains,
        "queries": queries,
        "limit": limit,
        "layers": {
            layer.key: {
                "label": LAYER_LABELS[layer.key],
                "checked": layer.checked,
                "broken": len(layer.broken),
                "pageless": layer.pageless,
            }
            for layer in layers
        },
        "broken": [
            {"layer": ref.layer, "source": ref.source, "url": ref.url} for ref in broken
        ],
    }


def print_report(report: dict) -> None:
    """Print per-layer counts, then up to 20 broken URLs grouped by their page."""
    for key in ("a", "b", "c"):
        layer = report["layers"][key]
        extra = ""
        if layer["pageless"]:
            extra = " ({} have no page)".format(layer["pageless"])
        print(
            "  layer ({}) {:<13} checked {:>5} / broken {}{}".format(
                key, layer["label"], layer["checked"], layer["broken"], extra
            )
        )

    broken = report["broken"]
    if broken:
        print(f"\n  broken links ({len(broken)}):")
        grouped: dict[tuple[str, str], list[dict]] = {}
        for ref in broken:
            grouped.setdefault((ref["layer"], ref["source"]), []).append(ref)
        shown = 0
        for (layer, source), refs in grouped.items():
            print(f"    {source} -> {LAYER_LABELS[layer]}")
            for ref in refs:
                if shown >= MAX_SHOWN:
                    break
                print("      {}".format(ref["url"]))
                shown += 1
            if shown >= MAX_SHOWN:
                break
        if len(broken) > shown:
            print(f"    ... {len(broken) - shown} more")

    print()
    if broken:
        print(
            "  {} broken link(s) across {} domain(s).".format(
                len(broken), len(report["domains"])
            )
        )
    else:
        print(
            "  all links resolve: {} domain(s), {} layer(s) clean.".format(
                len(report["domains"]), len(report["layers"])
            )
        )


def main(argv: list[str] | None = None) -> int:
    """Check a running portal and report; 0 when every layer is clean, else 1."""
    parser = argparse.ArgumentParser(
        description="Check every link a running Hermes Portal emits, in three layers."
    )
    parser.add_argument(
        "base_url",
        nargs="?",
        default=DEFAULT_BASE,
        help=f"portal base URL (default {DEFAULT_BASE})",
    )
    parser.add_argument(
        "--queries",
        default="",
        help="comma-separated search words for layers (a) and (c)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"per-domain search cap, as the palette sends (default {DEFAULT_LIMIT})",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable result")
    args = parser.parse_args(argv)

    queries = [part.strip() for part in args.queries.split(",") if part.strip()]
    if not queries:
        queries = list(DEFAULT_QUERIES)
    base = args.base_url.rstrip("/")

    report = build_report(base, queries, args.limit)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"\n  checking {base}")
        print_report(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
