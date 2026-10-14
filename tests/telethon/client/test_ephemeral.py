import copy
import datetime
import unittest

from legacytl import TelegramClient, utils, Button
from legacytl.sessions import MemorySession
from legacytl.tl import types, functions, TLObject

F = functions
T = types

NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
CHAN = T.PeerChannel(500)

USERNAMES = {
    "alice": T.InputPeerUser(user_id=10, access_hash=1010),
    "bob": T.InputPeerUser(user_id=11, access_hash=1111),
    "chan": T.InputPeerChannel(channel_id=500, access_hash=5050),
}


class FakeClient(TelegramClient):
    """
    An offline client for unit tests: requests are captured instead of
    sent, and canned results (request class -> result or ``(request) ->
    result``; a list value is consumed one item per call) are returned.
    """

    def __init__(self, responses=None, *, self_id=1, bot=False):
        super().__init__(MemorySession(), 1, "x" * 32, receive_updates=False)
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
            raise AssertionError("Unexpected request {}".format(type(request).__name__))
        if isinstance(result, list):
            result = result.pop(0)
        if callable(result) and not isinstance(result, TLObject):
            result = result(request)
        return result

    async def get_input_entity(self, peer):
        if peer in ("me", "self"):
            return T.InputPeerSelf()
        if isinstance(peer, str):
            return USERNAMES[peer.lstrip("@").lower()]
        if isinstance(peer, int):
            i, cls = utils.resolve_id(peer)
            if cls is T.PeerUser:
                return T.InputPeerUser(user_id=i, access_hash=0)
            if cls is T.PeerChannel:
                return T.InputPeerChannel(channel_id=i, access_hash=0)
            return T.InputPeerChat(chat_id=i)
        if isinstance(peer, T.PeerUser):
            return T.InputPeerUser(user_id=peer.user_id, access_hash=0)
        if isinstance(peer, T.PeerChannel):
            return T.InputPeerChannel(channel_id=peer.channel_id, access_hash=0)
        if isinstance(peer, T.PeerChat):
            return T.InputPeerChat(chat_id=peer.chat_id)
        return utils.get_input_peer(peer)

    def of(self, cls):
        """All captured requests of the given class."""
        return [r for r in self.requests if isinstance(r, cls)]


def updates(*items, users=(), chats=()):
    return T.Updates(
        updates=list(items), users=list(users), chats=list(chats), date=NOW, seq=0
    )


def _eph(mid, **kw):
    return T.EphemeralMessage(
        id=mid, from_id=T.PeerUser(1), receiver_id=10, date=NOW, message="x", **kw
    )


class EphemeralTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_edit_delete(self):
        sent = _eph(3, peer_id=CHAN, out=True)
        c = FakeClient(
            {
                F.ephemeral.SendMessageRequest: updates(
                    T.UpdateNewEphemeralMessage(message=sent)
                ),
                F.ephemeral.EditMessageRequest: updates(
                    T.UpdateEditEphemeralMessage(message=sent)
                ),
                F.ephemeral.DeleteMessageRequest: True,
            },
            bot=True,
        )
        got = await c.send_ephemeral_message(
            "chan",
            "alice",
            "**hi**",
            query_id=99,
            buttons=Button.inline("ok", b"ok"),
            reply_to_ephemeral=2,
            noforwards=True,
        )
        self.assertIs(got, sent)
        r = c.requests[-1]
        self.assertEqual(r.peer.channel_id, 500)
        self.assertEqual(r.receiver_id, T.InputUser(10, 1010))
        self.assertEqual(r.message, "hi")
        self.assertIsInstance(r.entities[0], T.MessageEntityBold)
        self.assertEqual(r.query_id, 99)
        self.assertIsInstance(r.reply_markup, T.ReplyInlineMarkup)
        self.assertEqual(r.reply_to, T.InputReplyToEphemeralMessage(2))
        self.assertTrue(r.noforwards)
        self.assertIsNone(r.welcome)

        await c.send_ephemeral_message(
            None, "alice", "plain", reply_to=5, parse_mode=None, rich="# T"
        )
        r = c.requests[-1]
        self.assertIsNone(r.peer)
        self.assertEqual(r.reply_to, T.InputReplyToMessage(5))
        self.assertIsNone(r.entities)
        self.assertEqual(r.rich_message, T.InputRichMessageMarkdown(markdown="# T"))

        self.assertIs(
            await c.edit_ephemeral_message("chan", "alice", sent, "new", parse_mode=None),
            sent,
        )
        r = c.requests[-1]
        self.assertEqual((r.id, r.message, r.receiver_id.user_id), (3, "new", 10))

        await c.edit_ephemeral_message("chan", "alice", 3, buttons=Button.inline("x"))
        self.assertIsNone(c.requests[-1].message)

        self.assertTrue(await c.delete_ephemeral_message(None, "alice", 3))
        r = c.requests[-1]
        self.assertEqual((r.peer, r.id), (None, 3))


class EphemeralRegistrationTest(unittest.TestCase):
    def test_methods_on_client(self):
        for name in (
            "send_ephemeral_message",
            "edit_ephemeral_message",
            "delete_ephemeral_message",
        ):
            self.assertTrue(callable(getattr(TelegramClient, name)), name)
            self.assertIn("Example", getattr(TelegramClient, name).__doc__, name)


if __name__ == "__main__":
    unittest.main()
