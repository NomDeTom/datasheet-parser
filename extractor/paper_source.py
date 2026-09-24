"""
paper_source.py — read a research paper for papers.py and sidecars.py.

Two backends, same result shape:

  * Docling (preferred): its layout model reads two-column journal pages in the right order
    (checked on Croce 2018 p2 against pymupdf's column-geometry reading: same lines, same order),
    labels the title, and gives section headings with page numbers — the outline pymupdf had to
    guess from regexes ("noisy on IEEE layouts").
  * pypdfium2 (light tier, no Docling): each page read as its left half then its right half — the
    two-column order without a layout model. Full-width lines (title, some abstracts) come out
    split across the halves; good enough to search, not to quote.

Identifiers, year and abstract come from the same patterns papers.py always used, applied to
whichever text was read. PDF metadata (title, author, subject, creation date) from pypdf.
Nothing here interprets the paper — that is the reading pass (.claude/skills/paper-importer).
"""
import hashlib
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PAGE_MARKER = "=== PAGE %d ==="     # trm.py's convention, so `trm.py find` works on a paper cache

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>|]+?)(?=[\s\"<>|,;)]|$)")
ARXIV_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5}(?:v\d+)?)")
# numbered headings as IEEE/Springer set them: "I. INTRODUCTION", "2 Related work", "A. Capture"
HEADING_NUM = re.compile(r"^\s*(?:(?P<roman>[IVX]{1,5})\.|(?P<num>\d{1,2}(?:\.\d{1,2}){0,2})\.?|"
                         r"(?P<alpha>[A-H])\.)\s+(?P<title>\S.*)$")
STOP_HEADINGS = {"references", "acknowledgment", "acknowledgments", "acknowledgement",
                 "acknowledgements"}


@dataclass
class Paper:
    pdf: Path
    pages: list                      # text per page, 0-based list
    backend: str                     # "docling" | "pypdfium2"
    title: str | None = None
    authors: str | None = None
    front: str = ""                  # all page-1 text incl. running header/footer (ids live there)
    headings: list = field(default_factory=list)   # [(page, text)] in reading order
    meta: dict = field(default_factory=dict)       # pypdf document info

    @property
    def text(self):
        return "".join(f"\n{PAGE_MARKER % (i + 1)}\n{t}" for i, t in enumerate(self.pages))


def _docling_cache(pdf: Path):
    c = HERE / "output" / "docling" / hashlib.sha256(pdf.read_bytes()).hexdigest()[:16]
    return c if c.is_dir() else None


def _meta(pdf: Path) -> dict:
    from pypdf import PdfReader
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        info = PdfReader(str(pdf)).metadata or {}
        return {k.lstrip("/").lower(): str(v) for k, v in info.items() if v}
    except Exception:                                           # noqa: BLE001
        return {}


_NOT_AUTHORS = re.compile(r"^(abstract|index terms|keywords|received|accepted|digital object|"
                          r"homepage|p-issn|e-issn|©|copyright|this article|emails?:)", re.I)
_AFFILIATION = re.compile(r"\s*(?:\(\d\)\s*)?\b(?:Institute|Universit|Faculty|Department|School|"
                          r"Laborator|College|Centre|Center|Dept\.)", re.I)
_NAME = re.compile(r"\b[A-Z][a-z]+(?:[- ][A-Z][a-z]+)*\b")


def _author_line(text: str) -> str | None:
    """A page-1 text item that reads as a list of names -> the names, affiliations cut off."""
    if _NOT_AUTHORS.match(text) or len(text) > 600:
        return None
    head = _AFFILIATION.split(text, maxsplit=1)[0]
    head = re.sub(r"[∗*†‡§¶]|\(\d\)|(?<=[a-z])\s?\d\b|\s\d(?=\s*[,·&]|\s+and\b|$)", "", head)
    head = re.sub(r"\s+([,·&])", r"\1", " ".join(head.split())).strip(" ,&·")
    if len(_NAME.findall(head)) >= 2 and re.search(r",|\band\b|·|&", head):
        return head
    return None


def _from_docling(pdf: Path, cache: Path, n_pages: int):
    pages, title, headings = {}, None, []
    for f in sorted(cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].pages.json")):
        pages.update({int(k): v for k, v in json.loads(f.read_text(encoding="utf-8")).items()})
    if len(pages) < n_pages:
        return None                                  # conversion unfinished: use the fallback
    for js in sorted(cache.glob("p[0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9].json")):
        doc = json.loads(js.read_text(encoding="utf-8"))
        for t in doc.get("texts", []):
            page = (t.get("prov") or [{}])[0].get("page_no")
            txt = " ".join((t.get("text") or "").split())
            if t.get("label") == "title" and title is None and txt:
                title = txt
            elif t.get("label") == "section_header" and txt:
                headings.append((page, txt))
    body = [re.sub(r"<!-- image -->\n*", "", pages[n]).strip() for n in sorted(pages)]
    # Title and authors from page 1: the author line is the first text item that reads as a list
    # of names; the title is the heading just before it. (The first heading is not always the
    # title: "REV I EW", a journal name.) Falls back to the longest page-1 heading.
    authors, page1 = None, []
    first = sorted(cache.glob("p0001-*[0-9].json"))
    if first:
        doc = json.loads(first[0].read_text(encoding="utf-8"))
        page1 = [(t.get("label"), " ".join((t.get("text") or "").split())) for t in doc.get("texts", [])
                 if (t.get("prov") or [{}])[0].get("page_no") == 1]
    last_header = None
    for label, txt in page1:
        if label in ("section_header", "title"):
            last_header = txt
        elif label == "text" and last_header and (names := _author_line(txt)):
            title, authors = title or last_header, names
            break
    if title is None:
        heads = [txt for label, txt in page1 if label == "section_header" and len(txt.split()) >= 4]
        title = max(heads, key=len) if heads else (headings[0][1] if headings else None)
    headings = [h for h in headings if not (h[0] == 1 and h[1] == title)]
    front = "\n".join(txt for _, txt in page1)
    return Paper(pdf, body, "docling", title=title, authors=authors, headings=headings, front=front)


def _from_pdfium(pdf: Path):
    import pypdfium2 as pdfium
    doc = pdfium.PdfDocument(str(pdf))
    pages = []
    for page in doc:
        w, h = page.get_size()
        tp = page.get_textpage()
        left = tp.get_text_bounded(left=0, bottom=0, right=w / 2, top=h)
        right = tp.get_text_bounded(left=w / 2, bottom=0, right=w, top=h)
        t = (left + "\n" + right).replace("­", "")
        pages.append(re.sub(r"(\w)-\r?\n(\w)", r"\1\2", t))     # "inter-\nference" -> "interference"
    doc.close()
    return Paper(pdf, pages, "pypdfium2")


def read(pdf: Path) -> Paper:
    pdf = Path(pdf)
    import pypdfium2 as pdfium
    n = len(pdfium.PdfDocument(str(pdf)))
    cache = _docling_cache(pdf)
    paper = (_from_docling(pdf, cache, n) if cache else None) or _from_pdfium(pdf)
    paper.meta = _meta(pdf)
    return paper


# ── what the paper says about itself ─────────────────────────────────────────
def ids(paper: Paper):
    """The paper's own DOI / arXiv id. Searched on page 1 only (title block, running header,
    footer): searching the whole text returns the first *cited* DOI from the references
    (Hariyawan 2023 came out as 10.1155/2017/9324035). None is better than someone else's id."""
    first = paper.front or (paper.pages[0] if paper.pages else "")
    doi, arx = DOI_RE.search(first), ARXIV_RE.search(first)
    return (doi.group(1).rstrip(".") if doi else None, arx.group(1) if arx else None)


def title(paper: Paper):
    meta = (paper.meta.get("title") or "").strip()
    if meta and not meta.lower().endswith(".pdf") and len(meta) > 12 and " " in meta:
        return meta
    return paper.title


def year(paper: Paper):
    m = re.search(r"D:(\d{4})", paper.meta.get("creationdate") or "")
    if m:
        return m.group(1)
    m = re.search(r"\b(19|20)\d{2}\b", paper.text[:3000])
    return m.group(0) if m else None


def abstract(paper: Paper):
    m = re.search(r"Abstract\s*[—\-–:.]?\s*(.+?)(?=\n\s*(?:#+\s*)?(?:Index Terms|Keywords|"
                  r"I\.\s+INTRODUCTION|1\s+Introduction|1\.\s+Introduction))",
                  paper.text, re.S | re.I)
    return re.sub(r"\s*\n\s*", " ", m.group(1)).strip() if m else None


def outline(paper: Paper):
    """[(page, label, title)] — Docling's section headings, else numbered lines in the text."""
    out, seen = [], set()
    source = paper.headings or [(i + 1, ln) for i, t in enumerate(paper.pages)
                                for ln in t.splitlines()]
    for page, line in source:
        m = HEADING_NUM.match(line.lstrip("# ").strip())
        if not m and not paper.headings:
            continue
        label = (m.group("roman") or m.group("num") or m.group("alpha")) if m else ""
        head = (m.group("title") if m else line).strip()
        if head.lower() in seen or len(head.split()) > 12:
            continue
        seen.add(head.lower())
        out.append((page, label, head))
        if head.lower().rstrip(".") in STOP_HEADINGS:
            break
    return out


def metadata(paper: Paper) -> dict:
    """The block that goes into a paper's .data.json (document.paper)."""
    doi, arx = ids(paper)
    # the page-1 author line first: PDF metadata authors are often one name of several, or the
    # publisher ("shm publisher")
    return {"title": title(paper), "authors": paper.authors or paper.meta.get("author") or None,
            "year": year(paper), "venue": paper.meta.get("subject") or None,
            "doi": doi, "arxiv": arx, "abstract": abstract(paper),
            "outline": [{"page": p, "label": lab, "title": t} for p, lab, t in outline(paper)],
            "read_by": paper.backend}
