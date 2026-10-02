"""Binary session storage with authenticated at-rest encryption.

The on-disk container is either:

    LGBINS1\\0 | payload                      (plaintext)
    LGBINC1\\0 | nonce(12) | AES-256-GCM(ct)  (encrypted, AAD = magic)

The payload itself is a little-endian binary structure (``LGBINP1\\0``)
holding everything ``SQLiteSession`` used to store: DC info, auth keys,
takeout id, cached entities, sent files and update states.

The encryption key is kept in a *separate* file, so a stolen ``.lsession``
file is useless without it.
"""
import base64
import binascii
import datetime
import hashlib
import os
import string
import struct
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ..crypto import AuthKey
from ..tl import types
from .memory import MemorySession, _SentFileType


EXTENSION = '.lsession'
CURRENT_VERSION = 1
KEY_SIZE = 32
NONCE_SIZE = 12

_PLAIN_MAGIC = b'LGBINS1\0'
_CRYPT_MAGIC = b'LGBINC1\0'
_PAYLOAD_MAGIC = b'LGBINP1\0'
_NONE_LENGTH = 0xffffffff

_KEY_ENV = ('LEGACY_SESSION_KEY', 'LEGACY_SESSION_KEY')
_KEY_FILE_ENV = ('LEGACY_SESSION_KEY_FILE', 'LEGACY_SESSION_KEY_FILE')


class BinarySessionError(ValueError):
    """Base error for binary session storage."""


class SessionKeyMissingError(BinarySessionError):
    """Raised when an encryption key is required but not configured."""


class SessionDecryptError(BinarySessionError):
    """Raised when a session file cannot be decrypted.

    Either the key is wrong/missing or the file was corrupted/tampered with.
    """


class _Reader:
    def __init__(self, data):
        self.data = data
        self.offset = 0

    def read(self, size):
        end = self.offset + size
        if end > len(self.data):
            raise BinarySessionError('Truncated binary session')
        chunk = self.data[self.offset:end]
        self.offset = end
        return chunk

    def unpack(self, fmt):
        size = struct.calcsize(fmt)
        return struct.unpack(fmt, self.read(size))


def _pack_bytes(value):
    if value is None:
        return struct.pack('<I', _NONE_LENGTH)
    return struct.pack('<I', len(value)) + value


def _unpack_bytes(reader):
    length, = reader.unpack('<I')
    if length == _NONE_LENGTH:
        return None
    return reader.read(length)


def _pack_string(value):
    if value is None:
        return _pack_bytes(None)
    return _pack_bytes(str(value).encode('utf-8'))


def _unpack_string(reader):
    value = _unpack_bytes(reader)
    if value is None:
        return None
    return value.decode('utf-8')


def _pack_optional_int(value):
    if value is None:
        return b'\0'
    return b'\1' + struct.pack('<q', int(value))


def _unpack_optional_int(reader):
    marker = reader.read(1)
    if marker == b'\0':
        return None
    if marker != b'\1':
        raise BinarySessionError('Invalid optional integer marker in binary session')
    value, = reader.unpack('<q')
    return value


def _is_hex(text):
    return bool(text) and all(c in string.hexdigits for c in text)


def _decode_key_material(data):
    """Decodes key material from raw bytes/str (hex:/base64:/bare hex/raw)."""
    if isinstance(data, str):
        data = data.encode('utf-8')

    data = data.strip()
    if not data:
        raise SessionKeyMissingError('Empty session encryption key')

    try:
        text = data.decode('utf-8').strip()
    except UnicodeDecodeError:
        return data

    lowered = text.lower()
    if lowered.startswith('hex:'):
        return binascii.unhexlify(text[4:])
    if lowered.startswith('base64:'):
        return base64.b64decode(text[7:])
    if lowered.startswith('b64:'):
        return base64.b64decode(text[4:])
    if len(text) == KEY_SIZE * 2 and _is_hex(text):
        return binascii.unhexlify(text)

    return data


def _normalize_key(secret):
    """Turns arbitrary key material into a 32-byte AES-256 key."""
    if secret is None:
        raise SessionKeyMissingError('Session encryption key is not configured')
    if len(secret) == KEY_SIZE:
        return bytes(secret)
    return hashlib.sha256(bytes(secret)).digest()


def _env_value(names):
    for name in names:
        value = os.environ.get(name)
        if value:
            return value


def load_or_create_key_file(path, create=True):
    """Reads the encryption key from ``path``, generating it if missing.

    Newly created key files contain 32 random bytes hex-encoded and are
    written with ``0600`` permissions.
    """
    path = os.fspath(path)
    try:
        with open(path, 'rb') as f:
            data = f.read()
    except FileNotFoundError:
        if not create:
            raise SessionKeyMissingError(
                'Session key file does not exist: {}'.format(path)) from None
        key = os.urandom(KEY_SIZE)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            # Race with another process: fall back to reading it
            return load_or_create_key_file(path, create=False)
        try:
            os.write(fd, binascii.hexlify(key) + b'\n')
            os.fsync(fd)
        finally:
            os.close(fd)
        return key

    return _normalize_key(_decode_key_material(data))


class BinarySession(MemorySession):
    """Binary session storage with authenticated at-rest encryption.

    When ``encrypted`` is true (the default), the on-disk file contains only
    AES-256-GCM ciphertext. The key is taken from (in order): the ``key``
    argument, the ``key_file`` argument, the ``LEGACY_SESSION_KEY_FILE``/
    ``LEGACY_SESSION_KEY_FILE`` or ``LEGACY_SESSION_KEY``/
    ``LEGACY_SESSION_KEY`` environment variables. Without the key the
    session file is useless.
    """

    EXTENSION = EXTENSION

    def __init__(self, session_id=None, *, key=None, key_file=None,
                 create_key=True, encrypted=True,
                 store_tmp_auth_key_on_disk=False):
        super().__init__()
        self.filename = ':memory:'
        self.save_entities = True
        self.encrypted = encrypted
        self.store_tmp_auth_key_on_disk = store_tmp_auth_key_on_disk
        self._key_file = os.fspath(key_file) if key_file else None
        self._create_key = create_key
        self._key = (
            _normalize_key(_decode_key_material(key)) if key is not None else None
        )
        self._dirty = False
        self._cipher = None
        self._init_indexes()

        if session_id:
            self.filename = os.fspath(session_id)
            if not self.filename.endswith(EXTENSION):
                self.filename += EXTENSION

        if self.filename != ':memory:' and os.path.isfile(self.filename):
            with open(self.filename, 'rb') as f:
                data = f.read()
            if data:
                self._load(data)
        else:
            self._dirty = True

    def clone(self, to_instance=None):
        cloned = to_instance or self.__class__(
            key=self._key,
            key_file=self._key_file,
            create_key=self._create_key,
            encrypted=self.encrypted,
            store_tmp_auth_key_on_disk=self.store_tmp_auth_key_on_disk,
        )
        cloned.save_entities = self.save_entities
        return cloned

    def copy_from(self, session):
        """Copies DC info, auth key, update states and caches from ``session``."""
        self.set_dc(session.dc_id, session.server_address, session.port)
        self.auth_key = session.auth_key
        self.tmp_auth_key = getattr(session, '_tmp_auth_key', None)
        self.takeout_id = session.takeout_id

        for entity_id, state in session.get_update_states():
            self._update_states[entity_id] = state

        # Only in-memory sessions keep their caches in these attributes
        self._store_rows(getattr(session, '_entities', ()))
        self._files.update(getattr(session, '_files', {}))
        self._dirty = True

    def set_dc(self, dc_id, server_address, port):
        super().set_dc(dc_id, server_address, port)
        self._dirty = True

    @MemorySession.auth_key.setter
    def auth_key(self, value):
        self._auth_key = value
        self._dirty = True

    @MemorySession.tmp_auth_key.setter
    def tmp_auth_key(self, value):
        self._tmp_auth_key = value
        self._dirty = True

    @MemorySession.takeout_id.setter
    def takeout_id(self, value):
        self._takeout_id = value
        self._dirty = True

    def set_update_state(self, entity_id, state):
        super().set_update_state(entity_id, state)
        self._dirty = True

    def process_entities(self, tlo):
        if not self.save_entities:
            return

        if self._store_rows(self._entities_to_rows(tlo)):
            self._dirty = True

    def _init_indexes(self):
        # MemorySession scans the whole set on every lookup. A userbot asks
        # for peers constantly, so keep the same rows addressable in O(1)
        self._by_id = {}
        self._by_username = {}
        self._by_phone = {}
        self._by_name = {}

    def _reindex(self):
        self._init_indexes()
        for row in self._entities:
            self._index_row(row)

    def _index_row(self, row):
        entity_id, _, username, phone, name = row
        self._by_id[entity_id] = row
        if username:
            self._by_username[username] = row
        if phone is not None:
            self._by_phone[phone] = row
        if name:
            self._by_name[name] = row

    def _unindex_row(self, row):
        entity_id, _, username, phone, name = row
        self._by_id.pop(entity_id, None)
        if username and self._by_username.get(username) == row:
            del self._by_username[username]
        if phone is not None and self._by_phone.get(phone) == row:
            del self._by_phone[phone]
        if name and self._by_name.get(name) == row:
            del self._by_name[name]

    def _row_ref(self, row):
        return (row[0], row[1]) if row else None

    def get_entity_rows_by_phone(self, phone):
        return self._row_ref(self._by_phone.get(phone))

    def get_entity_rows_by_username(self, username):
        return self._row_ref(self._by_username.get(username))

    def get_entity_rows_by_name(self, name):
        return self._row_ref(self._by_name.get(name))

    def get_entity_rows_by_id(self, id, exact=True):
        if exact:
            return self._row_ref(self._by_id.get(id))

        from .. import utils
        from ..tl.types import PeerChannel, PeerChat, PeerUser

        for marked in (
            utils.get_peer_id(PeerUser(id)),
            utils.get_peer_id(PeerChat(id)),
            utils.get_peer_id(PeerChannel(id)),
        ):
            row = self._by_id.get(marked)
            if row:
                return row[0], row[1]

    def _store_rows(self, rows):
        """
        Stores entity rows keeping exactly one row per peer id.

        ``MemorySession`` keeps a plain set of rows, so every rename or
        username change adds a new row next to the stale one. On disk that
        means the file grows forever, and lookups (which return the first
        match) may resolve a username to whoever used to own it.

        :return: Whether anything actually changed.
        """
        changed = False
        for row in rows:
            current = self._by_id.get(row[0])
            if current == row:
                continue

            if current is not None:
                self._unindex_row(current)
                self._entities.discard(current)

            self._entities.add(row)
            self._index_row(row)
            changed = True

        return changed

    def cache_file(self, md5_digest, file_size, instance):
        super().cache_file(md5_digest, file_size, instance)
        self._dirty = True

    def save(self):
        if self.filename == ':memory:' or not self._dirty:
            return

        payload = self._serialize_payload()
        data = self._encrypt(payload) if self.encrypted else _PLAIN_MAGIC + payload
        self._write_atomic(data)
        self._dirty = False

    def close(self):
        self.save()

    def delete(self):
        if self.filename == ':memory:':
            return True
        try:
            os.remove(self.filename)
            return True
        except OSError:
            return False

    @classmethod
    def list_sessions(cls):
        return [os.path.splitext(os.path.basename(f))[0]
                for f in os.listdir('.') if f.endswith(EXTENSION)]

    def _load(self, data):
        if data.startswith(_PLAIN_MAGIC):
            payload = data[len(_PLAIN_MAGIC):]
        elif data.startswith(_CRYPT_MAGIC):
            payload = self._decrypt(data)
        else:
            raise BinarySessionError('Invalid binary session magic')

        self._needs_compaction = False
        self._deserialize_payload(payload)
        self._dirty = self._needs_compaction

    def _cipher_for(self):
        key = self._get_key()
        cipher = self._cipher
        if cipher is None or self._cipher[0] != key:
            cipher = (key, AESGCM(key))
            self._cipher = cipher
        return cipher[1]

    def _get_key(self):
        if self._key is not None:
            return self._key

        if self._key_file:
            self._key = load_or_create_key_file(
                self._key_file, create=self._create_key)
            return self._key

        key_file = _env_value(_KEY_FILE_ENV)
        if key_file:
            self._key = load_or_create_key_file(key_file, create=False)
            return self._key

        raw = _env_value(_KEY_ENV)
        if raw:
            self._key = _normalize_key(_decode_key_material(raw))
            return self._key

        raise SessionKeyMissingError(
            'BinarySession encryption key is not configured. Pass key/key_file '
            'or set LEGACY_SESSION_KEY_FILE / LEGACY_SESSION_KEY.'
        )

    def _encrypt(self, payload):
        nonce = os.urandom(NONCE_SIZE)
        ciphertext = self._cipher_for().encrypt(nonce, payload, _CRYPT_MAGIC)
        return _CRYPT_MAGIC + nonce + ciphertext

    def _decrypt(self, data):
        # 16 is the size of the GCM authentication tag
        if len(data) < len(_CRYPT_MAGIC) + NONCE_SIZE + 16:
            raise SessionDecryptError('Truncated encrypted binary session')

        nonce = data[len(_CRYPT_MAGIC):len(_CRYPT_MAGIC) + NONCE_SIZE]
        ciphertext = data[len(_CRYPT_MAGIC) + NONCE_SIZE:]
        try:
            return self._cipher_for().decrypt(nonce, ciphertext, _CRYPT_MAGIC)
        except InvalidTag:
            raise SessionDecryptError(
                'Cannot decrypt session: wrong key or corrupted file'
            ) from None

    def _serialize_payload(self):
        parts = [_PAYLOAD_MAGIC, struct.pack('<H', CURRENT_VERSION)]

        auth_key = self._auth_key.key if self._auth_key else None
        tmp_auth_key = (
            self._tmp_auth_key.key
            if self.store_tmp_auth_key_on_disk and self._tmp_auth_key
            else None
        )
        parts.append(struct.pack('<ii', int(self._dc_id or 0), int(self._port or 0)))
        parts.append(_pack_string(self._server_address))
        parts.append(_pack_bytes(auth_key))
        parts.append(_pack_bytes(tmp_auth_key))
        parts.append(_pack_optional_int(self._takeout_id))

        entities = sorted(
            self._entities,
            key=lambda row: (
                row[0],
                row[2] or '',
                str(row[3]) if row[3] is not None else '',
                row[4] or '',
            ),
        )
        parts.append(struct.pack('<I', len(entities)))
        for entity_id, entity_hash, username, phone, name in entities:
            parts.append(struct.pack('<qq', int(entity_id), int(entity_hash)))
            parts.append(_pack_string(username))
            parts.append(_pack_string(phone))
            parts.append(_pack_string(name))

        states = sorted(self._update_states.items(), key=lambda item: item[0])
        parts.append(struct.pack('<I', len(states)))
        for entity_id, state in states:
            parts.append(struct.pack(
                '<qiiqi',
                int(entity_id),
                int(state.pts),
                int(state.qts),
                self._datetime_to_timestamp(state.date),
                int(state.seq),
            ))

        files = sorted(
            self._files.items(),
            key=lambda item: (item[0][0], item[0][1], item[0][2].value),
        )
        parts.append(struct.pack('<I', len(files)))
        for (md5_digest, file_size, file_type), (file_id, file_hash) in files:
            parts.append(_pack_bytes(md5_digest))
            parts.append(struct.pack(
                '<qBqq',
                int(file_size),
                int(file_type.value),
                int(file_id),
                int(file_hash),
            ))

        return b''.join(parts)

    def _deserialize_payload(self, payload):
        reader = _Reader(payload)
        if reader.read(len(_PAYLOAD_MAGIC)) != _PAYLOAD_MAGIC:
            raise BinarySessionError('Invalid binary session payload')

        version, = reader.unpack('<H')
        if version != CURRENT_VERSION:
            raise BinarySessionError(
                'Unsupported binary session version {}'.format(version))

        self._dc_id, self._port = reader.unpack('<ii')
        self._server_address = _unpack_string(reader)

        auth_key = _unpack_bytes(reader)
        tmp_auth_key = _unpack_bytes(reader)
        self._auth_key = AuthKey(data=auth_key) if auth_key else None
        self._tmp_auth_key = AuthKey(data=tmp_auth_key) if tmp_auth_key else None
        self._takeout_id = _unpack_optional_int(reader)

        entities_count, = reader.unpack('<I')
        rows = {}
        for _ in range(entities_count):
            entity_id, entity_hash = reader.unpack('<qq')
            # Older builds could store several rows per id; the last one wins
            rows[entity_id] = (
                entity_id,
                entity_hash,
                _unpack_string(reader),
                _unpack_string(reader),
                _unpack_string(reader),
            )
        self._entities = set(rows.values())
        self._reindex()
        # Rewrite the file compacted on the next save
        self._needs_compaction = len(self._entities) != entities_count

        states_count, = reader.unpack('<I')
        self._update_states = {}
        for _ in range(states_count):
            entity_id, pts, qts, date, seq = reader.unpack('<qiiqi')
            self._update_states[entity_id] = types.updates.State(
                pts=pts,
                qts=qts,
                date=datetime.datetime.fromtimestamp(date, tz=datetime.timezone.utc),
                seq=seq,
                unread_count=0,
            )

        files_count, = reader.unpack('<I')
        self._files = {}
        for _ in range(files_count):
            md5_digest = _unpack_bytes(reader)
            file_size, file_type, file_id, file_hash = reader.unpack('<qBqq')
            self._files[(md5_digest, file_size, _SentFileType(file_type))] = (
                file_id, file_hash,
            )

    @staticmethod
    def _datetime_to_timestamp(value):
        if value is None:
            return 0
        return int(value.timestamp())

    def _write_atomic(self, data):
        directory = os.path.dirname(os.path.abspath(self.filename)) or '.'
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            dir=directory,
            prefix=os.path.basename(self.filename) + '.',
            suffix='.tmp',
        )
        closed = False
        try:
            os.chmod(tmp_path, 0o600)
            view = memoryview(data)
            while view:
                # os.write may write less than asked for
                view = view[os.write(fd, view):]
            os.fsync(fd)
            closed = True
            os.close(fd)
            os.replace(tmp_path, self.filename)
            os.chmod(self.filename, 0o600)
        except BaseException:
            if not closed:
                try:
                    os.close(fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
