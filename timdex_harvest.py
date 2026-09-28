#!/usr/bin/env python3
"""
Harvest metadata from MIT's TIMDEX GraphQL API, scoped to just the
ArchivesSpace source (MIT Libraries Distinctive Collections finding aids) —
NOT the general Alma/Primo library catalog.

Endpoint: https://timdex.mit.edu/graphql

Use this as a COMPLEMENT to oai_harvest.py, not a replacement:
- TIMDEX normalizes metadata into a richer common schema (subjects,
  contributors, dates, rights, related items, holdings, etc.) which can be
  handy for cross-referencing or enrichment.
- Its `search` query only exposes a `from` argument for pagination (no
  adjustable page size), so for a very large collection it will need many
  more requests than the OAI-PMH route. Use oai_harvest.py as your primary
  full bulk export.

USAGE
-----
    # First, confirm the exact sourceFilter key TIMDEX uses for ArchivesSpace
    # (don't guess it - the aggregation is the source of truth):
    python timdex_harvest.py --list-sources

    # Then harvest (auto-detects the ArchivesSpace key if you skip --source-key):
    python timdex_harvest.py --output records_timdex.jsonl

Requires: `pip install requests`
"""
import argparse
import json
import logging
import time

import requests

GRAPHQL_ENDPOINT = "https://timdex.mit.edu/graphql"
USER_AGENT = (
    "MIT-DistinctiveCollections-Harvester/1.0 "
    "(Women@MIT Fellowship research project; contact: YOUR_EMAIL@mit.edu)"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("timdex_harvest")

RECORD_FIELDS = """
    timdexRecordId
    title
    alternateTitles { kind value }
    source
    sourceLink
    contentType
    format
    contributors { kind value affiliation mitAffiliated identifier }
    dates { kind value note range { gte lte } }
    subjects { kind value }
    notes { kind value }
    identifiers { kind value }
    relatedItems { relationship description uri itemType }
    rights { kind description uri }
    languages
    physicalDescription
    holdings { collection location callnumber summary notes format }
    links { kind url text restrictions }
    summary
"""


def post_graphql(query, variables=None, retries=5, backoff=5):
    payload = {"query": query, "variables": variables or {}}
    for attempt in range(1, retries + 1):
        try:
            resp = requests.post(
                GRAPHQL_ENDPOINT, json=payload,
                headers={"User-Agent": USER_AGENT}, timeout=60,
            )
            if resp.status_code == 200:
                data = resp.json()
                if "errors" in data:
                    raise RuntimeError(f"GraphQL errors: {data['errors']}")
                return data["data"]
            if resp.status_code in (429, 503):
                wait = backoff * attempt
                log.warning("Got HTTP %s, retrying in %ss", resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            wait = backoff * attempt
            log.warning("Request error: %s - retrying in %ss", e, wait)
            time.sleep(wait)
    raise RuntimeError("Failed to reach TIMDEX GraphQL endpoint after retries")


def get_source_keys():
    """Query the `source` aggregation to discover the exact sourceFilter
    key(s) TIMDEX uses, rather than guessing a string like "archivesspace"."""
    query = "query { search { aggregations { source { key docCount } } } }"
    data = post_graphql(query)
    return data["search"]["aggregations"]["source"]


def find_archivesspace_key(source_keys):
    return [
        s for s in source_keys
        if "archivesspace" in s["key"].lower() or "archives space" in s["key"].lower()
    ]


def harvest(output_path, source_key, delay=0.5):
    query = (
        "query($from: String, $sourceFilter: [String!]) { "
        "  search(from: $from, sourceFilter: $sourceFilter) { "
        "    hits "
        "    records { " + RECORD_FIELDS + " } "
        "  } "
        "}"
    )

    fetched = 0
    from_offset = 0
    total_hits = None

    with open(output_path, "w", encoding="utf-8") as out_f:
        while True:
            variables = {"from": str(from_offset), "sourceFilter": [source_key]}
            data = post_graphql(query, variables)
            search = data["search"]
            total_hits = search["hits"]
            records = search["records"]

            if not records:
                break

            for rec in records:
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            fetched += len(records)
            out_f.flush()

            log.info("Fetched %s / %s records (from=%s)", fetched, total_hits, from_offset)

            from_offset += len(records)
            if total_hits is not None and from_offset >= total_hits:
                break
            time.sleep(delay)

    log.info(
        "Done: %s records written to %s (source reported %s total hits)",
        fetched, output_path, total_hits,
    )


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output", default="records_timdex.jsonl")
    parser.add_argument(
        "--list-sources", action="store_true",
        help="Print all source-aggregation keys with record counts and exit "
             "(use this to confirm the exact ArchivesSpace key)",
    )
    parser.add_argument(
        "--source-key", default=None,
        help="Exact sourceFilter key to use; if omitted, auto-detected via the source aggregation",
    )
    parser.add_argument("--delay", type=float, default=0.5,
                         help="Seconds to wait between requests (default: 0.5)")
    args = parser.parse_args()

    source_keys = get_source_keys()

    if args.list_sources:
        for s in source_keys:
            print(f"{s['key']}\t({s['docCount']} records)")
        return

    key = args.source_key
    if not key:
        matches = find_archivesspace_key(source_keys)
        if not matches:
            log.error(
                "Could not auto-detect an ArchivesSpace source key. "
                "Run with --list-sources to see the available options and pass "
                "--source-key explicitly."
            )
            return
        if len(matches) > 1:
            log.warning("Multiple candidate source keys found: %s - using the first.", matches)
        key = matches[0]["key"]
        log.info("Using detected source key: %r", key)

    harvest(args.output, key, delay=args.delay)


if __name__ == "__main__":
    main()
