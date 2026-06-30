from __future__ import annotations

import html
import re
from bs4 import BeautifulSoup


def html_to_plain_text(value: str | None) -> str:
    """Convert ATS/Google Jobs HTML fragments into readable plain text.

    Many job APIs return descriptions with <div>, <p>, <ul>, class names, &nbsp;,
    and other markup. This function preserves paragraph/list structure while
    removing HTML tags and decoding entities.
    """
    if not value:
        return ""
    text = str(value)
    # Decode entities first so BeautifulSoup sees normal characters.
    text = html.unescape(text).replace("\xa0", " ")

    # If it does not look like HTML, still normalize whitespace/entities.
    if "<" not in text or ">" not in text:
        return normalize_plain_text(text)

    soup = BeautifulSoup(text, "html.parser")
    # Remove scripts/styles and noisy non-content tags.
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()

    # Add line breaks around block elements and bullets before list items.
    for br in soup.find_all("br"):
        br.replace_with("\n")
    for li in soup.find_all("li"):
        li.insert_before("\n- ")
        li.append("\n")
    for block in soup.find_all(["p", "div", "section", "article", "h1", "h2", "h3", "h4", "ul", "ol"]):
        block.insert_before("\n")
        block.append("\n")

    return normalize_plain_text(soup.get_text(" "))


def normalize_plain_text(value: str | None) -> str:
    if not value:
        return ""
    text = html.unescape(str(value)).replace("\xa0", " ")
    text = text.replace("&mdash;", "—").replace("&amp;", "&")
    # Keep paragraph/list line breaks but remove excessive spacing.
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Clean spaces before punctuation caused by get_text separators.
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.strip()


def clean_job_text(value: str | None) -> str:
    return html_to_plain_text(value)
