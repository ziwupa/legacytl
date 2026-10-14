import re

from .common import EventBuilder, EventCommon, name_inner_event
from .. import utils
from ..tl import types, functions
from ..tl.custom.sendergetter import SenderGetter


def _ephemeral_chat(msg):
    """Chat where an :tl:`EphemeralMessage` is shown."""
    if msg.peer_id is not None:
        return msg.peer_id
    # No peer: the private chat between the bot and the receiver
    if msg.out:
        return types.PeerUser(msg.receiver_id)
    return msg.from_id


def _compile_pattern(pattern):
    if pattern is None or callable(pattern):
        return pattern
    if isinstance(pattern, str):
        return re.compile(pattern).match
    if hasattr(pattern, 'match') and callable(pattern.match):
        return pattern.match
    raise TypeError('Invalid pattern type given')


@name_inner_event
class EphemeralMessage(EventBuilder):
    """
    Occurs when an ephemeral message (visible to a single user) is sent,
    edited or deleted (:tl:`UpdateNewEphemeralMessage`,
    :tl:`UpdateEditEphemeralMessage`, :tl:`UpdateDeleteEphemeralMessages`).

    Args:
        new, edited, deleted (`bool`, optional):
            Restrict to these kinds. By default all of them are handled.

        pattern (`str` | `callable` | `Pattern`, optional):
            Only messages whose text matches (never matches deletions).

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.EphemeralMessage(edited=False, deleted=False))
            async def handler(event):
                print('Only', event.receiver_id, 'sees:', event.text)
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None,
                 new=None, edited=None, deleted=None, pattern=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        if new is None and edited is None and deleted is None:
            new = edited = deleted = True
        self.new = bool(new)
        self.edited = bool(edited)
        self.deleted = bool(deleted)
        self.pattern = _compile_pattern(pattern)

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateNewEphemeralMessage):
            return cls.Event(update.message, kind='new')
        elif isinstance(update, types.UpdateEditEphemeralMessage):
            return cls.Event(update.message, kind='edited')
        elif isinstance(update, types.UpdateDeleteEphemeralMessages):
            return cls.Event(None, kind='deleted', peer=update.peer, ids=update.ids)

    def filter(self, event):
        if not getattr(self, event.kind):
            return
        if self.pattern:
            if event.message is None:
                return
            match = self.pattern(event.text or '')
            if not match:
                return
            event.pattern_match = match
        return super().filter(event)

    class Event(EventCommon, SenderGetter):
        """
        Represents an ephemeral message event.

        Members:
            kind (`str`):
                ``'new'``, ``'edited'`` or ``'deleted'``.

            message (:tl:`EphemeralMessage`, optional):
                The message (`None` for deletions).

            deleted_ids (`list`):
                The deleted message IDs (deletions only).

            pattern_match (`obj`):
                Result of the ``pattern`` filter, if any.
        """
        def __init__(self, message, *, kind, peer=None, ids=None):
            if message is not None:
                peer = _ephemeral_chat(message)
                msg_id = message.id
            else:
                msg_id = (ids or [None])[0]
            super().__init__(chat_peer=peer, msg_id=msg_id)
            SenderGetter.__init__(self, utils.get_peer_id(message.from_id) if message else None)
            self.kind = kind
            self.message = message
            self.deleted_ids = list(ids or [])
            self.pattern_match = None

        def _set_client(self, client):
            super()._set_client(client)
            if self._sender_id:
                self._sender, self._input_sender = utils._get_entity_pair(
                    self._sender_id, self._entities, client._mb_entity_cache)

        @property
        def new(self):
            return self.kind == 'new'

        @property
        def edited(self):
            return self.kind == 'edited'

        @property
        def deleted(self):
            return self.kind == 'deleted'

        @property
        def id(self):
            return self._message_id

        @property
        def text(self):
            """The raw text of the message (`None` for deletions)."""
            return self.message.message if self.message else None

        @property
        def receiver_id(self):
            """ID of the only user who can see the message."""
            return self.message.receiver_id if self.message else None

        @property
        def media(self):
            return self.message.media if self.message else None

        @property
        def reply_markup(self):
            return self.message.reply_markup if self.message else None

        def _target(self):
            peer = self.message.peer_id if self.message else None
            return peer, self.message.receiver_id

        async def edit(self, *args, **kwargs):
            """Edits this message (`edit_ephemeral_message()`; bots only)."""
            peer, receiver = self._target()
            return await self._client.edit_ephemeral_message(
                peer, receiver, self.message.id, *args, **kwargs)

        async def delete(self):
            """Deletes this message (`delete_ephemeral_message()`; bots only)."""
            peer, receiver = self._target()
            return await self._client.delete_ephemeral_message(peer, receiver, self.message.id)


@name_inner_event
class EphemeralCallbackQuery(EventBuilder):
    """
    Occurs when a user presses an inline button of an ephemeral message
    (:tl:`UpdateEphemeralBotCallbackQuery`). Works like `CallbackQuery`.

    Args:
        data (`bytes` | `str` | `callable` | `Pattern`, optional):
            Same as in `CallbackQuery`.

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.EphemeralCallbackQuery(data=b'ok'))
            async def handler(event):
                await event.answer('Thanks!')
                await event.edit('Confirmed ✅')
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None, data=None, pattern=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        if data is not None and pattern is not None:
            raise ValueError('Only pass either data or pattern not both.')
        match = data if data is not None else pattern
        if isinstance(match, str):
            match = match.encode('utf-8')
        if data is not None and isinstance(match, bytes):
            self.match = match
        elif isinstance(match, bytes):
            self.match = re.compile(match).match
        elif match is None or callable(match):
            self.match = match
        elif hasattr(match, 'match') and callable(match.match):
            if not isinstance(getattr(match, 'pattern', b''), bytes):
                match = re.compile(match.pattern.encode('utf-8'), match.flags & ~re.UNICODE)
            self.match = match.match
        else:
            raise TypeError('Invalid data or pattern type given')

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateEphemeralBotCallbackQuery):
            peer = update.peer or (
                _ephemeral_chat(update.message) if update.message else types.PeerUser(update.user_id))
            return cls.Event(update, peer)

    def filter(self, event):
        if self.match is not None:
            if callable(self.match):
                event.data_match = event.pattern_match = self.match(event.data)
                if not event.data_match:
                    return
            elif event.data != self.match:
                return
        return super().filter(event)

    class Event(EventCommon, SenderGetter):
        """
        Represents an ephemeral callback query.

        Members:
            query (:tl:`UpdateEphemeralBotCallbackQuery`):
                The raw update.

            message (:tl:`EphemeralMessage`):
                The ephemeral message whose button was pressed.

            data_match, pattern_match (`obj`):
                Result of the ``data``/``pattern`` filter, if callable.
        """
        def __init__(self, query, peer):
            super().__init__(chat_peer=peer, msg_id=query.msg_id)
            SenderGetter.__init__(self, query.user_id)
            self.query = query
            self.message = query.message
            self.data_match = None
            self.pattern_match = None
            self._answered = False

        def _set_client(self, client):
            super()._set_client(client)
            self._sender, self._input_sender = utils._get_entity_pair(
                self.sender_id, self._entities, client._mb_entity_cache)

        @property
        def id(self):
            """The query ID (use it as ``query_id`` when answering with a message)."""
            return self.query.query_id

        @property
        def message_id(self):
            return self.query.msg_id

        @property
        def data(self):
            return self.query.data

        @property
        def chat_instance(self):
            return self.query.chat_instance

        async def answer(self, message=None, cache_time=0, *, url=None, alert=False):
            """
            Answers the query (stops the loading animation), like
            `CallbackQuery.Event.answer`.
            """
            if self._answered:
                return
            self._answered = True
            return await self._client(functions.messages.SetBotCallbackAnswerRequest(
                query_id=self.query.query_id,
                cache_time=cache_time,
                alert=alert,
                message=message,
                url=url
            ))

        async def respond(self, *args, **kwargs):
            """
            Sends a new ephemeral message to the same user in the same chat,
            answering this query (`send_ephemeral_message()` with
            ``query_id`` set).
            """
            kwargs.setdefault('query_id', self.query.query_id)
            return await self._client.send_ephemeral_message(
                self.query.peer, self.query.user_id, *args, **kwargs)

        async def edit(self, *args, **kwargs):
            """Edits the ephemeral message whose button was pressed."""
            self._client.loop.create_task(self.answer())
            return await self._client.edit_ephemeral_message(
                self.query.peer, self.query.user_id, self.query.msg_id, *args, **kwargs)

        async def delete(self):
            """Deletes the ephemeral message whose button was pressed."""
            self._client.loop.create_task(self.answer())
            return await self._client.delete_ephemeral_message(
                self.query.peer, self.query.user_id, self.query.msg_id)
