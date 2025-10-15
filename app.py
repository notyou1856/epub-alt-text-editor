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
    epub_path = "temp.epub"
    with open(epub_path, "wb") as f:
        f.write(uploaded_file.read())

    book = epub.read_epub(epub_path)
    image_items = list(book.get_items_of_type(ITEM_IMAGE))
    st.subheader(f"Extracted {len(image_items)} images")

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

                # 🛡 Try to load image safely
                try:
                    if img_item.media_type in ["image/jpeg", "image/png", "image/gif"]:
                        img_bytes = img_item.get_content()
                        c.image(io.BytesIO(img_bytes), caption=f"{img_item.file_name}")
                    else:
                        c.warning(f"Unsupported format: {img_item.media_type}")
                except Exception as e:
                    c.warning(f"Preview not available: {img_item.file_name} — {str(e)}")

                key = f"alt_{idx}"
                alt_texts[img_item.file_name] = c.text_input(
                    f"Alt text for: {os.path.basename(img_item.file_name)}",
                    value="",
                    max_chars=150,
                    key=key,
                    help="Describe the image succinctly. This will be written to the <img alt='...'> attribute."
                )
                idx += 1

    # Save Updated EPUB
    if st.button("Save Updated EPUB"):
        def norm_epub_path(p: str) -> str:
            return p.replace("\\", "/")

        alt_by_path = {norm_epub_path(k): v.strip() for k, v in alt_texts.items() if v and v.strip()}
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
                    if src_norm in alt_by_path:
                        new_alt = alt_by_path[src_norm]
                    else:
                        new_alt = None
                        src_base = os.path.basename(src_norm)
                        for k, v in alt_by_path.items():
                            if os.path.basename(k) == src_base:
                                new_alt = v
                                break
                    if new_alt:
                        if img.get("alt") != new_alt:
                            img["alt"] = new_alt
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
            )

        # ✅ Embed preview (OUTSIDE the file context)
        st.markdown("### Preview EPUB (external viewer)")
        viewer_url = "https://futurepress.github.io/epub.js-reader/?epub=updated.epub"
        st.components.v1.iframe(viewer_url, height=600, scrolling=True)

            


