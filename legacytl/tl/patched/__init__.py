import inspect

from .. import types, alltlobjects
from ..custom.message import Message as _Message

_RAW = (types.MessageEmpty, types.MessageService, types.Message)

try:
    from ..tlobject import _slot_members
except ImportError:
    def _slot_members(cls):
        members = {}
        for klass in reversed(cls.__mro__):
            for name in getattr(klass, '__slots__', ()):
                members[name] = klass
        return tuple(members.items())

_CUSTOM_SLOTS = getattr(_Message, '_custom_slots', ())


def _raw_fields(raw):
    """Names of the TL fields of a generated ``raw`` type."""
    slots = getattr(raw, '__slots__', None)
    if slots is not None:
        return frozenset(slots)
    params = inspect.signature(raw.__init__).parameters
    return frozenset(name for name in params if name != 'self')


def _slots(base):
    # Every attribute custom.Message sets lives in a slot instead of a __dict__
    have = {name for name, _ in _slot_members(base)}
    names = [name for raw in _RAW for name in getattr(raw, '__slots__', ())]
    names.extend(_CUSTOM_SLOTS)
    return tuple(dict.fromkeys(n for n in names if n not in have))


def _tolerant_init(raw):
    """
    ``custom.Message.__init__`` lists the fields of the schema it was written
    for. The schema is loaded at runtime, so newer layers may add fields to
    ``raw`` it doesn't know about: those are set as plain attributes instead.
    Returns ``None`` when no wrapper is needed (the common, fast case).
    """
    params = inspect.signature(_Message.__init__).parameters
    accepted = frozenset(params)
    fields = _raw_fields(raw)
    unknown = fields - accepted
    required = [name for name, p in params.items()
                if name != 'self' and p.default is p.empty and name not in fields]
    if not unknown and not required:
        return None

    init = _Message.__init__

    def __init__(self, *args, **kwargs):
        extra = None
        if not accepted.issuperset(kwargs):
            extra = {k: kwargs.pop(k) for k in list(kwargs) if k not in accepted}
        for name in required:
            kwargs.setdefault(name, None)
        init(self, *args, **kwargs)
        if extra:
            for k, v in extra.items():
                setattr(self, k, v)

    __init__.__doc__ = init.__doc__
    return __init__


def _patch(raw):
    ns = {'__slots__': _slots(raw)}
    init = _tolerant_init(raw)
    if init is not None:
        ns['__init__'] = init
    cls = type(raw.__name__, (_Message, raw), ns)
    cls.__module__ = __name__
    cls.__qualname__ = raw.__name__
    setattr(types, raw.__name__, cls)
    alltlobjects.tlobjects[cls.CONSTRUCTOR_ID] = cls
    return cls


MessageEmpty = _patch(types.MessageEmpty)
MessageService = _patch(types.MessageService)
Message = _patch(types.Message)
