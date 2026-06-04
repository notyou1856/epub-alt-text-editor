import base64
import csv
import hashlib
import html
import io
import mimetypes
import os
import posixpath
import re
import tempfile
import time
import urllib.parse
import zipfile
from collections import Counter
from typing import Any, Dict, List, Set, Tuple

import streamlit as st
from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub
from openai import OpenAI
from PIL import Image, UnidentifiedImageError

import os
import streamlit as st

# Detect environment
ENV = os.getenv("ENVIRONMENT", "PROD")

if ENV == "DEV":
    st.markdown(
        """
        <div style="
            background-color: #ff4b4b;
            padding: 10px;
            border-radius: 8px;
            text-align: center;
            font-weight: bold;
            color: white;
            margin-bottom: 10px;
        ">
            🚧 DEV ENVIRONMENT — TESTING ONLY 🚧
        </div>
        """,
        unsafe_allow_html=True
    )


# ----------------------------
# Page config
# ----------------------------
st.set_page_config(page_title="EPUB Alt Text Editor (MVP)", layout="wide")
#st.title("📘 IDEA EPUB Alt Text Editor (Single-Image Paging)")
if ENV == "DEV":
    st.title("🧪 IDEA EPUB Alt Text Editor (DEV)")
else:
    st.title("📘 IDEA EPUB Alt Text Editor (Single-Image Paging)")


# ----------------------------
# OpenAI client
# ----------------------------
api_key = os.getenv("OPENAI_API_KEY")
client = OpenAI(api_key=api_key) if api_key else None


# ----------------------------
# SAFE EPUB LOADER
# ----------------------------
def _epub_read_options() -> Dict[str, bool]:
    return {
        "ignore_ncx": True,
        "ignore_nav": True,
    }


def _zip_normalized_names(epub_zip: zipfile.ZipFile) -> set[str]:
    return {norm_href(name) for name in epub_zip.namelist()}


def find_missing_manifest_items(path: str) -> List[str]:
    """Find OPF manifest files that are absent from the EPUB archive."""
    missing: List[str] = []

    with zipfile.ZipFile(path, "r") as epub_zip:
        archive_names = _zip_normalized_names(epub_zip)
        try:
            container_xml = epub_zip.read("META-INF/container.xml")
        except KeyError:
            return missing

        container_soup = BeautifulSoup(container_xml, "xml")
        rootfile = container_soup.find("rootfile")
        opf_path = norm_href(rootfile.get("full-path", "") if rootfile else "")
        if not opf_path:
            return missing

        try:
            opf_xml = epub_zip.read(opf_path)
        except KeyError:
            return [opf_path]

        opf_dir = posixpath.dirname(opf_path)
        opf_soup = BeautifulSoup(opf_xml, "xml")
        for item in opf_soup.find_all("item"):
            href = item.get("href", "")
            if not href:
                continue
            resolved_href = norm_href(posixpath.join(opf_dir, href))
            if resolved_href and resolved_href not in archive_names:
                missing.append(resolved_href)

    return missing


def read_epub_with_missing_placeholders(path: str, missing_items: List[str]) -> epub.EpubBook:
    """Load malformed EPUBs by adding temporary placeholder files to a copied archive."""
    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        tmp_path = tmp.name

    try:
        with zipfile.ZipFile(path, "r") as source_zip, zipfile.ZipFile(
            tmp_path, "w", compression=zipfile.ZIP_DEFLATED
        ) as repaired_zip:
            for item in source_zip.infolist():
                repaired_zip.writestr(item, source_zip.read(item.filename))

            existing_names = _zip_normalized_names(source_zip)
            for missing_href in missing_items:
                href = norm_href(missing_href)
                if href and href not in existing_names:
                    repaired_zip.writestr(href, b"")

        book = epub.read_epub(tmp_path, options=_epub_read_options())
        book._missing_archive_items = {norm_href(item) for item in missing_items}
        return book
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def safe_read_epub(path: str):
    """Attempt to load EPUB while tolerating bad nav/toc and missing manifest files."""
    try:
        return epub.read_epub(path, options=_epub_read_options())
    except Exception as first_exc:
        try:
            return epub.read_epub(path)
        except Exception:
            missing_items = find_missing_manifest_items(path)
            if not missing_items:
                raise first_exc
            return read_epub_with_missing_placeholders(path, missing_items)


# ----------------------------
# Helpers
# ----------------------------
def norm_href(href: str) -> str:
    """Normalize an EPUB internal href without breaking ../ traversal."""
    if href is None:
        return ""
    href = href.strip()
    href = urllib.parse.unquote(href)
    href = href.replace("\\", "/")
    href = href.split("#", 1)[0].split("?", 1)[0]
    href = posixpath.normpath(href)
    if href == ".":
        href = ""
    while href.startswith("./"):
        href = href[2:]
    href = href.lstrip("/")
    return href


def resolve_img_href(doc_href: str, img_src: str) -> str:
    doc_dir = posixpath.dirname(norm_href(doc_href))
    src = norm_href(img_src)
    if not src:
        return ""
    if doc_dir:
        return norm_href(posixpath.join(doc_dir, src))
    return src


def build_manifest_image_map(book: epub.EpubBook) -> Dict[str, epub.EpubItem]:
    manifest: Dict[str, epub.EpubItem] = {}
    for item in book.get_items():
        media_type = getattr(item, "media_type", "") or ""
        if media_type.startswith("image/"):
            manifest[norm_href(getattr(item, "file_name", "") or "")] = item
    return manifest


def safe_render_image(image_bytes: bytes):
    image = Image.open(io.BytesIO(image_bytes))
    image.verify()
    image = Image.open(io.BytesIO(image_bytes))
    return image


def image_bytes_hash(image_bytes: bytes) -> str:
    return hashlib.sha256(image_bytes).hexdigest()


def normalize_alt_text(text: str, max_words: int = 30) -> str:
    cleaned = " ".join((text or "").strip().split())
    if not cleaned:
        return ""
    words = cleaned.split()
    if len(words) <= max_words:
        return cleaned
    return " ".join(words[:max_words])




PLACEHOLDER_ALTS = {
    "",
    "illustration",
    "illustrated",
    "image",
    "img",
    "figure",
    "photo",
    "picture",
    "graphic",
}

def is_placeholder_alt(text: str) -> bool:
    return normalize_alt_text(text).lower() in PLACEHOLDER_ALTS


def generate_alt_text_suggestion(image_bytes: bytes, image_path: str = "") -> str:
    if client is None:
        raise RuntimeError("OPENAI_API_KEY is not set.")

    mime_type = mimetypes.guess_type(image_path)[0] or "image/jpeg"
    image_b64 = base64.b64encode(image_bytes).decode("utf-8")
    data_url = f"data:{mime_type};base64,{image_b64}"

    prompt = (
        "You are writing alt text for an EPUB accessibility workflow. "
        "Write concise, useful alt text for this image. "
        "Do not start with 'image of' or 'picture of'. "
        "If there is visible text that matters, include it. "
        "If the image is purely decorative, return exactly: decorative. "
        "Keep the alt text to 30 words or fewer."
    )

    response = client.responses.create(
        model="gpt-4.1-mini",
        input=[
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt},
                    {"type": "input_image", "image_url": data_url},
                ],
            }
        ],
    )

    return normalize_alt_text(response.output_text or "")


def generate_alt_text_with_cache(image_bytes: bytes, image_path: str = "") -> Tuple[str, bool]:
    img_hash = image_bytes_hash(image_bytes)
    cached_value = st.session_state.ai_alt_cache.get(img_hash)
    if cached_value is not None:
        return cached_value, True

    suggestion = generate_alt_text_suggestion(image_bytes, image_path)
    st.session_state.ai_alt_cache[img_hash] = suggestion
    return suggestion, False


def extract_image_entries(
    book: epub.EpubBook,
) -> Tuple[List[Dict[str, Any]], Dict[str, epub.EpubItem]]:
    doc_items = list(book.get_items_of_type(ITEM_DOCUMENT))
    manifest_images = build_manifest_image_map(book)

    entries: List[Dict[str, Any]] = []
    for doc in doc_items:
        doc_href = getattr(doc, "file_name", "") or ""

        try:
            raw = doc.get_content()
            html = raw.decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            continue

        for img in soup.find_all("img"):
            src = img.get("src", "")
            if not src:
                continue

            resolved = resolve_img_href(doc_href, src)
            alt = (img.get("alt") or "").strip()
            occ_idx = len(
                [
                    e
                    for e in entries
                    if e.get("doc_href") == norm_href(doc_href)
                    and e.get("src") == norm_href(src)
                ]
            )
            key = f"{norm_href(doc_href)}|{norm_href(src)}|{occ_idx}"

            entries.append(
                {
                    "key": key,
                    "doc_href": norm_href(doc_href),
                    "src": norm_href(src),
                    "resolved_href": resolved,
                    "existing_alt": alt,
                }
            )

    return entries, manifest_images




PREFLIGHT_SCOPE_MESSAGE = (
    "This tool currently updates only image alt text. It does not repair WCAG issues, "
    "malformed XHTML, reading order, table markup, headings, links, metadata, or EPUB "
    "navigation. These can be added later as a separate remediation module."
)


def _new_issue(severity: str, check: str, detail: str = "", count: int = 0) -> Dict[str, Any]:
    return {
        "severity": severity,
        "check": check,
        "detail": detail,
        "count": count,
    }


def _spine_idref(entry: Any) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, tuple) and entry:
        first = entry[0]
        if hasattr(first, "id") and getattr(first, "id", None):
            return str(first.id).strip()
        return str(first or "").strip()
    if hasattr(entry, "id") and getattr(entry, "id", None):
        return str(entry.id).strip()
    return ""


def _item_properties(item: Any) -> Set[str]:
    props = getattr(item, "properties", None) or []
    if isinstance(props, str):
        return set(props.split())
    try:
        return {str(prop) for prop in props}
    except TypeError:
        return set()


def analyze_epub_preflight(book: epub.EpubBook) -> Dict[str, Any]:
    doc_items = list(book.get_items_of_type(ITEM_DOCUMENT))
    manifest_images = build_manifest_image_map(book)
    entries, _ = extract_image_entries(book)
    referenced_images = {entry["resolved_href"] for entry in entries if entry.get("resolved_href")}
    missing_archive_items = set(getattr(book, "_missing_archive_items", set()) or set())
    missing_archive_image_items = {
        href for href in missing_archive_items if href.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"))
    }
    manifest_ids = {
        str(getattr(item, "id", "") or "")
        for item in book.get_items()
        if getattr(item, "id", None)
    }

    alt_counts = {
        "total_image_files_in_manifest": len(manifest_images),
        "total_image_references_in_xhtml": len(entries),
        "images_missing_alt_attribute": 0,
        "images_with_empty_alt": 0,
        "images_with_placeholder_alt": 0,
        "images_with_alt_text_over_30_words": 0,
        "duplicate_alt_text_count": 0,
        "image_references_missing_from_manifest": 0,
        "unreferenced_image_files": 0,
    }
    structure_counts = {
        "xhtml_document_count": len(doc_items),
        "opf_spine_item_count": len(getattr(book, "spine", []) or []),
        "missing_or_malformed_spine_entries": 0,
        "missing_nav_toc": 0,
        "duplicate_ids_in_xhtml": 0,
        "documents_missing_lang_or_xml_lang": 0,
        "heading_order_jumps": 0,
        "tables_without_th_or_header_associations": 0,
        "empty_links_or_anchors": 0,
        "pagebreak_markers_count": 0,
        "missing_referenced_resources_from_image_src_attributes": 0,
    }
    issues: List[Dict[str, Any]] = []
    alt_values: List[str] = []
    missing_manifest_hrefs: Set[str] = set()
    missing_lang_docs: List[str] = []
    duplicate_id_details: List[str] = []
    heading_jump_details: List[str] = []
    table_details: List[str] = []
    empty_link_details: List[str] = []

    for doc in doc_items:
        doc_href = norm_href(getattr(doc, "file_name", "") or "")
        try:
            doc_html = doc.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(doc_html, "html.parser")
        except Exception as exc:
            issues.append(_new_issue("Needs Review", "Could not parse XHTML document", f"{doc_href}: {exc}", 1))
            continue

        html_tag = soup.find("html")
        if not html_tag or not (html_tag.get("lang") or html_tag.get("xml:lang")):
            structure_counts["documents_missing_lang_or_xml_lang"] += 1
            missing_lang_docs.append(doc_href)

        ids = [str(tag.get("id")) for tag in soup.find_all(attrs={"id": True})]
        for id_value, count in Counter(ids).items():
            if count > 1:
                extras = count - 1
                structure_counts["duplicate_ids_in_xhtml"] += extras
                duplicate_id_details.append(f"{doc_href}#{id_value} appears {count} times")

        last_heading_level = 0
        for heading in soup.find_all(re.compile(r"^h[1-6]$")):
            level = int(heading.name[1])
            if last_heading_level and level > last_heading_level + 1:
                structure_counts["heading_order_jumps"] += 1
                heading_jump_details.append(f"{doc_href}: h{last_heading_level} to h{level}")
            last_heading_level = level

        for table_index, table in enumerate(soup.find_all("table"), start=1):
            has_th = bool(table.find("th"))
            has_association = bool(table.find(attrs={"headers": True}) or table.find(attrs={"scope": True}))
            if not has_th and not has_association:
                structure_counts["tables_without_th_or_header_associations"] += 1
                table_details.append(f"{doc_href}: table {table_index}")

        for link in soup.find_all("a"):
            has_target = bool((link.get("href") or "").strip() or (link.get("id") or "").strip() or (link.get("name") or "").strip())
            has_text = bool(link.get_text(" ", strip=True))
            if not has_target or not has_text:
                structure_counts["empty_links_or_anchors"] += 1
                empty_link_details.append(doc_href)

        structure_counts["pagebreak_markers_count"] += len(
            soup.find_all(attrs={"epub:type": lambda value: value and "pagebreak" in str(value).split()})
        )
        structure_counts["pagebreak_markers_count"] += len(
            soup.find_all(attrs={"role": lambda value: str(value).lower() == "doc-pagebreak"})
        )

        for img in soup.find_all("img"):
            src = img.get("src", "")
            resolved = resolve_img_href(doc_href, src)
            alt_attr = img.get("alt")
            alt_text = (alt_attr or "").strip()

            if resolved and (resolved not in manifest_images or resolved in missing_archive_items):
                missing_manifest_hrefs.add(resolved)

            if not img.has_attr("alt"):
                alt_counts["images_missing_alt_attribute"] += 1
            elif alt_text == "":
                alt_counts["images_with_empty_alt"] += 1
            elif is_placeholder_alt(alt_text):
                alt_counts["images_with_placeholder_alt"] += 1

            if len(alt_text.split()) > 30:
                alt_counts["images_with_alt_text_over_30_words"] += 1

            normalized = " ".join(alt_text.lower().split())
            if normalized:
                alt_values.append(normalized)

    duplicate_alt_counts = Counter(alt_values)
    alt_counts["duplicate_alt_text_count"] = sum(
        count - 1 for count in duplicate_alt_counts.values() if count > 1
    )
    alt_counts["image_references_missing_from_manifest"] = len(missing_manifest_hrefs)
    alt_counts["unreferenced_image_files"] = len((set(manifest_images) - missing_archive_image_items) - referenced_images)
    structure_counts["missing_referenced_resources_from_image_src_attributes"] = len(missing_manifest_hrefs)

    malformed_spine_details: List[str] = []
    for entry in getattr(book, "spine", []) or []:
        idref = _spine_idref(entry)
        if not idref or idref not in manifest_ids:
            malformed_spine_details.append(str(entry))
    structure_counts["missing_or_malformed_spine_entries"] = len(malformed_spine_details)

    has_nav = any(
        "nav" in _item_properties(item)
        or norm_href(getattr(item, "file_name", "") or "").endswith("nav.xhtml")
        for item in doc_items
    )
    has_ncx = any((getattr(item, "media_type", "") or "") == "application/x-dtbncx+xml" for item in book.get_items())
    has_toc = bool(getattr(book, "toc", None))
    if not (has_nav or has_ncx or has_toc):
        structure_counts["missing_nav_toc"] = 1

    unreferenced_missing_images = missing_archive_image_items - referenced_images
    if missing_manifest_hrefs:
        issues.append(_new_issue("Critical", "Missing image files from manifest", ", ".join(sorted(missing_manifest_hrefs)[:10]), len(missing_manifest_hrefs)))
    if unreferenced_missing_images:
        issues.append(_new_issue("Critical", "Missing unreferenced image files", ", ".join(sorted(unreferenced_missing_images)[:10]), len(unreferenced_missing_images)))
    if alt_counts["images_missing_alt_attribute"]:
        issues.append(_new_issue("Needs Review", "Images missing alt attribute", "", alt_counts["images_missing_alt_attribute"]))
    if alt_counts["images_with_empty_alt"]:
        issues.append(_new_issue("Needs Review", "Images with empty alt", "", alt_counts["images_with_empty_alt"]))
    if alt_counts["images_with_placeholder_alt"]:
        issues.append(_new_issue("Needs Review", "Images with placeholder alt", "", alt_counts["images_with_placeholder_alt"]))
    if alt_counts["duplicate_alt_text_count"]:
        issues.append(_new_issue("Needs Review", "Duplicate alt text", "", alt_counts["duplicate_alt_text_count"]))
    if structure_counts["duplicate_ids_in_xhtml"]:
        issues.append(_new_issue("Needs Review", "Duplicate IDs in XHTML", "; ".join(duplicate_id_details[:10]), structure_counts["duplicate_ids_in_xhtml"]))
    if structure_counts["documents_missing_lang_or_xml_lang"]:
        issues.append(_new_issue("Needs Review", "Documents missing lang or xml:lang", ", ".join(missing_lang_docs[:10]), structure_counts["documents_missing_lang_or_xml_lang"]))
    if structure_counts["tables_without_th_or_header_associations"]:
        issues.append(_new_issue("Needs Review", "Tables without th or header associations", ", ".join(table_details[:10]), structure_counts["tables_without_th_or_header_associations"]))
    if structure_counts["empty_links_or_anchors"]:
        issues.append(_new_issue("Needs Review", "Empty links or anchors", ", ".join(sorted(set(empty_link_details))[:10]), structure_counts["empty_links_or_anchors"]))
    if alt_counts["images_with_alt_text_over_30_words"]:
        issues.append(_new_issue("Warning", "Alt text over 30 words", "", alt_counts["images_with_alt_text_over_30_words"]))
    if structure_counts["heading_order_jumps"]:
        issues.append(_new_issue("Warning", "Heading order jumps", "; ".join(heading_jump_details[:10]), structure_counts["heading_order_jumps"]))
    if alt_counts["unreferenced_image_files"]:
        issues.append(_new_issue("Warning", "Unreferenced image files", "", alt_counts["unreferenced_image_files"]))
    if structure_counts["missing_or_malformed_spine_entries"]:
        issues.append(_new_issue("Warning", "Missing or malformed spine entries", "; ".join(malformed_spine_details[:10]), structure_counts["missing_or_malformed_spine_entries"]))
    if structure_counts["missing_nav_toc"]:
        issues.append(_new_issue("Warning", "Missing nav/toc", "", 1))

    if any(issue["severity"] == "Critical" for issue in issues):
        summary = "Do not process until fixed"
    elif any(issue["severity"] == "Needs Review" for issue in issues):
        summary = "Review recommended"
    else:
        summary = "Low-risk file"

    return {
        "summary": summary,
        "alt_text_readiness": alt_counts,
        "epub_structure_health": structure_counts,
        "issues": issues,
    }


def display_count_rows(rows: List[Tuple[str, int]]) -> None:
    for label, value in rows:
        st.write(f"**{label}:** {value}")


def preflight_export_rows(report: Dict[str, Any]) -> List[Dict[str, Any]]:
    alt_counts = report.get("alt_text_readiness", {})
    structure_counts = report.get("epub_structure_health", {})
    issues = report.get("issues", [])
    issue_lookup = {issue.get("check", ""): issue for issue in issues}
    issue_aliases = {
        "Image references missing from manifest": "Missing image files from manifest",
        "Missing referenced resources from image src attributes": "Missing image files from manifest",
        "Duplicate alt text count": "Duplicate alt text",
    }

    checks = [
        ("Alt Text Readiness", "Total image files in manifest", "total_image_files_in_manifest", "Pass"),
        ("Alt Text Readiness", "Total image references in XHTML", "total_image_references_in_xhtml", "Pass"),
        ("Alt Text Readiness", "Images missing alt attribute", "images_missing_alt_attribute", "Needs Review"),
        ("Alt Text Readiness", "Images with empty alt", "images_with_empty_alt", "Needs Review"),
        ("Alt Text Readiness", "Images with placeholder alt", "images_with_placeholder_alt", "Needs Review"),
        ("Alt Text Readiness", "Images with alt text over 30 words", "images_with_alt_text_over_30_words", "Warning"),
        ("Alt Text Readiness", "Duplicate alt text count", "duplicate_alt_text_count", "Needs Review"),
        ("Alt Text Readiness", "Image references missing from manifest", "image_references_missing_from_manifest", "Critical"),
        ("Alt Text Readiness", "Unreferenced image files", "unreferenced_image_files", "Warning"),
        ("EPUB Structure Health", "XHTML/document count", "xhtml_document_count", "Pass"),
        ("EPUB Structure Health", "OPF/spine item count", "opf_spine_item_count", "Pass"),
        ("EPUB Structure Health", "Missing or malformed spine entries", "missing_or_malformed_spine_entries", "Warning"),
        ("EPUB Structure Health", "Missing nav/toc", "missing_nav_toc", "Warning"),
        ("EPUB Structure Health", "Duplicate IDs in XHTML", "duplicate_ids_in_xhtml", "Needs Review"),
        ("EPUB Structure Health", "Documents missing lang or xml:lang", "documents_missing_lang_or_xml_lang", "Needs Review"),
        ("EPUB Structure Health", "Heading order jumps", "heading_order_jumps", "Warning"),
        ("EPUB Structure Health", "Tables without th or header associations", "tables_without_th_or_header_associations", "Needs Review"),
        ("EPUB Structure Health", "Empty links or anchors", "empty_links_or_anchors", "Needs Review"),
        ("EPUB Structure Health", "Pagebreak markers count", "pagebreak_markers_count", "Pass"),
        ("EPUB Structure Health", "Missing referenced resources from image src attributes", "missing_referenced_resources_from_image_src_attributes", "Critical"),
    ]

    rows: List[Dict[str, Any]] = []
    for category, label, key, issue_severity in checks:
        count_source = alt_counts if category == "Alt Text Readiness" else structure_counts
        count = count_source.get(key, 0)
        matching_issue = issue_lookup.get(label) or issue_lookup.get(issue_aliases.get(label, ""))
        severity = matching_issue.get("severity", issue_severity) if matching_issue else issue_severity
        if not count:
            severity = "Pass"
        rows.append(
            {
                "category": category,
                "check": label,
                "severity": severity,
                "count": count,
                "detail": matching_issue.get("detail", "") if matching_issue else "",
            }
        )

    rows.append(
        {
            "category": "Scope & Remediation Boundary",
            "check": "Current tool scope",
            "severity": "Pass",
            "count": "",
            "detail": PREFLIGHT_SCOPE_MESSAGE,
        }
    )
    return rows


def preflight_report_to_csv(report: Dict[str, Any]) -> bytes:
    output = io.StringIO()
    fieldnames = ["category", "check", "severity", "count", "detail"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(preflight_export_rows(report))
    return output.getvalue().encode("utf-8-sig")


def preflight_report_to_html(report: Dict[str, Any], file_name: str = "") -> str:
    rows = preflight_export_rows(report)
    escaped_file_name = html.escape(file_name or "Uploaded EPUB")
    escaped_summary = html.escape(report.get("summary", "Low-risk file"))
    table_rows = "\n".join(
        "<tr>"
        f"<td>{html.escape(str(row['category']))}</td>"
        f"<td>{html.escape(str(row['check']))}</td>"
        f"<td>{html.escape(str(row['severity']))}</td>"
        f"<td>{html.escape(str(row['count']))}</td>"
        f"<td>{html.escape(str(row['detail']))}</td>"
        "</tr>"
        for row in rows
    )
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>EPUB Preflight Report</title>
  <style>
    body {{ font-family: Arial, sans-serif; margin: 2rem; color: #1f2933; }}
    h1 {{ margin-bottom: 0.25rem; }}
    .summary {{ font-size: 1.1rem; font-weight: 700; margin: 1rem 0; }}
    table {{ border-collapse: collapse; width: 100%; }}
    th, td {{ border: 1px solid #d9dee3; padding: 0.55rem; text-align: left; vertical-align: top; }}
    th {{ background: #f3f5f7; }}
    td:nth-child(3), td:nth-child(4) {{ white-space: nowrap; }}
    @media print {{ body {{ margin: 0.5in; }} }}
  </style>
</head>
<body>
  <h1>EPUB Preflight Report</h1>
  <p><strong>File:</strong> {escaped_file_name}</p>
  <p class="summary">Summary: {escaped_summary}</p>
  <table>
    <thead>
      <tr>
        <th>Category</th>
        <th>Check</th>
        <th>Severity</th>
        <th>Count</th>
        <th>Detail</th>
      </tr>
    </thead>
    <tbody>
      {table_rows}
    </tbody>
  </table>
</body>
</html>"""


def display_preflight_report(report: Dict[str, Any], file_name: str = "") -> None:
    if not report:
        return

    st.subheader("Preflight Report")
    summary = report.get("summary", "Low-risk file")
    if summary == "Do not process until fixed":
        st.error(summary)
    elif summary == "Review recommended":
        st.warning(summary)
    else:
        st.success(summary)

    alt_counts = report.get("alt_text_readiness", {})
    structure_counts = report.get("epub_structure_health", {})
    issues = report.get("issues", [])
    alt_tab, structure_tab, scope_tab = st.tabs(
        ["Alt Text Readiness", "EPUB Structure Health", "Scope & Remediation Boundary"]
    )

    with alt_tab:
        display_count_rows(
            [
                ("Total image files in manifest", alt_counts.get("total_image_files_in_manifest", 0)),
                ("Total image references in XHTML", alt_counts.get("total_image_references_in_xhtml", 0)),
                ("Images missing alt attribute", alt_counts.get("images_missing_alt_attribute", 0)),
                ("Images with empty alt", alt_counts.get("images_with_empty_alt", 0)),
                ("Images with placeholder alt", alt_counts.get("images_with_placeholder_alt", 0)),
                ("Images with alt text over 30 words", alt_counts.get("images_with_alt_text_over_30_words", 0)),
                ("Duplicate alt text count", alt_counts.get("duplicate_alt_text_count", 0)),
                ("Image references missing from manifest", alt_counts.get("image_references_missing_from_manifest", 0)),
                ("Unreferenced image files", alt_counts.get("unreferenced_image_files", 0)),
            ]
        )

    with structure_tab:
        display_count_rows(
            [
                ("XHTML/document count", structure_counts.get("xhtml_document_count", 0)),
                ("OPF/spine item count", structure_counts.get("opf_spine_item_count", 0)),
                ("Missing or malformed spine entries", structure_counts.get("missing_or_malformed_spine_entries", 0)),
                ("Missing nav/toc", structure_counts.get("missing_nav_toc", 0)),
                ("Duplicate IDs in XHTML", structure_counts.get("duplicate_ids_in_xhtml", 0)),
                ("Documents missing lang or xml:lang", structure_counts.get("documents_missing_lang_or_xml_lang", 0)),
                ("Heading order jumps", structure_counts.get("heading_order_jumps", 0)),
                ("Tables without th or header associations", structure_counts.get("tables_without_th_or_header_associations", 0)),
                ("Empty links or anchors", structure_counts.get("empty_links_or_anchors", 0)),
                ("Pagebreak markers count", structure_counts.get("pagebreak_markers_count", 0)),
                ("Missing referenced resources from image src attributes", structure_counts.get("missing_referenced_resources_from_image_src_attributes", 0)),
            ]
        )

    with scope_tab:
        st.write(PREFLIGHT_SCOPE_MESSAGE)

    with st.expander("Preflight issue details"):
        if not issues:
            st.write("Pass: no preflight issues found.")
        else:
            for issue in issues:
                detail = f" - {issue['detail']}" if issue.get("detail") else ""
                st.write(f"**{issue['severity']}**: {issue['check']} ({issue.get('count', 0)}){detail}")

    export_col1, export_col2 = st.columns(2)
    with export_col1:
        st.download_button(
            "Download Preflight CSV",
            data=preflight_report_to_csv(report),
            file_name="preflight-report.csv",
            mime="text/csv",
        )
    with export_col2:
        st.download_button(
            "Download Printable HTML",
            data=preflight_report_to_html(report, file_name),
            file_name="preflight-report.html",
            mime="text/html",
        )

def apply_updates_to_book(
    book: epub.EpubBook, updates: Dict[str, Dict[str, Any]]
) -> Tuple[int, int]:
    doc_items = list(book.get_items_of_type(ITEM_DOCUMENT))
    docs_modified = 0
    tags_modified = 0

    for doc in doc_items:
        doc_href = norm_href(getattr(doc, "file_name", "") or "")

        try:
            html = doc.get_content().decode("utf-8", errors="ignore")
            soup = BeautifulSoup(html, "html.parser")
        except Exception:
            continue

        changed_doc = False
        seen_counts: Dict[str, int] = {}

        for img in soup.find_all("img"):
            src = norm_href(img.get("src", "") or "")
            if not src:
                continue

            occ = seen_counts.get(src, 0)
            seen_counts[src] = occ + 1
            key = f"{doc_href}|{src}|{occ}"

            if key not in updates:
                continue

            alt = normalize_alt_text(updates[key].get("alt") or "")
            if img.get("alt") != alt:
                img["alt"] = alt
                changed_doc = True
                tags_modified += 1

        if changed_doc:
            doc.set_content(str(soup).encode("utf-8"))
            docs_modified += 1

    return docs_modified, tags_modified


def ensure_book_title(book: epub.EpubBook) -> bool:
    """Ensure the EPUB has a usable title before export."""
    try:
        titles = book.get_metadata("DC", "title")
    except Exception:
        titles = []

    has_title = False
    for item in titles or []:
        value = item[0] if isinstance(item, tuple) and item else item
        if isinstance(value, str) and value.strip():
            has_title = True
            break

    if not has_title:
        try:
            book.set_title("Untitled EPUB")
            return True
        except Exception:
            return False
    return False


def sanitize_toc_node(node: Any, counter: List[int]) -> Tuple[Any, bool]:
    """Recursively clean TOC titles so ebooklib can write NCX safely."""
    changed = False

    if isinstance(node, list):
        cleaned_items = []
        for child in node:
            cleaned_child, child_changed = sanitize_toc_node(child, counter)
            cleaned_items.append(cleaned_child)
            changed = changed or child_changed
        return cleaned_items, changed

    if isinstance(node, tuple):
        cleaned_items = []
        for child in node:
            cleaned_child, child_changed = sanitize_toc_node(child, counter)
            cleaned_items.append(cleaned_child)
            changed = changed or child_changed
        return tuple(cleaned_items), changed

    title = getattr(node, "title", None)
    if title is None or not isinstance(title, str) or not title.strip():
        fallback = f"Section {counter[0]}"
        counter[0] += 1
        try:
            setattr(node, "title", fallback)
            changed = True
        except Exception:
            pass

    return node, changed


def sanitize_book_for_write(book: epub.EpubBook) -> Tuple[epub.EpubBook, List[str]]:
    """Apply low-risk metadata and TOC fixes before saving."""
    notes: List[str] = []

    if ensure_book_title(book):
        notes.append("Missing book title was replaced with a fallback title.")

    try:
        original_toc = getattr(book, "toc", ()) or ()
        cleaned_toc, toc_changed = sanitize_toc_node(original_toc, [1])
        if toc_changed:
            book.toc = cleaned_toc
            notes.append("Saved with TOC cleanup applied.")
    except Exception:
        pass

    return book, notes


def sanitize_spine(book: epub.EpubBook) -> Tuple[epub.EpubBook, List[str]]:
    """Normalize spine entries into XML-safe strings so ebooklib can serialize them."""
    notes: List[str] = []
    original_spine = list(getattr(book, "spine", []) or [])
    clean_spine: List[Any] = []
    changed = False

    for entry in original_spine:
        if isinstance(entry, str):
            value = entry.strip()
            if value:
                clean_spine.append(value)
            else:
                changed = True
            continue

        if isinstance(entry, tuple):
            if not entry:
                changed = True
                continue

            first = entry[0]
            if hasattr(first, "id") and getattr(first, "id", None):
                idref = str(first.id)
            elif first is None:
                idref = ""
            else:
                idref = str(first).strip()

            if not idref:
                changed = True
                continue

            attrs: Dict[str, str] = {}
            if len(entry) > 1:
                second = entry[1]

                if isinstance(second, dict):
                    for k, v in second.items():
                        if k is None or v is None:
                            changed = True
                            continue
                        key = str(k).strip()
                        if not key:
                            changed = True
                            continue
                        if isinstance(v, bool):
                            attrs[key] = "yes" if v else "no"
                            changed = True
                        else:
                            attrs[key] = str(v)
                elif isinstance(second, str):
                    attrs["linear"] = "no" if second.strip().lower() == "no" else "yes"
                    changed = True
                elif isinstance(second, bool):
                    attrs["linear"] = "yes" if second else "no"
                    changed = True
                elif second is not None:
                    attrs["linear"] = str(second)
                    changed = True

            clean_spine.append((idref, attrs) if attrs else idref)
            if idref != first or attrs:
                changed = True
            continue

        if hasattr(entry, "id") and getattr(entry, "id", None):
            clean_spine.append(str(entry.id))
            changed = True
            continue

        changed = True

    if clean_spine:
        book.spine = clean_spine
        if changed:
            notes.append("Saved with spine sanitization applied.")

    return book, notes


def rebuild_spine_from_documents(book: epub.EpubBook) -> Tuple[epub.EpubBook, bool]:
    """Rebuild the spine from XHTML documents when publisher spine metadata is malformed."""
    doc_ids: List[str] = []

    for item in book.get_items():
        item_id = getattr(item, "id", None)
        media_type = getattr(item, "media_type", "") or ""
        if media_type == "application/xhtml+xml" and item_id:
            doc_ids.append(str(item_id))

    if not doc_ids:
        return book, False

    if "nav" in doc_ids:
        doc_ids = ["nav"] + [item_id for item_id in doc_ids if item_id != "nav"]

    book.spine = doc_ids
    return book, True


def write_book_with_fallbacks(book: epub.EpubBook) -> Tuple[io.BytesIO, List[str]]:
    """Write EPUB with defensive fallbacks for malformed TOC and spine metadata."""
    book, notes = sanitize_book_for_write(book)
    book, spine_notes = sanitize_spine(book)
    notes.extend(spine_notes)

    output = io.BytesIO()
    errors: List[str] = []

    try:
        epub.write_epub(output, book)
        output.seek(0)
        return output, notes
    except Exception as exc:
        errors.append(f"initial save failed: {exc}")

    try:
        book.toc = tuple()
        if "Saved with TOC cleanup applied." not in notes:
            notes.append("Saved with TOC cleanup applied.")
        notes.append("Publisher TOC metadata was too malformed to preserve fully, so the TOC was flattened for export.")

        output = io.BytesIO()
        epub.write_epub(output, book)
        output.seek(0)
        return output, notes
    except Exception as exc:
        errors.append(f"save after TOC flatten failed: {exc}")

    rebuilt, rebuilt_ok = rebuild_spine_from_documents(book)
    if rebuilt_ok:
        book = rebuilt
        book, more_spine_notes = sanitize_spine(book)
        for note in more_spine_notes:
            if note not in notes:
                notes.append(note)
        notes.append("Publisher spine metadata was rebuilt from document order for export.")

        try:
            output = io.BytesIO()
            epub.write_epub(output, book)
            output.seek(0)
            return output, notes
        except Exception as exc:
            errors.append(f"save after spine rebuild failed: {exc}")

    raise RuntimeError(" | ".join(errors))


# ----------------------------
# Session state setup
# ----------------------------
uploaded_file = st.file_uploader("Upload an EPUB file", type=["epub"])

if "entries" not in st.session_state:
    st.session_state.entries = []

if "updates" not in st.session_state:
    st.session_state.updates = {}

if "img_index" not in st.session_state:
    st.session_state.img_index = 0

if "book_bytes" not in st.session_state:
    st.session_state.book_bytes = None

if "manifest_images" not in st.session_state:
    st.session_state.manifest_images = {}

if "preflight_report" not in st.session_state:
    st.session_state.preflight_report = None

if "ai_status" not in st.session_state:
    st.session_state.ai_status = {}

if "ai_alt_cache" not in st.session_state:
    st.session_state.ai_alt_cache = {}


# ----------------------------
# Upload/reset helpers
# ----------------------------
def clear_widget_state_for_entries(entries: List[Dict[str, Any]]) -> None:
    for entry in entries:
        key = entry["key"]
        text_key = f"alt_text_{key}"
        pending_key = f"pending_ai_{key}"
        if text_key in st.session_state:
            del st.session_state[text_key]
        if pending_key in st.session_state:
            del st.session_state[pending_key]


def reset_for_new_upload(epub_bytes: bytes) -> None:
    old_entries = st.session_state.entries if "entries" in st.session_state else []
    clear_widget_state_for_entries(old_entries)

    st.session_state.book_bytes = epub_bytes
    st.session_state.img_index = 0
    st.session_state.updates = {}
    st.session_state.ai_status = {}

    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        tmp.write(epub_bytes)
        tmp_path = tmp.name

    book = safe_read_epub(tmp_path)
    entries, manifest = extract_image_entries(book)

    st.session_state.entries = entries
    st.session_state.manifest_images = manifest
    st.session_state.preflight_report = analyze_epub_preflight(book)

    for entry in entries:
        st.session_state.updates[entry["key"]] = {
            "alt": normalize_alt_text(entry.get("existing_alt", ""))
        }


def generate_missing_alt_text(entries, updates, manifest_images) -> Tuple[int, int, int]:
    generated = 0
    reused_cache = 0
    skipped = 0

    for entry in entries:
        key = entry["key"]
        current_alt = normalize_alt_text(updates.get(key, {}).get("alt") or "")
        existing_alt = normalize_alt_text(entry.get("existing_alt", "") or "")
        effective_alt = current_alt if current_alt else existing_alt

        if not is_placeholder_alt(effective_alt):
            skipped += 1
            continue

        img_item = manifest_images.get(entry["resolved_href"])
        if not img_item:
            skipped += 1
            continue

        image_bytes = img_item.get_content()
        suggestion, from_cache = generate_alt_text_with_cache(
            image_bytes, entry["resolved_href"]
        )
        suggestion = normalize_alt_text(suggestion)

        updates[key] = {"alt": suggestion}
        text_key = f"alt_text_{key}"
        pending_key = f"pending_ai_{key}"
        if text_key in st.session_state:
            st.session_state[pending_key] = suggestion
        else:
            updates[key] = {"alt": suggestion}

        st.session_state.ai_status[key] = "cached" if from_cache else "generated"

        if from_cache:
            reused_cache += 1
        else:
            generated += 1
            time.sleep(0.4)

    return generated, reused_cache, skipped


# ----------------------------
# App UI
# ----------------------------
if uploaded_file:
    epub_bytes = uploaded_file.getvalue()

    if st.session_state.book_bytes != epub_bytes:
        reset_for_new_upload(epub_bytes)

    entries = st.session_state.entries
    updates = st.session_state.updates
    manifest_images = st.session_state.manifest_images
    preflight_report = st.session_state.preflight_report

    display_preflight_report(preflight_report, uploaded_file.name)
    st.markdown("---")

    if not entries:
        st.error("No images found.")
        st.stop()

    total = len(entries)
    idx = st.session_state.img_index

    nav_col1, nav_col2 = st.columns(2)
    with nav_col1:
        if st.button("Prev") and idx > 0:
            st.session_state.img_index -= 1
            st.rerun()
    with nav_col2:
        if st.button("Next") and idx < total - 1:
            st.session_state.img_index += 1
            st.rerun()

    bulk_col1, bulk_col2 = st.columns(2)
    with bulk_col1:
        if st.button("✨ Generate missing / placeholder alt text"):
            try:
                with st.spinner("Generating alt text for missing / placeholder images..."):
                    generated, reused_cache, skipped = generate_missing_alt_text(
                        entries, updates, manifest_images
                    )
                st.success(
                    f"Done. New AI suggestions: {generated}. Reused from cache: {reused_cache}. Skipped: {skipped}."
                )
                st.rerun()
            except Exception as exc:
                st.error(f"Bulk alt text generation failed: {exc}")
    with bulk_col2:
        st.caption(
            f"AI cache entries in this session: {len(st.session_state.ai_alt_cache)}"
        )

    entry = entries[st.session_state.img_index]
    key = entry["key"]

    st.write(f"Image {st.session_state.img_index + 1} of {total}")
    st.write(f"Path: {entry['resolved_href']}")

    img_item = manifest_images.get(entry["resolved_href"])
    if img_item:
        try:
            img_bytes = img_item.get_content()
            pil_img = safe_render_image(img_bytes)
            st.image(pil_img, width=450)
        except UnidentifiedImageError:
            st.warning("Image could not render.")
            img_bytes = b""
    else:
        st.warning("Image file was not found in the EPUB manifest.")
        img_bytes = b""

    text_key = f"alt_text_{key}"
    pending_key = f"pending_ai_{key}"

    if text_key not in st.session_state:
        st.session_state[text_key] = normalize_alt_text(updates[key]["alt"])

    if pending_key in st.session_state:
        pending_value = normalize_alt_text(st.session_state[pending_key])
        st.session_state[text_key] = pending_value
        updates[key] = {"alt": pending_value}
        del st.session_state[pending_key]

    st.text_area("Alt Text", key=text_key, height=120)

    effective_alt = normalize_alt_text(st.session_state[text_key] or entry.get("existing_alt", "") or "")
    if is_placeholder_alt(effective_alt):
        st.caption("⚠️ Placeholder alt text detected. AI generation will replace it.")

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("✨ Generate alt text suggestion", key=f"gen_{key}"):
            if not img_item:
                st.warning("No image file was found for this entry.")
            else:
                try:
                    with st.spinner("Generating alt text suggestion..."):
                        suggestion, from_cache = generate_alt_text_with_cache(
                            img_item.get_content(), entry["resolved_href"]
                        )

                    if suggestion:
                        st.session_state[pending_key] = normalize_alt_text(suggestion)
                        st.session_state.ai_status[key] = (
                            "cached" if from_cache else "generated"
                        )
                        st.rerun()
                    else:
                        st.session_state.ai_status[key] = "empty"
                        st.warning("The model returned an empty suggestion.")
                except Exception as exc:
                    st.session_state.ai_status[key] = "error"
                    st.error(f"Alt text generation failed: {exc}")

    with btn_col2:
        if st.button("Clear", key=f"clear_{key}"):
            st.session_state[pending_key] = ""
            st.rerun()

    updates[key] = {"alt": normalize_alt_text(st.session_state[text_key])}

    status = st.session_state.ai_status.get(key)
    if status == "cached":
        st.caption("Suggestion loaded from cache.")
    elif status == "generated":
        st.caption("Suggestion generated by AI.")

    st.markdown("---")

    if st.button("💾 Save updated EPUB"):
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
                tmp.write(st.session_state.book_bytes)
                tmp_path = tmp.name

            book = safe_read_epub(tmp_path)
            docs_modified, tags_modified = apply_updates_to_book(book, updates)
            output, save_notes = write_book_with_fallbacks(book)

            success_msg = f"Saved. Docs modified: {docs_modified}, images updated: {tags_modified}"
            st.success(success_msg)
            for note in save_notes:
                st.info(note)

            st.download_button(
                "Download EPUB",
                data=output,
                file_name="updated.epub",
                mime="application/epub+zip",
            )
        except Exception as exc:
            st.error(f"Save failed: {exc}")
else:
    st.info("Upload an EPUB to begin.")

