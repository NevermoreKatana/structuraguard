"""Общие content markers для разграничения XML и HTML fragments."""

HTML_ROOT_TAGS = frozenset(
    {
        "html",
        "head",
        "body",
        "title",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "p",
        "div",
        "span",
        "table",
        "ul",
        "ol",
        "li",
        "a",
        "form",
        "img",
        "script",
        "style",
        "iframe",
        "meta",
        "link",
        "section",
        "article",
    }
)
