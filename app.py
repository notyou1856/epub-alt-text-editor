
import streamlit as st
from ebooklib import epub
from ebooklib import ITEM_DOCUMENT
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError

import io
import os
import posixpath
import tempfile
import urllib.parse
from typing import Dict, List, Tuple, Any


# ----------------------------
# Page config
# ----------------------------
st.set_page_config(page_title="EPUB Alt Text Editor (MVP)", layout="wide")
st.title("📘 EPUB Alt Text Editor (Single-Image Paging MVP)")


# ----------------------------
# SAFE EPUB LOADER
# ----------------------------
def safe_read_epub(path):
    """Attempt to load EPUB ignoring bad TOC files (common publisher issue)."""
    try:
        return epub.read_epub(
            path,
            options={
                "ignore_ncx": True,
                "ignore_nav": True
            }
        )
    except Exception:
        return epub.read_epub(path)


# ----------------------------
# Helpers
# ----------------------------
def norm_href(href: str) -> str:
    if href is None:
        return ""
    href = href.strip()
    href = urllib.parse.unquote(href)
    href = href.replace("\\", "/")
    href = href.split("#", 1)[0].split("?", 1)[0]
    href = posixpath.normpath(href)
    if href == ".":
        href = ""
    return href.lstrip("./")


def resolve_img_href(doc_href: str, img_src: str) -> str:
    doc_dir = posixpath.dirname(norm_href(doc_href))
    src = norm_href(img_src)
    if not src:
        return ""
    if doc_dir:
        return norm_href(posixpath.join(doc_dir, src))
    return src


def build_manifest_image_map(book: epub.EpubBook) -> Dict[str, epub.EpubItem]:
    m: Dict[str, epub.EpubItem] = {}
    for item in book.get_items():
        mt = getattr(item, "media_type", "") or ""
        if mt.startswith("image/"):
            m[norm_href(getattr(item, "file_name", "") or "")] = item
    return m


def safe_render_image(image_bytes: bytes):
    im = Image.open(io.BytesIO(image_bytes))
    im.verify()
    im = Image.open(io.BytesIO(image_bytes))
    return im


def extract_image_entries(book: epub.EpubBook) -> Tuple[List[Dict[str, Any]], Dict[str, epub.EpubItem]]:
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

            occ_idx = len([e for e in entries if e.get("doc_href") == norm_href(doc_href) and e.get("src") == norm_href(src)])
            key = f"{norm_href(doc_href)}|{norm_href(src)}|{occ_idx}"

            entries.append({
                "key": key,
                "doc_href": norm_href(doc_href),
                "src": norm_href(src),
                "resolved_href": resolved,
                "existing_alt": alt,
            })

    return entries, manifest_images


def apply_updates_to_book(book: epub.EpubBook, updates: Dict[str, Dict[str, Any]]) -> Tuple[int, int]:
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

            alt = (updates[key].get("alt") or "").strip()

            if img.get("alt") != alt:
                img["alt"] = alt
                changed_doc = True
                tags_modified += 1

        if changed_doc:
            doc.set_content(str(soup).encode("utf-8"))
            docs_modified += 1

    return docs_modified, tags_modified


uploaded_file = st.file_uploader("Upload an EPUB file", type=["epub"])

if "entries" not in st.session_state:
    st.session_state.entries = []

if "updates" not in st.session_state:
    st.session_state.updates = {}

if "img_index" not in st.session_state:
    st.session_state.img_index = 0

if "book_bytes" not in st.session_state:
    st.session_state.book_bytes = None


def reset_for_new_upload(epub_bytes):

    st.session_state.book_bytes = epub_bytes
    st.session_state.img_index = 0
    st.session_state.updates = {}

    with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
        tmp.write(epub_bytes)
        tmp_path = tmp.name

    book = safe_read_epub(tmp_path)

    entries, manifest = extract_image_entries(book)

    st.session_state.entries = entries
    st.session_state.manifest_images = manifest

    for e in entries:
        st.session_state.updates[e["key"]] = {
            "alt": e.get("existing_alt", "")
        }


if uploaded_file:

    epub_bytes = uploaded_file.getvalue()

    if st.session_state.book_bytes != epub_bytes:
        reset_for_new_upload(epub_bytes)

    entries = st.session_state.entries
    updates = st.session_state.updates
    manifest_images = st.session_state.manifest_images

    if not entries:
        st.error("No images found.")
        st.stop()

    total = len(entries)
    idx = st.session_state.img_index

    col1, col2 = st.columns(2)

    with col1:
        if st.button("Prev") and idx > 0:
            st.session_state.img_index -= 1

    with col2:
        if st.button("Next") and idx < total - 1:
            st.session_state.img_index += 1

    entry = entries[st.session_state.img_index]
    key = entry["key"]

    st.write(f"Image {st.session_state.img_index+1} of {total}")
    st.write(f"Path: {entry['resolved_href']}")

    img_item = manifest_images.get(entry["resolved_href"])

    if img_item:
        try:
            img_bytes = img_item.get_content()
            pil_img = safe_render_image(img_bytes)
            st.image(pil_img, width=450)
        except UnidentifiedImageError:
            st.warning("Image could not render.")

    alt = st.text_area("Alt Text", value=updates[key]["alt"], height=120)

    updates[key] = {"alt": alt}

    st.markdown("---")

    if st.button("💾 Save updated EPUB"):

        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp:
            tmp.write(st.session_state.book_bytes)
            tmp_path = tmp.name

        book = safe_read_epub(tmp_path)

        docs_modified, tags_modified = apply_updates_to_book(book, updates)

        output = io.BytesIO()
        epub.write_epub(output, book)
        output.seek(0)

        st.success(f"Saved. Docs modified: {docs_modified}, images updated: {tags_modified}")

        st.download_button(
            "Download EPUB",
            data=output,
            file_name="updated.epub",
            mime="application/epub+zip"
        )

else:
    st.info("Upload an EPUB to begin.")
