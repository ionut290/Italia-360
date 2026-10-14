#!/usr/bin/env python3
"""Fetch configured public RSS feeds; do not fabricate articles or neighborhood tags."""
import calendar
import datetime as dt
import email.utils
import hashlib
import json
import pathlib
import re
import urllib.request
import xml.etree.ElementTree as ET
from urllib.parse import urlparse

ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCES = json.loads((ROOT / "sources.json").read_text(encoding="utf-8"))["sources"]
OUT = ROOT / "data" / "news.json"
NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}
QUARTIERI = ["Borgo Panigale-Reno", "Navile", "Porto-Saragozza", "San Donato-San Vitale", "Santo Stefano", "Savena"]

def clean(value):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]*>", " ", value or "")).strip()[:500]

def child_text(node, names):
    for name in names:
        el = node.find(name, NS)
        if el is not None and el.text:
            return el.text.strip()
    return ""

def date_iso(raw):
    if not raw:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(raw)
        return parsed.astimezone(dt.timezone.utc).isoformat()
    except (TypeError, ValueError, OverflowError):
        try:
            return dt.datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
        except ValueError:
            return ""

def read_feed(source):
    req = urllib.request.Request(source["url"], headers={"User-Agent":"Italia360NewsBot/1.0 (+https://github.com/ionut290/Italia-360)", "Accept":"application/rss+xml, application/atom+xml, application/xml, text/xml"})
    with urllib.request.urlopen(req, timeout=20) as response:
        data = response.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Feed exceeds 2 MB")
    root = ET.fromstring(data)
    items = root.findall(".//item") or root.findall(".//atom:entry", NS)
    result = []
    for node in items[:60]:
        title = clean(child_text(node, ["title", "atom:title"]))
        link = child_text(node, ["link"])
        if not link:
            for a in node.findall("atom:link", NS):
                if a.get("rel", "alternate") == "alternate":
                    link = a.get("href", "")
                    break
        link = link.strip()
        if link.startswith("http://") and urlparse(link).hostname in ("www.tper.it", "tper.it"):
            link = "https://" + link[len("http://"):]
        parsed = urlparse(link)
        if not title or parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        date = date_iso(child_text(node, ["pubDate", "atom:published", "atom:updated", "dc:date"]))
        description = clean(child_text(node, ["description", "atom:summary"]))
        # No automatic quartier assignment from ambiguous mentions.
        result.append({"id": hashlib.sha256(link.encode()).hexdigest()[:20], "title":title[:180], "url":link, "date":date, "description":description[:260], "source":source["name"], "category":source["category"], "quartiere":""})
    return result


def read_events(source):
    """Bologna Cultura official OpenDataSoft records, including verified quartier labels."""
    req = urllib.request.Request(source["api_url"], headers={
        "User-Agent": "Italia360NewsBot/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=25) as response:
        data = response.read(3_000_001)
    if len(data) > 3_000_000:
        raise ValueError("API response exceeds 3 MB")
    payload = json.loads(data)
    records = payload.get("results", [])
    if not isinstance(records, list):
        raise ValueError("Invalid API results")
    names = {"Borgo Panigale - Reno":"Borgo Panigale-Reno",
             "Porto - Saragozza":"Porto-Saragozza",
             "San Donato - San Vitale":"San Donato-San Vitale"}
    result = []
    now = dt.datetime.now(dt.timezone.utc)
    for record in records:
        title = clean(str(record.get("titolo") or record.get("title") or ""))
        url = str(record.get("url") or "").strip()
        parsed = urlparse(url)
        if not title or parsed.scheme != "https" or not parsed.hostname:
            continue
        start = str(record.get("start") or "")
        end = str(record.get("end") or "")
        try:
            when = dt.datetime.fromisoformat(start[:10]).replace(tzinfo=dt.timezone.utc)
            until = dt.datetime.fromisoformat(end[:10]).replace(tzinfo=dt.timezone.utc) if end else when
            if until < now - dt.timedelta(days=1) or when > now + dt.timedelta(days=120):
                continue
        except ValueError:
            continue
        area = names.get(str(record.get("quartiere") or "").strip(), str(record.get("quartiere") or "").strip())
        if area not in QUARTIERI:
            area = ""
        description = clean(str(record.get("descrizione") or record.get("description") or ""))
        result.append({"id": hashlib.sha256(url.encode()).hexdigest()[:20],
                       "title": title[:180], "url": url, "date": when.isoformat(),
                       "description": description[:260], "source": source["name"],
                       "category": source["category"], "quartiere": area})
    return result

def main():
    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            pass
    old = previous.get("items", [])
    collected, errors, ok = [], [], 0
    for source in SOURCES:
        if source.get("type") not in ("rss", "opendatasoft") or not source.get("enabled"):
            continue
        try:
            articles = read_events(source) if source.get('type') == 'opendatasoft' else read_feed(source)
            if not articles:
                raise ValueError("Il feed non contiene articoli validi")
            collected.extend(articles)
            ok += 1
            print(source["id"], len(articles))
        except Exception as exc:
            errors.append({"source":source["name"], "error":str(exc)[:160]})
            print("ERROR", source["id"], exc)
            for x in old:
                if x.get("source") != source["name"] or not x.get("date"):
                    continue
                try:
                    published = dt.datetime.fromisoformat(x["date"].replace("Z", "+00:00"))
                    if published.tzinfo and published >= dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=7):
                        collected.append(x)
                except ValueError:
                    continue
    seen = {}
    for article in collected:
        seen[article["id"]] = article
    items = sorted(seen.values(), key=lambda x:x.get("date") or "", reverse=True)[:200]
    OUT.parent.mkdir(exist_ok=True)
    payload = {"updated_at":dt.datetime.now(dt.timezone.utc).isoformat(), "items":items, "errors":errors, "feeds_ok":ok}
    OUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    print("Saved",len(items),"articles")
if __name__ == "__main__":
    main()
