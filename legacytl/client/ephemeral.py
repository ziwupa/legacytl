import typing

from .. import utils, hints
from ..tl import types, functions
from .messages import _to_input_rich

if typing.TYPE_CHECKING:
    from .telegramclient import TelegramClient


def _iter_updates(result):
    """Yields every :tl:`Update` contained in an :tl:`Updates` result."""
    if isinstance(result, types.UpdateShort):
        yield result.update
    elif isinstance(result, (types.Updates, types.UpdatesCombined)):
        yield from result.updates


def _ephemeral_id(message):
    if isinstance(message, int):
        return message
    if isinstance(message, types.EphemeralMessage):
        return message.id
    msg = getattr(message, 'message', None)  # an events.EphemeralMessage.Event
    if isinstance(msg, types.EphemeralMessage):
        return msg.id
    return utils.get_message_id(message)


def _find_ephemeral(result, update_type):
    for update in _iter_updates(result):
        if isinstance(update, update_type):
            return update.message
    return None


class EphemeralMethods:
    """
    Bot-side ephemeral messages (``ephemeral.*``): messages visible to a
    single user of a chat only.
    """

    async def _ephemeral_common(self: 'TelegramClient', chat, receiver):
        peer = await self.get_input_entity(chat) if chat is not None else None
        receiver = utils.get_input_user(await self.get_input_entity(receiver))
        return peer, receiver

    async def _ephemeral_content(self: 'TelegramClient', message, parse_mode, formatting_entities, file):
        entities = formatting_entities
        if message is not None and formatting_entities is None:
            message, entities = await self._parse_message_text(message, parse_mode)
        media = None
        if file is not None:
            _, media, _ = await self._file_to_media(file)
            if media is None:
                raise TypeError('Cannot use {!r} as ephemeral media'.format(file))
        return message, entities or None, media

    async def send_ephemeral_message(
            self: 'TelegramClient',
            chat: 'typing.Optional[hints.EntityLike]',
            receiver: 'hints.EntityLike',
            message: str = '',
            *,
            query_id: int = None,
            reply_to: 'typing.Union[int, types.TypeInputReplyTo]' = None,
            reply_to_ephemeral: int = None,
            buttons: 'typing.Optional[hints.MarkupLike]' = None,
            parse_mode: typing.Optional[str] = (),
            formatting_entities: 'typing.Optional[typing.List[types.TypeMessageEntity]]' = None,
            file: 'typing.Optional[hints.FileLike]' = None,
            rich: 'typing.Union[str, types.TypeInputRichMessage]' = None,
            invert_media: bool = False,
            noforwards: bool = False,
            welcome: bool = False,
            anchor: bool = False
    ) -> 'typing.Optional[types.EphemeralMessage]':
        """
        Sends an ephemeral message (``ephemeral.sendMessage``) which only
        ``receiver`` will see in ``chat``. Bots only.

        Arguments
            chat (`entity` | `None`):
                The chat where the message should appear. `None` omits the
                peer (the private chat between the bot and ``receiver``).

            receiver (`entity`):
                The only user that will see the message.

            message (`str`, optional):
                The text, parsed with ``parse_mode``.

            query_id (`int`, optional):
                The query this message answers (e.g. the ``query_id`` of an
                ephemeral callback query).

            reply_to (`int` | :tl:`InputReplyTo`, optional):
                Reply to a normal message of ``chat``.

            reply_to_ephemeral (`int`, optional):
                Reply to an ephemeral message instead.

            buttons (`list`, optional):
                Same as in `send_message()`.

            parse_mode (`object`, optional):
                Same as in `send_message()`.

            formatting_entities (`list`, optional):
                Pre-built entities; disables ``parse_mode``.

            file (`file`, optional):
                Media to attach (anything `send_file()` understands).

            rich (`str` | :tl:`InputRichMessage`, optional):
                A rich message; a `str` is taken as Markdown source.

            invert_media, noforwards, welcome, anchor (`bool`, optional):
                The flags of the same name in ``ephemeral.sendMessage``.

        Returns
            The sent :tl:`EphemeralMessage`, or `None` if Telegram didn't
            echo it back.

        Example
            .. code-block:: python

                @client.on(events.EphemeralCallbackQuery)
                async def handler(event):
                    await client.send_ephemeral_message(
                        event.chat_id, event.sender_id, 'Only you can see this',
                        query_id=event.id)
        """
        peer, receiver = await self._ephemeral_common(chat, receiver)
        message, entities, media = await self._ephemeral_content(
            message, parse_mode, formatting_entities, file)

        if reply_to_ephemeral is not None:
            reply = types.InputReplyToEphemeralMessage(id=_ephemeral_id(reply_to_ephemeral))
        elif reply_to is None or getattr(reply_to, 'SUBCLASS_OF_ID', None) == 0x8c71131d:
            # 0x8c71131d == crc32(b'InputReplyTo')
            reply = reply_to
        else:
            reply = types.InputReplyToMessage(reply_to_msg_id=utils.get_message_id(reply_to))

        result = await self(functions.ephemeral.SendMessageRequest(
            peer=peer,
            receiver_id=receiver,
            query_id=query_id,
            message=message or '',
            entities=entities,
            media=media,
            reply_markup=self.build_reply_markup(buttons),
            rich_message=_to_input_rich(rich),
            reply_to=reply,
            invert_media=invert_media or None,
            noforwards=noforwards or None,
            welcome=welcome or None,
            anchor=anchor or None
        ))
        return _find_ephemeral(result, types.UpdateNewEphemeralMessage)

    async def edit_ephemeral_message(
            self: 'TelegramClient',
            chat: 'typing.Optional[hints.EntityLike]',
            receiver: 'hints.EntityLike',
            message: 'typing.Union[int, types.EphemeralMessage]',
            text: str = None,
            *,
            buttons: 'typing.Optional[hints.MarkupLike]' = None,
            parse_mode: typing.Optional[str] = (),
            formatting_entities: 'typing.Optional[typing.List[types.TypeMessageEntity]]' = None,
            file: 'typing.Optional[hints.FileLike]' = None,
            rich: 'typing.Union[str, types.TypeInputRichMessage]' = None,
            invert_media: bool = False,
            welcome: bool = False
    ) -> 'typing.Optional[types.EphemeralMessage]':
        """
        Edits an ephemeral message (``ephemeral.editMessage``). Only what
        you pass is changed.

        Arguments
            chat, receiver:
                Same as in `send_ephemeral_message()`.

            message (`int` | :tl:`EphemeralMessage`):
                The ephemeral message to edit.

            text (`str`, optional):
                New text.

        Returns
            The edited :tl:`EphemeralMessage`, if echoed back, or `None`.

        Example
            .. code-block:: python

                msg = await client.send_ephemeral_message(chat, user, 'Loading…')
                await client.edit_ephemeral_message(chat, user, msg, 'Done!')
        """
        peer, receiver = await self._ephemeral_common(chat, receiver)
        text, entities, media = await self._ephemeral_content(
            text, parse_mode, formatting_entities, file)
        result = await self(functions.ephemeral.EditMessageRequest(
            peer=peer,
            receiver_id=receiver,
            id=_ephemeral_id(message),
            message=text,
            entities=entities,
            media=media,
            reply_markup=self.build_reply_markup(buttons),
            rich_message=_to_input_rich(rich),
            invert_media=invert_media or None,
            welcome=welcome or None
        ))
        return _find_ephemeral(result, types.UpdateEditEphemeralMessage)

    async def delete_ephemeral_message(
            self: 'TelegramClient',
            chat: 'typing.Optional[hints.EntityLike]',
            receiver: 'hints.EntityLike',
            message: 'typing.Union[int, types.EphemeralMessage]'
    ) -> bool:
        """
        Deletes an ephemeral message (``ephemeral.deleteMessage``).

        Example
            .. code-block:: python

                await client.delete_ephemeral_message(chat, user, msg)
        """
        peer, receiver = await self._ephemeral_common(chat, receiver)
        return await self(functions.ephemeral.DeleteMessageRequest(
            peer=peer, receiver_id=receiver, id=_ephemeral_id(message)))
