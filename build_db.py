#!/usr/bin/env python
"""
build_db.py — build a SQLite database from the vault's `.data.json` and `.text.md` sidecars.

    python build_db.py                        # -> <vault>/autonotes.sqlite
    python build_db.py -o /tmp/autonotes.sqlite

The sidecars are canonical; this database is derived. It is rebuilt from scratch every time
(seconds), written to a temporary file and swapped in atomically, so a reader never sees a
half-built file and a sync tool never sees a partial write. Do not edit it; edit the sources.

Tables:
    documents(doc, file, path, sha256, pages, doc_type, category, product_type, title,
              manufacturer, revision, parts, lcsc, doi, arxiv, authors, year, original_name, json)
    params(doc, key, min, typ, max, unit, confidence, contested, verified_by, verified_on,
           page, flags)
    readings(doc, key, source, extractor, page, file, min, max)
    enrichment(doc, source, file, category, date, field, value)
    spec_rows(doc, page, section, grp, symbol, parameter, conditions, min, typ, max, unit,
              extractor, confidence, flags)
    registers(doc, name, address, reset, page, description)
    register_fields(doc, register, address, bits, name, access, reset, description)
    pages(doc, page, text)      -- FTS5 full-text index over every page of every .text.md

Example queries:
    SELECT doc, min, max FROM params WHERE key='vin' AND max >= 24 AND confidence='high';
    SELECT doc, key FROM params WHERE contested AND verified_by IS NULL;   -- needs a person
    SELECT * FROM readings WHERE doc='tps55287' AND key='vin';            -- why it is contested
    SELECT doc, page, snippet(pages, 2, '[', ']', '…', 12) FROM pages WHERE pages MATCH 'IQ NEAR inversion';
"""
import argparse
import json
import os
import re
import sqlite3
from pathlib import Path

import vaultpath

SCHEMA = """
CREATE TABLE documents(doc TEXT PRIMARY KEY, file TEXT, path TEXT, sha256 TEXT, pages INTEGER,
    doc_type TEXT, category TEXT, product_type TEXT, title TEXT, manufacturer TEXT,
    revision TEXT, parts TEXT, lcsc TEXT, doi TEXT, arxiv TEXT, authors TEXT, year TEXT,
    original_name TEXT, json TEXT);
CREATE TABLE params(doc TEXT, key TEXT, min REAL, typ REAL, max REAL, unit TEXT,
    confidence TEXT CHECK (confidence IN ('high','medium','low','none')),
    contested INTEGER, verified_by TEXT, verified_on TEXT, page INTEGER, flags TEXT);
-- every reading behind a param: the evidence, not just the verdict
CREATE TABLE readings(doc TEXT, key TEXT, source TEXT, extractor TEXT, page INTEGER,
    file TEXT, min REAL, max REAL);
-- external tables (vendor exports, later distributor data), one row per field
CREATE TABLE enrichment(doc TEXT, source TEXT, file TEXT, category TEXT, date TEXT,
    field TEXT, value TEXT);
CREATE TABLE spec_rows(doc TEXT, page INTEGER NOT NULL, section TEXT, grp TEXT, symbol TEXT,
    parameter TEXT, conditions TEXT, min REAL, typ REAL, max REAL, unit TEXT, extractor TEXT,
    confidence TEXT CHECK (confidence IN ('high','medium','low','none')), flags TEXT);
CREATE TABLE registers(doc TEXT, name TEXT, address TEXT, reset TEXT, page INTEGER,
    description TEXT);
CREATE TABLE register_fields(doc TEXT, register TEXT, address TEXT, bits TEXT, name TEXT,
    access TEXT, reset TEXT, description TEXT);
CREATE VIRTUAL TABLE pages USING fts5(doc UNINDEXED, page UNINDEXED, text,
    tokenize = 'unicode61');
CREATE INDEX params_key ON params(key, confidence);
CREATE INDEX spec_doc ON spec_rows(doc, page);
CREATE INDEX spec_symbol ON spec_rows(symbol);
"""
_PAGE = re.compile(r"^=== PAGE (\d+) ===$", re.M)


def split_pages(text: str):
    body = text.split("\n---\n", 1)[-1]            # drop frontmatter
    marks = list(_PAGE.finditer(body))
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(body)
        yield int(m.group(1)), body[m.end():end].strip()


def build(vault: Path, out: Path):
    tmp = out.with_name(out.name + ".tmp")
    if tmp.exists():
        tmp.unlink()
    db = sqlite3.connect(tmp)
    db.executescript(SCHEMA)
    root = vaultpath.root_of(vault)                  # AutoNotes/
    n_docs = n_pages = 0
    for data_file in sorted(root.rglob("*.data.json")):
        data = json.loads(data_file.read_text(encoding="utf-8"))
        d = data.get("document", {})
        doc = data_file.name[: -len(".data.json")]
        ids, paper = d.get("ids") or {}, d.get("paper") or {}
        db.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (doc, d.get("file"), data_file.parent.relative_to(root).as_posix(),
                    d.get("sha256"), d.get("pages"), d.get("doc_type"), d.get("category"),
                    d.get("product_type"), d.get("title"), d.get("manufacturer"),
                    d.get("revision"), json.dumps(d.get("parts") or []),
                    ids.get("lcsc"), ids.get("doi"), ids.get("arxiv"), paper.get("authors"),
                    paper.get("year"), d.get("original_name"),
                    json.dumps(d, ensure_ascii=False)))
        for p in data.get("params", []):
            v = p.get("verified") if isinstance(p.get("verified"), dict) else {}
            db.execute("INSERT INTO params VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                       (doc, p["key"], p.get("min"), p.get("typ"), p.get("max"), p.get("unit"),
                        p.get("confidence"), int(bool(p.get("contested"))), v.get("by"),
                        v.get("on"), p.get("page"), json.dumps(p.get("flags") or [])))
            db.executemany("INSERT INTO readings VALUES (?,?,?,?,?,?,?,?)",
                           [(doc, p["key"], r.get("source"), r.get("extractor"), r.get("page"),
                             r.get("file"), r.get("min"), r.get("max"))
                            for r in p.get("readings", [])])
        for source, e in (data.get("enrichment") or {}).items():
            fields = {**(e.get("values") or {}), **(e.get("unmapped") or {})}
            db.executemany("INSERT INTO enrichment VALUES (?,?,?,?,?,?,?)",
                           [(doc, source, e.get("file"), e.get("category"), e.get("date"),
                             k, str(v)) for k, v in fields.items()])
        db.executemany("INSERT INTO spec_rows VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       [(doc, r["page"], r.get("section"), r.get("group"), r.get("symbol"),
                         r.get("parameter"), r.get("conditions"), r.get("min"), r.get("typ"),
                         r.get("max"), r.get("unit"), r.get("extractor"), r.get("confidence"),
                         json.dumps(r.get("flags") or [])) for r in data.get("spec_rows", [])])
        for reg in data.get("registers", []):
            db.execute("INSERT INTO registers VALUES (?,?,?,?,?,?)",
                       (doc, reg.get("name"), reg.get("address"), reg.get("reset"),
                        reg.get("page") or reg.get("source_page"), reg.get("description")))
            db.executemany("INSERT INTO register_fields VALUES (?,?,?,?,?,?,?,?)",
                           [(doc, reg.get("name"), reg.get("address"), f.get("bits"),
                             f.get("name"), f.get("access"), f.get("reset"), f.get("description"))
                            for f in reg.get("fields", [])])
        text_file = data_file.with_name(doc + ".text.md")
        if text_file.exists():
            rows = list(split_pages(text_file.read_text(encoding="utf-8")))
            db.executemany("INSERT INTO pages VALUES (?,?,?)", [(doc, n, t) for n, t in rows])
            n_pages += len(rows)
        n_docs += 1
    db.commit()
    db.close()
    os.replace(tmp, out)
    return n_docs, n_pages


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--vault", help="AutoNotes vault (see vaultpath.py)")
    ap.add_argument("-o", "--output", type=Path, help="default: <AutoNotes>/autonotes.sqlite")
    args = ap.parse_args()
    vault = vaultpath.find_vault(args.vault)
    out = args.output or vaultpath.root_of(vault) / "autonotes.sqlite"
    docs, pages = build(vault, out)
    print(f"{out}: {docs} documents, {pages} pages indexed, {out.stat().st_size / 2**20:.1f} MB")


if __name__ == "__main__":
    main()
