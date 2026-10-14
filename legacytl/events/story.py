from .common import EventBuilder, EventCommon, name_inner_event
from ..tl import types


@name_inner_event
class Story(EventBuilder):
    """
    Occurs on story activity:

    * A story is posted, edited or deleted (:tl:`UpdateStory`).
    * Stories of a peer were marked as read (:tl:`UpdateReadStories`).
    * A reaction to a story was sent (:tl:`UpdateSentStoryReaction`,
      :tl:`UpdateNewStoryReaction`).

    Args:
        new, deleted, read, reaction (`bool`, optional):
            Restrict to these kinds (``new`` also covers edits, since
            Telegram uses the same update). By default all are handled.

    Example
        .. code-block:: python

            from legacytl import events

            @client.on(events.Story(read=False, reaction=False))
            async def handler(event):
                if event.deleted:
                    print('story', event.story_id, 'deleted in', event.chat_id)
                else:
                    print('new story from', event.chat_id, event.story.caption)
    """
    def __init__(self, chats=None, *, blacklist_chats=False, func=None,
                 new=None, deleted=None, read=None, reaction=None):
        super().__init__(chats, blacklist_chats=blacklist_chats, func=func)
        if all(x is None for x in (new, deleted, read, reaction)):
            new = deleted = read = reaction = True
        self.kinds = {k for k, v in (('new', new), ('deleted', deleted),
                                     ('read', read), ('reaction', reaction)) if v}

    @classmethod
    def build(cls, update, others=None, self_id=None):
        if isinstance(update, types.UpdateStory):
            kind = 'deleted' if isinstance(update.story, types.StoryItemDeleted) else 'new'
            return cls.Event(update.peer, kind, story=update.story, story_id=update.story.id)
        elif isinstance(update, types.UpdateReadStories):
            return cls.Event(update.peer, 'read', max_id=update.max_id)
        elif isinstance(update, (types.UpdateSentStoryReaction, types.UpdateNewStoryReaction)):
            return cls.Event(update.peer, 'reaction', story_id=update.story_id,
                             reaction=update.reaction,
                             outgoing=isinstance(update, types.UpdateSentStoryReaction))

    def filter(self, event):
        if event.kind not in self.kinds:
            return
        return super().filter(event)

    class Event(EventCommon):
        """
        Members:
            kind (`str`):
                ``'new'``, ``'deleted'``, ``'read'`` or ``'reaction'``.

            story (:tl:`StoryItem`, optional):
                The story (``new``/``deleted``; may be :tl:`StoryItemSkipped`).

            story_id (`int`, optional):
                ID of the story.

            max_id (`int`, optional):
                Read up to this story (``read`` only).

            reaction (:tl:`Reaction`, optional):
                The reaction (``reaction`` only).

            outgoing (`bool`):
                For reactions: `True` if *you* reacted (from another session).
        """
        def __init__(self, peer, kind, *, story=None, story_id=None, max_id=None,
                     reaction=None, outgoing=False):
            super().__init__(chat_peer=peer, msg_id=story_id)
            self.kind = kind
            self.story = story
            self.story_id = story_id
            self.max_id = max_id
            self.reaction = reaction
            self.outgoing = outgoing

        @property
        def new(self):
            return self.kind == 'new'

        @property
        def deleted(self):
            return self.kind == 'deleted'

        @property
        def read(self):
            return self.kind == 'read'

        @property
        def reacted(self):
            return self.kind == 'reaction'

        async def mark_read(self):
            """Marks the stories of this peer as read up to this story."""
            return await self._client.read_stories(
                await self.get_input_chat(), self.max_id or self.story_id)

        async def react(self, reaction='❤', **kwargs):
            """Reacts to this story (`react_story()`)."""
            return await self._client.react_story(
                await self.get_input_chat(), self.story_id, reaction, **kwargs)
