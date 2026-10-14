from .common import EventBuilder, EventCommon, name_inner_event
from .. import utils
from ..tl import types
from ..tl.custom.sendergetter import SenderGetter


def _poll_ids(poll_id):
    if poll_id is None:
        return None
    if not utils.is_list_like(poll_id):
        poll_id = [poll_id]
    return {getattr(p, 'id', p) for p in poll_id}


@name_inner_event
class PollUpdate(EventBuilder):
    """
    Occurs when a poll changes: new results, or the poll got closed/edited
    (:tl:`UpdateMessagePoll`).

    Note that Telegram often omits the chat (``chat_id`` may be `None`),
    so prefer ``poll_id=`` over ``chats=`` to filter.

    Args:
        poll_id (`int` | `list`, optional):
            Only these polls (:tl:`Poll` objects are accepted too).

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.PollUpdate)
            async def handler(event):
                print(event.poll_id, event.total_voters, event.closed)
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None, poll_id=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        self.poll_ids = _poll_ids(poll_id)

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateMessagePoll):
            return cls.Event(update)

    def filter(self, event):
        if self.poll_ids is not None and event.poll_id not in self.poll_ids:
            return
        return super().filter(event)

    class Event(EventCommon):
        """
        Members:
            poll_id (`int`):
                The poll ID.

            poll (:tl:`Poll`, optional):
                The poll itself (only when it changed, e.g. closed).

            results (:tl:`PollResults`):
                The current results.

            message_id (`int`, optional):
                The message containing the poll, when known.

            top_msg_id (`int`, optional):
                Forum topic, when known.
        """
        def __init__(self, update):
            super().__init__(chat_peer=update.peer, msg_id=update.msg_id)
            self.poll_id = update.poll_id
            self.poll = update.poll
            self.results = update.results
            self.message_id = update.msg_id
            self.top_msg_id = update.top_msg_id

        @property
        def closed(self):
            """`True` if the poll is known to be closed."""
            return bool(self.poll and self.poll.closed)

        @property
        def total_voters(self):
            return getattr(self.results, 'total_voters', None)


@name_inner_event
class PollVote(EventBuilder):
    """
    Occurs when someone votes (or retracts the vote) in a non-anonymous
    poll sent by the bot (:tl:`UpdateMessagePollVote`).

    The update carries no chat; the ``chats=`` filter is matched against
    the *voter* instead.

    Args:
        poll_id (`int` | `list`, optional):
            Only these polls.

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.PollVote)
            async def handler(event):
                if event.retracted:
                    print(event.voter_id, 'retracted their vote')
                else:
                    print(event.voter_id, 'chose', event.positions)
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None, poll_id=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        self.poll_ids = _poll_ids(poll_id)

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateMessagePollVote):
            return cls.Event(update)

    def filter(self, event):
        if self.poll_ids is not None and event.poll_id not in self.poll_ids:
            return
        return super().filter(event)

    class Event(EventCommon, SenderGetter):
        """
        Members:
            poll_id (`int`):
                The poll ID.

            voter (:tl:`Peer`):
                Who voted (also through `sender` / `get_sender()`).

            options (`list` of `bytes`):
                The chosen options (empty if the vote was retracted).

            positions (`list` of `int`):
                Indices of the chosen options.
        """
        def __init__(self, update):
            # The voter is the only peer we have; use it as "chat" for filtering
            super().__init__(chat_peer=update.peer)
            SenderGetter.__init__(self, utils.get_peer_id(update.peer))
            self.poll_id = update.poll_id
            self.voter = update.peer
            self.options = update.options
            self.positions = update.positions

        def _set_client(self, client):
            super()._set_client(client)
            self._sender, self._input_sender = utils._get_entity_pair(
                self.sender_id, self._entities, client._mb_entity_cache)

        @property
        def voter_id(self):
            return self.sender_id

        @property
        def retracted(self):
            """`True` if the voter took their vote back."""
            return not self.options
