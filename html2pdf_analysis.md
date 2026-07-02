# Deep Technical Analysis: `html2pdf.py`

A batch HTML→PDF conversion tool built on Playwright (headless Chromium) and pypdf, with a custom "React-like" component injection layer, LaTeX rendering support, and cross-document hyperlink rewiring for a table-of-contents-driven merged PDF output.

---

## 1. Technicalities

### 1.1 Core stack
| Library | Role |
|---|---|
| `playwright.async_api` | Drives headless Chromium to load HTML and print to PDF (this is the actual rendering engine — not `wkhtmltopdf` or `weasyprint`) |
| `BeautifulSoup` (`bs4`, `lxml` parser) | Parses/mutates HTML DOM before rendering (component injection, link rewriting, `<base>` tag insertion) |
| `pypdf` (`PdfWriter`/`PdfReader`) | Merges individually-rendered PDFs into one file, builds outline/bookmarks (ToC), and rewrites internal link annotations at the PDF object level |
| `asyncio` | Concurrency control for rendering many pages in parallel |
| `argparse` | CLI interface |

### 1.2 Architecture / control flow
The script is organized into two cooperating classes plus a functional CLI layer:

1. **`ComponentInjector`** — pure HTML preprocessing (string/DOM level, synchronous, runs in a thread via `asyncio.to_thread`).
2. **`HTMLToPDFConverter`** — orchestrates the async Playwright rendering pipeline and the pypdf merge/rewire stage.
3. **Module-level functions** (`resolve_input_paths`, `format_toc_name`, `async_main`, `main`) — CLI glue.

Pipeline per file:
```
input path → ComponentInjector.process_html() → temp processed HTML
           → Playwright: goto(file://...) → optional LaTeX render → paging CSS injection → page.pdf()
           → temp single PDF
(after all files) → pypdf merge (with ToC bookmarks) → rewire internal <a href> links into PDF /GoTo actions
           → final output PDF
```

### 1.3 Concurrency model
- A single Chromium **browser** and **browser context** are shared across all renders (`convert_to_temp_pdfs`), but each HTML file gets its own **page** (tab).
- An `asyncio.Semaphore(max_concurrent)` (default 5, `--workers`) throttles how many pages render simultaneously — prevents unbounded memory/CPU usage on large batches.
- `asyncio.gather` runs all render tasks concurrently, and `asyncio.to_thread` is used to run the synchronous BeautifulSoup preprocessing off the event loop, avoiding blocking the asyncio loop during large-file DOM parsing.

### 1.4 Low-level PDF manipulation
The most sophisticated part of the script is `_rewire_internal_links`. Rather than treating PDFs as opaque blobs, it walks the raw PDF object graph:
- Iterates every page's `/Annots` (annotations).
- Filters for `/Subtype == /Link` with a `/A` (action) dictionary of subtype `/URI`.
- Matches URIs against the custom `http://internal-pdf/<encoded-path>` scheme (see §1.5) it seeded earlier.
- Converts a **web URI action** (`/S /URI`) into a **PDF-native internal jump action** (`/S /GoTo`) pointing at an indirect object reference to the destination page, using `/Fit` framing.
- If the link's target file isn't part of this conversion job (not in `page_map`), it strips the action entirely rather than leaving a dangling/broken URI.

This is real PDF spec-level manipulation (`NameObject`, `ArrayObject`, indirect references) — not something achievable via the high-level `PdfWriter` merge API alone.

### 1.5 Link-path resolution trick
Since Chromium's PDF export can't natively preserve "jump to another rendered document" semantics, the script uses a clever two-phase trick:
1. **Before rendering:** every relative `<a href="...">` in the source HTML (excluding `http(s)://`, `mailto:`, `tel:`, `data:`, and `#`-only anchors) is rewritten to an absolute filesystem path, then re-encoded as a synthetic pseudo-URL: `http://internal-pdf/<url-quoted-absolute-path>[#fragment]`. Chromium prints this into the PDF as a normal external-link annotation.
2. **After all files are merged:** `_rewire_internal_links` scans for that `internal-pdf` scheme, matches it against a `page_map` (absolute source path → starting page index in the merged PDF), and rewrites it to a real internal `/GoTo` link.

This means cross-file `<a href="other-page.html">` links in the original HTML become working in-PDF navigation links after merge — a non-trivial feature most simple HTML-to-PDF tools lack.

### 1.6 `<base>` tag injection
To ensure relative asset paths (images, CSS, JS) resolve correctly when Chromium loads the HTML from a temp file (which may live in a different directory than the original), the script computes the *original* file's directory as a `file://` URL and injects a `<base href="...">` tag into `<head>` (creating `<head>` if absent). This decouples "where the temp processed file lives" from "where relative assets should resolve against."

### 1.7 Error handling & resource cleanup
- Per-page failures are caught and logged (`_render_single_page`'s try/except), and failed items are filtered out of `results` rather than aborting the whole batch.
- All temp files (processed HTML + intermediate single-page PDFs) are tracked in `self.temp_files_to_cleanup` and removed in a `finally` block (`cleanup()`), even on merge failure.
- Playwright-level errors during the whole batch (e.g., browser launch failure) trigger `cleanup()` and re-raise, aborting the run cleanly.

### 1.8 Notable technical caveat
`enable_latex` is stored on the converter but LaTeX rendering is actually decided **per-page** by content sniffing (`"$$" in content or "\\[" in content or "\\(" in content`) **or** the `enable_latex` flag — so `--latex` forces it on for every page regardless of content, while by default it's auto-detected. Minor inconsistency: the constructor flag is named as if it's the sole switch, but auto-detection happens unconditionally either way.

---

## 2. Functionalities

### 2.1 Custom HTML "component" system (`ComponentInjector`)
This is the standout feature — a lightweight, server-side, React/Vue-like custom component system for raw HTML:

- Any non-standard tag name found in the source HTML (checked against a hardcoded set of ~90 standard HTML5 tag names, `STANDARD_HTML_TAGS`) is treated as a **custom component**, e.g. `<my-card title="Hello"></my-card>`.
- The injector looks for `<components_dir>/<tag-name>.html` as that component's template.
- **Placeholder substitution:** inside the component template, `{placeholder}` tokens (regex `\{\s*([a-zA-Z0-9_-]+)\s*\}`, case-insensitive key matching) are replaced by:
  1. The matching HTML attribute value on the custom tag, if present (e.g., `{title}` ← `title="Hello"`).
  2. Special-cased `{content}` placeholder ← the custom tag's **inner HTML** (slot-like children pass-through), if no matching attribute exists.
  3. Empty string if neither is found.
- The rendered component template's body content replaces the custom tag in-place in the DOM (inserted after, original tag extracted/removed).
- Templates are cached in-memory (`_component_cache`) per converter run to avoid redundant disk reads.

This effectively gives HTML authors a "slots + props" templating mechanism without any JS framework or build step — components resolve to plain HTML before Chromium ever sees the page.

### 2.2 Paging & print-layout helpers (`_inject_paging_helpers`)
Injected via `page.add_style_tag` unless disabled (`--no-helpers`):
- `.pdf-page` and `.pdf-flex-page` utility classes give authors explicit "one PDF page = one div" control (`height: 100vh`, forced page breaks via `break-after`/`page-break-after`).
- Zeroes out default body margin/padding for print media.
- An `.auto-scale-to-fit` JS-based auto-shrink: measures an element's bounding box against the viewport and applies a CSS `transform: scale()` (never upscaling, `min(...,1)`) so oversized content (e.g., a wide table or diagram) is shrunk to fit the page instead of being clipped.

### 2.3 LaTeX math rendering (`_render_latex`)
- Injects KaTeX (CSS/JS) and its `auto-render` extension from a CDN (jsdelivr, pinned to v0.16.8).
- Waits for `window.renderMathInElement` to be defined before invoking it, avoiding race conditions.
- Calls `renderMathInElement` with `throwOnError: false` so malformed LaTeX degrades gracefully instead of crashing the render.
- Auto-triggered by content sniffing for `$$`, `\[`, or `\(` delimiters, or forced on with `--latex`.

### 2.4 Batch rendering with concurrency limits
Multiple HTML files/directories render in parallel (bounded by `--workers`), each in its own Playwright page/tab within a shared browser context — much faster than sequential rendering for large document sets, while still bounding resource usage.

### 2.5 Merge, Table of Contents, and append mode
- All individually-rendered PDFs are merged in input order into a single output PDF via `pypdf.PdfWriter`.
- Each source file contributes a **bookmark/outline entry** (`add_outline_item`) titled by its filename (or directory name for folder-grouped sets), pointing at the first page of that section — producing a navigable PDF ToC/sidebar automatically.
- `--append` mode: if the output file already exists, new content is appended after existing pages (bookmarks and page indices offset accordingly) rather than overwritten.

### 2.6 Cross-document internal hyperlinks
As detailed in §1.4–1.5: relative `<a href>` links between input HTML files become clickable in-PDF navigation links in the final merged document, correctly pointing to the right page even after merge/reordering. Dead links (pointing to files not included in the batch) are cleaned up (URI action removed) rather than left broken.

### 2.7 Flexible input resolution (`resolve_input_paths`)
Accepts a mix of individual files and directories in the same invocation:
- A **file** path is added directly, ToC title = filename stem.
- A **directory** is scanned (sorted, non-recursive at top level) for `.html`/`.htm` files, each added individually.
- A **nested subdirectory** is treated as a "chapter": its `.htm*` files are sorted and glob-matched case-insensitively; the *first* file gets a ToC entry named after the subdirectory, and the rest are added silently (`toc_name=None`) — implying they're continuation pages of the same logical section, not new bookmarks.

### 2.8 ToC name formatting
`--fix-names` flag: turns filenames like `chapter-1_intro` into a human-readable ToC label `Chapter 1 Intro` (replace `-`/`_` with spaces, title-case).

---

## 3. Usage Ways

### 3.1 Prerequisites
```bash
pip install beautifulsoup4 lxml playwright pypdf
playwright install chromium
```

### 3.2 Basic single-file conversion
```bash
python html2pdf.py -i report.html -o report.pdf
```

### 3.3 Batch conversion of multiple explicit files
```bash
python html2pdf.py -i chapter1.html chapter2.html chapter3.html -o book.pdf
```

### 3.4 Convert an entire directory (each `.html` file → one section)
```bash
python html2pdf.py -i ./docs -o manual.pdf
```

### 3.5 Directory-of-directories ("chapters" with sub-pages)
```
docs/
  intro.html
  chapter-1/
    page1.html
    page2.html
  chapter-2/
    ...
python html2pdf.py -i ./docs -o manual.pdf --fix-names
```
Each `chapter-*` folder becomes one ToC bookmark; its internal pages are merged as continuation pages.

### 3.6 Using custom HTML components
```bash
python html2pdf.py -i page.html -o out.pdf -c ./components
```
Where `page.html` might contain:
```html
<invoice-header company="Acme Inc" date="2026-07-01"></invoice-header>
<my-note>This is important context.</my-note>
```
and `./components/invoice-header.html` / `./components/my-note.html` contain templates using `{company}`, `{date}`, or `{content}` placeholders.

### 3.7 Enabling LaTeX rendering explicitly
```bash
python html2pdf.py -i math.html -o math.pdf --latex
```
(Auto-detected anyway if `$$...$$` or `\(...\)` appears in the page.)

### 3.8 Controlling concurrency
```bash
python html2pdf.py -i ./docs -o out.pdf --workers 10
```

### 3.9 Custom paper size, disabling backgrounds/helpers
```bash
python html2pdf.py -i page.html -o out.pdf -f Letter --no-background --no-helpers
```

### 3.10 Appending to an existing PDF
```bash
python html2pdf.py -i addendum.html -o existing_report.pdf --append
```

### 3.11 Full flag reference
| Flag | Description | Default |
|---|---|---|
| `-i, --inputs` | One or more HTML files and/or directories (required) | — |
| `-o, --output` | Output PDF path (required) | — |
| `-a, --append` | Append to existing output PDF | off |
| `-f, --format` | Paper format (A4, Letter, etc.) | `A4` |
| `-c, --components` | Directory of custom component templates | none |
| `--latex` | Force-enable KaTeX rendering | auto-detect |
| `--workers` | Max concurrent Chromium pages | 5 |
| `--no-background` | Disable printing of CSS backgrounds | backgrounds on |
| `--no-helpers` | Disable paging/auto-scale CSS/JS injection | helpers on |
| `--fix-names` | Prettify ToC titles from filenames | off |

---

## 4. Features (Summary List)

- ✅ Headless-Chromium-accurate rendering (full CSS3/modern layout support, unlike simpler HTML→PDF converters)
- ✅ Batch and directory-based input, including nested "chapter" folder structures
- ✅ Concurrent rendering with configurable worker pool (`asyncio.Semaphore`)
- ✅ Automatic PDF Table of Contents / outline bookmarks per file or folder
- ✅ Append-to-existing-PDF mode
- ✅ Custom reusable HTML "components" with prop-like attribute placeholders and slot-like inner content
- ✅ Automatic LaTeX math rendering via KaTeX, with content auto-detection
- ✅ Print-friendly CSS helpers: explicit page-break utility classes (`.pdf-page`, `.pdf-flex-page`) and an auto-shrink-to-fit class for oversized elements
- ✅ Correct relative asset resolution via injected `<base>` tag
- ✅ **Working cross-document hyperlinks** in the merged PDF (rewritten from HTML `<a href>` to native PDF `/GoTo` actions) — including graceful removal of dead links
- ✅ Configurable paper format and background-graphics printing
- ✅ Robust temp-file cleanup and per-file error isolation (one bad file doesn't kill the whole batch)
- ✅ CLI-first design with sensible defaults, suitable for scripting/CI pipelines

---

## 5. Overall Description

`html2pdf.py` is a **production-oriented, developer-facing document generation pipeline**, not a simple format-converter script. It positions itself as a professional alternative to lightweight tools like `wkhtmltopdf` or `weasyprint` by using a real browser engine (headless Chromium via Playwright) for pixel/CSS-accurate rendering, while layering on a set of features clearly aimed at generating **long-form, multi-file, book-or-report-style PDF documents** — think technical manuals, generated invoices/reports, documentation exports, or academic papers with math.

Three design decisions distinguish it from a typical "convert HTML to PDF" utility:

1. **It treats a document set, not a single file, as the unit of work.** The directory/sub-directory input resolution, automatic ToC/bookmark generation, and append mode all suggest it's meant to assemble many individually-authored HTML fragments into one cohesive, navigable, multi-chapter PDF — the way a static site generator assembles pages into a book.

2. **It reimplements a minimal component/templating system on top of raw HTML**, giving authors of the source HTML fragments a "props + slots" model (custom tags resolved against external template files with `{placeholder}` substitution) without requiring a JS build toolchain, front-end framework, or template engine like Jinja2. This is a lightweight, DOM-level macro-expansion system implemented via BeautifulSoup before the page ever reaches Chromium.

3. **It manipulates the *output* PDF at the object level**, not just at rendering time. The internal-link rewiring logic (`_rewire_internal_links`) is genuinely sophisticated: it exploits the fact that Chromium can render normal web hyperlinks into PDF link annotations, then post-processes those annotations after all files are merged to convert them into native intra-document `/GoTo` navigation actions. This lets authors write plain relative `<a href="other-chapter.html">` links in their source HTML and get correctly working "jump to page" links in the final merged PDF — a feature genuinely difficult to achieve with most PDF tooling, and absent from nearly all HTML-to-PDF converters.

Engineering-wise, the script demonstrates solid async design (bounded concurrency via semaphore, thread-offloading of CPU-bound DOM parsing, per-task error isolation so one bad file doesn't abort a large batch), careful resource cleanup (tracked temp files removed in `finally` blocks), and defensive handling of edge cases (missing component files, dangling links, missing `<head>`/`<html>` elements, existing vs. non-existing append targets).

Its limitations/risks worth noting for a technical reviewer:
- **Security surface**: it loads and executes arbitrary local HTML/JS in a real browser (`page.evaluate`, unsanitized component HTML injection, unsanitized `{content}` slot pass-through) — fine for trusted internal documents, but not something to point at untrusted/user-submitted HTML without sandboxing, since it's equivalent to running arbitrary JS.
- **External network dependency**: LaTeX rendering pulls KaTeX from a public CDN at render time — no offline/vendored fallback, so builds fail or hang without internet access to `cdn.jsdelivr.net`.
- **No PDF/A, no accessibility (tagged PDF), no password/encryption support** — pypdf's `write()` here is used purely for merging/outline/link-rewriting, not for compliance-grade output.
- **Chromium print-to-PDF quirks** (e.g., `overflow: hidden` clipping, `100vh` in headless print context) are papered over by the injected paging CSS, but authors still need to understand Chromium's print CSS model to use `.pdf-page`/`.auto-scale-to-fit` effectively.
- The component system offers no recursive/nested component resolution guarantee — it appears to do a single pass over `all_tags` rather than iterating until no custom tags remain, so a component template that itself contains another custom tag would likely NOT get expanded.

Overall, this is a well-architected, moderately advanced internal tooling script — the kind of thing built for a documentation pipeline, an internal reporting system, or an automated report-generation service — rather than a general-purpose public library. Its most valuable and least common feature is the PDF-level internal-link rewiring; its most novel developer-experience feature is the zero-dependency HTML component/templating layer.
