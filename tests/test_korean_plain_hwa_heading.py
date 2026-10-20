import hashlib

import extract_txt


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_plain_hwa_heading_without_dot_is_supported_but_plain_hoe_prose_is_not():
    text = (
        "1\uD654 First title\n"
        "First body.\n"
        "171\uD68C ordinary prose is not a chapter heading.\n"
        "2\uD654 Second title\n"
        "Second body.\n"
        "413\uD654. Existing dotted format\n"
        "Third body.\n"
    )

    result = extract_txt.extract_txt_text(text, digest(text))

    assert [item["source_number"] for item in result["chapters"]] == [1, 2, 413]
    assert [item["heading_format"] for item in result["chapters"]] == [
        "number_plain_hwa",
        "number_plain_hwa",
        "number_dot",
    ]
    assert (
        "171\uD68C ordinary prose is not a chapter heading."
        in result["chapters"][0]["text"]
    )
