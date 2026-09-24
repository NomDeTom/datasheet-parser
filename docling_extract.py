#!/usr/bin/env python
"""
docling_extract.py — layout-aware extraction with Docling, chunked and resumable.

Docling renders each page and runs layout detection (RT-DETRv2) and table-structure
recognition (TableFormer) over it; the text itself still comes from the PDF's text layer. It
recovers table columns that pdfplumber merges, and headings that plain text loses. It is also
slow on CPU (~7-8 s/page) and memory-hungry, so this script:

  * converts in page chunks (default 20) to bound memory,
  * writes each chunk atomically and skips chunks already on disk, so an interrupted batch
    resumes where it stopped,
  * keys the cache to the PDF's SHA-256, so a changed PDF is never mixed with a stale cache,
  * never embeds pictures: figures become a placeholder comment, not base64.

Usage:
    python docling_extract.py datasheet.pdf [more.pdf ...]
    python docling_extract.py --list files.txt          # one PDF path per line
    python docling_extract.py big.pdf --pages 40-60     # only part of a document
    python docling_extract.py x.pdf --chunk 10 --table-mode fast
    python docling_extract.py x.pdf --reexport          # redo markdown from the cached chunks
    python docling_extract.py scan.pdf --ocr            # no text layer: full-page RapidOCR

Output, per PDF, in output/docling/<sha256[:16]>/ — keyed by content, not name, so renaming or
moving a PDF never invalidates (or duplicates) hours of conversion:
    manifest.json          sha256, page count, versions, options, per-chunk timing and peak RSS
    pNNNN-MMMM.json        the DoclingDocument for that chunk (tables with page + bbox provenance)
    pNNNN-MMMM.pages.json  {page_no: markdown} for that chunk

`sidecars.py` assembles these into the vault's full-text and JSON sidecars; it runs in the
normal environment. This script needs Docling, which is large (~2 GB with models) and is best
kept in its own venv. If `docling` is not importable here and DOCLING_PYTHON names an
interpreter that has it, the script re-runs itself under that interpreter. Recommended
environment for CPU-only machines (see README):

    HF_HUB_OFFLINE=1        after the first run has fetched the models
    OMP_NUM_THREADS=<cores> keep torch from oversubscribing
    TMPDIR=<disk path>      if /tmp is RAM-backed
"""
import argparse
import gc
import hashlib
import json
import os
import re
import resource
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE / "output"
SCHEMA = 1


def _reexec_or_die():
    alt = os.environ.get("DOCLING_PYTHON")
    # compare unresolved: venvs often symlink to one shared base interpreter
    if alt and os.path.abspath(alt) != os.path.abspath(sys.executable) and not os.environ.get("_DOCLING_REEXEC"):
        os.environ["_DOCLING_REEXEC"] = "1"
        os.execv(alt, [alt, str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.exit("docling is not importable by this interpreter.\n"
             "Install it (pip install docling), or set DOCLING_PYTHON to the python of a venv "
             "that has it.")


try:
    from docling.datamodel.base_models import InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions, TableFormerMode
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling_core.types.doc import ImageRefMode
    import pypdfium2
except ImportError:
    _reexec_or_die()


def cache_dir(digest: str, ocr: bool = False) -> Path:
    """Text-layer runs and OCR runs of the same PDF are different evidence, cached apart."""
    return OUTPUT / "docling" / (digest[:16] + ("-ocr" if ocr else ""))


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def page_count(path: Path) -> int:
    pdf = pypdfium2.PdfDocument(str(path))
    try:
        return len(pdf)
    finally:
        pdf.close()


def versions():
    from importlib.metadata import version
    return {k: version(k) for k in ("docling", "docling-core", "docling-parse")}


def peak_rss_mb() -> float:
    # ru_maxrss is KiB on Linux, bytes on macOS
    r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return round(r / (1 << 20 if sys.platform == "darwin" else 1 << 10), 1)


def write_atomic(path: Path, text: str):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def make_converter(table_mode: str, timeout: float | None, ocr: bool = False):
    opts = PdfPipelineOptions()
    # Datasheets normally have a text layer, and OCR over it only costs time (3.6x, identical
    # tables). --ocr is for the ones that don't: scans, image tiles, text drawn as outlines. Full-
    # page OCR, because an outline-text page has no bitmap region for Docling to target.
    opts.do_ocr = ocr
    if ocr:
        from docling.datamodel.pipeline_options import RapidOcrOptions
        opts.ocr_options = RapidOcrOptions(force_full_page_ocr=True)
    opts.do_table_structure = True
    opts.table_structure_options.mode = (TableFormerMode.FAST if table_mode == "fast"
                                         else TableFormerMode.ACCURATE)
    opts.generate_picture_images = False      # no picture bitmaps kept, nothing to embed
    opts.generate_page_images = False
    if timeout:
        opts.document_timeout = timeout
    return DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=opts)})


_LEADER = re.compile(r"(?:\s?\.){4,}\s?")


def page_markdown(doc, a: int, b: int) -> dict:
    """{page_no: markdown} with compact tables — Docling otherwise pads every cell to align
    columns, and contents pages become kilobytes of dot leaders."""
    out = {}
    for p in range(a, b + 1):
        md = doc.export_to_markdown(page_no=p, image_mode=ImageRefMode.PLACEHOLDER,
                                    compact_tables=True)
        out[p] = _LEADER.sub(" … ", md)
    return out


def reexport(pdf: Path) -> int:
    """Rebuild every chunk's pages.json from its stored DoclingDocument, without re-converting."""
    from docling_core.types.doc import DoclingDocument
    out_dir = cache_dir(sha256(pdf), getattr(reexport, "ocr", False))
    n = 0
    for js in sorted(out_dir.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        a, b = (int(x) for x in js.stem[1:].split("-"))
        doc = DoclingDocument.model_validate_json(js.read_text(encoding="utf-8"))
        write_atomic(js.with_name(js.stem + ".pages.json"),
                     json.dumps(page_markdown(doc, a, b), ensure_ascii=False, indent=0))
        n += 1
    return n


def chunks(first: int, last: int, size: int):
    a = first
    while a <= last:
        yield a, min(a + size - 1, last)
        a += size


def chunk_name(a: int, b: int) -> str:
    return f"p{a:04d}-{b:04d}"


def load_manifest(out_dir: Path, pdf: Path, digest: str, n_pages: int, options: dict):
    path = out_dir / "manifest.json"
    if path.exists():
        m = json.loads(path.read_text(encoding="utf-8"))
        if m.get("sha256") != digest:
            raise SystemExit(f"{out_dir}: sha256 prefix collision with {m.get('source')}")
        if m.get("options") != options:
            raise SystemExit(f"{out_dir}: cache was made with options {m.get('options')}; "
                             f"now {options}. Move it aside to re-extract.")
        return m
    return {"schema": SCHEMA, "source": pdf.name, "sha256": digest, "pages": n_pages,
            "options": options, "versions": versions(), "chunks": {}}


def extract(pdf: Path, converter, args) -> dict:
    digest = sha256(pdf)
    n = page_count(pdf)
    first, last = 1, n
    if args.pages:
        a, _, b = args.pages.partition("-")
        first, last = max(1, int(a)), min(n, int(b or a))
    out_dir = cache_dir(digest, args.ocr)
    out_dir.mkdir(parents=True, exist_ok=True)
    options = {"ocr": "rapidocr-full-page" if args.ocr else False,
               "table_mode": args.table_mode, "images": "placeholder"}
    manifest = load_manifest(out_dir, pdf, digest, n, options)

    todo = [(a, b) for a, b in chunks(first, last, args.chunk)
            if not (out_dir / f"{chunk_name(a, b)}.pages.json").exists()]
    done = 0
    print(f"{pdf.name}: {n} pages, {len(todo)} chunk(s) to do", flush=True)
    for a, b in todo:
        name = chunk_name(a, b)
        t0 = time.monotonic()
        status = "ok"
        try:
            res = converter.convert(str(pdf), page_range=(a, b), raises_on_error=False)
            doc = res.document
            status = res.status.value if hasattr(res.status, "value") else str(res.status)
            if status not in ("success", "partial_success"):
                raise RuntimeError(f"conversion {status}: {[e.error_message for e in res.errors][:3]}")
            pages = page_markdown(doc, a, b)
            write_atomic(out_dir / f"{name}.json",
                         json.dumps(doc.export_to_dict(), ensure_ascii=False))
            write_atomic(out_dir / f"{name}.pages.json",
                         json.dumps(pages, ensure_ascii=False, indent=0))
        except Exception as exc:  # one bad chunk must not stop a batch; it is recorded and retried next run
            status = f"error: {type(exc).__name__}: {exc}"[:300]
        dt = time.monotonic() - t0
        manifest["chunks"][name] = {"status": status, "seconds": round(dt, 1),
                                    "s_per_page": round(dt / (b - a + 1), 2),
                                    "peak_rss_mb": peak_rss_mb()}
        write_atomic(out_dir / "manifest.json", json.dumps(manifest, indent=1))
        print(f"  {name}  {status:10.10}  {dt:6.1f}s  {dt / (b - a + 1):5.2f} s/p  "
              f"rss {peak_rss_mb():.0f} MB", flush=True)
        done += 1
        gc.collect()
    return manifest


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pdfs", nargs="*", type=Path)
    ap.add_argument("--list", type=Path, help="file with one PDF path per line")
    ap.add_argument("--pages", help="restrict to a page range, e.g. 40-60")
    ap.add_argument("--chunk", type=int, default=20, help="pages per conversion (memory bound)")
    ap.add_argument("--table-mode", choices=("accurate", "fast"), default="accurate")
    ap.add_argument("--timeout", type=float, default=None, help="per-chunk timeout, seconds")
    ap.add_argument("--ocr", action="store_true",
                    help="full-page OCR (RapidOCR) for PDFs without a usable text layer; "
                         "cached separately from text-layer runs")
    ap.add_argument("--reexport", action="store_true",
                    help="only rebuild page markdown from cached chunks (fast, no conversion)")
    args = ap.parse_args()

    pdfs = list(args.pdfs)
    if args.list:
        pdfs += [Path(l.strip()) for l in args.list.read_text(encoding="utf-8").splitlines()
                 if l.strip() and not l.startswith("#")]
    if not pdfs:
        ap.error("no PDFs given")

    reexport.ocr = args.ocr
    if args.reexport:
        for pdf in pdfs:
            print(f"{pdf.name}: {reexport(pdf)} chunk(s) re-exported")
        return

    converter = make_converter(args.table_mode, args.timeout, args.ocr)
    failed = 0
    for pdf in pdfs:
        if not pdf.is_file():
            print(f"!! missing: {pdf}", flush=True)
            failed += 1
            continue
        m = extract(pdf, converter, args)
        failed += any(c["status"] not in ("success", "partial_success")
                      for c in m["chunks"].values())
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
