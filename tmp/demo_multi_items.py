"""
Demonstration of ijson.multi_items: a single underlying read feeding several
subscriptions over one streaming API response.

Run with the python backend:
    IJSON_BACKEND=python python tmp/demo_multi_items.py
"""

import io

import ijson


class CountingReader:
    """File-like object that counts how many times the source is read."""

    def __init__(self, data):
        self._buffer = io.BytesIO(data)
        self.reads = 0

    def read(self, size):
        self.reads += 1
        return self._buffer.read(size)


# One response carrying dotted object keys, an empty array and nested matches.
RESPONSE = b'''{
  "records": [
    {"id": 1, "attachments": [{"name": "a"}, {"name": "b"}]},
    {"id": 2, "attachments": []}
  ],
  "a.b": {"x.y": 7}
}'''

RULES = [
    ("records", "records.item"),
    ("attachments", "records.item.attachments.item"),
    # Tuple paths keep dotted keys verbatim instead of splitting on "."
    ("dotted", ("a.b", "x.y")),
]


def main():
    reader = CountingReader(RESPONSE)
    print("streaming SubscriptionItem objects (one underlying read):")
    for item in ijson.multi_items(reader, RULES, buf_size=16):
        print(" ", item)
    print("source reads for multi_items:", reader.reads)

    separate = 0
    for prefix in ("records.item", "records.item.attachments.item"):
        one = CountingReader(RESPONSE)
        list(ijson.items(one, prefix, buf_size=16))
        separate += one.reads
    print("source reads with two separate items() calls:", separate)


if __name__ == "__main__":
    main()
