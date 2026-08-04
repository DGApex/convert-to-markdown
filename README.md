# convert-to-markdown

**Anything → Markdown, routed to whichever engine actually wins on that format.**

*[🇪🇸 Leer en español](README.es.md)*

A [Claude Code](https://claude.com/claude-code) skill (usable as a plain CLI too). There is no
single best converter, so this one does not pretend otherwise: it inspects each input and dispatches
it to the tool that measurably handles that format best.

| Input | Engine | Why |
|---|---|---|
| `.pdf` | **[pdf-inspector](https://github.com/firecrawl/pdf-inspector)** | Layout-aware: headings, multi-column, tables. ~20× faster |
| `http(s)://` | **[Firecrawl CLI](https://github.com/firecrawl/cli)** | Renders SPAs and strips nav/footer boilerplate |
| docx, pptx, xlsx, epub, msg, csv, json, zip, audio, images | **[MarkItDown](https://github.com/microsoft/markitdown)** | The only one covering this long tail |
| Scanned PDF | → **[super-ocr](../super-ocr)** | pdf-inspector detects it but does no OCR |

## Credits — this is a router, not an engine

| Tool | Licence |
|---|---|
| **[firecrawl/pdf-inspector](https://github.com/firecrawl/pdf-inspector)** | MIT |
| **[microsoft/markitdown](https://github.com/microsoft/markitdown)** | MIT |
| **[firecrawl/cli](https://github.com/firecrawl/cli)** | MIT |
| **[PyMuPDF](https://github.com/pymupdf/PyMuPDF)** | AGPL-3.0 |
| **[astral-sh/uv](https://github.com/astral-sh/uv)** | Apache-2.0/MIT |

## Why MarkItDown was taken off PDFs

On [opendataloader-bench](https://github.com/firecrawl/pdf-inspector) (200 PDFs, no OCR)
pdf-inspector scores **0.875** overall against markitdown's **0.583**, with **0.814 vs 0.000** on
tables (TEDS) and **0.788 vs 0.000** on headings. Firecrawl publishes that benchmark themselves, so
it was verified locally on arXiv:1706.03762 (15 pages, two columns):

| | pdf-inspector | markitdown |
|---|---|---|
| Headings detected | 38 | 0 |
| Paragraphs | reflowed correctly | broken at the PDF's physical lines |
| Table cells filled | 87% | 48% |
| Word spacing | correct | glued (`TheTransformerachieves`) |
| Time | 0.07 s | 1.7 s |

**But it is not a clean sweep, and this matters:** on Table 2 of that same paper pdf-inspector
produced a hollow grid (`|||20|`) while markitdown did recover the model names and BLEU scores. On
visually complex pages no coordinate-based extractor is reliable. Hence `--pdf-engine both`, and
hence the router computes a **table-health metric** and warns you when the extraction comes out
hollow instead of silently handing back an empty grid.

## Requirements

- **[uv](https://github.com/astral-sh/uv)** — the only hard requirement. Dependencies live in the
  script's [PEP 723](https://peps.python.org/pep-0723/) header and resolve into uv's global cache.
- **Optional, for URLs:** the [Firecrawl CLI](https://github.com/firecrawl/cli) (`npm install -g
  firecrawl-cli`) plus a Firecrawl account. Scraping consumes credits.

## Install

### As a Claude Code skill

```bash
git clone https://github.com/<you>/convert-to-markdown .claude/skills/convert-to-markdown
```

### As a plain CLI

```bash
git clone https://github.com/<you>/convert-to-markdown
uv run convert-to-markdown/scripts/convert.py report.pdf
```

No `pip install`, no virtualenv, nothing in your system Python.

## Usage

```bash
uv run scripts/convert.py <file|folder|url ...> [flags]
```

| Flag | What it does |
|---|---|
| `--out-dir DIR` | destination (default `converted`) |
| `--recursive` | descend into subfolders |
| `--pdf-engine pdf-inspector\|markitdown\|both` | `both` writes both results for comparison |
| `--url-engine auto\|firecrawl\|markitdown` | `auto` = Firecrawl when installed **and** authenticated |
| `--check-tools` | report engine availability and exit. **Run this before a batch of URLs** |
| `--overwrite`, `--no-front-matter`, `--enable-plugins` | as expected |

Last line of stdout is machine-readable:

```
CONVERT_JSON {"converted": 3, "engines": {...}, "encoding_repairs": 265, "firecrawl": {...}, ...}
```

### Examples

```bash
uv run scripts/convert.py report.pdf
uv run scripts/convert.py docs/ --recursive --out-dir converted
uv run scripts/convert.py https://example.com/post
uv run scripts/convert.py balance.pdf --pdf-engine both     # compare engines on a hard table
uv run scripts/convert.py --check-tools                     # preflight
```

## Before converting URLs

```bash
uv run scripts/convert.py --check-tools
```

```
firecrawl CLI : ready  (v1.19.6)  credits 1,000 / 1,000
```

| Result | What to do |
|---|---|
| `installed: true, authenticated: true` | go ahead |
| `installed: true, authenticated: false` | run `firecrawl auth --api-key fc-…` yourself |
| `installed: false` | `npm install -g firecrawl-cli`, then authenticate |

**Why the check tests authentication and not just PATH:** an unauthenticated CLI passes a
`which` test and then fails at scrape time, mid-batch. And **do not test `FIRECRAWL_API_KEY`** — the
CLI also authenticates from credentials stored by `firecrawl auth`, so that variable is empty on
perfectly working machines. We verified exactly that: env var unset, CLI authenticated, 1,000
credits. Checking the variable would have produced a false negative and sent the user to reinstall
something that worked.

**What is at stake if you skip this:** the router falls back to MarkItDown for URLs, which does a
plain fetch — **no JavaScript rendering, and nav/footer boilerplate kept**. On an SPA that is a
near-empty page. The fallback is no longer silent (it warns per file, and the reason lands in each
record's `warnings`), but asking first beats explaining afterwards.

> Never paste an API key into a chat, and don't let an agent run `firecrawl auth` with your key on
> your behalf. It is a credential — you type it into your own terminal.

## Spanish (and any accented) PDFs: the repair you cannot see working

pdf-inspector **mis-resolves subset Type1 fonts** (`enc=T1_x`), which are extremely common in
designed documents. It either deletes the accented glyph — `Producción` → `Produccin` — or emits the
bare accent — `áreas` → `Æreas`, `técnico` → `tØcnico`. Its own `has_encoding_issues` flag stays
**`false`**, so nothing warns you, and the damage reads as a typo rather than a bug.

Measured on a 41-page Spanish document: **156 defects, silently**. PyMuPDF reads the very same fonts
correctly, so it is used as the reference, and the count is reported as `encoding_repairs`. On that
document: **156 → 7**.

Substitutions happen only when the reference offers a single unambiguous candidate. The leftovers
are words whose accent falls on the first letter (`Áreas` vs `áreas`), where the accent takes the
capitalisation clue with it. The reference here is the **whole document**, which is more ambiguous
than the per-page reference [super-ocr](../super-ocr) uses — that is why the same file leaves 7
residuals here and 1 there.

**If you convert non-English PDFs, this is the single most valuable thing in this repo.**

## Other things worth knowing

- **Force UTF-8 on stdout.** The script prints `→` and `·`. Under a cp1252 console it converts
  everything correctly and then dies on the summary line, taking the `CONVERT_JSON` contract down
  with it — work done, result lost. Fixed here; worth copying into your own tools.
- **Subprocess output needs an explicit encoding too.** The first version of the Firecrawl check
  reported `NOT authenticated` for a perfectly authenticated CLI, because `subprocess` decoded the
  CLI's emoji output as cp1252 and threw. A check that lies is worse than no check.
- **Do not use `markitdown[all]`.** It pulls the Azure extras, and `azure-ai-contentunderstanding`
  only exists as a pre-release, which `uv` rejects by default (pip does not) — the whole resolution
  fails. The PEP 723 header pins exactly the extras this router delegates.
- **Scanned PDFs are detected, not converted.** The router reports `needs_ocr` and points you at
  [super-ocr](../super-ocr).

## Licence

MIT — see [LICENSE](LICENSE), including notes on PyMuPDF's AGPL-3.0 status and the fact that
Firecrawl is a paid third-party service.
