import base64
import io
import mimetypes
import os
import posixpath
import tempfile
import urllib.parse
from typing import Any, Dict, List, Tuple

import streamlit as st
from bs4 import BeautifulSoup
from ebooklib import ITEM_DOCUMENT, epub
from openai import OpenAI
from PIL import Image, UnidentifiedImageError


# ----------------------------
# Page config
# ----------------------------
st.set_page_config(page_title="EPUB Alt Text Editor (MVP)", layout="wide")
st.title("📘 IDEA EPUB Alt Text Editor (Single-Image Paging)")


# ----------------------------
# OpenAI client
# ----------------------------
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    st.error("OPENAI_API_KEY is not set.")
    st.stop()

client = OpenAI(api_key=api_key)


# ----------------------------
# SAFE EPUB LOADER
# ----------------------------
def safe_read_epub(path: str):
    """Attempt to load EPUB while tolerating bad nav/toc files."""
    try:
        return epub.read_epub(
            path,
            options={
                "ignore_ncx": True,
                "ignore_nav": True,
            },
        )
    except Exception:
        return epub.read_epub(path)


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


def generate_alt_text_suggestion(image_bytes: bytes, image_path: str = "") -> str:
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
        model="gpt-5.4",
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

    return (response.output_text or "").strip()


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

            alt = (updates[key].get("alt") or "").strip()
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

if "ai_status" not in st.session_state:
    st.session_state.ai_status = {}


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

    for entry in entries:
        st.session_state.updates[entry["key"]] = {
            "alt": entry.get("existing_alt", "")
        }


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
    else:
        st.warning("Image file was not found in the EPUB manifest.")

    text_key = f"alt_text_{key}"
    pending_key = f"pending_ai_{key}"

    if text_key not in st.session_state:
        st.session_state[text_key] = updates[key]["alt"]

    if pending_key in st.session_state:
        st.session_state[text_key] = st.session_state[pending_key]
        updates[key] = {"alt": st.session_state[pending_key]}
        del st.session_state[pending_key]

    st.text_area("Alt Text", key=text_key, height=120)

    btn_col1, btn_col2 = st.columns(2)
    with btn_col1:
        if st.button("✨ Generate alt text suggestion", key=f"gen_{key}"):
            if not img_item:
                st.warning("No image file was found for this entry.")
            else:
                try:
                    with st.spinner("Generating alt text suggestion..."):
                        suggestion = generate_alt_text_suggestion(
                            img_item.get_content(), entry["resolved_href"]
                        )

                    if suggestion:
                        st.session_state[pending_key] = suggestion
                        st.session_state.ai_status[key] = "generated"
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

    updates[key] = {"alt": st.session_state[text_key]}

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
