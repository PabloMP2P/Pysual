"""Display-only text normalization for captions with one allocated line."""


def single_line(text: str) -> str:
    """Replace line breaks without changing the stored or editable value."""
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
