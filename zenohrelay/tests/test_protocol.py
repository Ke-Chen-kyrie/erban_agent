import pytest

from protocol import MAGIC, decode_frame, encode_frame


def test_plain_jpeg_round_trip():
    jpeg = b"\xff\xd8plain-jpeg\xff\xd9"
    assert encode_frame(jpeg) == jpeg
    assert decode_frame(jpeg) == (jpeg, None)


def test_metadata_round_trip():
    jpeg = b"\xff\xd8image\xff\xd9"
    metadata = {"seq": 7, "faces": [{"name": "张三"}]}
    payload = encode_frame(jpeg, metadata)
    assert payload.startswith(MAGIC)
    assert decode_frame(payload) == (jpeg, metadata)


def test_truncated_metadata_is_rejected():
    payload = encode_frame(b"jpeg", {"seq": 1})
    with pytest.raises(ValueError):
        decode_frame(payload[:-5])
