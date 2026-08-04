---
name: convert-to-markdown
description: "Convert files, folders and URLs to Markdown, routing each input to the best engine for its format: PDF → pdf-inspector (Firecrawl, layout-aware), URLs → Firecrawl scrape, and Office/audio/EPUB/CSV/ZIP → MarkItDown. Self-contained skill — ships its own router."
argument-hint: "<file|folder|url ...> [--out-dir=converted] [--recursive] [--pdf-engine=both] [--no-front-matter] [--overwrite]"
user-invocable: true
allowed-tools: Bash, PowerShell, Read, Glob, AskUserQuestion
---

Converts heterogeneous inputs to Markdown. **There is no longer a single engine**: the router
`scripts/convert.py` sends each input to whichever engine wins on that format.

| Input | Engine | Why |
|---|---|---|
| `.pdf` | **[pdf-inspector](https://github.com/firecrawl/pdf-inspector)** (Rust, MIT) | Layout-aware: headings, multi-column, tables. ~20× faster |
| doc, docx, ppt, pptx, xls, xlsx, odt, ods, odp, rtf, csv | **[anydoc](https://github.com/firecrawl/anydoc)** (Rust, MIT) | Beats MarkItDown on every one of these, at 4.7 ms median vs 134.8 |
| `http(s)://` | **Firecrawl CLI** (`scrape --only-main-content`) | Renders SPAs and strips nav/footer. Consumes credits |
| epub, msg, zip, images, audio, html, json, xml, ipynb | **[MarkItDown](https://github.com/microsoft/markitdown)** | The long tail anydoc does not cover, plus epub, where MarkItDown wins |
| Scanned PDF | → **`super-ocr`** skill | pdf-inspector detects it but does no OCR |

**Why anydoc took the office formats (2026-08-04).** Firecrawl's own blind benchmark (Claude
Sonnet 5 as judge against LibreOffice-rendered ground truth, positions swapped to cancel bias,
479 verdicts) scores anydoc 80 overall on 14/14 formats against MarkItDown's 65 on 6/14, and per
format it wins everywhere except `.epub` (74 vs 77): docx 86 vs 72, pptx 76 vs 59, xlsx 70 vs 55,
xls 77 vs 64. So epub stays on MarkItDown and everything else moved.

**PDFs deliberately stay on pdf-inspector** even though anydoc embeds it. Verified on a 41-page
PDF: the two return **byte-identical Markdown**, same 38,723 characters. Calling the library
directly is what exposes `pdf_type`, `page_count` and `pages_needing_ocr`, which this router needs
in order to tell you a document is a scan and belongs in `super-ocr`.

> anydoc inherits pdf-inspector's accent bug exactly (measured: 121 mojibake characters plus 49
> accent-deleted words on the same document, `has_encoding_issues` reporting `false`). The repair
> described below therefore matters just as much to anydoc users.

**Self-contained**: the router lives at `scripts/convert.py`, next to this file, so the
`convert-to-markdown/` folder can be copied into another project and still work.

### Spanish (and any accented) PDFs: the repair you cannot see working

pdf-inspector **mis-resolves subset Type1 fonts** (`enc=T1_x`), which are extremely common in
designed documents. It either deletes the accented glyph — `Producción` → `Produccin` — or emits
the bare accent — `áreas` → `Æreas`, `técnico` → `tØcnico`. Its own `has_encoding_issues` flag
stays **`false`**, so nothing warns you, and the damage reads as a typo rather than a bug.

Measured on a 41-page Spanish rider: **156 defects, silently**. PyMuPDF reads the very same fonts
correctly, so `_repair_encoding()` now runs on every PDF using it as the reference, and reports the
count as `encoding_repairs` in `CONVERT_JSON`. On that rider: **156 → 7**.

Substitutions happen only when the reference offers a single unambiguous candidate — the leftovers
are words whose accent falls on the first letter (`Áreas` vs `áreas`), where the accent takes the
capitalisation clue with it. Note the reference here is the **whole document**, which is more
ambiguous than the per-page reference `super-ocr` uses; that is why the same file leaves 7 residuals
here and 1 there.

### Why MarkItDown was taken off PDFs

On opendataloader-bench (200 PDFs, no OCR) pdf-inspector scores **0.875** overall against
markitdown's **0.583**, with **0.814 vs 0.000** on tables (TEDS) and **0.788 vs 0.000** on headings.
Firecrawl publishes that benchmark itself, so it was verified locally on arXiv:1706.03762
(15 pages, two columns):

| | pdf-inspector | markitdown |
|---|---|---|
| Headings detected | 38 | 0 |
| Paragraphs | reflowed correctly | broken at the PDF's physical lines |
| Table cells filled | 87% | 48% |
| Word spacing | correct | glued (`TheTransformerachieves`) |
| Time | 0.07 s | 1.7 s |

**But it is not a clean sweep, and this matters:** on Table 2 of that same paper pdf-inspector
produced a hollow grid (`|||20|`) while markitdown did recover the model names and BLEU scores.
On visually complex pages no coordinate-based extractor is reliable. Hence `--pdf-engine both`
and the table-health metric in §5.

When this skill is invoked:

## 1. Parse arguments

- `<file|folder|url ...>` (required) — files, folders (add `--recursive` to descend) or URLs.
- `--out-dir=<dir>` (default `converted`) — destination for the `.md` files.
- `--pdf-engine=pdf-inspector|markitdown|both` (default `pdf-inspector`) — `both` writes the two
  results (`<name>.md` and `<name>-markitdown.md`) for eyeball comparison.
- `--office-engine=anydoc|markitdown|both` (default `anydoc`) — same idea for Office formats. A
  failure in the comparison run never discards the primary result; it prints a warning and keeps
  going. (That is not hypothetical: on the test machine MarkItDown's `.xlsx` path fails because
  Windows Application Control blocks a pandas DLL, while anydoc, being pure Rust, is unaffected.)
- `--url-engine=auto|firecrawl|markitdown` (default `auto` → firecrawl if it is on PATH).
- `--recursive`, `--overwrite`, `--no-front-matter`, `--enable-plugins`.

- `--check-tools` — report engine availability and exit without converting. See §1.1.

With no inputs, ask via `AskUserQuestion` what to convert.

### 1.1 Before converting URLs: check Firecrawl, and ask before installing

**Run this whenever the inputs include a URL:**

```powershell
uv run .claude\skills\convert-to-markdown\scripts\convert.py --check-tools
```

It prints a human line plus `TOOLS_JSON {"firecrawl": {...}}` — parse that. Three outcomes:

| `TOOLS_JSON` | What it means | What to do |
|---|---|---|
| `installed: true, authenticated: true` | ready | convert, nothing to ask |
| `installed: true, authenticated: false` | the CLI is on PATH but has no credentials | **ask** the user to run `firecrawl auth --api-key fc-…` themselves |
| `installed: false` | not installed | **ask** via `AskUserQuestion` whether to install it |

With approval, install with npm (Node is a prerequisite):

```powershell
npm install -g firecrawl-cli
```

Then have the **user** authenticate. **Never ask them to paste an API key into the conversation,
and never run `firecrawl auth` with a key on their behalf** — it is a credential; they type it into
their own terminal. `npx firecrawl` also works without installing globally.

**Why check auth and not just PATH:** an unauthenticated CLI passes `shutil.which()` and then fails
at scrape time, mid-batch. And do **not** test `FIRECRAWL_API_KEY`: the CLI also authenticates from
credentials stored by `firecrawl auth`, so the variable is empty on perfectly working machines
(verified 2026-08-04 on this one — env var unset, CLI authenticated, 1,000 credits).

**What is at stake if you skip this:** the router falls back to MarkItDown for URLs, which does a
plain fetch — **no JavaScript rendering, and nav/footer boilerplate kept**. On an SPA that is a
near-empty page. The fallback is no longer silent: it warns per file and the reason lands in each
record's `warnings`, but asking first is better than explaining afterwards.

## 2. Run with uv — no venv, no install step

This skill **owns no environment and installs nothing system-wide**. Its dependencies are declared
inside the script itself, in the **PEP 723** header of `scripts/convert.py`:

```python
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "pdf-inspector>=0.2.6",
#   "markitdown[docx,pptx,xlsx,xls,outlook,pdf,audio-transcription,youtube-transcription]>=0.1.6",
# ]
# ///
```

[`uv`](https://github.com/astral-sh/uv) reads that header, resolves everything into a shared global
cache and runs. **Always invoke it this way**, never with a bare `python`:

```powershell
uv run .claude\skills\convert-to-markdown\scripts\convert.py <inputs...> [flags]
```

The first run resolves ~50 packages; later runs start from the cache in under a second.

> **Why the extras are enumerated instead of `markitdown[all]`** (real bug, 2026-08-03):
> `[all]` pulls in the Azure extras, and `azure-ai-contentunderstanding` **only exists as a
> pre-release** (`>=1.2.0b1`). pip accepts those; **uv rejects them by default** → the whole
> resolution fails with *"No solution found"*. They are also cloud services requiring an API key,
> useless in a local-first skill. The enumerated extras are exactly the formats the router
> delegates to MarkItDown. If Azure were ever needed: `uv run --prerelease=allow`.

The Firecrawl CLI is **external**: it lives in npm global (`firecrawl --status` shows credits).
Without it the router simply falls back to MarkItDown for URLs and everything else is unchanged.

> **Never `python convert.py`.** Windows ships a Microsoft Store stub at
> `...\WindowsApps\python.exe` that is not Python, and the global Python does not have these
> dependencies (by design: the skill does not pollute the system).

### 2.1 If `uv` is not installed

```powershell
Get-Command uv -ErrorAction SilentlyContinue
```

If that returns nothing, **ask the user with `AskUserQuestion` before installing** — `uv` is a global
binary and changes their machine, not just this project. Never install it unprompted.

With their approval:

```powershell
winget install --exact --id astral-sh.uv --source winget --accept-package-agreements --accept-source-agreements
```

**Verified gotcha:** winget prints *"Path environment variable modified; restart your shell"* — the
PATH **does not refresh** in the current session. For the rest of the run, resolve the binary by
full path:

```powershell
$uv = "$env:LOCALAPPDATA\Microsoft\WinGet\Links\uv.exe"
if (-not (Test-Path $uv)) {
    $uv = (Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter uv.exe |
           Select-Object -First 1).FullName
}
& $uv run .claude\skills\convert-to-markdown\scripts\convert.py <inputs...>
```

Without winget, the official installer: `irm https://astral.sh/uv/install.ps1 | iex` (drops the
binary in `$env:USERPROFILE\.local\bin\uv.exe`).

## 3. Run the router

```powershell
uv run .claude\skills\convert-to-markdown\scripts\convert.py <inputs...> --out-dir <dir> [flags]
```

## 4. Read the result

Last line of stdout:
`CONVERT_JSON {"converted": N, "failed": M, "engines": {...}, "needs_ocr": [...], "results": [...]}`.
Parse that line — do not infer from the human log.

## 5. React to the router's signals

The router does not hand back garbage silently; it emits two actionable warnings:

- **`needs_ocr` non-empty** — that PDF is scanned or has pages with no text layer. The `.md` will be
  incomplete. **Offer the user to run it through the `super-ocr` skill**, which sends it to
  Unlimited-OCR on the local GPU.
- **`table_health < 0.6`** — tables came out with more than 40% empty cells. Retry that file with
  `--pdf-engine both` and compare; if both fail, it is a table only a VLM can reconstruct →
  `super-ocr --force-ocr`.

`table_health` is the fraction of non-empty cells across the Markdown's tables, computed by the
router. It also goes into the front-matter, so every `.md` is self-diagnosing.

## 6. Self-anneal on failure

Per-file errors don't abort the batch; each one lands in `results[].error`.

- **`uv` is not recognized as a command** — either it isn't installed (§2.1) or winget installed it
  in this same session and PATH didn't refresh: use the full path from §2.1.
- **`ModuleNotFoundError`** — it was invoked with `python` instead of `uv run`. The skill installs
  nothing into the system Python, by design.
- **`No solution found when resolving script dependencies`** — the PEP 723 header was edited and a
  dependency with only pre-release versions got in (typically by going back to `markitdown[all]`);
  see the note in §2.
- **`not found`** — mistyped path.
- **Unsupported extension** — not in the router's set; tell the user.
- **`firecrawl scrape failed`** — out of credits, no API key or no network. Retry with
  `--url-engine markitdown` (worse quality, but offline and free).
- **Corrupt or password-protected file** — report and skip.

If a recurring, fixable issue appears, update this SKILL.md and/or `scripts/convert.py` — the system
gets stronger (self-annealing loop, per CLAUDE.md).

## 7. Final report

```
=== convert-to-markdown ===
Converted: N   Failed: M   →  <out_dir>/   engines={pdf-inspector: a, markitdown: b, firecrawl: c}
- <source>  →  <out_dir>/<name>.md   (<chars> chars, <engine>)
- <source>  →  FAILED: <error>
~ <n> PDF(s) need OCR → offer the super-ocr skill
```

## Notes

- YAML front-matter by default (`source`, `title`, `converted_at`, `converter`, `pdf_type`, `pages`,
  `table_health`); disable with `--no-front-matter`. It keeps **which engine produced each `.md`**
  traceable.
- Everything is local and free **except URLs**, which consume Firecrawl credits.
- **Scope: project skill.** It lives in `.claude/skills/convert-to-markdown/` in this repo and
  installs nothing into the system Python: its dependencies live in uv's cache. The global copy at
  `~/.claude/Skills/convert-to-markdown/` is a separate thing — this is the local evolution, not a
  replacement.
- **Portability: literal copy-paste.** The folder is 2 text files (~21 KB). Ctrl+C / Ctrl+V into
  another project and `uv run` executes it with no install step. The only thing that does not travel
  in the folder is the Firecrawl CLI (npm global) — without it the router falls back to MarkItDown
  for URLs and everything else is unchanged.
- Requirement on the target machine: `uv` (§2.1). The dependency cache is global and shared, so
  copying the skill N times costs no extra disk.
