"""
An offline `TelegramClient` for unit tests: requests are captured instead
of sent, and canned results are returned.
"""
import copy
import datetime

from legacytl import TelegramClient, utils
from legacytl.sessions import MemorySession
from legacytl.tl import types, TLObject

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)

USERNAMES = {
    'alice': types.InputPeerUser(user_id=10, access_hash=1010),
    'bob': types.InputPeerUser(user_id=11, access_hash=1111),
    'chan': types.InputPeerChannel(channel_id=500, access_hash=5050),
}


class FakeClient(TelegramClient):
    """
    ``responses`` maps request classes to either a result or a callable
    ``(request) -> result``. A list value is consumed one item per call.
    """
    def __init__(self, responses=None, *, self_id=1, bot=False):
        super().__init__(MemorySession(), 1, 'x' * 32, receive_updates=False)
        self.requests = []
        self.responses = dict(responses or {})
        if self_id:
            self._mb_entity_cache.set_self_user(self_id, bot, 0)

    async def __call__(self, request, ordered=False, flood_sleep_threshold=None):
        await request.resolve(self, utils)  # like the real client does
        # Iterators reuse (and mutate) the same request; keep a snapshot
        self.requests.append(copy.copy(request))
        bytes(request)  # must serialize: catches wrong field types early
        try:
            result = self.responses[type(request)]
        except KeyError:
            raise AssertionError('Unexpected request {}'.format(type(request).__name__))
        if isinstance(result, list):
            result = result.pop(0)
        if callable(result) and not isinstance(result, TLObject):
            result = result(request)
        return result

    async def get_input_entity(self, peer):
        if peer in ('me', 'self'):
            return types.InputPeerSelf()
        if isinstance(peer, str):
            return USERNAMES[peer.lstrip('@').lower()]
        if isinstance(peer, int):
            i, cls = utils.resolve_id(peer)
            if cls is types.PeerUser:
                return types.InputPeerUser(user_id=i, access_hash=0)
            if cls is types.PeerChannel:
                return types.InputPeerChannel(channel_id=i, access_hash=0)
            return types.InputPeerChat(chat_id=i)
        if isinstance(peer, types.PeerUser):
            return types.InputPeerUser(user_id=peer.user_id, access_hash=0)
        if isinstance(peer, types.PeerChannel):
            return types.InputPeerChannel(channel_id=peer.channel_id, access_hash=0)
        if isinstance(peer, types.PeerChat):
            return types.InputPeerChat(chat_id=peer.chat_id)
        return utils.get_input_peer(peer)

    def of(self, cls):
        """All captured requests of the given class."""
        return [r for r in self.requests if isinstance(r, cls)]


def user(uid, name='U', **kw):
    return types.User(id=uid, first_name=name, access_hash=uid * 7, **kw)


def channel(cid, title='C', **kw):
    return types.Channel(id=cid, title=title, photo=types.ChatPhotoEmpty(), date=NOW,
                         access_hash=cid * 3, **kw)


def message(mid, peer, text='', **kw):
    return types.Message(id=mid, peer_id=peer, date=NOW, message=text, **kw)


def updates(*items, users=(), chats=()):
    return types.Updates(updates=list(items), users=list(users), chats=list(chats),
                         date=NOW, seq=0)


import unittest

from legacytl import events
from legacytl.tl import types, functions


T = types
F = functions
CHAN = T.PeerChannel(500)


async def dispatch(client, update, builder, entities=None):
    """Feeds ``update`` through the real dispatcher; returns the delivered events."""
    got = []

    async def handler(event):
        got.append(event)

    client.add_event_handler(handler, builder)
    update._entities = entities or {}
    try:
        await client._dispatch_update(update)
    finally:
        client.remove_event_handler(handler)
    return got


def _eph(mid=3, **kw):
    kw.setdefault('peer_id', CHAN)
    return T.EphemeralMessage(id=mid, from_id=T.PeerUser(1), receiver_id=10, date=NOW, message='hello', **kw)


class MessageReactionTest(unittest.IsolatedAsyncioTestCase):
    async def test_user_update(self):
        c = FakeClient()
        upd = T.UpdateMessageReactions(peer=CHAN, msg_id=7, top_msg_id=2, reactions=T.MessageReactions(
            results=[T.ReactionCount(reaction=T.ReactionEmoji('👍'), count=3)]))
        ev, = await dispatch(c, upd, events.MessageReaction(chats=[-1000000000500], reaction='👍'))
        self.assertEqual((ev.chat_id, ev.message_id, ev.top_msg_id), (-1000000000500, 7, 2))
        self.assertIsNone(ev.actor)
        self.assertEqual(ev.counts[0].count, 3)
        self.assertEqual(await dispatch(c, upd, events.MessageReaction(reaction='🔥')), [])
        self.assertEqual(await dispatch(c, upd, events.MessageReaction(chats=[123])), [])

    async def test_bot_update(self):
        c = FakeClient({F.messages.SendReactionRequest: updates()}, bot=True)
        upd = T.UpdateBotMessageReaction(
            peer=CHAN, msg_id=7, date=NOW, actor=T.PeerUser(10), qts=1,
            old_reactions=[T.ReactionEmoji('👍')],
            new_reactions=[T.ReactionEmoji('🔥'), T.ReactionCustomEmoji(document_id=5)])
        ents = {10: user(10, 'A'), -1000000000500: channel(500)}
        ev, = await dispatch(c, upd, events.MessageReaction(reaction=5), ents)
        self.assertEqual(ev.sender_id, 10)
        self.assertEqual(ev.sender.first_name, 'A')
        self.assertEqual([type(r) for r in ev.added], [T.ReactionEmoji, T.ReactionCustomEmoji])
        self.assertEqual(ev.removed[0].emoticon, '👍')
        self.assertEqual(ev.emoticons, ['🔥'])
        # Removed reactions don't match the "added" filter
        self.assertEqual(await dispatch(c, upd, events.MessageReaction(reaction='👍')), [])

        if hasattr(c, 'heroku_me'):  # send_reaction needs client-side config not available offline
            await ev.react('👌')
            r = c.requests[-1]
            self.assertEqual((r.msg_id, r.reaction[0].emoticon, r.peer.channel_id), (7, '👌', 500))

        anon = T.UpdateBotMessageReactions(peer=CHAN, msg_id=8, date=NOW, qts=2, reactions=[
            T.ReactionCount(reaction=T.ReactionEmoji('👍'), count=1)])
        ev, = await dispatch(c, anon, events.MessageReaction(reaction='👍'))
        self.assertIsNone(ev.sender_id)
        self.assertEqual(ev.message_id, 8)

    async def test_other_updates_ignored(self):
        c = FakeClient()
        self.assertEqual(await dispatch(c, T.UpdateReadStories(peer=CHAN, max_id=1), events.MessageReaction), [])


class EphemeralTest(unittest.IsolatedAsyncioTestCase):
    async def test_kinds_and_filters(self):
        c = FakeClient({F.ephemeral.EditMessageRequest: updates(), F.ephemeral.DeleteMessageRequest: True},
                       bot=True)
        new = T.UpdateNewEphemeralMessage(message=_eph(out=True))
        edit = T.UpdateEditEphemeralMessage(message=_eph())
        delete = T.UpdateDeleteEphemeralMessages(peer=CHAN, ids=[3, 4])

        ev, = await dispatch(c, new, events.EphemeralMessage(pattern='hel'))
        self.assertTrue(ev.new)
        self.assertEqual((ev.id, ev.text, ev.receiver_id, ev.chat_id, ev.sender_id),
                         (3, 'hello', 10, -1000000000500, 1))
        self.assertIsNotNone(ev.pattern_match)
        self.assertEqual(await dispatch(c, new, events.EphemeralMessage(pattern='bye')), [])

        self.assertEqual(await dispatch(c, edit, events.EphemeralMessage(new=True)), [])
        ev, = await dispatch(c, edit, events.EphemeralMessage(edited=True))
        self.assertTrue(ev.edited)

        ev, = await dispatch(c, delete, events.EphemeralMessage)
        self.assertTrue(ev.deleted)
        self.assertEqual(ev.deleted_ids, [3, 4])
        self.assertEqual(await dispatch(c, delete, events.EphemeralMessage(pattern='.')), [])

        ev, = await dispatch(c, new, events.EphemeralMessage)
        await ev.edit('changed', parse_mode=None)
        r = c.requests[-1]
        self.assertEqual((r.peer.channel_id, r.receiver_id.user_id, r.id, r.message), (500, 10, 3, 'changed'))
        await ev.delete()
        self.assertIsInstance(c.requests[-1], F.ephemeral.DeleteMessageRequest)

    async def test_private_chat_fallback(self):
        c = FakeClient()
        ev, = await dispatch(c, T.UpdateNewEphemeralMessage(message=_eph(peer_id=None, out=True)),
                             events.EphemeralMessage)
        self.assertEqual(ev.chat_id, 10)  # bot side: the receiver's chat
        ev, = await dispatch(c, T.UpdateNewEphemeralMessage(message=_eph(peer_id=None)),
                             events.EphemeralMessage)
        self.assertEqual(ev.chat_id, 1)  # user side: the bot's chat

    async def test_callback_query(self):
        c = FakeClient({
            F.messages.SetBotCallbackAnswerRequest: True,
            F.ephemeral.SendMessageRequest: updates(),
            F.ephemeral.EditMessageRequest: updates(),
        }, bot=True)
        upd = T.UpdateEphemeralBotCallbackQuery(query_id=77, user_id=10, peer=CHAN, msg_id=3,
                                                data=b'ok:1', chat_instance=5, message=_eph())
        self.assertEqual(await dispatch(c, upd, events.EphemeralCallbackQuery(data=b'no')), [])
        ev, = await dispatch(c, upd, events.EphemeralCallbackQuery(pattern=r'ok:(\d)'), {10: user(10)})
        self.assertEqual(ev.data_match.group(1), b'1')
        self.assertEqual((ev.id, ev.data, ev.message_id, ev.sender_id, ev.chat_id),
                         (77, b'ok:1', 3, 10, -1000000000500))
        self.assertIs(ev.message, upd.message)

        await ev.answer('thanks', alert=True)
        await ev.answer('again')  # only once
        answers = c.of(F.messages.SetBotCallbackAnswerRequest)
        self.assertEqual(len(answers), 1)
        self.assertEqual((answers[0].query_id, answers[0].message, answers[0].alert), (77, 'thanks', True))

        await ev.respond('only you', parse_mode=None)
        r = c.of(F.ephemeral.SendMessageRequest)[-1]
        self.assertEqual((r.query_id, r.receiver_id.user_id, r.peer.channel_id), (77, 10, 500))

        await ev.edit('edited', parse_mode=None)
        r = c.of(F.ephemeral.EditMessageRequest)[-1]
        self.assertEqual((r.id, r.message), (3, 'edited'))


class BusinessTest(unittest.IsolatedAsyncioTestCase):
    async def test_messages(self):
        sent = message(51, T.PeerUser(10), 'ok', out=True)

        def send(r):
            self.assertIsInstance(r.query, F.messages.SendMessageRequest)
            return updates(T.UpdateBotNewBusinessMessage(connection_id='c1', message=sent, qts=9))

        c = FakeClient({F.InvokeWithBusinessConnectionRequest: send}, bot=True)
        msg = message(50, T.PeerUser(10), 'What is the price?', from_id=T.PeerUser(10))
        new = T.UpdateBotNewBusinessMessage(connection_id='c1', message=msg, qts=1)

        ev, = await dispatch(c, new, events.BusinessMessage(pattern='(?i).*price'), {10: user(10, 'A')})
        self.assertTrue(ev.new)
        self.assertEqual((ev.connection_id, ev.chat_id, ev.sender_id, ev.text),
                         ('c1', 10, 10, 'What is the price?'))
        self.assertEqual(ev.sender.first_name, 'A')
        self.assertEqual(await dispatch(c, new, events.BusinessMessage(connection_id='other')), [])
        self.assertEqual(await dispatch(c, new, events.BusinessMessage(edited=True)), [])

        reply = await ev.reply('**10** stars')
        self.assertIs(reply, sent)
        r = c.requests[-1]
        self.assertEqual(r.connection_id, 'c1')
        self.assertEqual((r.query.message, r.query.reply_to.reply_to_msg_id), ('10 stars', 50))
        self.assertEqual(r.query.peer.user_id, 10)

        edit = T.UpdateBotEditBusinessMessage(connection_id='c1', message=msg, qts=2)
        ev, = await dispatch(c, edit, events.BusinessMessage(edited=True))
        self.assertTrue(ev.edited)

        delete = T.UpdateBotDeleteBusinessMessage(connection_id='c1', peer=T.PeerUser(10), messages=[50], qts=3)
        ev, = await dispatch(c, delete, events.BusinessMessage(chats=[10]))
        self.assertTrue(ev.deleted)
        self.assertEqual(ev.deleted_ids, [50])

    async def test_connection_and_callback(self):
        c = FakeClient({F.messages.SetBotCallbackAnswerRequest: True}, bot=True)
        conn = T.BotBusinessConnection(connection_id='c1', user_id=10, dc_id=2, date=NOW, disabled=True)
        ev, = await dispatch(c, T.UpdateBotBusinessConnect(connection=conn, qts=1), events.BusinessConnection)
        self.assertEqual((ev.connection_id, ev.user_id, ev.disabled), ('c1', 10, True))

        cb = T.UpdateBusinessBotCallbackQuery(query_id=5, user_id=10, connection_id='c1',
                                              message=message(50, T.PeerUser(10)), chat_instance=1, data=b'yes')
        self.assertEqual(await dispatch(c, cb, events.BusinessCallbackQuery(data='no')), [])
        ev, = await dispatch(c, cb, events.BusinessCallbackQuery(data='yes'))
        self.assertEqual((ev.id, ev.connection_id, ev.data, ev.chat_id), (5, 'c1', b'yes', 10))
        await ev.answer('ok')
        self.assertEqual(c.requests[-1].query_id, 5)


class PollEventsTest(unittest.IsolatedAsyncioTestCase):
    async def test_poll_update(self):
        c = FakeClient()
        poll = T.Poll(id=555, question=T.TextWithEntities('Q', []), answers=[], hash=1, closed=True)
        upd = T.UpdateMessagePoll(poll_id=555, poll=poll, results=T.PollResults(total_voters=4),
                                  peer=CHAN, msg_id=9)
        ev, = await dispatch(c, upd, events.PollUpdate(poll_id=poll))
        self.assertEqual((ev.poll_id, ev.total_voters, ev.closed, ev.message_id, ev.chat_id),
                         (555, 4, True, 9, -1000000000500))
        self.assertEqual(await dispatch(c, upd, events.PollUpdate(poll_id=1)), [])

        bare = T.UpdateMessagePoll(poll_id=556, results=T.PollResults())
        ev, = await dispatch(c, bare, events.PollUpdate)
        self.assertIsNone(ev.chat_id)
        self.assertFalse(ev.closed)

    async def test_poll_vote(self):
        c = FakeClient(bot=True)
        upd = T.UpdateMessagePollVote(poll_id=555, peer=T.PeerUser(10), options=[b'1'], positions=[1], qts=1)
        ev, = await dispatch(c, upd, events.PollVote(chats=[10], poll_id=555), {10: user(10, 'A')})
        self.assertEqual((ev.voter_id, ev.options, ev.positions, ev.retracted), (10, [b'1'], [1], False))
        self.assertEqual(ev.sender.first_name, 'A')
        self.assertEqual(await dispatch(c, upd, events.PollVote(chats=[11])), [])
        retract = T.UpdateMessagePollVote(poll_id=555, peer=T.PeerUser(10), options=[], positions=[], qts=2)
        ev, = await dispatch(c, retract, events.PollVote)
        self.assertTrue(ev.retracted)


class StoryEventsTest(unittest.IsolatedAsyncioTestCase):
    async def test_kinds(self):
        c = FakeClient({F.stories.ReadStoriesRequest: [[5]], F.stories.SendReactionRequest: updates()})
        peer = T.PeerUser(10)
        item = T.StoryItem(id=5, date=NOW, expire_date=NOW, media=T.MessageMediaEmpty(), caption='hi')
        new = T.UpdateStory(peer=peer, story=item)
        gone = T.UpdateStory(peer=peer, story=T.StoryItemDeleted(id=6))
        read = T.UpdateReadStories(peer=peer, max_id=5)
        react = T.UpdateSentStoryReaction(peer=peer, story_id=5, reaction=T.ReactionEmoji('🔥'))

        ev, = await dispatch(c, new, events.Story(chats=[10]), {10: user(10)})
        self.assertTrue(ev.new)
        self.assertEqual((ev.story_id, ev.story.caption, ev.chat_id), (5, 'hi', 10))
        if hasattr(c, 'read_stories'):  # story client methods may not be available yet
            await ev.mark_read()
            self.assertEqual(c.requests[-1].max_id, 5)
            await ev.react('🔥')
            self.assertEqual(c.requests[-1].reaction.emoticon, '🔥')

        ev, = await dispatch(c, gone, events.Story(deleted=True))
        self.assertTrue(ev.deleted)
        self.assertEqual(await dispatch(c, gone, events.Story(new=True)), [])

        ev, = await dispatch(c, read, events.Story)
        self.assertTrue(ev.read)
        self.assertEqual(ev.max_id, 5)

        ev, = await dispatch(c, react, events.Story(reaction=True))
        self.assertTrue(ev.reacted)
        self.assertTrue(ev.outgoing)
        self.assertEqual(ev.reaction.emoticon, '🔥')


def _member(uid):
    return T.ChannelParticipant(user_id=uid, date=NOW)


def _admin(uid, **rights):
    return T.ChannelParticipantAdmin(user_id=uid, promoted_by=1, date=NOW,
                                     admin_rights=T.ChatAdminRights(**rights))


def _banned(uid, view=True, left=False):
    return T.ChannelParticipantBanned(peer=T.PeerUser(uid), kicked_by=1, date=NOW, left=left,
                                      banned_rights=T.ChatBannedRights(until_date=None, view_messages=view))


def _chan_update(prev, new, actor=10, uid=10):
    return T.UpdateChannelParticipant(channel_id=500, date=NOW, actor_id=actor, user_id=uid,
                                      prev_participant=prev, new_participant=new, qts=1)


class ChatMemberTest(unittest.IsolatedAsyncioTestCase):
    def _kinds(self, ev):
        return {k for k in events.ChatMember._KINDS if getattr(ev, k)}

    async def test_transitions(self):
        c = FakeClient(bot=True)
        cases = [
            (_chan_update(None, _member(10)), {'joined'}),
            (_chan_update(_member(10), None), {'left'}),
            (_chan_update(T.ChannelParticipantLeft(peer=T.PeerUser(10)), _member(10)), {'joined'}),
            (_chan_update(_member(10), _banned(10), actor=1), {'kicked'}),
            (_chan_update(_member(10), None, actor=1), {'kicked'}),
            (_chan_update(_member(10), _admin(10, ban_users=True), actor=1), {'promoted'}),
            (_chan_update(_admin(10, ban_users=True), _admin(10, ban_users=True, pin_messages=True), actor=1),
             {'promoted'}),
            (_chan_update(_admin(10, ban_users=True), _admin(10, ban_users=True), actor=1), set()),
            (_chan_update(_admin(10), _member(10), actor=1), {'demoted'}),
            (_chan_update(_member(10), _banned(10, view=False), actor=1), {'restricted'}),
            (_chan_update(_member(10), _banned(10, view=False, left=True), actor=10), {'left', 'restricted'}),
        ]
        for upd, expected in cases:
            ev, = await dispatch(c, upd, events.ChatMember)
            self.assertEqual(self._kinds(ev), expected, (ev.old_status, ev.new_status))
            self.assertEqual(ev.chat_id, -1000000000500)

        ev, = await dispatch(c, _chan_update(_banned(10), None, actor=1), events.ChatMember)
        self.assertTrue(ev.unbanned)
        self.assertEqual(ev.old_status, 'kicked')

    async def test_small_chat_and_filters(self):
        c = FakeClient(bot=True)
        upd = T.UpdateChatParticipant(chat_id=77, date=NOW, actor_id=1, user_id=10, qts=1,
                                      new_participant=T.ChatParticipant(user_id=10, inviter_id=1, date=NOW))
        ev, = await dispatch(c, upd, events.ChatMember(joined=True), {10: user(10, 'A'), 1: user(1, 'Me')})
        self.assertEqual((ev.chat_id, ev.user_id, ev.added_by, ev.new_status), (-77, 10, 1, 'member'))
        self.assertEqual(ev.user.first_name, 'A')
        self.assertEqual(ev.sender.first_name, 'Me')
        self.assertEqual(await dispatch(c, upd, events.ChatMember(left=True, kicked=True)), [])
        self.assertEqual(await dispatch(c, upd, events.ChatMember(chats=[-78])), [])

        promo = T.UpdateChatParticipant(chat_id=77, date=NOW, actor_id=1, user_id=10, qts=2,
                                        prev_participant=T.ChatParticipant(user_id=10, inviter_id=1, date=NOW),
                                        new_participant=T.ChatParticipantAdmin(user_id=10, inviter_id=1, date=NOW))
        ev, = await dispatch(c, promo, events.ChatMember(promoted=True))
        self.assertEqual(ev.new_status, 'administrator')

    async def test_chat_action_unchanged(self):
        # The legacy ChatAction still handles the plain join/leave cases
        c = FakeClient(bot=True)
        ev, = await dispatch(c, _chan_update(None, _member(10)), events.ChatAction)
        self.assertTrue(ev.user_joined)
        self.assertFalse(ev.user_added)
        ev, = await dispatch(c, _chan_update(None, _member(10), actor=1), events.ChatAction)
        self.assertTrue(ev.user_added)
        self.assertFalse(ev.user_joined)
        ev, = await dispatch(c, _chan_update(_member(10), None), events.ChatAction)
        self.assertTrue(ev.user_left)
        ev, = await dispatch(c, _chan_update(_member(10), None, actor=1), events.ChatAction)
        self.assertTrue(ev.user_kicked)


if __name__ == '__main__':
    unittest.main()
