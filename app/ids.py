"""Time-ordered UUIDs (RFC 9562 version 7), so primary keys sort by creation time."""

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    millis = time.time_ns() // 1_000_000
    rand = int.from_bytes(os.urandom(10), "big")
    value = (millis & ((1 << 48) - 1)) << 80
    value |= 0x7 << 76  # version
    value |= (rand >> 68) << 64  # 12 random bits
    value |= 0b10 << 62  # variant
    value |= rand & ((1 << 62) - 1)  # 62 random bits
    return uuid.UUID(int=value)
