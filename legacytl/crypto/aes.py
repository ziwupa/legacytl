"""
AES IGE implementation in Python.

The fastest correct backend among cryptg, libssl and an IGE loop over
``cryptography``'s AES-ECB is picked on first use by a tiny benchmark.
pyaes is the last resort. ``LEGACYTL_AES_BACKEND`` forces a backend
(``cryptg``, ``libssl``, ``cryptography`` or ``pyaes``).
"""
import os
import logging
import time
from . import libssl


__log__ = logging.getLogger(__name__)


try:
    import cryptg
except ImportError:
    cryptg = None

try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None

BACKENDS = ('cryptg', 'libssl', 'cryptography', 'pyaes')

_KAT_KEY = bytes(range(32))
_KAT_IV = bytes(range(100, 132))
_KAT_PLAIN = bytes((i * 7 + 3) & 255 for i in range(64))
_KAT_CIPHER = bytes.fromhex(
    'b1558c09fe02f8becabd61286c912b60285bba5790d4828ea98199e7d606f403'
    '67ba8a99cffa54faa69ed9e90995e4eb67bb674b8e8584512e33bb540c0534cd'
)


def _py_encrypt_ige(plain_text, key, iv):
    enc = Cipher(algorithms.AES(key), modes.ECB()).encryptor().update
    fb = int.from_bytes
    iv1 = fb(iv[:16], 'little')
    iv2 = fb(iv[16:32], 'little')
    data = memoryview(plain_text)
    out = []
    append = out.append
    for i in range(0, len(data), 16):
        p = fb(data[i:i + 16], 'little')
        iv1 = fb(enc((p ^ iv1).to_bytes(16, 'little')), 'little') ^ iv2
        iv2 = p
        append(iv1.to_bytes(16, 'little'))
    return b''.join(out)


def _py_decrypt_ige(cipher_text, key, iv):
    dec = Cipher(algorithms.AES(key), modes.ECB()).decryptor().update
    fb = int.from_bytes
    iv1 = fb(iv[:16], 'little')
    iv2 = fb(iv[16:32], 'little')
    data = memoryview(cipher_text)
    out = []
    append = out.append
    for i in range(0, len(data), 16):
        c = fb(data[i:i + 16], 'little')
        iv2 = fb(dec((c ^ iv2).to_bytes(16, 'little')), 'little') ^ iv1
        iv1 = c
        append(iv2.to_bytes(16, 'little'))
    return b''.join(out)


def _pyaes_decrypt_ige(cipher_text, key, iv):
    import pyaes
    iv1 = iv[:len(iv) // 2]
    iv2 = iv[len(iv) // 2:]

    aes = pyaes.AES(key)

    plain_text = []
    blocks_count = len(cipher_text) // 16

    cipher_text_block = [0] * 16
    for block_index in range(blocks_count):
        for i in range(16):
            cipher_text_block[i] = \
                cipher_text[block_index * 16 + i] ^ iv2[i]

        plain_text_block = aes.decrypt(cipher_text_block)

        for i in range(16):
            plain_text_block[i] ^= iv1[i]

        iv1 = cipher_text[block_index * 16:block_index * 16 + 16]
        iv2 = plain_text_block

        plain_text.extend(plain_text_block)

    return bytes(plain_text)


def _pyaes_encrypt_ige(plain_text, key, iv):
    import pyaes
    iv1 = iv[:len(iv) // 2]
    iv2 = iv[len(iv) // 2:]

    aes = pyaes.AES(key)

    cipher_text = []
    blocks_count = len(plain_text) // 16

    for block_index in range(blocks_count):
        plain_text_block = list(
            plain_text[block_index * 16:block_index * 16 + 16]
        )
        for i in range(16):
            plain_text_block[i] ^= iv1[i]

        cipher_text_block = aes.encrypt(plain_text_block)

        for i in range(16):
            cipher_text_block[i] ^= iv2[i]

        iv1 = cipher_text_block
        iv2 = plain_text[block_index * 16:block_index * 16 + 16]

        cipher_text.extend(cipher_text_block)

    return bytes(cipher_text)


def _candidate(name):
    """Returns ``(encrypt, decrypt)`` for the backend, or ``None``."""
    if name == 'cryptg':
        if cryptg:
            return cryptg.encrypt_ige, cryptg.decrypt_ige
    elif name == 'libssl':
        if libssl.encrypt_ige and libssl.decrypt_ige:
            return libssl.encrypt_ige, libssl.decrypt_ige
    elif name == 'cryptography':
        if Cipher is not None:
            return _py_encrypt_ige, _py_decrypt_ige
    elif name == 'pyaes':
        try:
            import pyaes  # noqa: F401
        except ImportError:
            return None
        return _pyaes_encrypt_ige, _pyaes_decrypt_ige
    return None


def _verified(name):
    pair = _candidate(name)
    if pair is None:
        return None
    try:
        enc, dec = pair
        if (enc(_KAT_PLAIN, _KAT_KEY, _KAT_IV) == _KAT_CIPHER
                and dec(_KAT_CIPHER, _KAT_KEY, _KAT_IV) == _KAT_PLAIN):
            return pair
    except Exception as e:
        __log__.info('AES backend %s failed: %s: %s', name, type(e).__name__, e)
        return None
    __log__.warning('AES backend %s produced wrong results, skipping it', name)
    return None


def _speed(pair, size=16 * 1024):
    enc, dec = pair
    data = bytes(size)
    best = float('inf')
    for _ in range(3):
        start = time.perf_counter()
        dec(enc(data, _KAT_KEY, _KAT_IV), _KAT_KEY, _KAT_IV)
        best = min(best, time.perf_counter() - start)
        if best > 0.001:
            break
    return best


_backend = None
_encrypt = None
_decrypt = None


def _select():
    global _backend, _encrypt, _decrypt
    forced = os.environ.get('LEGACYTL_AES_BACKEND', '').strip().lower()
    chosen = None
    if forced:
        pair = _verified(forced) if forced in BACKENDS else None
        if pair:
            chosen = forced, pair
        else:
            __log__.warning('AES backend %r is not available, choosing automatically', forced)

    if chosen is None:
        timed = []
        for name in ('cryptg', 'libssl', 'cryptography'):
            pair = _verified(name)
            if pair:
                timed.append((_speed(pair), name, pair))
        if timed:
            _, name, pair = min(timed, key=lambda t: t[0])
            chosen = name, pair

    if chosen is None:
        pair = _verified('pyaes') or _candidate('pyaes')
        if pair is None:
            raise ImportError('no AES implementation available (install cryptography or cryptg)')
        chosen = 'pyaes', pair

    _backend, (_encrypt, _decrypt) = chosen
    __log__.info('Using %s for AES-IGE', _backend)
    return _backend


def get_backend():
    """Returns the name of the AES-IGE backend in use, selecting it if needed."""
    return _backend or _select()


def set_backend(name=None):
    """
    Forces the given backend (or re-runs the automatic selection with ``None``).
    Raises ``ValueError`` if the backend is not available or misbehaves.
    """
    global _backend, _encrypt, _decrypt
    if name is None:
        _backend = None
        return _select()
    pair = _verified(name)
    if not pair:
        raise ValueError('AES backend {!r} is not available'.format(name))
    _backend, (_encrypt, _decrypt) = name, pair
    return name


class AES:
    """
    Class that servers as an interface to encrypt and decrypt
    text through the AES IGE mode.
    """
    @staticmethod
    def decrypt_ige(cipher_text, key, iv):
        """
        Decrypts the given text in 16-bytes blocks by using the
        given key and 32-bytes initialization vector.
        """
        if type(cipher_text) is not bytes:
            cipher_text = bytes(cipher_text)
        if _decrypt is None:
            _select()
        return _decrypt(cipher_text, key, iv)

    @staticmethod
    def encrypt_ige(plain_text, key, iv):
        """
        Encrypts the given text in 16-bytes blocks by using the
        given key and 32-bytes initialization vector.
        """
        if type(plain_text) is not bytes:
            plain_text = bytes(plain_text)
        padding = len(plain_text) % 16
        if padding:
            plain_text += os.urandom(16 - padding)

        if _encrypt is None:
            _select()
        return _encrypt(plain_text, key, iv)
