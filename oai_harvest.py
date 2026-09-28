#!/usr/bin/env python3
"""
Harvest ALL public metadata from MIT's ArchivesSpace instance — i.e. MIT
Libraries Distinctive Collections finding aids — via OAI-PMH.

This is the "public interface" endpoint the MIT Digital Archivist pointed to:
    https://archivesspace.mit.edu/oai?verb=ListRecords&metadataPrefix=oai_dcterms

OAI-PMH's resumptionToken mechanism is designed exactly for this kind of full
bulk harvest (it's how ArchiveGrid harvests MIT's data too), so this is the
primary/recommended tool for a complete export.

USAGE
-----
    # See what OAI "sets" exist (may correspond to different repositories/
    # collecting areas within ArchivesSpace) before deciding scope:
    python oai_harvest.py --list-sets

    # Harvest everything (all sets), writing to ./aspace_oai_harvest/:
    python oai_harvest.py --output-dir ./aspace_oai_harvest

    # Harvest just one set:
    python oai_harvest.py --output-dir ./aspace_oai_harvest --set some-set-spec

OUTPUT
------
<output-dir>/records.jsonl   one JSON object per line, e.g.:
    {
      "identifier": "oai:archivesspace/repositories/2/resources/123",
      "datestamp": "2023-04-01",
      "deleted": false,
      "setSpecs": ["..."],
      "metadata": {"title": [...], "creator": [...], "description": [...], ...}
    }
<output-dir>/raw_pages/page_00001.xml, page_00002.xml, ...
    the raw OAI-PMH XML responses, kept for provenance / re-parsing if needed
<output-dir>/.resume_token
    lets you Ctrl-C and rerun the same command later to pick back up, or
    rerun from scratch by deleting this file

Requires: `pip install requests`
"""
import argparse
import json
import logging
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import requests

OAI_ENDPOINT = "https://archivesspace.mit.edu/oai"
OAI_NS = "{http://www.openarchives.org/OAI/2.0/}"

# Be a good citizen: identify yourself and how to reach you. Please edit the
# contact email below before running a large harvest.
USER_AGENT = (
    "MIT-DistinctiveCollections-Harvester/1.0 "
    "(Women@MIT Fellowship research project; contact: YOUR_EMAIL@mit.edu)"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("oai_harvest")


def strip_ns(tag):
    return tag.split("}", 1)[-1] if "}" in tag else tag


def elem_to_dict(elem):
    """Flatten an XML element's children into {tag: [values]}, dropping
    namespace prefixes. Handles oai_dcterms's flat list-of-elements shape;
    falls back to nested dicts for any deeper structure."""
    out = {}
    for child in elem:
        tag = strip_ns(child.tag)
        text = (child.text or "").strip()
        if len(child) == 0:
            if text:
                out.setdefault(tag, []).append(text)
        else:
            out.setdefault(tag, []).append(elem_to_dict(child))
    return out


def fetch(params, retries=5, backoff=5):
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                OAI_ENDPOINT, params=params,
                headers={"User-Agent": USER_AGENT}, timeout=60,
            )
            if resp.status_code == 200:
                return resp.text
            if resp.status_code in (429, 503):
                wait = backoff * attempt
                log.warning("Got HTTP %s, retrying in %ss (attempt %s/%s)",
                            resp.status_code, wait, attempt, retries)
                time.sleep(wait)
                continue
            resp.raise_for_status()
        except requests.RequestException as e:
            wait = backoff * attempt
            log.warning("Request error: %s - retrying in %ss", e, wait)
            time.sleep(wait)
    raise RuntimeError(f"Failed to fetch OAI-PMH response after {retries} attempts: {params}")


def list_sets():
    xml_text = fetch({"verb": "ListSets"})
    root = ET.fromstring(xml_text)
    sets = []
    for set_el in root.iter(f"{OAI_NS}set"):
        spec = set_el.find(f"{OAI_NS}setSpec")
        name = set_el.find(f"{OAI_NS}setName")
        sets.append({
            "setSpec": spec.text if spec is not None else None,
            "setName": name.text if name is not None else None,
        })
    return sets


def parse_list_records_page(xml_text):
    root = ET.fromstring(xml_text)

    error = root.find(f"{OAI_NS}error")
    if error is not None:
        raise RuntimeError(f"OAI-PMH error ({error.get('code')}): {error.text}")

    lr = root.find(f"{OAI_NS}ListRecords")
    if lr is None:
        return [], None

    records_out = []
    for record_el in lr.findall(f"{OAI_NS}record"):
        header = record_el.find(f"{OAI_NS}header")
        identifier = header.findtext(f"{OAI_NS}identifier")
        datestamp = header.findtext(f"{OAI_NS}datestamp")
        deleted = header.get("status") == "deleted"
        set_specs = [s.text for s in header.findall(f"{OAI_NS}setSpec")]

        rec = {
            "identifier": identifier,
            "datestamp": datestamp,
            "deleted": deleted,
            "setSpecs": set_specs,
            "metadata": None,
        }

        if not deleted:
            metadata_el = record_el.find(f"{OAI_NS}metadata")
            if metadata_el is not None and len(metadata_el) > 0:
                dc_root = metadata_el[0]  # the oai_dcterms wrapper element
                rec["metadata"] = elem_to_dict(dc_root)

        records_out.append(rec)

    resumption_token = None
    token_el = lr.find(f"{OAI_NS}resumptionToken")
    if token_el is not None and token_el.text:
        resumption_token = token_el.text.strip()

    return records_out, resumption_token


def harvest(output_dir: Path, metadata_prefix="oai_dcterms", set_spec=None, delay=1.0):
    output_dir.mkdir(parents=True, exist_ok=True)
    records_path = output_dir / "records.jsonl"
    token_path = output_dir / ".resume_token"
    raw_dir = output_dir / "raw_pages"
    raw_dir.mkdir(exist_ok=True)

    resumption_token = None
    page_num = 0
    total = 0

    if token_path.exists():
        saved = token_path.read_text().strip()
        if saved == "DONE":
            log.info("Harvest already marked complete (%s). Delete .resume_token to redo.", token_path)
            return
        if saved:
            resumption_token = saved
            log.info("Resuming harvest from saved resumption token")

    mode = "a" if resumption_token else "w"
    with open(records_path, mode, encoding="utf-8") as out_f:
        while True:
            page_num += 1
            if resumption_token:
                params = {"verb": "ListRecords", "resumptionToken": resumption_token}
            else:
                params = {"verb": "ListRecords", "metadataPrefix": metadata_prefix}
                if set_spec:
                    params["set"] = set_spec

            log.info("Fetching page %s ...", page_num)
            xml_text = fetch(params)
            (raw_dir / f"page_{page_num:05d}.xml").write_text(xml_text, encoding="utf-8")

            records, resumption_token = parse_list_records_page(xml_text)
            for rec in records:
                out_f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            total += len(records)
            out_f.flush()

            log.info("Page %s: %s records (running total: %s)", page_num, len(records), total)

            if resumption_token:
                token_path.write_text(resumption_token)
                time.sleep(delay)  # politeness delay between requests
            else:
                token_path.write_text("DONE")
                break

    log.info("Harvest complete: %s total records written to %s", total, records_path)


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--output-dir", default="./aspace_oai_harvest",
                         help="Directory to write output into (default: ./aspace_oai_harvest)")
    parser.add_argument("--metadata-prefix", default="oai_dcterms",
                         help="OAI metadataPrefix (default: oai_dcterms)")
    parser.add_argument("--set", dest="set_spec", default=None,
                         help="Limit harvest to one OAI setSpec (see --list-sets)")
    parser.add_argument("--delay", type=float, default=1.0,
                         help="Seconds to wait between page requests (default: 1.0)")
    parser.add_argument("--list-sets", action="store_true",
                         help="List available OAI sets and exit")
    args = parser.parse_args()

    if args.list_sets:
        for s in list_sets():
            print(f"{s['setSpec']}\t{s['setName']}")
        return

    harvest(
        Path(args.output_dir),
        metadata_prefix=args.metadata_prefix,
        set_spec=args.set_spec,
        delay=args.delay,
    )


if __name__ == "__main__":
    main()
