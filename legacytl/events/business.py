import re

from .common import EventBuilder, EventCommon, name_inner_event
from .. import utils
from ..tl import types, functions
from ..tl.custom.sendergetter import SenderGetter
def _iter_updates(result):
    """Yields every :tl:`Update` contained in an :tl:`Updates` result."""
    if isinstance(result, types.UpdateShort):
        yield result.update
    elif isinstance(result, (types.Updates, types.UpdatesCombined)):
        yield from result.updates


def _result_entities(result):
    """``{marked_id: entity}`` for the users and chats in ``result``."""
    entities = {}
    for entity in (getattr(result, 'users', None) or ()):
        try:
            entities[utils.get_peer_id(entity)] = entity
        except TypeError:
            continue
    for entity in (getattr(result, 'chats', None) or ()):
        try:
            entities[utils.get_peer_id(entity)] = entity
        except TypeError:
            continue
    return entities


def _first_message(client, result, input_chat=None):
    """
    Returns the first new (or edited) message found in an :tl:`Updates`
    result, already initialized as a custom `Message`, or `None`.
    """
    entities = _result_entities(result)
    found = None
    for update in _iter_updates(result):
        if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage,
                               types.UpdateEditMessage, types.UpdateEditChannelMessage)):
            found = update.message
            if isinstance(update, (types.UpdateNewMessage, types.UpdateNewChannelMessage)):
                break
    if found is not None:
        found._finish_init(client, entities, input_chat)
    return found


async def _invoke_business(client, connection_id, entity, message, *, reply_to=None,
                           buttons=None, parse_mode=(), formatting_entities=None,
                           link_preview=True, silent=None):
    """Sends a text message on behalf of a business account."""
    if formatting_entities is None:
        message, formatting_entities = await client._parse_message_text(message, parse_mode)
    request = functions.messages.SendMessageRequest(
        peer=entity,
        message=message,
        entities=formatting_entities or None,
        no_webpage=not link_preview,
        reply_to=None if reply_to is None else types.InputReplyToMessage(
            reply_to_msg_id=utils.get_message_id(reply_to)),
        reply_markup=client.build_reply_markup(buttons),
        silent=silent
    )
    result = await client(functions.InvokeWithBusinessConnectionRequest(
        connection_id=connection_id, query=request))
    # Business sends come back as updateBotNewBusinessMessage, not updateNewMessage
    entities = _result_entities(result)
    for update in _iter_updates(result):
        if isinstance(update, types.UpdateBotNewBusinessMessage):
            update.message._finish_init(client, entities, entity)
            return update.message
    return _first_message(client, result, entity)


class _BusinessMessageMixin:
    async def respond(self, message, **kwargs):
        """
        Sends a message to the same chat on behalf of the business account
        (``invokeWithBusinessConnection`` + ``messages.sendMessage``).

        Accepts ``reply_to``, ``buttons``, ``parse_mode``,
        ``formatting_entities``, ``link_preview`` and ``silent``.
        """
        return await _invoke_business(
            self._client, self.connection_id, await self.get_input_chat(), message, **kwargs)

    async def reply(self, message, **kwargs):
        """Like `respond`, but replying to the message."""
        kwargs['reply_to'] = self._message_id
        return await self.respond(message, **kwargs)


@name_inner_event
class BusinessMessage(EventBuilder):
    """
    Occurs for messages of a connected business account, received by the
    bot (:tl:`UpdateBotNewBusinessMessage`, :tl:`UpdateBotEditBusinessMessage`,
    :tl:`UpdateBotDeleteBusinessMessage`).

    Args:
        new, edited, deleted (`bool`, optional):
            Restrict to these kinds. By default all of them are handled.

        connection_id (`str` | `list`, optional):
            Only these business connections.

        pattern (`str` | `callable` | `Pattern`, optional):
            Only messages whose text matches (never matches deletions).

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.BusinessMessage(edited=False, deleted=False, pattern='(?i)price'))
            async def handler(event):
                if not event.message.out:
                    await event.reply('Our prices: …')
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None,
                 new=None, edited=None, deleted=None, connection_id=None, pattern=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        if new is None and edited is None and deleted is None:
            new = edited = deleted = True
        self.new = bool(new)
        self.edited = bool(edited)
        self.deleted = bool(deleted)
        if connection_id is not None and not utils.is_list_like(connection_id):
            connection_id = [connection_id]
        self.connection_ids = None if connection_id is None else set(connection_id)
        if isinstance(pattern, str):
            pattern = re.compile(pattern).match
        elif pattern is not None and not callable(pattern):
            pattern = pattern.match
        self.pattern = pattern

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateBotNewBusinessMessage):
            return cls.Event('new', update.connection_id, message=update.message,
                             reply_to_message=update.reply_to_message)
        elif isinstance(update, types.UpdateBotEditBusinessMessage):
            return cls.Event('edited', update.connection_id, message=update.message,
                             reply_to_message=update.reply_to_message)
        elif isinstance(update, types.UpdateBotDeleteBusinessMessage):
            return cls.Event('deleted', update.connection_id, peer=update.peer,
                             ids=update.messages)

    def filter(self, event):
        if not getattr(self, event.kind):
            return
        if self.connection_ids is not None and event.connection_id not in self.connection_ids:
            return
        if self.pattern:
            if event.message is None:
                return
            match = self.pattern(event.message.message or '')
            if not match:
                return
            event.pattern_match = match
        return super().filter(event)

    class Event(_BusinessMessageMixin, EventCommon, SenderGetter):
        """
        Represents a business message event.

        Members:
            kind (`str`):
                ``'new'``, ``'edited'`` or ``'deleted'``.

            connection_id (`str`):
                The business connection the message belongs to.

            message (`Message <legacytl.tl.custom.message.Message>`, optional):
                The message (`None` for deletions). Note that methods like
                ``message.reply()`` would act as the *bot*; use the event's
                `respond`/`reply` to act as the business account.

            reply_to_message (`Message`, optional):
                The message being replied to, if Telegram included it.

            deleted_ids (`list`):
                The deleted message IDs (deletions only).
        """
        def __init__(self, kind, connection_id, *, message=None, reply_to_message=None,
                     peer=None, ids=None):
            if message is not None:
                peer = message.peer_id
                msg_id = message.id
            else:
                msg_id = (ids or [None])[0]
            super().__init__(chat_peer=peer, msg_id=msg_id)
            SenderGetter.__init__(self, getattr(message, 'sender_id', None) if message else None)
            self.kind = kind
            self.connection_id = connection_id
            self.message = message
            self.reply_to_message = reply_to_message
            self.deleted_ids = list(ids or [])
            self.pattern_match = None

        def _set_client(self, client):
            super()._set_client(client)
            for m in (self.message, self.reply_to_message):
                if isinstance(m, (types.Message, types.MessageService)):
                    m._finish_init(client, self._entities, None)
            if self.message is not None:
                self._sender_id = self.message.sender_id
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
        def text(self):
            return self.message.text if self.message is not None else None

        @property
        def raw_text(self):
            return self.message.raw_text if self.message is not None else None


@name_inner_event
class BusinessConnection(EventBuilder):
    """
    Occurs when a business account connects, changes or disconnects the
    bot (:tl:`UpdateBotBusinessConnect`).

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.BusinessConnection)
            async def handler(event):
                state = 'disabled' if event.disabled else 'enabled'
                print(event.user_id, state, event.connection_id)
    """
    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateBotBusinessConnect):
            return cls.Event(update.connection)

    class Event(EventCommon, SenderGetter):
        """
        Members:
            connection (:tl:`BotBusinessConnection`):
                The raw connection (``connection_id``, ``user_id``,
                ``dc_id``, ``date``, ``rights``, ``disabled``).
        """
        def __init__(self, connection):
            super().__init__(chat_peer=types.PeerUser(connection.user_id))
            SenderGetter.__init__(self, connection.user_id)
            self.connection = connection

        def _set_client(self, client):
            super()._set_client(client)
            self._sender, self._input_sender = utils._get_entity_pair(
                self.sender_id, self._entities, client._mb_entity_cache)

        @property
        def connection_id(self):
            return self.connection.connection_id

        @property
        def user_id(self):
            return self.connection.user_id

        @property
        def disabled(self):
            return bool(self.connection.disabled)

        @property
        def rights(self):
            return self.connection.rights


@name_inner_event
class BusinessCallbackQuery(EventBuilder):
    """
    Occurs when a user presses an inline button of a message sent by the
    bot on behalf of a business account (:tl:`UpdateBusinessBotCallbackQuery`).

    Example
        .. code-block:: python

            @client.on(events.BusinessCallbackQuery(data=b'yes'))
            async def handler(event):
                await event.answer('Got it')
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None, data=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        if isinstance(data, str):
            data = data.encode('utf-8')
        if hasattr(data, 'match') and not isinstance(data, bytes):
            data = data.match
        self.data = data

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateBusinessBotCallbackQuery):
            return cls.Event(update)

    def filter(self, event):
        if self.data is not None:
            if callable(self.data):
                event.data_match = self.data(event.data or b'')
                if not event.data_match:
                    return
            elif event.data != self.data:
                return
        return super().filter(event)

    class Event(_BusinessMessageMixin, EventCommon, SenderGetter):
        """
        Members:
            query (:tl:`UpdateBusinessBotCallbackQuery`):
                The raw update.

            message (`Message`):
                The message whose button was pressed.
        """
        def __init__(self, query):
            super().__init__(chat_peer=query.message.peer_id, msg_id=query.message.id)
            SenderGetter.__init__(self, query.user_id)
            self.query = query
            self.message = query.message
            self.data_match = None
            self._answered = False

        def _set_client(self, client):
            super()._set_client(client)
            if isinstance(self.message, (types.Message, types.MessageService)):
                self.message._finish_init(client, self._entities, None)
            self._sender, self._input_sender = utils._get_entity_pair(
                self.sender_id, self._entities, client._mb_entity_cache)

        @property
        def id(self):
            return self.query.query_id

        @property
        def connection_id(self):
            return self.query.connection_id

        @property
        def data(self):
            return self.query.data

        async def answer(self, message=None, cache_time=0, *, url=None, alert=False):
            """Answers the query, like `CallbackQuery.Event.answer`."""
            if self._answered:
                return
            self._answered = True
            return await self._client(functions.messages.SetBotCallbackAnswerRequest(
                query_id=self.query.query_id, cache_time=cache_time,
                alert=alert, message=message, url=url))
