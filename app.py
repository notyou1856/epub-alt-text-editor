import streamlit as st
from ebooklib import epub
from bs4 import BeautifulSoup
from PIL import Image, UnidentifiedImageError
import fitz  # pymupdf
import io
import os
import zipfile
import tempfile
import html

# Title
st.title("EPUB and PDF Image Alt Text Editor")

# EPUB validator

def run_epub_validation(epub_path):
    st.subheader("EPUB Validation Results")
    epub_passed = True
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            namelist = z.namelist()
            if 'META-INF/container.xml' not in namelist:
                epub_passed = False
                st.error("Missing container.xml file. Not a valid EPUB.")
            if not any(name.endswith('.xhtml') or name.endswith('.html') for name in namelist):
                st.warning("EPUB may be missing readable XHTML/HTML content.")
            else:
                st.success("Basic EPUB structure looks valid.")
                st.write(f"Contains {len(namelist)} files.")
        if epub_passed:
            st.success("EPUB Validation Passed ✅")
        else:
            st.error("EPUB Validation Failed ❌")
        return epub_passed
    except zipfile.BadZipFile:
        st.error("This is not a valid ZIP archive. EPUB appears corrupted.")
        return False


def is_valid_epub(epub_path):
    try:
        with zipfile.ZipFile(epub_path, 'r') as z:
            required_files = ['META-INF/container.xml']
            for req_file in required_files:
                if req_file not in z.namelist():
                    return False
            return True
    except:
        return False

# Upload File
uploaded_file = st.file_uploader("Upload an EPUB or PDF file", type=["epub", "pdf"])

if uploaded_file:
    file_type = uploaded_file.name.split(".")[-1].lower()
    alt_texts = {}

    if file_type == "epub":
        with tempfile.NamedTemporaryFile(delete=False, suffix=".epub") as tmp_file:
            tmp_file.write(uploaded_file.read())
            temp_epub_path = tmp_file.name

        if not is_valid_epub(temp_epub_path):
            run_epub_validation(temp_epub_path)
            st.error("The uploaded file is not a valid EPUB format. Please check the file and try again.")
            st.stop()

        try:
            book = epub.read_epub(temp_epub_path)
            existing_alt_texts = {}
            for item in book.items:
                if item.media_type == 'application/xhtml+xml':
                    if not item.content:
                        st.warning(f"Skipped empty XHTML content: {item.file_name}")
                        continue
                    soup = BeautifulSoup(item.content.decode("utf-8"), "html.parser")
                    for img_tag in soup.find_all("img"):
                        src = img_tag.get("src")
                        if src and img_tag.get("alt"):
                            existing_alt_texts[src] = img_tag.get("alt")
        except Exception as e:
            st.error(f"The EPUB could not be loaded: {str(e)}")
            st.stop()

        image_files = [item for item in book.items if isinstance(item, epub.EpubImage)]

        st.subheader(f"Extracted {len(image_files)} images from EPUB")
        for idx, img in enumerate(image_files):
            if hasattr(img, "media_type") and img.media_type in ["image/jpeg", "image/png", "image/gif"]:
                try:
                    test_image = Image.open(io.BytesIO(img.content))
                    test_image.verify()
                    st.image(io.BytesIO(img.content), caption=f"Image {idx+1}")
                except (UnidentifiedImageError, Exception):
                    st.warning(f"Image {idx+1} could not be displayed (possibly corrupted or unsupported format).")
            else:
                st.warning(f"Image {idx+1} is not a supported format.")
            default_alt = existing_alt_texts.get(img.file_name, "")
            alt_text = st.text_input(f"Enter alt text for Image {idx+1}", max_chars=30, key=f"alt-{idx}", value=default_alt)
            alt_texts[img.file_name] = alt_text

        

        if st.button("Save Updated EPUB"):
            for item in book.items:
                if item.media_type == 'application/xhtml+xml':
                    soup = BeautifulSoup(item.content.decode("utf-8"), "html.parser")
                    for img_tag in soup.find_all("img"):
                        src = img_tag.get("src")
                        if src in alt_texts and alt_texts[src]:
                            clean_alt = html.escape(alt_texts[src].strip())
                            img_tag["alt"] = clean_alt
                    item.content = str(soup).encode("utf-8")

            updated_epub_path = "updated.epub"
            try:
                book.add_item(epub.EpubNav())
                book.add_item(epub.EpubNcx())
                book.spine = book.spine or ['nav']
                epub.write_epub(updated_epub_path, book)
                run_epub_validation(updated_epub_path)
                st.success("EPUB updated with alt text successfully!")
                with open(updated_epub_path, "rb") as f:
                    st.download_button("Download Updated EPUB", f, file_name="updated.epub")
                st.markdown("[Click here to preview EPUB](https://futurepress.github.io/epub.js-reader/?epub=updated.epub)")
                st.info("You can preview this EPUB below if your browser supports inline EPUB viewing:")
                st.components.v1.iframe("https://futurepress.github.io/epub.js-reader/?epub=updated.epub", height=600, scrolling=True)
            except Exception as e:
                st.error(f"Failed to save EPUB: {str(e)}")

    elif file_type == "pdf":
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
