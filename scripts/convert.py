# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pdf-inspector>=0.2.6",
#   "firecrawl-anydoc",
#   "markitdown[docx,pptx,xlsx,xls,outlook,pdf,audio-transcription,youtube-transcription]>=0.1.6",
#   "pymupdf>=1.27",
# ]
# ///
# ^ PEP 723: this block IS the skill's dependency manifest.
#   `uv run convert.py ...` resolves it into a global cache and executes. No venv, no
#   requirements.txt, no install step — which is why this folder is copy-paste portable.
#
#   Do NOT use markitdown[all]: it pulls in the Azure extras (az-doc-intel and
#   az-content-understanding), and azure-ai-contentunderstanding only exists as a
#   PRE-RELEASE (>=1.2.0b1). uv rejects pre-releases by default (pip doesn't) → the whole
#   resolution fails. They are also cloud services requiring an API key, useless in a
#   local-first skill. The extras above are exactly the formats the router delegates to
#   MarkItDown.
"""
convert.py — deterministic router from "anything" to Markdown.

Replaces the old markitdown_convert.py. The difference: there is no longer ONE engine but
three, and each input goes to whichever wins on that format:

    .pdf            → pdf-inspector  (firecrawl/pdf-inspector, Rust, layout-aware)
    http(s)://      → firecrawl CLI  (scrape --only-main-content)
    everything else → MarkItDown     (docx, pptx, xlsx, epub, zip, msg, audio, csv…)

Why not MarkItDown for PDFs: on opendataloader-bench (200 PDFs) pdf-inspector scores 0.875
overall vs markitdown's 0.583, and is ~20× faster. Verified locally on arXiv:1706.03762
(15 pages, two columns): 38 headings vs 0, reflowed paragraphs vs broken lines, 0.07 s vs 1.7 s.

Honest caveats, measured rather than marketed:
  · pdf-inspector does NOT do OCR. If the PDF is scanned it says so (pdf_type) and this
    script reports it as 'needs OCR' → use the `super-ocr` skill.
  · On dense scientific tables MarkItDown sometimes rebuilds the grid better. Hence
    --pdf-engine both, and hence this script computes a table-health metric and warns when
    the extraction comes out hollow.

Last line of stdout: CONVERT_JSON {...}

Examples:
    uv run convert.py report.pdf
    uv run convert.py docs\\ --recursive --out-dir converted
    uv run convert.py https://example.com/post
    uv run convert.py balance.pdf --pdf-engine both
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

# Office formats go to anydoc (firecrawl/anydoc): pure Rust, and on Firecrawl's own blind
# benchmark it beats MarkItDown on every one of these — docx 86 vs 72, pptx 76 vs 59,
# xlsx 70 vs 55, xls 77 vs 64 — at 4.7 ms median against 134.8 ms.
ANYDOC_EXT = {
    ".doc", ".docx", ".docm",
    ".ppt", ".pptx", ".pptm", ".ppsx", ".ppsm", ".pps", ".pot",
    ".xls", ".xlsx", ".xlsm", ".xlsb",
    ".odt", ".ods", ".odp",
    ".rtf", ".csv",
}

# Formats MarkItDown covers and anydoc does not: audio transcription, images, archives,
# Outlook mail, notebooks and web/data text. Plus .epub, which is the one format where
# MarkItDown measurably wins (77 vs 74 on the same benchmark), so it keeps it.
MARKITDOWN_EXT = {
    ".epub", ".msg", ".ipynb",
    ".html", ".htm", ".json", ".xml", ".rss", ".atom", ".txt", ".md",
    ".zip",
    ".jpg", ".jpeg", ".png", ".gif", ".bmp", ".tiff", ".tif",
    ".mp3", ".wav", ".m4a", ".flac", ".ogg",
}

# PDFs stay on pdf-inspector rather than anydoc even though anydoc embeds it and returns
# byte-identical Markdown (verified on a 41-page PDF: same 38,723 chars). Calling the library
# directly is what exposes pdf_type, page_count and pages_needing_ocr, which this router needs
# to tell you a document is a scan and belongs in super-ocr.
PDF_EXT = {".pdf"}
SUPPORTED_EXT = ANYDOC_EXT | MARKITDOWN_EXT | PDF_EXT


def _is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _safe_slug(value: str) -> str:
    keep = "-_."
    cleaned = "".join(c if (c.isalnum() or c in keep) else "-" for c in value).strip("-")
    return cleaned or "converted"


def _url_stem(url: str) -> str:
    parsed = urlparse(url)
    tail = Path(parsed.path).stem or parsed.netloc or "url"
    return _safe_slug(tail)


def _unique_path(out_dir: Path, stem: str, overwrite: bool) -> Path:
    target = out_dir / f"{stem}.md"
    if overwrite or not target.exists():
        return target
    i = 2
    while True:
        candidate = out_dir / f"{stem}-{i}.md"
        if not candidate.exists():
            return candidate
        i += 1


def _kind_for(suffix: str) -> str:
    suffix = suffix.lower()
    if suffix in PDF_EXT:
        return "pdf"
    if suffix in ANYDOC_EXT:
        return "office"
    return "file"


def _collect_inputs(raw_inputs: list[str], recursive: bool) -> list[tuple[str, str]]:
    """Return [(kind, value)] where kind is 'url', 'pdf', 'office', 'file' or 'missing'."""
    jobs: list[tuple[str, str]] = []
    for item in raw_inputs:
        if _is_url(item):
            jobs.append(("url", item))
            continue
        path = Path(item)
        if path.is_dir():
            globber = path.rglob("*") if recursive else path.glob("*")
            for child in sorted(globber):
                if child.is_file() and child.suffix.lower() in SUPPORTED_EXT:
                    jobs.append((_kind_for(child.suffix), str(child)))
        elif path.is_file():
            jobs.append((_kind_for(path.suffix), str(path)))
        else:
            jobs.append(("missing", item))
    return jobs


def _front_matter(meta: dict) -> str:
    lines = ["---"]
    for key, value in meta.items():
        if value is None or value == []:
            continue
        if isinstance(value, (list, tuple)):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        elif isinstance(value, str):
            lines.append(f'{key}: "{value.replace(chr(34), chr(39))}"')
        else:
            lines.append(f"{key}: {value}")
    lines.append("---\n\n")
    return "\n".join(lines)


# --------------------------------------------------------- accent repair (subset Type1)
#
# pdf-inspector mis-resolves subset Type1 fonts (enc=T1_x): it either deletes the accented
# glyph ('Producción' → 'Produccin') or emits the bare accent ('áreas' → 'Æreas'). Its own
# has_encoding_issues flag stays FALSE, so nothing warns you. Measured on a 41-page Spanish
# rider: 156 defects, silently. PyMuPDF reads the very same fonts correctly, so it is used as
# the reference. Ported from the super-ocr skill, where this was diagnosed 2026-08-04.

_LIGATURES = {"ﬀ": "ff", "ﬁ": "fi", "ﬂ": "fl", "ﬃ": "ffi", "ﬄ": "ffl"}
_MOJIBAKE = "˝ˇˆ˜´`"           # loose Type1 accent glyphs
_STRAY = _MOJIBAKE + "ÆæŒœØø"  # …and the letters they get mistaken for (á→Æ, ú→œ, é→Ø)
_WORD_RE = re.compile(r"[A-Za-zÀ-ɏ" + re.escape(_MOJIBAKE) + r"]+")


def _ascii_key(word: str) -> str:
    """'PRODUCCIÓN', 'Produccin' and 'PRODUCCIN' all collapse to 'produccin'."""
    return "".join(c for c in word if c.isascii() and c.isalpha()).lower()


def _case_shape(word: str) -> tuple[bool, bool, bool]:
    """Measured on the ASCII skeleton — the stray glyph carries its own case and would
    otherwise poison the predicates: 'tØcnico'.islower() is False thanks to the 'Ø'."""
    skeleton = "".join(c for c in word if c.isascii() and c.isalpha())
    return (skeleton.isupper(), skeleton.islower(), skeleton[:1].isupper())


def _is_damaged(word: str) -> bool:
    """A stray accent glyph proves the true word is accented — 'serÆ' is 'será', never 'ser'."""
    return any(c in _STRAY for c in word)


def _pdf_page_texts(pdf_path: str) -> list[str]:
    import fitz  # PyMuPDF

    doc = fitz.open(pdf_path)
    texts = [page.get_text() for page in doc]
    doc.close()
    return texts


def _repair_encoding(markdown: str, reference: str) -> tuple[str, int]:
    """Restore the accents pdf-inspector drops, using PyMuPDF's reading as ground truth.

    _ascii_key() strips non-ASCII rather than transliterating it, so a healthy ASCII word can
    only collide with an accented one when it *is* the accent-deleted form — exactly the damage
    signature. Substitutions happen only when the reference offers one unambiguous candidate.
    """
    variants: dict[str, set[str]] = {}
    for word in _WORD_RE.findall(reference):
        variants.setdefault(_ascii_key(word), set()).add(word)

    repairs = 0

    def fix(match: re.Match) -> str:
        nonlocal repairs
        token = match.group(0)
        candidates = variants.get(_ascii_key(token))
        if not candidates or token in candidates:
            return token
        if len(candidates) > 1 and _is_damaged(token):
            accented = {c for c in candidates if any(not ch.isascii() for ch in c)}
            if accented:
                candidates = accented
        if len(candidates) > 1:
            shape = _case_shape(token)
            same_case = [c for c in candidates if _case_shape(c) == shape]
            if len(same_case) != 1:
                return token
            candidates = set(same_case)
        repairs += 1
        return next(iter(candidates))

    out = _WORD_RE.sub(fix, markdown)
    for ligature, plain in _LIGATURES.items():
        out = out.replace(ligature, plain)
    return out, repairs


_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_SEPARATOR_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")


def _table_health(markdown: str) -> float | None:
    """Fraction of NON-empty cells across the Markdown's tables. None when there are none.

    A well-extracted table fills its cells; a badly extracted one looks like '|||20|'.
    Used to warn the user instead of silently handing back a hollow grid.
    """
    cells_total = 0
    cells_filled = 0
    for line in markdown.splitlines():
        if not _TABLE_ROW_RE.match(line) or _SEPARATOR_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        cells_total += len(cells)
        cells_filled += sum(1 for c in cells if c)
    if cells_total == 0:
        return None
    return round(cells_filled / cells_total, 3)


# --------------------------------------------------------------------------- engines


def _convert_pdf_inspector(path: str) -> dict:
    import pdf_inspector

    result = pdf_inspector.process_pdf(path)
    markdown = result.markdown or ""
    try:
        markdown, repairs = _repair_encoding(markdown, "\n".join(_pdf_page_texts(path)))
    except Exception:  # a PyMuPDF failure must never cost the whole conversion
        repairs = 0
    return {
        "engine": "pdf-inspector",
        "markdown": markdown,
        "encoding_repairs": repairs,
        "title": result.title or None,
        "pdf_type": result.pdf_type,
        "confidence": round(float(result.confidence), 3),
        "pages": result.page_count,
        "pages_needing_ocr": sorted(set(result.pages_needing_ocr or [])),
        "has_encoding_issues": bool(result.has_encoding_issues),
        "pages_with_tables": list(result.pages_with_tables or []),
        "table_health": _table_health(markdown),
    }


def _convert_anydoc(path: str) -> dict:
    """Office formats through firecrawl/anydoc (Rust, MIT, no external calls)."""
    import anydoc

    markdown = anydoc.to_markdown(path)
    if isinstance(markdown, (bytes, bytearray)):
        markdown = markdown.decode("utf-8", "replace")
    return {
        "engine": "anydoc",
        "markdown": markdown,
        "title": None,
        "table_health": _table_health(markdown),
    }


def _convert_markitdown(value: str, enable_plugins: bool = False) -> dict:
    from markitdown import MarkItDown

    result = MarkItDown(enable_plugins=enable_plugins).convert(value)
    markdown = getattr(result, "markdown", None) or getattr(result, "text_content", "") or ""
    return {
        "engine": "markitdown",
        "markdown": markdown,
        "title": getattr(result, "title", None),
        "table_health": _table_health(markdown),
    }


_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _firecrawl_status(timeout: int = 90) -> dict:
    """Is the Firecrawl CLI installed **and usable**?

    Checks `firecrawl --status`, the CLI's own read-only report: it consumes no credits and
    covers auth and the remaining balance. Testing FIRECRAWL_API_KEY instead would be wrong —
    the CLI also authenticates from credentials stored by `firecrawl auth`, and that is the
    common setup, so the env var is empty on perfectly working machines (verified 2026-08-04).
    Being on PATH is not enough either: an unauthenticated CLI fails at scrape time, mid-batch.
    """
    exe = shutil.which("firecrawl")
    info: dict = {"installed": bool(exe), "path": exe, "authenticated": False, "credits": None}
    if not exe:
        return info
    try:
        # encoding/errors are NOT optional here: the CLI prints emoji and ANSI colour, and on a
        # Windows console `text=True` decodes as cp1252 → UnicodeDecodeError → a FALSE
        # "not authenticated", which is worse than no check at all (verified 2026-08-04).
        proc = subprocess.run(
            [exe, "--status"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
        return info
    out = _ANSI_RE.sub("", (proc.stdout or "") + (proc.stderr or ""))
    info["authenticated"] = ("Authenticated" in out) and ("Not authenticated" not in out)
    match = re.search(r"Credits:\s*([\d,]+\s*/\s*[\d,]+)", out)
    if match:
        info["credits"] = " ".join(match.group(1).split())
    version = re.search(r"cli\s+v([\d.]+)", out)
    if version:
        info["version"] = version.group(1)
    return info


def _convert_firecrawl(url: str, timeout: int = 180) -> dict:
    """Scrape through the Firecrawl CLI. Consumes credits from the configured account."""
    exe = shutil.which("firecrawl")
    if not exe:
        raise FileNotFoundError("the firecrawl CLI is not on PATH")
    tmp = Path(tempfile.mkdtemp(prefix="fc_scrape_")) / "page.md"
    proc = subprocess.run(
        [exe, "scrape", url, "--only-main-content", "-o", str(tmp)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )
    if proc.returncode != 0 or not tmp.exists():
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise RuntimeError(f"firecrawl scrape failed: {detail[-1] if detail else 'no output'}")
    return {
        "engine": "firecrawl",
        "markdown": tmp.read_text(encoding="utf-8", errors="replace"),
        "title": None,
    }


# ---------------------------------------------------------------------------- main


def _force_utf8_stdio() -> None:
    """Windows consoles default to cp1252, and this script prints '→', '·' and accented paths.

    Without this the run converts everything correctly and then dies on the summary line, taking
    the CONVERT_JSON contract down with it — the worst possible failure, since callers parse that
    line. Verified 2026-08-04: fine under PowerShell, fatal under Git Bash.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001 — a stream that cannot be reconfigured is not fatal
            pass


def main() -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="Convert files, folders and URLs to Markdown, routing to the best engine.",
    )
    parser.add_argument(
        "inputs", nargs="*",
        help="Files, folders and/or URLs. Optional only with --check-tools.",
    )
    parser.add_argument("--out-dir", default="converted", help="Destination folder (default: converted/).")
    parser.add_argument("--recursive", action="store_true", help="Descend into subfolders.")
    parser.add_argument(
        "--no-front-matter", dest="front_matter", action="store_false",
        help="Do not prepend the YAML metadata block.",
    )
    parser.add_argument("--overwrite", action="store_true", help="Overwrite existing .md files.")
    parser.add_argument(
        "--pdf-engine", choices=["pdf-inspector", "markitdown", "both"], default="pdf-inspector",
        help="Engine for PDFs (default: pdf-inspector). 'both' writes both for comparison.",
    )
    parser.add_argument(
        "--office-engine", choices=["anydoc", "markitdown", "both"], default="anydoc",
        help="Engine for Word/PowerPoint/Excel/OpenDocument/RTF/CSV (default: anydoc). "
             "'both' writes both results for comparison.",
    )
    parser.add_argument(
        "--url-engine", choices=["auto", "firecrawl", "markitdown"], default="auto",
        help="Engine for URLs (default: auto → firecrawl if installed, else markitdown).",
    )
    parser.add_argument("--enable-plugins", action="store_true", help="Enable MarkItDown plugins.")
    parser.add_argument(
        "--check-tools", action="store_true",
        help="Report engine availability (Firecrawl CLI installed / authenticated / credits) "
             "and exit without converting anything. Run this before a batch of URLs.",
    )
    args = parser.parse_args()

    if args.check_tools:
        fc = _firecrawl_status()
        state = ("missing" if not fc["installed"]
                 else "installed but NOT authenticated" if not fc["authenticated"]
                 else "ready")
        print(f"  firecrawl CLI : {state}"
              + (f"  (v{fc['version']})" if fc.get("version") else "")
              + (f"  credits {fc['credits']}" if fc.get("credits") else ""))
        if fc["installed"]:
            print(f"  path          : {fc['path']}")
        print("TOOLS_JSON " + json.dumps({"firecrawl": fc}, ensure_ascii=False))
        return 0

    if not args.inputs:
        parser.error("no inputs given (inputs are optional only with --check-tools)")

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs = _collect_inputs(args.inputs, args.recursive)

    results: list[dict] = []

    # Probe the Firecrawl CLI once, and only when a URL is actually queued.
    fc_status: dict | None = None
    if any(kind == "url" for kind, _ in jobs) and args.url_engine in ("auto", "firecrawl"):
        fc_status = _firecrawl_status()
        if not fc_status["installed"]:
            print("  ~ Firecrawl CLI not installed — URLs fall back to MarkItDown, which does "
                  "NOT render JavaScript and keeps nav/footer boilerplate.", file=sys.stderr)
            print("    install: npm install -g firecrawl-cli   then: firecrawl auth --api-key fc-…",
                  file=sys.stderr)
        elif not fc_status["authenticated"]:
            print("  ~ Firecrawl CLI is installed but NOT authenticated — URLs fall back to "
                  "MarkItDown.", file=sys.stderr)
            print("    authenticate with: firecrawl auth --api-key fc-…", file=sys.stderr)

    def write_comparison(source: str, stem: str, payload_fn, suffix: str) -> None:
        """Second-opinion conversion for the 'both' modes.

        Deliberately swallows its own failure: the comparison is a convenience, and letting it
        raise would discard the primary result that was already written to disk. Observed with
        an .xlsx whose MarkItDown path was blocked by a machine policy — the anydoc output
        existed but never reached the JSON report.
        """
        try:
            alt = write_out(source, stem, payload_fn(), suffix)
        except Exception as exc:  # noqa: BLE001
            print(f"    ~ comparison run failed ({type(exc).__name__}); primary result kept",
                  file=sys.stderr)
            return
        results.append(alt)
        print(f"    + {alt['engine']} → {alt['output']} ({alt['chars']} chars)")

    def write_out(source: str, stem: str, payload: dict, suffix: str = "") -> dict:
        markdown = payload.pop("markdown", "")
        target = _unique_path(out_dir, stem + suffix, args.overwrite)
        meta = {
            "source": source,
            "title": payload.get("title"),
            "converted_at": _dt.datetime.now().isoformat(timespec="seconds"),
            "converter": payload["engine"],
            "pdf_type": payload.get("pdf_type"),
            "pages": payload.get("pages"),
            "table_health": payload.get("table_health"),
        }
        body = (_front_matter(meta) if args.front_matter else "") + markdown
        target.write_text(body, encoding="utf-8")
        record = {"source": source, "status": "ok", "output": str(target), "chars": len(markdown)}
        record.update({k: v for k, v in payload.items() if k != "title"})
        return record

    for kind, value in jobs:
        if kind == "missing":
            results.append({"source": value, "status": "error", "error": "not found"})
            print(f"  ! not found: {value}", file=sys.stderr)
            continue
        try:
            if kind == "url":
                stem = _url_stem(value)
                engine = args.url_engine
                usable = bool(fc_status and fc_status["installed"] and fc_status["authenticated"])
                if engine == "auto":
                    engine = "firecrawl" if usable else "markitdown"
                payload = _convert_firecrawl(value) if engine == "firecrawl" else _convert_markitdown(value, args.enable_plugins)
                record = write_out(value, stem, payload)
                if args.url_engine == "auto" and engine == "markitdown":
                    # Say it per-file too: a warning printed once at the top of a long batch
                    # is a warning nobody reads.
                    reason = ("the Firecrawl CLI is not installed" if not (fc_status or {}).get("installed")
                              else "the Firecrawl CLI is not authenticated")
                    record["warnings"] = [
                        f"scraped with MarkItDown because {reason}: no JavaScript rendering and "
                        "nav/footer boilerplate is kept. Content may be partial"
                    ]

            elif kind == "pdf":
                stem = _safe_slug(Path(value).stem)
                if args.pdf_engine == "markitdown":
                    record = write_out(value, stem, _convert_markitdown(value, args.enable_plugins))
                else:
                    payload = _convert_pdf_inspector(value)
                    warnings = []
                    if payload["pdf_type"] != "text_based" or payload["pages_needing_ocr"]:
                        warnings.append(
                            f"'{payload['pdf_type']}' PDF with {len(payload['pages_needing_ocr'])} "
                            "page(s) needing OCR → use the super-ocr skill"
                        )
                    health = payload.get("table_health")
                    if health is not None and health < 0.6:
                        warnings.append(
                            f"tables have {int((1 - health) * 100)}% empty cells → "
                            "retry with --pdf-engine both and compare"
                        )
                    record = write_out(value, stem, payload)
                    if warnings:
                        record["warnings"] = warnings
                    if args.pdf_engine == "both":
                        write_comparison(
                            value, stem,
                            lambda: _convert_markitdown(value, args.enable_plugins),
                            "-markitdown",
                        )
            elif kind == "office":
                stem = _safe_slug(Path(value).stem)
                if args.office_engine == "markitdown":
                    record = write_out(value, stem, _convert_markitdown(value, args.enable_plugins))
                else:
                    record = write_out(value, stem, _convert_anydoc(value))
                    if args.office_engine == "both":
                        write_comparison(
                            value, stem,
                            lambda: _convert_markitdown(value, args.enable_plugins),
                            "-markitdown",
                        )

            else:
                stem = _safe_slug(Path(value).stem)
                record = write_out(value, stem, _convert_markitdown(value, args.enable_plugins))

            results.append(record)
            print(f"  + {value} → {record['output']} ({record['chars']} chars, {record['engine']})")
            for warning in record.get("warnings", []):
                print(f"    ~ {warning}")
        except Exception as exc:  # noqa: BLE001 — one failure must not abort the batch
            results.append({"source": value, "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            print(f"  ! failed: {value} :: {type(exc).__name__}: {exc}", file=sys.stderr)

    ok = sum(1 for r in results if r.get("status") == "ok")
    failed = len(results) - ok
    engines: dict[str, int] = {}
    needs_ocr = [r["source"] for r in results if r.get("pages_needing_ocr") or
                 (r.get("pdf_type") and r["pdf_type"] != "text_based")]
    for r in results:
        if r.get("status") == "ok":
            engines[r["engine"]] = engines.get(r["engine"], 0) + 1
    repaired = sum(r.get("encoding_repairs", 0) for r in results)
    summary = {
        "converted": ok,
        "failed": failed,
        "out_dir": str(out_dir),
        "engines": engines,
        "needs_ocr": needs_ocr,
        "encoding_repairs": repaired,
        "firecrawl": fc_status,
        "results": results,
    }
    print(f"\nDone: {ok} converted, {failed} failed → {out_dir}/  engines={engines}")
    if repaired:
        print(f"  ~ repaired {repaired} mis-encoded word(s) in PDFs (subset Type1 fonts)")
    if needs_ocr:
        print(f"  ~ {len(needs_ocr)} PDF(s) need OCR → super-ocr skill")
    print("CONVERT_JSON " + json.dumps(summary, ensure_ascii=False))
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
