import datetime
import typing

from . import helpers
from .tl import types, custom

_NAMES = {}


def _build():
    # Built on first use so that importing this module doesn't load
    # every TL class referenced in the unions below
    global Phone, Username, PeerID, Entity, FullEntity, EntityLike, EntitiesLike, ButtonLike, MarkupLike, TotalList, DateLike, LocalPath, ExternalUrl, BotFileID, FileLike, OutFileLike, MessageLike, MessageIDLike, ProgressCallback
    Phone = str
    Username = str
    PeerID = int
    Entity = typing.Union[types.User, types.Chat, types.Channel, types.Community]
    FullEntity = typing.Union[types.UserFull, types.messages.ChatFull, types.ChatFull, types.ChannelFull, types.CommunityFull]

    EntityLike = typing.Union[
        Phone,
        Username,
        PeerID,
        types.TypePeer,
        types.TypeInputPeer,
        Entity,
        FullEntity
    ]
    EntitiesLike = typing.Union[EntityLike, typing.Sequence[EntityLike]]

    ButtonLike = typing.Union[
        types.TypeKeyboardButton,
        types.TypeKeyboardInlineButton,
        custom.Button
    ]
    MarkupLike = typing.Union[
        types.TypeReplyMarkup,
        ButtonLike,
        typing.Sequence[ButtonLike],
        typing.Sequence[typing.Sequence[ButtonLike]]
    ]

    TotalList = helpers.TotalList

    DateLike = typing.Optional[typing.Union[float, datetime.datetime, datetime.date, datetime.timedelta]]

    LocalPath = str
    ExternalUrl = str
    BotFileID = str
    FileLike = typing.Union[
        LocalPath,
        ExternalUrl,
        BotFileID,
        bytes,
        typing.BinaryIO,
        types.TypeMessageMedia,
        types.TypeInputFile,
        types.TypeInputFileLocation,
        types.TypeInputMedia,
        types.TypePhoto,
        types.TypeInputPhoto,
        types.TypeDocument,
        types.TypeInputDocument
    ]

    # Can't use `typing.Type` in Python 3.5.2
    # See https://github.com/python/typing/issues/266
    try:
        OutFileLike = typing.Union[
            str,
            typing.Type[bytes],
            typing.BinaryIO
        ]
    except TypeError:
        OutFileLike = typing.Union[
            str,
            typing.BinaryIO
        ]

    MessageLike = typing.Union[str, types.Message]
    MessageIDLike = typing.Union[int, types.Message, types.TypeInputMessage]

    ProgressCallback = typing.Callable[[int, int], None]

    _NAMES.update((k, v) for k, v in globals().items() if k in __all__)


__all__ = ['Phone', 'Username', 'PeerID', 'Entity', 'FullEntity', 'EntityLike', 'EntitiesLike', 'ButtonLike', 'MarkupLike', 'TotalList', 'DateLike', 'LocalPath', 'ExternalUrl', 'BotFileID', 'FileLike', 'OutFileLike', 'MessageLike', 'MessageIDLike', 'ProgressCallback']


def __getattr__(name):
    if name in __all__:
        if not _NAMES:
            _build()
        return _NAMES[name]
    raise AttributeError('module {!r} has no attribute {!r}'.format(__name__, name))


def __dir__():
    return sorted(set(globals()).union(__all__))
