"""
This module holds the AESModeCTR wrapper class.
"""
try:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    Cipher = None


class AESModeCTR:
    """
    AES CTR mode with a custom 16-byte initial counter block.

    Uses ``cryptography`` if available, otherwise ``pyaes``.
    Encryption and decryption share the same keystream.
    """

    def __init__(self, key, iv):
        """
        Initializes the AES CTR mode with the given key/iv pair.

        :param key: the key to be used as bytes.
        :param iv: the bytes initialization vector. Must have a length of 16.
        """
        assert isinstance(key, bytes)
        assert isinstance(iv, bytes)
        assert len(iv) == 16
        if Cipher is not None:
            self._aes = None
            self._update = Cipher(algorithms.AES(key), modes.CTR(iv)).encryptor().update
        else:
            import pyaes
            self._aes = pyaes.AESModeOfOperationCTR(key)
            self._aes._counter._counter = list(iv)
            self._update = self._aes.encrypt

    def encrypt(self, data):
        """
        Encrypts the given plain text through AES CTR.

        :param data: the plain text to be encrypted.
        :return: the encrypted cipher text.
        """
        return self._update(data)

    def decrypt(self, data):
        """
        Decrypts the given cipher text through AES CTR

        :param data: the cipher text to be decrypted.
        :return: the decrypted plain text.
        """
        return self._update(data)
