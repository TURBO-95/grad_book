# `html2pdf.py` — Deep Dive Documentation

## Overview

`html2pdf.py` is a Python CLI tool that converts one or more HTML files into a
single, professional-quality PDF document. It is driven by **Playwright**
(headless Chromium) for rendering and **PyPDF** for PDF manipulation.

The tool layers several sophisticated features on top of a basic "print to PDF"
operation:

1. **React-like Component Injection** — custom HTML tags are expanded from
   template files before rendering.
2. **Dynamic Internal Link Rewiring** — relative HTML links become precise
   in-PDF page-jump actions.
3. **CSS Paged-Media Helpers** — injected `@media print` rules and a JS
   auto-scaler control page height and split points.
4. **Table-of-Contents (ToC) from Directory Structure** — subdirectories of the
   input folder map to named outline items (bookmarks) in the PDF.
5. **Concurrent Async Rendering** — multiple pages are rendered in parallel via
   an `asyncio` semaphore-bounded pool.

---

## Architecture at a Glance

```
CLI args
  └─► resolve_input_paths()          # Builds [(file_path, toc_title)] list
        └─► HTMLToPDFConverter
              ├─ convert_to_temp_pdfs()   # Async: renders HTML → temp PDFs
              │    ├─ ComponentInjector.process_html()  # Expand custom tags
              │    │    ├─ _get_component_template()    # Disk/cache lookup
              │    │    ├─ String interpolation         # {attr} replacement
              │    │    └─ Link masking                 # → http://internal-pdf/
              │    └─ _render_single_page()             # Playwright render
              │         └─ _inject_paging_helpers()     # CSS + JS injection
              └─ merge_and_save()         # Merge + rewire + write PDF
                   └─ _rewire_internal_links()          # URI → GoTo actions
```

---

## Feature 1 — React-like Component System

### Purpose
Allows HTML pages to use **custom tags** (e.g., `<chapter-header>`,
`<info-card>`) that are automatically expanded into full HTML before rendering.
This is analogous to React components: reusable, parameterised HTML fragments.

### Key Constant: `STANDARD_HTML_TAGS` (Lines 28–41)
A hardcoded set of all known HTML5 element names. Any tag whose name is **not**
in this set is classified as a custom component.

### Class: `ComponentInjector` (Lines 43–175)

#### `__init__(components_dir)` (Line 44)
- Receives the path to a directory containing `<component-name>.html` files.
- Initialises `_component_cache: Dict[str, str]` — an **in-memory cache** to
  avoid repeated disk reads when the same component appears in multiple pages.

#### `_get_component_template(component_name)` (Line 50)
- **Cache-first lookup**: if the component was already loaded, return it from
  RAM.
- On a cache miss, reads `<components_dir>/<component_name>.html` from disk,
  stores it in the cache, and returns the raw HTML string.
- Returns `None` if the file doesn't exist (caller logs an error and skips).

#### `process_html(original_html_path)` (Line 72) — Full Logic Flow

```
1. Parse the HTML with BeautifulSoup (lxml engine for speed).
2. Find ALL tags in the document.
3. Filter to custom tags (name ∉ STANDARD_HTML_TAGS).
4. Early-exit: if no custom tags → return original path unchanged (no processing cost).
5. For each custom tag:
   a. Fetch its template HTML (via _get_component_template).
   b. Perform {attr} string interpolation using the tag's HTML attributes.
      e.g. <info-card title="Hello"> fills {title} → "Hello" in the template.
   c. Strip any leftover {placeholders} that had no matching attribute.
   d. Parse the interpolated string into a new BS4 node (lxml).
   e. Extract the body children (avoids double-wrapping <html><body>).
   f. Replace the original custom tag with the expanded elements (insert_after + extract).
6. Link Masking pass (see Feature 2 below).
7. Inject a <base href="file:///..."> tag pointing to the original HTML's
   directory. This ensures CSS/image relative paths resolve correctly after
   the file is saved to a temp location.
8. Write the processed document to a secure temp file (tempfile.mkstemp).
9. Return the temp file path.
```

### How to Use Components
1. Create a `components/` directory (or any directory, pass with `-c`).
2. For a tag `<my-card title="Hello" color="blue">`, create
   `components/my-card.html`:
   ```html
   <div class="card" style="background:{color}">
     <h2>{title}</h2>
   </div>
   ```
3. Pass `-c ./components` on the CLI. The injector replaces every `<my-card>`
   in every HTML file with the expanded div.

---

## Feature 2 — Dynamic Internal Link Rewiring

This is a **two-phase** mechanism to translate relative HTML file links
(e.g., `href="chapter2.html"`) into correct **PDF page-jump actions**.

### Phase 1 — Link Masking (in `process_html`, Lines 132–154)

During HTML processing, every relative inter-page link is converted to a fake
URL:

```
href="chapter2.html"
  →  href="http://internal-pdf//abs/path/to/chapter2.html"
```

- External links (`http://`, `https://`, `mailto:`, etc.) and pure anchors
  (`#section`) are left untouched.
- The absolute path is **URL-encoded** (`urllib.parse.quote`) so it survives
  as a URL.
- Any `#hash` fragment is preserved in the fake URL (but stripped during
  rewiring — the jump always goes to the start of the target file, not an
  anchor within it).
- Playwright will render these as ordinary hyperlinks; Chromium embeds them
  as `/URI` link annotations in the generated PDF.

### Phase 2 — Link Rewiring (in `_rewire_internal_links`, Lines 281–312)

After all pages are merged into one PDF, this method:

```
1. Iterates over every page in the merged PdfWriter.
2. For each page, checks for /Annots (annotation array).
3. For each annotation of Subtype /Link with an /A (action) of type /URI:
   a. Check if the URI starts with "http://internal-pdf/".
   b. If yes → decode the path, strip #hash.
   c. Look up the decoded absolute path in page_map (built during merge).
   d. If found:
      - Change action type: /S /URI  →  /S /GoTo
      - Delete the /URI key.
      - Set /D (destination): [<target_page_indirect_ref>, /Fit]
        This makes the PDF jump to the exact page and fit it in view.
   e. If NOT found (target wasn't rendered): delete the /A action entirely
      (removes the broken link rather than leaving a dead click zone).
```

#### `page_map` construction (in `merge_and_save`, Lines 327–329)
As each temp PDF is appended to the merger, its **original HTML file's absolute
path** is recorded alongside the **zero-based page index** where it starts:
```python
page_map[original_abs_path] = current_page_index
```
This is the bridge between Phase 1 (path in fake URL) and Phase 2 (page number
in PDF).

---

## Feature 3 — CSS Paged-Media Helpers

Injected by `_inject_paging_helpers(page)` (Lines 189–208) into **every** page
after it loads in Playwright (when `--no-helpers` is not passed).

### Injected CSS (`@media print` block)

| Class | Effect |
|---|---|
| `.pdf-page` | Fixed `100vh` height, `break-after: page` — forces an exact one-page-tall block with a hard page break after it. `overflow: hidden` clips any overflowing content. |
| `.pdf-flex-page` | Same page-break behaviour but uses `display: flex; flex-direction: column` — allows flex-children to fill vertical space naturally within the page. |
| `body` | Zeroes out default browser margins/padding to prevent phantom whitespace pages. |

### Injected JavaScript (auto-scaler)

Targets elements with class `.auto-scale-to-fit`. For each such element, it:

1. Resets any existing `transform`.
2. Computes a scale factor: `min(viewport_width / element_width, viewport_height / element_height, 1)`.
3. If the element is **larger than the viewport** (scale < 1), applies
   `transform: scale(scale)` with `transform-origin: top left`.

This prevents wide tables, diagrams, or code blocks from being clipped at the
page edge.

### How to Use in HTML
```html
<!-- Exact one-page block, content clipped if too tall -->
<div class="pdf-page">
  <h1>Chapter 1</h1>
  <p>...</p>
</div>

<!-- Flex column page, children stretch to fill -->
<div class="pdf-flex-page">
  <header>...</header>
  <main style="flex:1">...</main>
  <footer>...</footer>
</div>

<!-- Wide diagram that auto-shrinks to fit one page -->
<div class="auto-scale-to-fit">
  <img src="wide-diagram.svg">
</div>
```

---

## Feature 4 — Table of Contents from Directory Structure

### `resolve_input_paths(input_args, fix_names)` (Lines 366–402)

Converts the raw CLI inputs into a flat list of `(file_path, toc_title)` tuples
that directly drive rendering order and ToC generation.

#### Rules

| Input type | Result |
|---|---|
| **Direct file** (`-i page.html`) | `(page.html, stem_as_title)` |
| **Directory** (`-i pages/`) | Recursively scanned (children sorted alphabetically) |
| **File in top-level dir** | `(file, stem_as_title)` — treated like a standalone page |
| **Subdirectory** | First file → `(file, dirname_as_title)`; remaining files → `(file, None)` |

The `None` ToC title signals to `merge_and_save` that no outline entry should
be created for that page (it's a continuation of the section, not a new section
header).

#### Example Directory Layout
```
pages/
├── intro.html              → ToC entry: "intro"
├── 01-overview/
│   ├── 01-summary.html     → ToC entry: "01-overview" (first file)
│   └── 02-details.html     → No ToC entry (continuation)
└── 02-deep-dive/
    ├── 01-part-a.html      → ToC entry: "02-deep-dive"
    └── 02-part-b.html      → No ToC entry
```

#### `--fix-names` flag
When passed, `format_toc_name()` transforms directory/file stems:
- Replaces `-` and `_` with spaces
- Applies Python's `.title()` (capitalises each word)

```
"01-overview"  →  "01 Overview"
"deep_dive"    →  "Deep Dive"
```

### ToC Embedding in PDF
In `merge_and_save` (Line 335):
```python
merger.add_outline_item(title=toc_title, page_number=current_page_index)
```
This creates a native PDF bookmark (outline item) that PDF readers display in
their navigation pane.

---

## Feature 5 — Concurrent Async Rendering

### Architecture
- `convert_to_temp_pdfs()` (Line 257): launches a single **Playwright browser**
  and one **browser context**, then fires off one coroutine per HTML file using
  `asyncio.gather()`.
- An `asyncio.Semaphore(max_concurrent)` gates how many pages render
  simultaneously (default: 5, controllable via `--workers`).

### `_render_single_page(context, html_item, semaphore)` (Line 210) — Flow

```
1. Acquire semaphore slot.
2. Check file existence; return None if missing.
3. Run ComponentInjector.process_html() in a thread pool
   (asyncio.to_thread) so BS4 parsing doesn't block the event loop.
4. Create a new browser page (tab).
5. Navigate to the file:// URL, wait for "load" event (timeout 30s).
6. Wait for document.fonts.ready (ensures web fonts are loaded).
7. Call page.emulate_media("print") — activates @media print CSS.
8. If helpers enabled: inject CSS + JS via _inject_paging_helpers().
9. Call page.pdf() → writes to temp PDF file.
10. Return (temp_pdf_path, toc_title, original_abs_path).
11. Close page; release semaphore.
```

### Failure Handling
- Per-page exceptions are caught and logged; `None` is returned.
- `asyncio.gather()` collects all results; `None`s are filtered out.
- The original file **order is preserved** (gather maintains task order).

---

## CLI Reference

```
python html2pdf.py -i <inputs...> -o <output.pdf> [options]
```

| Argument | Default | Description |
|---|---|---|
| `-i / --inputs` | *(required)* | One or more HTML files or directories |
| `-o / --output` | *(required)* | Output PDF file path |
| `-a / --append` | off | Append to existing PDF instead of overwriting |
| `-f / --format` | `A4` | Playwright paper format (A4, Letter, etc.) |
| `-c / --components` | `None` | Path to custom components directory |
| `--workers` | `5` | Max concurrent Playwright tabs |
| `--no-background` | on | Disable printing CSS backgrounds |
| `--no-helpers` | on | Disable `.pdf-page` / auto-scale injection |
| `--fix-names` | off | Prettify ToC titles (replace `-_` with space, title-case) |

---

## End-to-End Flow (Summary)

```
main()
 └─ asyncio.run(async_main())
      1. Parse CLI args
      2. resolve_input_paths() → [(html_path, toc_title), ...]
      3. HTMLToPDFConverter instantiated
      4. await convert_to_temp_pdfs():
           For each HTML file (up to 5 concurrently):
             a. ComponentInjector.process_html()
                  - Find custom tags
                  - Expand from template files
                  - Interpolate {attrs}
                  - Mask relative links → http://internal-pdf/...
                  - Inject <base href> for resource resolution
                  - Write temp HTML file
             b. Playwright: navigate, wait for fonts, emulate print
             c. Inject CSS (.pdf-page, .pdf-flex-page) + JS (auto-scale)
             d. page.pdf() → temp PDF file
      5. merge_and_save():
           a. (Optional) append existing PDF
           b. For each temp PDF:
              - Record page_map[html_abs_path] = page_index
              - Add outline item if toc_title is set
              - Append pages to merger
           c. _rewire_internal_links():
              - Scan all /Annots for /URI links matching http://internal-pdf/
              - Convert to /GoTo actions pointing to exact page in page_map
              - Remove broken links (unmapped targets)
           d. merger.write(output_path)
      6. cleanup(): delete all temp HTML and PDF files
```

---

## Dependencies

| Package | Role |
|---|---|
| `playwright` | Headless Chromium rendering engine |
| `beautifulsoup4` + `lxml` | HTML parsing & component injection |
| `pypdf` | PDF merging, outline items, annotation mutation |

---

## Known Behaviours / Gotchas

- **`#hash` anchors in cross-file links are ignored** at the PDF level. The
  link jumps to the *first page* of the target HTML's output, not to a named
  anchor within it (PDF anchor support would require named destinations to be
  embedded in the rendered PDFs).
- **`lxml` wraps fragments** in `<html><body>` when parsing component strings —
  the code explicitly extracts `.body.contents` to avoid injecting nested
  `<html>` tags.
- **Component attributes are global** across a render: a template string is
  mutated by one tag's attributes, and if multiple instances exist, each gets a
  fresh copy from the cache (the cache stores the *original* template, not the
  interpolated version — interpolation happens on a local copy of the string).
- **Semaphore protects memory** — with `max_concurrent=5` and large pages,
  peak RAM ~ 5 × page_RAM. Reduce `--workers` if memory is constrained.
- **`--append` mode** reads the existing PDF's page count to correctly offset
  `page_map` indices.
