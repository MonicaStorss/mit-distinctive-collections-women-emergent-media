# MIT Libraries Distinctive Collections — metadata harvest

Two scripts, for the two sources the MIT librarians pointed you to. Both are
independent — run one or both depending on what you need.

## Setup

```bash
pip install requests
```

Before running either script at scale, open it and edit the `USER_AGENT`
string to include your real contact email. This is standard etiquette for
bulk harvesting — it lets MIT's systems staff reach you if something about
your harvest needs attention, instead of just blocking you.

## 1. `oai_harvest.py` — primary tool, full bulk export

Harvests via OAI-PMH from `https://archivesspace.mit.edu/oai`, which is MIT's
ArchivesSpace instance — i.e. Distinctive Collections' finding aids — and is
**not** the same system as the general Alma/Primo library catalog, so you
don't need to filter anything out; the scope is already correct.

```bash
# See what OAI "sets" exist first (optional, but useful for scoping):
python oai_harvest.py --list-sets

# Full harvest of every record, in oai_dcterms (Dublin Core Terms) format:
python oai_harvest.py --output-dir ./aspace_oai_harvest

# Or just one set:
python oai_harvest.py --output-dir ./aspace_oai_harvest --set <setSpec>
```

Output:
- `records.jsonl` — one JSON object per line: `identifier`, `datestamp`,
  `deleted` flag, `setSpecs`, and a `metadata` dict of the Dublin Core Terms
  fields (title, creator, description, date, subject, identifier, etc. —
  each as a list of values).
- `raw_pages/page_00001.xml`, `page_00002.xml`, ... — the raw OAI-PMH
  responses, kept for provenance in case you need to re-parse anything.
- `.resume_token` — if the harvest is interrupted, rerunning the exact same
  command picks back up where it left off. Delete this file to start over.

This uses OAI-PMH's `resumptionToken` mechanism, which is built for exactly
this kind of complete, paginated bulk export (it's the same approach
ArchiveGrid uses to harvest MIT's data).

## 2. `timdex_harvest.py` — secondary tool, enriched/normalized view

Harvests via TIMDEX's GraphQL API (`https://timdex.mit.edu/graphql`),
filtered to just the ArchivesSpace source (excluding the Alma catalog and
DSpace@MIT).

```bash
# Confirm the exact sourceFilter key TIMDEX uses (don't guess it):
python timdex_harvest.py --list-sources

# Harvest (auto-detects the ArchivesSpace key):
python timdex_harvest.py --output records_timdex.jsonl
```

TIMDEX returns a richer, normalized schema per record (structured
contributors, dates, subjects, rights statements, related items, holdings),
which can be useful for cross-referencing or enrichment work. But its search
endpoint only exposes a `from` offset for pagination — no adjustable page
size — so for a very large collection it needs many more round-trips than
OAI-PMH. Treat this as a complement to `oai_harvest.py`, not your primary
export.

## Notes

- Both scripts retry with backoff on rate-limit/server errors and pace
  requests with a politeness delay (`--delay`, adjustable).
- If a harvest gets blocked by bot-detection/WAF rules, that's usually a
  generic anti-scraping measure rather than anything MIT staff intend to
  block for you — Thera Webb or Joe (the Digital Archivist) can likely
  whitelist your IP or suggest a lower-traffic window if this comes up.
- Converting `records.jsonl` to CSV/Excel for analysis is a one-liner with
  `pandas` (`pd.read_json(path, lines=True)`) once you've decided which
  metadata fields you want as columns — happy to help build that once you've
  seen the shape of the actual harvested data.
