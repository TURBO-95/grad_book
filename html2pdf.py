#!/usr/bin/env python3
import os
import logging
import argparse
import tempfile
import urllib.parse
import urllib.request
import re
from typing import List, Dict
from pathlib import Path

from bs4 import BeautifulSoup
import asyncio
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from pypdf import PdfWriter, PdfReader
from pypdf.generic import NameObject, ArrayObject

# Configure professional logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S"
)
logger = logging.getLogger(__name__)

# A reasonable list of standard HTML5 elements. 
# Anything not in this list will be treated as a Custom Component.
STANDARD_HTML_TAGS = {
    "a", "abbr", "address", "area", "article", "aside", "audio", "b", "base", 
    "bdi", "bdo", "blockquote", "body", "br", "button", "canvas", "caption", 
    "cite", "code", "col", "colgroup", "data", "datalist", "dd", "del", "details", 
    "dfn", "dialog", "div", "dl", "dt", "em", "embed", "fieldset", "figcaption", 
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "head", "header", 
    "hr", "html", "i", "iframe", "img", "input", "ins", "kbd", "label", "legend", 
    "li", "link", "main", "map", "mark", "meta", "meter", "nav", "noscript", "object", 
    "ol", "optgroup", "option", "output", "p", "param", "picture", "pre", "progress", 
    "q", "rp", "rt", "ruby", "s", "samp", "script", "section", "select", "small", 
    "source", "span", "strong", "style", "sub", "summary", "sup", "svg", "table", 
    "tbody", "td", "template", "textarea", "tfoot", "th", "thead", "time", "title", 
    "tr", "track", "u", "ul", "var", "video", "wbr"
}

class ComponentInjector:
    def __init__(self, components_dir: str):
        self.components_dir = Path(components_dir) if components_dir else None
        
        # IN-MEMORY CACHE: Stores loaded HTML components to eliminate repetitive Disk I/O
        self._component_cache: Dict[str, str] = {}

    def _get_component_template(self, component_name: str) -> str:
        """
        Fetches the raw HTML template of a custom component.
        Checks the in-memory cache first before hitting the disk.
        """
        # 1. Cache Hit: Return immediately from RAM
        if component_name in self._component_cache:
            return self._component_cache[component_name]

        # 2. Cache Miss: Resolve from Disk
        component_file = self.components_dir / f"{component_name}.html"

        if not component_file.exists():
            return None

        # Read, store in cache, and return
        with open(component_file, 'r', encoding='utf-8') as cf:
            component_html = cf.read()
            
        self._component_cache[component_name] = component_html
        return component_html

    def process_html(self, original_html_path: str) -> str:
        """
        Parses the HTML, finds custom components, injects them, and returns 
        the path to a new temporary HTML file ready for Playwright.
        """
        with open(original_html_path, 'r', encoding='utf-8') as f:
            # OPTIMIZATION: Replaced 'html.parser' with the ultra-fast C-based 'lxml' engine
            soup = BeautifulSoup(f, 'lxml')

        # Find all tags in the document
        all_tags = soup.find_all(True)
        
        # Filter for custom tags (case-insensitive due to BeautifulSoup's parser)
        custom_tags = [tag for tag in all_tags if tag.name not in STANDARD_HTML_TAGS]

        if not custom_tags:
            # If no custom components are used, return the original file to save processing time
            return original_html_path

        if not self.components_dir or not self.components_dir.is_dir():
            logger.warning(f"Custom tags found in {original_html_path}, but components directory "
                           f"'{self.components_dir}' is missing or invalid. Skipping injection.")
            return original_html_path

        logger.info(f"Injecting {len(custom_tags)} custom components into {os.path.basename(original_html_path)}")

        for tag in custom_tags:
            component_name = tag.name  # e.g., 'custom-card'
            
            # Use the new caching method
            component_html = self._get_component_template(component_name)

            if not component_html:
                logger.error(f"Component file not found: {component_name}.html. Element <{component_name}> left as-is.")
                continue

            # Simple string interpolation for arguments (e.g., {title})
            for attr_name, attr_value in tag.attrs.items():
                pattern = re.compile(r'\{\s*' + re.escape(attr_name) + r'\s*\}')
                component_html = pattern.sub(str(attr_value), component_html)

            # Clean up any leftover {placeholders} that weren't provided in the tag attributes
            component_html = re.sub(r'\{\s*[a-zA-Z0-9_-]+\s*\}', '', component_html)

            # Parse the interpolated component string into a BeautifulSoup object (using lxml here too)
            new_node = BeautifulSoup(component_html, 'lxml')
            
            # Since BeautifulSoup(..., 'lxml') wraps fragments in <html><body>, we extract just the children
            # to prevent accidentally injecting nested <html> tags into the document
            if new_node.body:
                new_elements = new_node.body.contents
            else:
                new_elements = new_node.contents

            # Replace the custom tag with the unpacked elements
            for el in reversed(new_elements):
                tag.insert_after(el)
            tag.extract()

        original_dir = os.path.dirname(os.path.abspath(original_html_path))
        for a_tag in soup.find_all('a', href=True):
            href = a_tag['href']
            
            # Leave external links and pure intra-page anchors alone
            if href.startswith(('http://', 'https://', 'mailto:', 'tel:', 'data:', '#')):
                continue
                
            # It's a relative inter-page link (e.g., 'chapter1.html')
            parts = href.split('#', 1)
            rel_path = parts[0]
            
            if rel_path:
                # Convert the relative path to an absolute path
                target_abs_path = os.path.normpath(os.path.join(original_dir, rel_path))
                
                # Mask it as a fake URL that we will intercept during PDF merge
                encoded_path = urllib.parse.quote(target_abs_path)
                
                # Preserve the anchor hash if it exists (though we jump to file start)
                if len(parts) > 1:
                    encoded_path += f"#{parts[1]}"
                    
                a_tag['href'] = f"http://internal-pdf/{encoded_path}"

        original_dir_url = urllib.parse.urljoin('file:', urllib.request.pathname2url(os.path.dirname(os.path.abspath(original_html_path)) + os.sep))
        base_tag = soup.new_tag("base", href=original_dir_url)
        
        # BUG FIX: Safely ensure the <head> tag exists before trying to inject into it
        if soup.head:
            soup.head.insert(0, base_tag)
        else:
            head_tag = soup.new_tag("head")
            head_tag.append(base_tag)
            if soup.html:
                soup.html.insert(0, head_tag)
            else:
                soup.insert(0, head_tag)

        # Save the processed HTML to a secure temporary file
        fd, temp_processed_path = tempfile.mkstemp(suffix=".html", prefix="processed_")
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(str(soup))

        return temp_processed_path


class HTMLToPDFConverter:
    def __init__(self, paper_format: str = "A4", print_background: bool = True, 
                 inject_helpers: bool = True, components_dir: str = None, 
                 max_concurrent: int = 5):
        self.paper_format = paper_format
        self.print_background = print_background
        self.inject_helpers = inject_helpers
        self.injector = ComponentInjector(components_dir)
        self.temp_files_to_cleanup = []
        self.max_concurrent = max_concurrent

    async def _inject_paging_helpers(self, page):
        """Injects modern CSS Paged Media rules and a JS auto-scaler asynchronously."""
        await page.add_style_tag(content="""
            @media print {
                .pdf-page { height: 100vh; width: 100%; break-after: page; page-break-after: always; box-sizing: border-box; position: relative; overflow: hidden; }
                .pdf-flex-page { display: flex; flex-direction: column; height: 100vh; width: 100%; break-after: page; page-break-after: always; box-sizing: border-box; }
                body { margin: 0 !important; padding: 0 !important; }
            }
        """)
        await page.evaluate("""
            () => {
                const elements = document.querySelectorAll('.auto-scale-to-fit');
                elements.forEach(el => {
                    el.style.transform = 'none';
                    el.style.transformOrigin = 'top left';
                    const scale = Math.min(window.innerWidth / el.getBoundingClientRect().width, window.innerHeight / el.getBoundingClientRect().height, 1);
                    if (scale < 1) { el.style.transform = `scale(${scale})`; }
                });
            }
        """)

    async def _render_single_page(self, context, html_item, semaphore):
        """Worker function to process and render a single PDF concurrently."""
        html_file, toc_title = html_item  # Unpack the tuple
        
        async with semaphore:
            if not os.path.exists(html_file):
                logger.warning(f"File not found: {html_file}. Skipping.")
                return None

            processed_html_path = await asyncio.to_thread(self.injector.process_html, html_file)
            
            if processed_html_path != html_file:
                self.temp_files_to_cleanup.append(processed_html_path)

            fd, temp_pdf_path = tempfile.mkstemp(suffix=".pdf")
            os.close(fd)
            self.temp_files_to_cleanup.append(temp_pdf_path)

            abs_path = os.path.abspath(processed_html_path)
            file_url = urllib.parse.urljoin('file:', urllib.request.pathname2url(abs_path))

            logger.info(f"Rendering: {os.path.basename(html_file)}")
            
            page = await context.new_page()
            try:
                await page.goto(file_url, wait_until="load", timeout=30000)
                await page.evaluate("document.fonts.ready")
                await page.emulate_media(media="print")

                if self.inject_helpers:
                    await self._inject_paging_helpers(page)
                
                await page.pdf(
                    path=temp_pdf_path,
                    format=self.paper_format,
                    print_background=self.print_background,
                    margin={"top": "10mm", "right": "10mm", "bottom": "10mm", "left": "10mm"}
                )
                original_abs_path = os.path.normpath(os.path.abspath(html_file))
                return temp_pdf_path, toc_title, original_abs_path
            except Exception as e:
                logger.error(f"Failed to render {html_file}: {str(e)}")
                return None
            finally:
                await page.close()


    async def convert_to_temp_pdfs(self, html_items: List[tuple[str, str]]) -> List[tuple[str, str]]:
        semaphore = asyncio.Semaphore(self.max_concurrent)
        
        try:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context()

                tasks = [
                    self._render_single_page(context, item, semaphore) 
                    for item in html_items
                ]
                
                results = await asyncio.gather(*tasks)
                await browser.close()
                
                # Filter out failures while maintaining original order
                return [res for res in results if res is not None]

        except Exception as e:
            logger.error(f"Playwright encountered an error: {str(e)}")
            self.cleanup()
            raise

    def _rewire_internal_links(self, merger: PdfWriter, page_map: Dict[str, int]):
        """Scans the PDF for masked internal links and converts them to precise Page Jumps."""
        for page in merger.pages:
            if "/Annots" in page:
                for annot_ref in page["/Annots"]:
                    annot = annot_ref.get_object()
                    # Look for Link Annotations
                    if annot.get("/Subtype") == "/Link" and "/A" in annot:
                        action = annot["/A"].get_object()
                        # Look for URL actions
                        if action.get("/S") == "/URI":
                            uri = action.get("/URI", "")
                            
                            # Intercept our masked links
                            if uri.startswith("http://internal-pdf/"):
                                encoded_path = uri[len("http://internal-pdf/"):]
                                # Strip #hash (We jump to the start of the file)
                                encoded_path = encoded_path.split('#')[0] 
                                target_abs_path = urllib.parse.unquote(encoded_path)
                                
                                if target_abs_path in page_map:
                                    target_page_idx = page_map[target_abs_path]
                                    target_page_obj = merger.pages[target_page_idx]
                                    
                                    # Mutate PDF action from URI to internal GoTo Page
                                    action[NameObject("/S")] = NameObject("/GoTo")
                                    del action["/URI"]
                                    # Set destination: [TargetPageReference, Fit Page]
                                    action[NameObject("/D")] = ArrayObject([target_page_obj.indirect_reference, NameObject("/Fit")])
                                else:
                                    # If the target file wasn't rendered, kill the broken link action
                                    del annot["/A"]

    def merge_and_save(self, pdf_files_with_toc: List[tuple[str, str, str]], output_path: str, append: bool = False):
        if not pdf_files_with_toc:
            return
            
        merger = PdfWriter()
        current_page_index = 0
        page_map = {} # Track which file starts on which page
        
        try:
            if append and os.path.exists(output_path):
                merger.append(output_path)
                current_page_index = len(merger.pages)
                
            for pdf_path, toc_title, original_abs_path in pdf_files_with_toc:
                # Map the original HTML file to its exact start page in the PDF
                page_map[original_abs_path] = current_page_index
                
                reader = PdfReader(pdf_path)
                num_pages = len(reader.pages)
                
                if toc_title:
                    merger.add_outline_item(title=toc_title, page_number=current_page_index)
                    
                merger.append(reader)
                current_page_index += num_pages
                
            # Perform the magic rewiring before writing to disk
            self._rewire_internal_links(merger, page_map)
                
            merger.write(output_path)
        except Exception as e:
            logger.error(f"Error during PDF merging: {str(e)}")
        finally:
            merger.close()
            self.cleanup()

    def cleanup(self):
        """Cleans up all intermediate files (both PDF and processed HTML)."""
        for path in self.temp_files_to_cleanup:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except Exception as e:
                logger.warning(f"Could not delete temporary file {path}: {str(e)}")
        self.temp_files_to_cleanup = []

def format_toc_name(name: str, fix_names: bool) -> str:
    """Helper to format directory/file names into readable ToC titles."""
    if not fix_names:
        return name
    return name.replace('-', ' ').replace('_', ' ').title()

def resolve_input_paths(input_args: List[str], fix_names: bool = False) -> List[tuple[str, str]]:
    """
    Scans inputs and maps HTML files to Table of Contents titles.
    Returns a list of tuples: (file_path, toc_title)
    """
    resolved_files = []
    
    for item in input_args:
        path = Path(item)
        if path.is_file():
            # Direct files passed get their stem as the ToC name
            toc_name = format_toc_name(path.stem, fix_names)
            resolved_files.append((str(path), toc_name))
            
        elif path.is_dir():
            # Get immediate children and sort alphabetically
            children = sorted(path.iterdir())
            
            for child in children:
                if child.is_file() and child.suffix.lower() in ['.html', '.htm']:
                    # Stray Page found in the top level directory
                    toc_name = format_toc_name(child.stem, fix_names)
                    resolved_files.append((str(child), toc_name))
                    
                elif child.is_dir():
                    # Sub-directory found
                    sub_files = sorted([p for p in child.glob("*.[hH][tT][mM]*") if p.is_file()])
                    if sub_files:
                        toc_name = format_toc_name(child.name, fix_names)
                        # Attach the Topic Name ONLY to the first file in this subdirectory
                        resolved_files.append((str(sub_files[0]), toc_name))
                        # The rest of the files in this subdirectory get 'None' so they 
                        # don't generate new ToC entries.
                        for sub_f in sub_files[1:]:
                            resolved_files.append((str(sub_f), None))
                            
    return resolved_files

async def async_main():
    parser = argparse.ArgumentParser(description="Professional HTML to PDF converter with React-like Component Support.")
    parser.add_argument("-i", "--inputs", nargs="+", required=True, help="List of HTML files OR Directories.")
    parser.add_argument("-o", "--output", required=True, help="Path to the output PDF file.")
    parser.add_argument("-a", "--append", action="store_true", help="Append to the output PDF.")
    parser.add_argument("-f", "--format", default="A4", help="Paper format (Default: A4).")
    parser.add_argument("-c", "--components", default=None, help="Path to the directory containing Custom Components.")
    # Added argument to let user control concurrent tab limits
    parser.add_argument("--workers", type=int, default=5, help="Number of concurrent pages to render (Default: 5).")
    parser.add_argument("--no-background", action="store_false", dest="background")
    parser.add_argument("--no-helpers", action="store_false", dest="helpers")
    parser.add_argument("--fix-names", action="store_true", help="Format ToC titles: replace '_' and '-' with spaces and capitalize words.")

    args = parser.parse_args()
    final_inputs = resolve_input_paths(args.inputs, args.fix_names)

    if not final_inputs:
        logger.error("No valid HTML files found. Exiting.")
        return

    converter = HTMLToPDFConverter(
        paper_format=args.format,
        print_background=args.background,
        inject_helpers=args.helpers,
        components_dir=args.components,
        max_concurrent=args.workers
    )

    # Await the new async method
    temp_pdfs = await converter.convert_to_temp_pdfs(final_inputs)
    
    if temp_pdfs:
        # Merging is still inherently synchronous, which is fine as PyPDF is extremely fast
        converter.merge_and_save(temp_pdfs, args.output, append=args.append)
        logger.info("Operation completed successfully.")

def main():
    # Bootstrap the asyncio event loop
    asyncio.run(async_main())

if __name__ == "__main__":
    main()