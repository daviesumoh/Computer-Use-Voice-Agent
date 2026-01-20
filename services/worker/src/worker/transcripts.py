import re

# 13 to 19 digits, optionally grouped by spaces or dashes: the shape of a payment card number.
# Narrow on purpose; shorter numbers (prices, order numbers, dates) must survive.
CARD_NUMBER = re.compile(r"(?<!\d)(?:\d[ -]?){12,18}\d(?!\d)")


def redact_for_storage(text: str) -> str:
    return CARD_NUMBER.sub("[card number removed]", text)
