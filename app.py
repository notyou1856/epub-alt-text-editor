import streamlit as st
from ebooklib import epub, ITEM_IMAGE, ITEM_DOCUMENT
from PIL import Image
from bs4 import BeautifulSoup
import io
import os

st.set_page_config(page_title="EPUB Image Alt Text Editor", layout="wide")
st.title("EPUB Image Alt Text Editor")

uploaded_file = st.file_uploader("Upload an EPUB file", type=["epub"])

if uploaded_file:
    # Save uploaded file to disk
    epub_path = "temp.epub"
    with open(epub_path, "wb") as f:
        f.write(uploaded_file.read())

    # Open book
    book = epub.read_epub(epub_path)

    # Collect images
    image_items = list(book.get_items_of_type(ITEM_IMAGE))
    st.subheader(f"Extracted {len(image_items)} images")

    # Present images + inputs
    # Map image href (EPUB internal path) -> entered alt text
    alt_texts = {}

    if image_items:
        cols_per_row = 2
        rows = (len(image_items) + cols_per_row - 1) // cols_per_row
        idx = 0
        for _ in range(rows):
            row_cols = st.columns(cols_per_row)
            for c in row_cols:
                if idx >= len(image_items):
                    break
                img_item = image_items[idx]
                # Display image
                try:
                    img_bytes = img_item.get_content()
                    c.image(io.BytesIO(img_bytes), caption=f"{img_item.file_name}")
                except Exception:
                    c.warning(f"Preview not available: {img_item.file_name}")

                # Text input with unique key
                key = f"alt_{idx}"
                alt_texts[img_item.file_name] = c.text_input(
                    f"Alt text for: {os.path.basename(img_item.file_name)}",
                    value="",
                    max_chars=150,  # give yourself more room; you can enforce 30 later if you wish
                    key=key,
                    help="Describe the image succinctly. This will be written to the <img alt='...'> attribute."
                )
                idx += 1

    # Save Updated EPUB
    if st.button("Save Updated EPUB"):
        # Build a lookup for quick matching: exact EPUB internal paths
        # (EPUB img src in XHTML is usually a relative path like 'Images/pic.jpg')
        # We'll match by normalized relative path
        def norm_epub_path(p: str) -> str:
            return p.replace("\\", "/")

        alt_by_path = {norm_epub_path(k): v.strip() for k, v in alt_texts.items() if v and v.strip()}

        # Update all XHTML documents: set <img alt="...">
        doc_items = list(book.get_items_of_type(ITEM_DOCUMENT))
        updated_count = 0
        img_tags_touched = 0

        for doc in doc_items:
            try:
                html = doc.get_content().decode("utf-8", errors="ignore")
                soup = BeautifulSoup(html, "html.parser")
                changed = False
                for img in soup.find_all("img"):
                    src = img.get("src", "")
                    if not src:
                        continue
                    src_norm = norm_epub_path(src)
                    # Try exact match; if not found, attempt basename fallback
                    if src_norm in alt_by_path:
                        new_alt = alt_by_path[src_norm]
                    else:
                        # Basename fallback (useful when paths vary but filenames are unique)
                        new_alt = None
                        src_base = os.path.basename(src_norm)
                        for k, v in alt_by_path.items():
                            if os.path.basename(k) == src_base:
                                new_alt = v
                                break
                    if new_alt:
                        if img.get("alt") != new_alt:
                            img["alt"] = new_alt
                            # (Optional) also mirror to title for better UX in some readers
                            img["title"] = new_alt
                            changed = True
                            img_tags_touched += 1
                if changed:
                    doc.set_content(str(soup).encode("utf-8"))
                    updated_count += 1
            except Exception as e:
                st.warning(f"Could not update {doc.file_name}: {e}")

        updated_epub_path = "updated.epub"
        epub.write_epub(updated_epub_path, book)
        st.success(f"EPUB updated: {updated_count} XHTML file(s) modified, {img_tags_touched} <img> tag(s) updated.")
        with open(updated_epub_path, "rb") as f:
            st.download_button(
                "Download Updated EPUB",
                data=f,
                file_name="updated.epub",
                mime="application/epub+zip"
            #embed preview (uses a 3rd party epub view via iframe)
            st.markdown("### Preview EPUB (external viewer)")
            viewer_url = "https://futurepress.github.io/epub.js-reader/?epub=updated.epub"
            st.components.v1.iframe(viewer_url, height=600, scrolling = TRUE)
            
            )

