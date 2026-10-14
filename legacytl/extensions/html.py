"""
Simple HTML -> Telegram entity parser.
"""

import re
import datetime
from collections import deque
from html import escape
from html.parser import HTMLParser
from typing import Optional, Tuple, List, Generator, List, Optional, cast
from abc import ABC, abstractmethod
from types import SimpleNamespace

from .. import helpers
from ..tl.types import (
    MessageEntityBold,
    MessageEntityItalic,
    MessageEntityCode,
    MessageEntityPre,
    MessageEntityEmail,
    MessageEntityUrl,
    MessageEntityTextUrl,
    MessageEntityMentionName,
    MessageEntityUnderline,
    MessageEntityStrike,
    MessageEntityBlockquote,
    MessageEntityCustomEmoji,
    TypeMessageEntity,
    MessageEntitySpoiler,
    MessageEntityFormattedDate,
)


# Bot API `format` letters of <tg-time> and the flags they stand for
DATE_FORMAT = (
    ("r", "relative"), ("w", "day_of_week"), ("d", "short_date"),
    ("D", "long_date"), ("t", "short_time"), ("T", "long_time"),
)


def _date_args(tag, attrs):
    """Arguments of `MessageEntityFormattedDate` from <tg-time> or <date>"""
    try:
        if tag == "tg-time":
            date = int(attrs["unix"])
            flags = {flag for char, flag in DATE_FORMAT if char in (attrs.get("format") or "")}
        else:
            moment = datetime.datetime.fromisoformat(attrs["date"].replace("Z", "+00:00"))
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            date = int(moment.timestamp())
            flags = {flag for _, flag in DATE_FORMAT if flag.replace("_", "-") in attrs}
    except (KeyError, TypeError, ValueError):
        return None

    return {"date": date, **{flag: True for flag in flags}}


def _unix(date):
    """`int` timestamp of a TL ``date`` (received objects carry a `datetime`)"""
    if isinstance(date, datetime.datetime):
        if date.tzinfo is None:
            date = date.replace(tzinfo=datetime.timezone.utc)
        return int(date.timestamp())
    try:
        return int(date)
    except (TypeError, ValueError):
        return 0


def _date_format(entity):
    if entity.relative:
        return "r"

    return "".join(
        char for char, flag in DATE_FORMAT[1:]
        if getattr(entity, flag)
        and not (char == "d" and entity.long_date)
        and not (char == "t" and entity.long_time)
    )


# Helpers from markdown.py
_add_surrogate = helpers.add_surrogate
_del_surrogate = helpers.del_surrogate


class HTMLToTelegramParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.text = ""
        self.entities = []
        self._building_entities = {}
        self._open_tags = deque()
        self._open_tags_meta = deque()

    def handle_starttag(self, tag, attrs):
        self._open_tags.appendleft(tag)
        self._open_tags_meta.appendleft(None)

        attrs = dict(attrs)
        EntityType = None
        args = {}
        if tag == "strong" or tag == "b":
            EntityType = MessageEntityBold
        elif tag == "em" or tag == "i":
            EntityType = MessageEntityItalic
        elif tag in ["tg-spoiler"]:
            EntityType = MessageEntitySpoiler
        elif tag == "u":
            EntityType = MessageEntityUnderline
        elif tag == "del" or tag == "s":
            EntityType = MessageEntityStrike
        elif tag == "blockquote":
            EntityType = MessageEntityBlockquote
            if "expandable" in attrs:
                args["collapsed"] = True
        elif tag == "code":
            try:
                # If we're in the middle of a <pre> tag, this <code> tag is
                # probably intended for syntax highlighting.
                #
                # Syntax highlighting is set with
                #     <code class='language-...'>codeblock</code>
                # inside <pre> tags
                pre = self._building_entities["pre"]
                try:
                    pre.language = attrs["class"][len("language-") :]
                except KeyError:
                    pass
            except KeyError:
                EntityType = MessageEntityCode
        elif tag == "pre":
            EntityType = MessageEntityPre
            args["language"] = ""
        elif tag == "a":
            try:
                url = attrs["href"]
            except KeyError:
                return
            if url.startswith("mailto:"):
                url = url[len("mailto:") :]
                EntityType = MessageEntityEmail
            else:
                if self.get_starttag_text() == url:
                    EntityType = MessageEntityUrl
                else:
                    EntityType = MessageEntityTextUrl
                    args["url"] = _del_surrogate(url)
                    url = None
            self._open_tags_meta.popleft()
            self._open_tags_meta.appendleft(url)
        elif tag == "tg-emoji":
            try:
                emoji_id = int(attrs["emoji-id"])
            except (KeyError, ValueError):
                return

            EntityType = MessageEntityCustomEmoji
            args["document_id"] = emoji_id
        elif tag == "emoji" and CUSTOM_EMOJIS:
            EntityType = MessageEntityCustomEmoji
            args["document_id"] = int(attrs["document_id"])
        elif tag in ("tg-time", "date"):
            args = _date_args(tag, attrs)
            if args is None:
                return

            EntityType = MessageEntityFormattedDate

        if EntityType and tag not in self._building_entities:
            self._building_entities[tag] = EntityType(
                offset=len(self.text),
                # The length will be determined when closing the tag.
                length=0,
                **args,
            )

    def handle_data(self, text):
        previous_tag = self._open_tags[0] if len(self._open_tags) > 0 else ""
        if previous_tag == "a":
            url = self._open_tags_meta[0]
            if url:
                text = url

        for tag, entity in self._building_entities.items():
            entity.length += len(text)

        self.text += text

    def handle_endtag(self, tag):
        try:
            self._open_tags.popleft()
            self._open_tags_meta.popleft()
        except IndexError:
            pass
        entity = self._building_entities.pop(tag, None)
        if entity:
            self.entities.append(entity)


def parse(html: str) -> Tuple[str, List[TypeMessageEntity]]:
    """
    Parses the given HTML message and returns its stripped representation
    plus a list of the MessageEntity's that were found.

    :param html: the message with HTML to be parsed.
    :return: a tuple consisting of (clean message, [message entities]).
    """
    if not html:
        return html, []

    parser = HTMLToTelegramParser()
    parser.feed(_add_surrogate(html))
    text = helpers.strip_text(parser.text, parser.entities)
    return _del_surrogate(text), parser.entities


CUSTOM_EMOJIS = True  # Can be disabled externally

# Based on https://github.com/aiogram/aiogram/blob/c43ff9b6f9dd62cd2d84272e5c460b904b4c3276/aiogram/utils/text_decorations.py

_ENTITY_METHOD_MAP = {
    MessageEntityBold: "bold",
    MessageEntityItalic: "italic",
    MessageEntitySpoiler: "spoiler",
    MessageEntityCode: "code",
    MessageEntityUnderline: "underline",
    MessageEntityStrike: "strikethrough",
}

_EMOJI_TAG_RE = re.compile(r"^<emoji document_id=\"?\d+?\"?>[^<]*?<\/emoji>$")


class TextDecoration(ABC):
    def apply_entity(self, entity, text: str) -> str:
        """
        Apply single entity to text
        :param entity:
        :param text:
        :return:
        """
        method = _ENTITY_METHOD_MAP.get(type(entity))
        if method is not None:
            if _EMOJI_TAG_RE.match(text):
                return text

            return cast(str, getattr(self, method)(value=text))
        if type(entity) == MessageEntityPre:
            return (
                self.pre_language(value=text, language=entity.language)
                if entity.language
                else self.pre(value=text)
            )
        if type(entity) == MessageEntityMentionName:
            return self.link(value=text, link=f"tg://user?id={entity.user_id}")
        if type(entity) == MessageEntityTextUrl:
            return self.link(value=text, link=cast(str, entity.url))
        if type(entity) == MessageEntityUrl:
            return self.link(value=text, link=text)
        if type(entity) == MessageEntityEmail:
            return self.link(value=text, link=f"mailto:{text}")
        if type(entity) == MessageEntityCustomEmoji and CUSTOM_EMOJIS:
            return self.custom_emoji(value=text, document_id=entity.document_id)
        if type(entity) == MessageEntityBlockquote:
            return self.blockquote(value=text, collapsed=bool(entity.collapsed))
        if type(entity) == MessageEntityFormattedDate:
            return self.formatted_date(
                value=text,
                date=entity.date,
                relative=entity.relative,
                short_time=entity.short_time,
                long_time=entity.long_time,
                short_date=entity.short_date,
                long_date=entity.long_date,
                day_of_week=entity.day_of_week,
            )

        return self.quote(text)

    def unparse(self, text: str, entities: Optional[list] = None) -> str:
        """
        Unparse message entities
        :param text: raw text
        :param entities: Array of MessageEntities
        :return:
        """
        return "".join(
            self._unparse_entities(
                self._add_surrogates(text),
                sorted(entities, key=lambda item: item.offset) if entities else [],
            )
        )

    def _unparse_entities(
        self,
        text: bytes,
        entities: list,
        offset: Optional[int] = None,
        length: Optional[int] = None,
    ) -> Generator[str, None, None]:
        if offset is None:
            offset = 0
        length = length or len(text)

        for index, entity in enumerate(entities):
            if entity.offset * 2 < offset:
                continue
            if entity.offset * 2 > offset:
                yield self.quote(
                    self._remove_surrogates(text[offset : entity.offset * 2])
                )
            start = entity.offset * 2
            offset = entity.offset * 2 + entity.length * 2

            sub_entities = list(
                filter(lambda e: e.offset * 2 < (offset or 0), entities[index + 1 :])
            )
            yield self.apply_entity(
                entity,
                "".join(
                    self._unparse_entities(
                        text, sub_entities, offset=start, length=offset
                    )
                ),
            )

        if offset < length:
            yield self.quote(self._remove_surrogates(text[offset:length]))

    @staticmethod
    def _add_surrogates(text: str):
        return text.encode("utf-16-le")

    @staticmethod
    def _remove_surrogates(text: bytes):
        return text.decode("utf-16-le")

    @abstractmethod
    def link(self, value: str, link: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def bold(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def italic(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def spoiler(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def code(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def pre(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def pre_language(self, value: str, language: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def underline(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def strikethrough(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def quote(self, value: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def custom_emoji(self, value: str, document_id: str) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def blockquote(
        self, value: str, collapsed: bool = False
    ) -> str:  # pragma: no cover
        pass

    @abstractmethod
    def formatted_date(
        self,
        value: str,
        date: datetime.datetime,
        relative: bool = False,
        short_time: bool = False,
        long_time: bool = False,
        short_date: bool = False,
        long_date: bool = False,
        day_of_week: bool = False,
    ) -> str:
        pass


class HtmlDecoration(TextDecoration):
    def link(self, value: str, link: str) -> str:
        return f'<a href="{link}">{value}</a>'

    def bold(self, value: str) -> str:
        return f"<strong>{value}</strong>"

    def italic(self, value: str) -> str:
        return f"<em>{value}</em>"

    def spoiler(self, value: str) -> str:
        return f"<tg-spoiler>{value}</tg-spoiler>"

    def code(self, value: str) -> str:
        return f"<code>{value}</code>"

    def pre(self, value: str) -> str:
        return f"<pre>{value}</pre>"

    def pre_language(self, value: str, language: str) -> str:
        return f'<pre><code class="language-{language}">{value}</code></pre>'

    def underline(self, value: str) -> str:
        return f"<u>{value}</u>"

    def strikethrough(self, value: str) -> str:
        return f"<s>{value}</s>"

    def quote(self, value: str) -> str:
        return escape(value, quote=False)

    def blockquote(self, value: str, collapsed: bool = False) -> str:
        if collapsed:
            return f"<blockquote expandable>{value}</blockquote>"
        return f"<blockquote>{value}</blockquote>"

    def custom_emoji(self, value: str, document_id: str) -> str:
        return f"<tg-emoji emoji-id={document_id}>{value}</tg-emoji>"

    def formatted_date(
        self,
        value: str,
        date: datetime.datetime,
        relative: bool = False,
        short_time: bool = False,
        long_time: bool = False,
        short_date: bool = False,
        long_date: bool = False,
        day_of_week: bool = False,
    ) -> str:
        flags = SimpleNamespace(
            relative=relative,
            short_time=short_time,
            long_time=long_time,
            short_date=short_date,
            long_date=long_date,
            day_of_week=day_of_week,
        )
        fmt = _date_format(flags)
        return '<tg-time unix="{}"{}>{}</tg-time>'.format(
            _unix(date), f' format="{fmt}"' if fmt else "", value
        )


html_decoration = HtmlDecoration()
unparse = html_decoration.unparse
