from .common import EventBuilder, EventCommon, name_inner_event
from .. import utils
from ..tl import types
from ..tl.custom.sendergetter import SenderGetter

_ADMIN = ('administrator', 'creator')


def member_status(participant):
    """
    Bot API-like status of a :tl:`ChannelParticipant` / :tl:`ChatParticipant`
    (or `None`): ``'creator'``, ``'administrator'``, ``'member'``,
    ``'restricted'``, ``'left'`` or ``'kicked'``.
    """
    if participant is None or isinstance(participant, types.ChannelParticipantLeft):
        return 'left'
    if isinstance(participant, (types.ChannelParticipantCreator, types.ChatParticipantCreator)):
        return 'creator'
    if isinstance(participant, (types.ChannelParticipantAdmin, types.ChatParticipantAdmin)):
        return 'administrator'
    if isinstance(participant, types.ChannelParticipantBanned):
        rights = participant.banned_rights
        if rights is not None and rights.view_messages:
            return 'kicked'
        return 'restricted'
    return 'member'


def _is_member(participant):
    status = member_status(participant)
    if status == 'restricted':
        return not participant.left
    return status in ('creator', 'administrator', 'member')


@name_inner_event
class ChatMember(EventBuilder):
    """
    Occurs when the membership/rights of a user in a group or channel
    change (:tl:`UpdateChannelParticipant`, :tl:`UpdateChatParticipant`).

    Unlike `ChatAction`, this is built from the participant diff, so it
    sees promotions, demotions and restrictions and works where service
    messages are hidden. Bots get these for chats they administer; user
    accounts get them for their own membership changes.

    Args:
        joined, left, kicked, promoted, demoted, restricted (`bool`, optional):
            Only handle events that match *any* of the given kinds.

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.ChatMember(joined=True))
            async def welcome(event):
                await client.send_message(event.chat_id, 'Welcome!')

            @client.on(events.ChatMember(promoted=True, demoted=True))
            async def admins(event):
                print(event.user_id, event.old_status, '→', event.new_status)
    """
    _KINDS = ('joined', 'left', 'kicked', 'promoted', 'demoted', 'restricted')

    def __init__(self, chats=None, *, blacklist_chats=False, func=None, joined=None,
                 left=None, kicked=None, promoted=None, demoted=None, restricted=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        given = dict(joined=joined, left=left, kicked=kicked, promoted=promoted,
                     demoted=demoted, restricted=restricted)
        self.kinds = None if all(v is None for v in given.values()) \
            else {k for k, v in given.items() if v}

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateChannelParticipant):
            return cls.Event(types.PeerChannel(update.channel_id), update)
        elif isinstance(update, types.UpdateChatParticipant):
            return cls.Event(types.PeerChat(update.chat_id), update)

    def filter(self, event):
        if self.kinds is not None and not any(getattr(event, k) for k in self.kinds):
            return
        return super().filter(event)

    class Event(EventCommon, SenderGetter):
        """
        Members:
            user_id (`int`):
                The user whose membership changed (`user` / `get_user()`).

            actor_id (`int`):
                Who made the change (equals ``user_id`` for self-joins/leaves;
                also available as `sender` / `get_sender()`).

            prev_participant, new_participant (optional):
                Raw :tl:`ChannelParticipant` / :tl:`ChatParticipant` before
                and after (`None` meaning "not in the chat").

            old_status, new_status (`str`):
                Their Bot API-like status (see `member_status`).

            invite (:tl:`ExportedChatInvite`, optional):
                The invite link used to join, if any.

            date (`datetime`):
                When it happened.
        """
        def __init__(self, peer, update):
            super().__init__(chat_peer=peer)
            SenderGetter.__init__(self, update.actor_id)
            self.user_id = update.user_id
            self.actor_id = update.actor_id
            self.date = update.date
            self.prev_participant = update.prev_participant
            self.new_participant = update.new_participant
            self.invite = update.invite
            self.via_chatlist = bool(getattr(update, 'via_chatlist', False))
            self.old_status = member_status(update.prev_participant)
            self.new_status = member_status(update.new_participant)
            self._user = self._input_user = None

        def _set_client(self, client):
            super()._set_client(client)
            self._sender, self._input_sender = utils._get_entity_pair(
                self.sender_id, self._entities, client._mb_entity_cache)
            self._user, self._input_user = utils._get_entity_pair(
                self.user_id, self._entities, client._mb_entity_cache)

        @property
        def was_member(self):
            return _is_member(self.prev_participant)

        @property
        def is_member(self):
            return _is_member(self.new_participant)

        @property
        def joined(self):
            """The user joined (or was added to) the chat."""
            return not self.was_member and self.is_member

        @property
        def added_by(self):
            """ID of whoever added the user, if it wasn't a self-join."""
            if self.joined and self.actor_id != self.user_id:
                return self.actor_id
            return None

        @property
        def kicked(self):
            """The user was removed or banned by someone else."""
            if self.new_status == 'kicked':
                return True
            return self.was_member and not self.is_member and self.actor_id != self.user_id

        @property
        def left(self):
            """The user left on their own."""
            return self.was_member and not self.is_member and not self.kicked

        @property
        def unbanned(self):
            """The user was removed from the ban list."""
            return self.old_status == 'kicked' and self.new_status != 'kicked'

        @property
        def promoted(self):
            """The user became an admin (or their admin rights changed)."""
            if self.new_status not in _ADMIN:
                return False
            if self.old_status not in _ADMIN:
                return True
            old = getattr(self.prev_participant, 'admin_rights', None)
            new = getattr(self.new_participant, 'admin_rights', None)
            return (old.to_dict() if old else None) != (new.to_dict() if new else None)

        @property
        def demoted(self):
            """The user stopped being an admin."""
            return self.old_status in _ADMIN and self.new_status not in _ADMIN

        @property
        def restricted(self):
            """The user is now restricted (but not banned)."""
            return self.new_status == 'restricted'

        @property
        def user(self):
            return self._user

        @property
        def input_user(self):
            return self._input_user

        async def get_user(self):
            if self._user is None and self._client:
                try:
                    self._user = await self._client.get_entity(self.user_id)
                except ValueError:
                    pass
            return self._user

        async def get_input_user(self):
            if self._input_user is None and self._client:
                try:
                    self._input_user = await self._client.get_input_entity(self.user_id)
                except ValueError:
                    pass
            return self._input_user
