from .common import EventBuilder, EventCommon, name_inner_event
from .. import utils
from ..tl import types
from ..tl.custom.sendergetter import SenderGetter


def _to_reaction(reaction):
    """
    Turns the friendly reaction shorthand into a :tl:`Reaction`.

    * `str` → :tl:`ReactionEmoji` (e.g. ``'👍'``).
    * `int` → :tl:`ReactionCustomEmoji` (the custom emoji document ID).
    * :tl:`Reaction` (incl. :tl:`ReactionPaid`) → passed through.
    * `None` → :tl:`ReactionEmpty`.
    """
    if reaction is None:
        return types.ReactionEmpty()
    if isinstance(reaction, bool):
        raise TypeError('Invalid reaction: {!r}'.format(reaction))
    if isinstance(reaction, str):
        return types.ReactionEmoji(emoticon=reaction)
    if isinstance(reaction, int):
        return types.ReactionCustomEmoji(document_id=reaction)
    if getattr(reaction, 'SUBCLASS_OF_ID', None) == 0x5da165a1:
        # crc32(b'Reaction')
        return reaction
    # The available reactions list hands out AvailableReaction objects
    emoticon = getattr(reaction, 'reaction', None)
    if isinstance(emoticon, str):
        return types.ReactionEmoji(emoticon=emoticon)
    raise TypeError('Invalid reaction: {!r}'.format(reaction))


def _reaction_key(reaction):
    """Hashable key for a :tl:`Reaction` (they don't define ``__eq__``)."""
    if isinstance(reaction, types.ReactionEmoji):
        return reaction.emoticon
    if isinstance(reaction, types.ReactionCustomEmoji):
        return reaction.document_id
    if isinstance(reaction, types.ReactionPaid):
        return '$'
    return None


@name_inner_event
class MessageReaction(EventBuilder):
    """
    Occurs when the reactions of a message change.

    * Users get :tl:`UpdateMessageReactions` (new totals, no actor).
    * Bots get :tl:`UpdateBotMessageReaction` (who changed what; the bot
      must be an admin to receive it) and :tl:`UpdateBotMessageReactions`
      (anonymous totals for channels).

    Args:
        reaction (`str` | `int` | :tl:`Reaction` | `list`, optional):
            Only handle events where one of these reactions was *added*
            (for actor updates) or is present (for count updates).

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.MessageReaction(reaction='👍'))
            async def handler(event):
                print(event.sender_id, 'liked', event.message_id)
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None, reaction=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        if reaction is None:
            self.reactions = None
        else:
            if not utils.is_list_like(reaction):
                reaction = [reaction]
            self.reactions = {_reaction_key(_to_reaction(r)) for r in reaction}

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateMessageReactions):
            return cls.Event(update.peer, update.msg_id, counts=update.reactions.results,
                             reactions=update.reactions, top_msg_id=update.top_msg_id)
        elif isinstance(update, types.UpdateBotMessageReaction):
            return cls.Event(update.peer, update.msg_id, actor=update.actor, date=update.date,
                             old=update.old_reactions, new=update.new_reactions)
        elif isinstance(update, types.UpdateBotMessageReactions):
            return cls.Event(update.peer, update.msg_id, counts=update.reactions,
                             date=update.date)

    def filter(self, event):
        if self.reactions is not None:
            if event.actor is not None:
                keys = {_reaction_key(r) for r in event.added}
            else:
                keys = {_reaction_key(c.reaction) for c in event.counts or ()}
            if not keys & self.reactions:
                return
        return super().filter(event)

    class Event(EventCommon, SenderGetter):
        """
        Represents a change in the reactions of a message.

        Members:
            message_id (`int`):
                The message whose reactions changed.

            actor (:tl:`Peer`, optional):
                Who changed their reaction (bot updates only). Also
                available through `sender` / `get_sender()`.

            old_reactions, new_reactions (`list` of :tl:`Reaction`):
                Previous and current reactions of ``actor`` (bot updates only).

            counts (`list` of :tl:`ReactionCount`, optional):
                Totals per reaction (user updates and anonymous bot updates).

            reactions (:tl:`MessageReactions`, optional):
                The full reactions object (user updates only).

            top_msg_id (`int`, optional):
                Forum topic of the message, when known.

            date (`datetime`, optional):
                When the change happened (bot updates only).
        """
        def __init__(self, peer, msg_id, *, actor=None, date=None, old=None, new=None,
                     counts=None, reactions=None, top_msg_id=None):
            super().__init__(chat_peer=peer, msg_id=msg_id)
            SenderGetter.__init__(self, utils.get_peer_id(actor) if actor else None)
            self.message_id = msg_id
            self.actor = actor
            self.date = date
            self.old_reactions = old or []
            self.new_reactions = new or []
            self.counts = counts
            self.reactions = reactions
            self.top_msg_id = top_msg_id
            self._message = None

        def _set_client(self, client):
            super()._set_client(client)
            if self._sender_id:
                self._sender, self._input_sender = utils._get_entity_pair(
                    self._sender_id, self._entities, client._mb_entity_cache)

        @property
        def added(self):
            """Reactions present in ``new_reactions`` but not in ``old_reactions``."""
            old = {_reaction_key(r) for r in self.old_reactions}
            return [r for r in self.new_reactions if _reaction_key(r) not in old]

        @property
        def removed(self):
            """Reactions present in ``old_reactions`` but not in ``new_reactions``."""
            new = {_reaction_key(r) for r in self.new_reactions}
            return [r for r in self.old_reactions if _reaction_key(r) not in new]

        @property
        def emoticons(self):
            """The plain emoji of ``new_reactions`` (custom emoji are skipped)."""
            return [r.emoticon for r in self.new_reactions if isinstance(r, types.ReactionEmoji)]

        async def get_message(self):
            """Fetches the message whose reactions changed."""
            if self._message is None:
                self._message = await self._client.get_messages(
                    await self.get_input_chat(), ids=self.message_id)
            return self._message

        async def react(self, reaction=None, **kwargs):
            """Sets *your* reaction on the same message (`send_reaction()`)."""
            return await self._client.send_reaction(
                await self.get_input_chat(), self.message_id, reaction, **kwargs)
