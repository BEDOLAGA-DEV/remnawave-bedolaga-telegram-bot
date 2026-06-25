import base64

from app.utils.incy_keymat import KEYMAT_A_B64, KEYMAT_B_B64


def test_keymat_blobs_decode_to_4096_bytes():
    assert len(base64.b64decode(KEYMAT_A_B64)) == 4096
    assert len(base64.b64decode(KEYMAT_B_B64)) == 4096
