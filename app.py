import streamlit as st
from ebooklib import epub
from bs4 import BeautifulSoup
from PIL import Image
import fitz  # pymupdf
import io
import os
import tempfile

# Title
st.title("EPUB and PDF Image Alt Text Editor")

# Upload File
uploaded_file = st.file_uploader("Upload an EPUB or PDF file", type=["epub", "pdf"])

if uploaded_file:
    file_type = uploaded_file.name.split(".")[-1].lower()
    alt_texts = {}

    if file_type == "epub":
        # Save uploaded EPUB to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp_file:
            tmp_file.write(uploaded_file.read())
            temp_epub_path = tmp_file.name

        book = epub.read_epub(temp_epub_path)
        image_files = [item for item in book.items if isinstance(item, epub.EpubImage)]

        st.subheader(f"Extracted {len(image_files)} images from EPUB")
        for idx, img in enumerate(image_files):
            st.image(io.BytesIO(img.content), caption=f"Image {idx+1}")
            alt_text = st.text_input(f"Enter alt text for Image {idx+1}", max_chars=30, key=f"alt-{idx}")
            alt_texts[img.file_name] = alt_text

        if st.button("Save Updated EPUB"):
            for item in book.items:
                if item.media_type == 'application/xhtml+xml':
                    soup = BeautifulSoup(item.content.decode("utf-8"), "html.parser")
                    for img_tag in soup.find_all("img"):
                        src = img_tag.get("src")
                        if src in alt_texts and alt_texts[src]:
                            img_tag["alt"] = alt_texts[src]
                    item.content = str(soup).encode("utf-8")

            updated_epub_path = "updated.epub"
            epub.write_epub(updated_epub_path, book)
            st.success("EPUB updated with alt text successfully!")
            with open(updated_epub_path, "rb") as f:
                st.download_button("Download Updated EPUB", f, file_name="updated.epub")

            st.markdown("[Click here to preview EPUB](https://futurepress.github.io/epub.js-reader/?epub=updated.epub)")
            st.info("Note: To preview, you may need to manually upload the EPUB to an online reader if direct previewing doesn't work on Streamlit.")

    elif file_type == "pdf":
        # Save uploaded PDF to a temporary file
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
            tmp_file.write(uploaded_file.read())
            temp_pdf_path = tmp_file.name

        doc = fitz.open(temp_pdf_path)
        image_counter = 0

        for page_index in range(len(doc)):
            page = doc[page_index]
            image_list = page.get_images(full=True)
            for img_index, img in enumerate(image_list):
                xref = img[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                st.image(io.BytesIO(image_bytes), caption=f"Page {page_index + 1}, Image {img_index + 1}")
                alt_text = st.text_input(f"Enter alt text for Page {page_index + 1}, Image {img_index + 1}", max_chars=30, key=f"pdf-{page_index}-{img_index}")
                alt_texts[f"page{page_index + 1}_img{img_index + 1}"] = alt_text
                image_counter += 1

        st.subheader(f"Extracted {image_counter} images from PDF")
        st.info("Note: PDF alt text saving is not yet supported.")
