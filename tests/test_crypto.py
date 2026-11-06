"""Vectors produced by the Monocypher build that ships in the echo-dot-assist firmware."""
from echo_voice_bridge import echonet

KEY = bytes(range(32))
NONCE = bytes(i + 50 for i in range(24))
AD = bytes(i + 90 for i in range(17))
PLAIN = bytes(i + 7 for i in range(40))
SECRET = bytes(200 - i for i in range(32))


def test_aead_matches_monocypher():
    cipher, mac = echonet.aead_lock(KEY, NONCE, AD, PLAIN)
    assert cipher.hex() == "0c7256d665ed46384c67637fd30245aaff40678f307a6c0a3eaf4572574ae5235d2c984a458c7141"
    assert mac.hex() == "bc1b4ac99b70a4a11534d435699ce21f"
    assert echonet.aead_unlock(KEY, NONCE, AD, cipher, mac) == PLAIN


def test_blake2b_matches_monocypher():
    derived = echonet.derive(KEY, b"hassmic arbitration mac")
    assert derived.hex() == "ad666464989298c24533490762c9eca715b139b846444ec6b76a28565f7ff5ca"
    assert echonet.blake2b(PLAIN, key=derived, size=16).hex() == "df8cdd39518642cfb70343bf37ca0619"


def test_x25519_matches_monocypher():
    public = echonet.nb.crypto_scalarmult_base(SECRET)
    assert public.hex() == "8dafcfeeae9045ee9a78d78a99fcf32e7781e441eb073096c342b8e2dcc3b612"
    assert echonet.x25519(SECRET, public).hex() == "72ca3f98a14602cc2bedc44a452d6c074a830544eb08ad442520f0f357cea477"


def test_sealed_network_key_opens_for_the_recipient():
    a_secret, a_public = echonet.x25519_keypair()
    b_secret, b_public = echonet.x25519_keypair()
    network_key = bytes(range(32, 64))
    import base64
    blob = base64.b64decode(echonet.seal_network_key(a_secret, a_public, b_public, 0x1234, network_key))
    assert len(blob) == 32 + 24 + 16 + 32 and blob[:32] == a_public
    wrap = echonet.wrap_key(b_secret, a_public, a_public, b_public, 0x1234)
    assert echonet.aead_unlock(wrap, blob[32:56], a_public, blob[72:], blob[56:72]) == network_key
