import streamlit as st
from ebooklib import epub
from PIL import Image
import io
import os

# Title
st.title("EPUB Image Alt Text Editor")

# Upload EPUB File
uploaded_file = st.file_uploader("Upload an EPUB file", type=["epub"])

if uploaded_file:
    # Save uploaded file temporarily
    epub_path = f"temp.epub"
    with open(epub_path, "wb") as f:
        f.write(uploaded_file.read())

    # Extract images from EPUB
    book = epub.read_epub(epub_path)
    image_files = [item for item in book.items if isinstance(item, epub.EpubImage)]
    
    st.subheader(f"Extracted {len(image_files)} images")

    alt_texts = {}

    for idx, img in enumerate(image_files):
        st.image(io.BytesIO(img.content), caption=f"Image {idx+1}")

        alt_text = st.text_input(f"Enter alt text for Image {idx+1}", max_chars=30)
        alt_texts[img.file_name] = alt_text

    # Save Updated EPUB
    if st.button("Save Updated EPUB"):
        for img in image_files:
            if img.file_name in alt_texts and alt_texts[img.file_name].strip():
                img.set_content(b"<!-- ALT: " + alt_texts[img.file_name].encode() + b" -->" + img.content)
        
        updated_epub_path = "updated.epub"
        epub.write_epub(updated_epub_path, book)
        st.success("EPUB updated successfully!")
        st.download_button("Download Updated EPUB", open(updated_epub_path, "rb"), file_name="updated.epub")
