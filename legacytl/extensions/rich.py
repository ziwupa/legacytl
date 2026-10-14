"""
Rich messages (:tl:`RichMessage`, layer 229+).

A received rich message is a tree of :tl:`PageBlock` (paragraphs, headings,
lists, tables, quotes, details, media…) whose texts are :tl:`RichText` nodes
(bold, links, custom emoji, dates, buttons…). This module converts that tree:

* `unparse` → the rich HTML dialect accepted by :tl:`InputRichMessageHTML`
  (Bot API "Rich HTML style"), so the message can be sent again;
* `to_text` / `to_entities` → plain text, or text plus :tl:`MessageEntity`
  for places that only understand ordinary messages;
* `files` / `to_input` → the :tl:`InputRichFile` list and a ready
  :tl:`InputRichMessageHTML` that reuse the original photos and documents;
* a tiny builder (`p`, `h`, `b`, `link`, `emoji`, `time`, `table`,
  `Builder`, …) to compose rich HTML without worrying about escaping.

Example::

    from legacytl.extensions import rich

    html = rich.unparse(message.rich_message)
    await client(SendMessageRequest(peer, '', rich_message=rich.to_input(message.rich_message)))

    page = rich.Builder().h(1, 'Report').p('Hello, ', rich.b('world'), '!')
    page.table([['Metric', 'Value'], ['Speed', rich.b('42')]], header=True)
    InputRichMessageHTML(html=str(page))

Nodes this module does not know are rendered as their plain text; it never
raises on unexpected input.
"""
from datetime import datetime, timezone

from ..helpers import add_surrogate, del_surrogate, strip_text
from ..tl import types
from .html import DATE_FORMAT, _date_format, _unix

__all__ = [
    'unparse', 'to_text', 'to_entities', 'files', 'to_input',
    'Html', 'raw', 'escape', 'Builder',
    'b', 'i', 'u', 's', 'code', 'spoiler', 'mark', 'sub', 'sup', 'br',
    'link', 'mention', 'emoji', 'time', 'math', 'button',
    'p', 'h', 'pre', 'quote', 'details', 'li', 'ul', 'ol', 'table', 'hr',
    'footer', 'media',
]

# Bot API `style` of <tg-button> ↔ flags of :tl:`RichButtonStyle`
BUTTON_STYLES = (
    ('primary', 'bg_primary'), ('danger', 'bg_danger'),
    ('success', 'bg_success'), ('link', 'link'),
)

_HEADINGS = {
    'PageBlockTitle': 1, 'PageBlockSubtitle': 2, 'PageBlockHeader': 2,
    'PageBlockSubheader': 3,
    **{'PageBlockHeading{}'.format(n): n for n in range(1, 7)},
}

_INLINE_TAGS = {
    'TextBold': 'b', 'TextItalic': 'i', 'TextUnderline': 'u', 'TextStrike': 's',
    'TextFixed': 'code', 'TextSubscript': 'sub', 'TextSuperscript': 'sup',
    'TextMarked': 'mark', 'TextSpoiler': 'tg-spoiler',
}

# Rich texts whose content Telegram detects again by itself on sending
_AUTO_ENTITIES = {
    'TextMention': 'MessageEntityMention', 'TextHashtag': 'MessageEntityHashtag',
    'TextBotCommand': 'MessageEntityBotCommand', 'TextCashtag': 'MessageEntityCashtag',
    'TextAutoUrl': 'MessageEntityUrl', 'TextAutoEmail': 'MessageEntityEmail',
    'TextAutoPhone': 'MessageEntityPhone', 'TextBankCard': 'MessageEntityBankCard',
}

_FLAT_ENTITIES = {
    'TextBold': 'MessageEntityBold', 'TextItalic': 'MessageEntityItalic',
    'TextUnderline': 'MessageEntityUnderline', 'TextStrike': 'MessageEntityStrike',
    'TextFixed': 'MessageEntityCode', 'TextSpoiler': 'MessageEntitySpoiler',
    **_AUTO_ENTITIES,
}


def _name(node):
    return type(node).__name__


def _esc(text):
    return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def _attr(value):
    return _esc(value).replace('"', '&quot;')


def _file_id(kind, media_id):
    # InputRichFile ids allow only [A-Za-z0-9_-]{1,64}
    return '{}{}'.format(kind, media_id).replace('-', '_')


# region Rich HTML

class _Context:
    def __init__(self, rich_message=None):
        self.photos = {}
        self.documents = {}
        for photo in getattr(rich_message, 'photos', None) or ():
            self.photos[getattr(photo, 'id', None)] = photo
        for document in getattr(rich_message, 'documents', None) or ():
            self.documents[getattr(document, 'id', None)] = document
        self.used = {}  # file id -> InputRichFile source (Photo | Document)

    def photo(self, photo_id):
        file_id = _file_id('p', photo_id)
        self.used.setdefault(file_id, self.photos.get(photo_id))
        return 'tg://photo?id=' + file_id

    def document(self, kind, document_id):
        file_id = _file_id('d', document_id)
        self.used.setdefault(file_id, self.documents.get(document_id))
        return 'tg://{}?id={}'.format(kind, file_id)


def _html_text(node, ctx, pre=False):
    """Inline HTML of a :tl:`RichText`"""
    if node is None:
        return ''
    if isinstance(node, str):
        text = _esc(node)
        return text if pre else text.replace('\n', '<br>')

    name = _name(node)
    try:
        return _HTML_TEXT[name](node, ctx, pre)
    except Exception:
        pass

    try:
        text = _esc(_plain_text(node))
    except Exception:
        return ''
    return text if pre else text.replace('\n', '<br>')


def _wrap(tag, **attrs):
    def render(node, ctx, pre):
        inner = _html_text(getattr(node, 'text', None), ctx, pre)
        if not inner:
            return ''
        extra = ''.join(
            ' {}="{}"'.format(key.replace('_', '-'), _attr(fn(node)))
            for key, fn in attrs.items()
        )
        return '<{}{}>{}</{}>'.format(tag, extra, inner, tag)
    return render


def _html_concat(node, ctx, pre):
    return ''.join(_html_text(t, ctx, pre) for t in getattr(node, 'texts', None) or ())


def _html_custom_emoji(node, ctx, pre):
    return '<tg-emoji emoji-id="{}">{}</tg-emoji>'.format(
        node.document_id, _esc(node.alt or ''))


def _html_date(node, ctx, pre):
    fmt = _date_format(node)
    return '<tg-time unix="{}"{}>{}</tg-time>'.format(
        _unix(node.date), ' format="{}"'.format(fmt) if fmt else '',
        _html_text(node.text, ctx, pre) or _esc(_default_date_text(node.date)))


def _html_anchor(node, ctx, pre):
    inner = _html_text(node.text, ctx, pre)
    if inner:
        return '<tg-reference name="{}">{}</tg-reference>'.format(_attr(node.name), inner)
    return '<a name="{}"></a>'.format(_attr(node.name))


def _button_attrs(button):
    """Attributes of <tg-button> for a :tl:`TextButton` / :tl:`PageButton`, or None"""
    kind = button.type
    name = _name(kind)
    if name == 'InlineButtonTypeUrl':
        attrs = [('type', 'url'), ('url', kind.url)]
    elif name == 'InlineButtonTypeWebView':
        attrs = [('type', 'web_app'), ('url', kind.url)]
    elif name in ('InlineButtonTypeUrlAuth', 'InputInlineButtonTypeUrlAuth'):
        attrs = [('type', 'login_url'), ('url', kind.url)]
        if getattr(kind, 'fwd_text', None):
            attrs.append(('forward-text', kind.fwd_text))
        if getattr(kind, 'request_write_access', None):
            attrs.append(('request-write-access', None))
    elif name == 'InlineButtonTypeCallback':
        data = kind.data
        if isinstance(data, bytes):
            data = data.decode('utf-8', 'replace')
        attrs = [('type', 'callback_data'), ('data', data)]
    elif name == 'InlineButtonTypeSwitchInline':
        if kind.same_peer:
            attrs = [('type', 'switch_inline_query_current_chat')]
        elif kind.peer_types:
            attrs = [('type', 'switch_inline_query_chosen_chat')]
        else:
            attrs = [('type', 'switch_inline_query')]
        attrs.append(('query', kind.query))
        for peer_type in kind.peer_types or ():
            allow = {
                'InlineQueryPeerTypePM': 'allow-user-chats',
                'InlineQueryPeerTypeBotPM': 'allow-bot-chats',
                'InlineQueryPeerTypeChat': 'allow-group-chats',
                'InlineQueryPeerTypeMegagroup': 'allow-group-chats',
                'InlineQueryPeerTypeBroadcast': 'allow-channel-chats',
            }.get(_name(peer_type))
            if allow and (allow, None) not in attrs:
                attrs.append((allow, None))
    elif name in ('InlineButtonTypeUserProfile', 'InputInlineButtonTypeUserProfile'):
        user_id = getattr(kind.user_id, 'user_id', kind.user_id)
        attrs = [('type', 'url'), ('url', 'tg://user?id={}'.format(user_id))]
    elif name == 'InlineButtonTypeCopy':
        attrs = [('type', 'copy_text'), ('text', kind.copy_text)]
    elif name == 'InlineButtonTypeDisabled':
        attrs = [('type', 'disabled')]
    else:
        return None  # game / buy buttons cannot be expressed in rich HTML

    style = getattr(button, 'style', None)
    for value, flag in BUTTON_STYLES:
        if style is not None and getattr(style, flag, None):
            attrs.insert(1, ('style', value))
            break
    return attrs


def _html_button(button, ctx, pre=False):
    inner = _html_text(button.text, ctx, pre)
    try:
        attrs = _button_attrs(button)
    except Exception:
        attrs = None
    if attrs is None:
        return inner
    return '<tg-button{}>{}</tg-button>'.format(''.join(
        ' {}'.format(key) if value is None else ' {}="{}"'.format(key, _attr(value))
        for key, value in attrs
    ), inner)


_HTML_TEXT = {
    'TextEmpty': lambda n, c, p: '',
    'TextPlain': lambda n, c, p: _html_text(n.text, c, p),
    **{name: _wrap(tag) for name, tag in _INLINE_TAGS.items()},
    'TextUrl': _wrap('a', href=lambda n: n.url),
    'TextEmail': _wrap('a', href=lambda n: 'mailto:' + n.email),
    'TextPhone': _wrap('a', href=lambda n: 'tel:' + n.phone),
    'TextMentionName': _wrap('a', href=lambda n: 'tg://user?id={}'.format(n.user_id)),
    'TextConcat': _html_concat,
    'TextImage': lambda n, c, p: '',  # inline images have no rich HTML form
    'TextAnchor': _html_anchor,
    'TextMath': lambda n, c, p: '<tg-math>{}</tg-math>'.format(_esc(n.source)),
    'TextCustomEmoji': _html_custom_emoji,
    'TextDate': _html_date,
    'TextDiff': lambda n, c, p: _html_text(n.text, c, p),
    'TextButton': _html_button,
    **{name: (lambda n, c, p: _html_text(n.text, c, p)) for name in _AUTO_ENTITIES},
}


def _html_caption(caption, ctx):
    if caption is None:
        return ''
    text = _html_text(getattr(caption, 'text', None), ctx)
    credit = _html_text(getattr(caption, 'credit', None), ctx)
    if credit:
        text += '<cite>{}</cite>'.format(credit)
    return text


def _figure(element, caption, ctx):
    caption = _html_caption(caption, ctx)
    if not caption:
        return element
    return '<figure>{}<figcaption>{}</figcaption></figure>'.format(element, caption)


def _html_media(block, ctx):
    """Bare media element (without caption) of a media block, or ''"""
    name = _name(block)
    spoiler = ' tg-spoiler' if getattr(block, 'spoiler', None) else ''
    if name == 'PageBlockPhoto':
        return '<img src="{}"{}/>'.format(ctx.photo(block.photo_id), spoiler)
    if name == 'PageBlockVideo':
        return '<video src="{}"{}></video>'.format(ctx.document('video', block.video_id), spoiler)
    if name == 'PageBlockAudio':
        return '<audio src="{}"></audio>'.format(ctx.document('audio', block.audio_id))
    if name == 'PageBlockDocument':
        return '<tg-document src="{}"></tg-document>'.format(
            ctx.document('document', block.document_id))
    if name in ('PageBlockMap', 'InputPageBlockMap'):
        geo = block.geo
        if getattr(geo, 'lat', None) is None:
            return ''
        return '<tg-map lat="{}" long="{}" zoom="{}"/>'.format(geo.lat, geo.long, block.zoom)
    if name == 'PageBlockCover':
        return _html_media(block.cover, ctx)
    return ''


def _html_list_item(item, ctx, ordered):
    attrs = ''
    if ordered:
        if getattr(item, 'value', None) is not None:
            attrs += ' value="{}"'.format(item.value)
        if getattr(item, 'type', None):
            attrs += ' type="{}"'.format(_attr(item.type))

    content = ''
    if getattr(item, 'checkbox', None):
        content = '<input type="checkbox" checked>' if item.checked else '<input type="checkbox">'

    blocks = getattr(item, 'blocks', None)
    if blocks is None:
        content += _html_text(getattr(item, 'text', None), ctx)
    else:
        parts = []
        for index, block in enumerate(blocks):
            if index == 0 and _name(block) == 'PageBlockParagraph':
                parts.append(_html_text(block.text, ctx))
            else:
                parts.append(_html_block(block, ctx))
        content += ''.join(parts)

    return '<li{}>{}</li>'.format(attrs, content)


def _html_cell(cell, ctx):
    tag = 'th' if cell.header else 'td'
    attrs = ''
    if cell.colspan:
        attrs += ' colspan="{}"'.format(cell.colspan)
    if cell.rowspan:
        attrs += ' rowspan="{}"'.format(cell.rowspan)
    if cell.align_center:
        attrs += ' align="center"'
    elif cell.align_right:
        attrs += ' align="right"'
    if cell.valign_middle:
        attrs += ' valign="middle"'
    elif cell.valign_bottom:
        attrs += ' valign="bottom"'
    return '<{0}{1}>{2}</{0}>'.format(tag, attrs, _html_text(cell.text, ctx))


def _html_blocks(blocks, ctx, sep='\n'):
    return sep.join(filter(None, (_html_block(block, ctx) for block in blocks or ())))


def _html_block(block, ctx):
    try:
        return _html_block_unsafe(block, ctx)
    except Exception:
        try:
            text = _plain_block(block)
        except Exception:
            return ''
        return '<p>{}</p>'.format(_esc(text).replace('\n', '<br>')) if text else ''


def _html_block_unsafe(block, ctx):
    name = _name(block)

    level = _HEADINGS.get(name)
    if level:
        inner = _html_text(block.text, ctx)
        return '<h{0}>{1}</h{0}>'.format(level, inner) if inner else ''

    if name in ('PageBlockParagraph', 'PageBlockKicker'):
        inner = _html_text(block.text, ctx)
        return '<p>{}</p>'.format(inner) if inner else ''
    if name == 'PageBlockFooter':
        inner = _html_text(block.text, ctx)
        return '<footer>{}</footer>'.format(inner) if inner else ''
    if name == 'PageBlockPreformatted':
        code = _esc(_plain_text(block.text))
        if block.language:
            return '<pre><code class="language-{}">{}</code></pre>'.format(
                _attr(block.language), code)
        return '<pre>{}</pre>'.format(code)
    if name == 'PageBlockDivider':
        return '<hr/>'
    if name == 'PageBlockAnchor':
        return '<a name="{}"></a>'.format(_attr(block.name))
    if name == 'PageBlockList':
        return '<ul>{}</ul>'.format(''.join(
            _html_list_item(item, ctx, False) for item in block.items))
    if name == 'PageBlockOrderedList':
        attrs = ''
        if block.start is not None:
            attrs += ' start="{}"'.format(block.start)
        if block.type:
            attrs += ' type="{}"'.format(_attr(block.type))
        if block.reversed:
            attrs += ' reversed'
        return '<ol{}>{}</ol>'.format(attrs, ''.join(
            _html_list_item(item, ctx, True) for item in block.items))
    if name in ('PageBlockBlockquote', 'PageBlockPullquote', 'PageBlockBlockquoteBlocks'):
        if name == 'PageBlockBlockquoteBlocks':
            inner = _html_blocks(block.blocks, ctx, '')
        else:
            inner = _html_text(block.text, ctx)
        cite = _html_text(block.caption, ctx)
        if cite:
            inner += '<cite>{}</cite>'.format(cite)
        if not inner:
            return ''
        if name == 'PageBlockPullquote':
            return '<aside>{}</aside>'.format(inner)
        return '<blockquote{}>{}</blockquote>'.format(
            ' expandable' if getattr(block, 'collapsed', None) else '', inner)
    if name in ('PageBlockPhoto', 'PageBlockVideo', 'PageBlockAudio',
                'PageBlockDocument', 'PageBlockMap', 'InputPageBlockMap'):
        element = _html_media(block, ctx)
        if not element:
            caption = _html_caption(block.caption, ctx)
            return '<p>{}</p>'.format(caption) if caption else ''
        return _figure(element, block.caption, ctx)
    if name == 'PageBlockCover':
        return _html_block(block.cover, ctx)
    if name in ('PageBlockCollage', 'PageBlockSlideshow'):
        tag = 'tg-collage' if name == 'PageBlockCollage' else 'tg-slideshow'
        items = ''.join(_html_media(item, ctx) for item in block.items)
        caption = _html_caption(block.caption, ctx)
        if caption:
            items += '<figcaption>{}</figcaption>'.format(caption)
        return '<{0}>{1}</{0}>'.format(tag, items) if items else ''
    if name == 'PageBlockTable':
        attrs = ''.join(' ' + flag for flag in ('bordered', 'striped', 'compact')
                        if getattr(block, flag, None))
        title = _html_text(block.title, ctx)
        rows = ''.join('<tr>{}</tr>'.format(''.join(_html_cell(cell, ctx) for cell in row.cells))
                       for row in block.rows)
        return '<table{}>{}{}</table>'.format(
            attrs, '<caption>{}</caption>'.format(title) if title else '', rows)
    if name == 'PageBlockDetails':
        return '<details{}><summary>{}</summary>{}</details>'.format(
            ' open' if block.open else '', _html_text(block.title, ctx),
            _html_blocks(block.blocks, ctx, ''))
    if name == 'PageBlockMath':
        return '<tg-math-block>{}</tg-math-block>'.format(_esc(block.source))
    if name == 'PageBlockThinking':
        inner = _html_text(block.text, ctx)
        return '<tg-thinking>{}</tg-thinking>'.format(inner) if inner else ''
    if name == 'PageBlockButtonRow':
        align = ('left' if block.align_left else 'center' if block.align_center
                 else 'right' if block.align_right else None)
        return '<tg-button-row{}>{}</tg-button-row>'.format(
            ' align="{}"'.format(align) if align else '',
            ''.join(_html_button(button, ctx) for button in block.buttons))
    if name == 'PageBlockAuthorDate':
        author = _html_text(block.author, ctx)
        date = _unix(block.published_date)
        stamp = '<tg-time unix="{}" format="d">{}</tg-time>'.format(
            date, _esc(_default_date_text(block.published_date))) if date else ''
        inner = ' • '.join(filter(None, (author, stamp)))
        return '<p>{}</p>'.format(inner) if inner else ''
    if name == 'PageBlockEmbed':
        parts = []
        if block.url:
            parts.append('<p><a href="{0}">{1}</a></p>'.format(_attr(block.url), _esc(block.url)))
        caption = _html_caption(block.caption, ctx)
        if caption:
            parts.append('<p>{}</p>'.format(caption))
        return '\n'.join(parts)
    if name == 'PageBlockEmbedPost':
        parts = []
        if block.author:
            parts.append('<p><b>{}</b></p>'.format(_esc(block.author)))
        parts.append(_html_blocks(block.blocks, ctx))
        caption = _html_caption(block.caption, ctx)
        if caption:
            parts.append('<p>{}</p>'.format(caption))
        return '<blockquote>{}</blockquote>'.format(''.join(filter(None, parts)))
    if name == 'PageBlockChannel':
        channel = block.channel
        title = _esc(getattr(channel, 'title', '') or '')
        username = getattr(channel, 'username', None)
        if username:
            return '<p><a href="https://t.me/{}">{}</a></p>'.format(
                _attr(username), title or '@' + _esc(username))
        return '<p>{}</p>'.format(title) if title else ''
    if name == 'PageBlockRelatedArticles':
        title = _html_text(block.title, ctx)
        items = ''.join('<li><a href="{}">{}</a></li>'.format(
            _attr(article.url), _esc(article.title or article.url))
            for article in block.articles)
        return ''.join(filter(None, (
            '<p><b>{}</b></p>'.format(title) if title else '',
            '<ul>{}</ul>'.format(items) if items else '',
        )))
    if name == 'PageBlockUnsupported':
        return ''

    text = _plain_block(block)
    return '<p>{}</p>'.format(_esc(text).replace('\n', '<br>')) if text else ''


def _blocks_of(rich_message):
    """``(blocks, context source)`` of whatever rich content was given"""
    if rich_message is None:
        return [], None
    if isinstance(rich_message, (list, tuple)):
        return list(rich_message), None
    # A message: use its rich_message
    inner = getattr(rich_message, 'rich_message', None)
    if inner is not None and not hasattr(rich_message, 'blocks'):
        return _blocks_of(inner)
    blocks = getattr(rich_message, 'blocks', None)
    if blocks is not None and not _name(rich_message).startswith('PageBlock'):
        return list(blocks), rich_message  # RichMessage, Page…
    return [rich_message], None


def _is_rich_text(node):
    if isinstance(node, str):
        return True
    try:
        return node.SUBCLASS_OF_ID == types.TextPlain.SUBCLASS_OF_ID
    except AttributeError:
        return False


def unparse(rich_message, *, sep='\n'):
    """
    Converts rich content to the rich HTML accepted by :tl:`InputRichMessageHTML`.

    :param rich_message:
        a :tl:`RichMessage`, a :tl:`Page`, a message with ``rich_message``,
        a list of :tl:`PageBlock`, a single :tl:`PageBlock` or a :tl:`RichText`.
    :param sep: string placed between top-level blocks.
    :return: the HTML string ('' for nothing).

    Photos and documents are referenced as ``tg://photo?id=p<photo id>``,
    ``tg://video?id=d<document id>`` etc.; `files` returns the matching
    :tl:`InputRichFile` list (or use `to_input` directly).
    """
    return _render_html(rich_message, sep)[0]


def _render_html(rich_message, sep='\n'):
    if _is_rich_text(rich_message) and rich_message is not None:
        ctx = _Context()
        return _html_text(rich_message, ctx), ctx
    blocks, source = _blocks_of(rich_message)
    ctx = _Context(source)
    return _html_blocks(blocks, ctx, sep), ctx


def files(rich_message):
    """
    The :tl:`InputRichFile` list for the media `unparse` references, built
    from the ``photos`` / ``documents`` of the :tl:`RichMessage`. Media that
    the message does not carry are skipped.
    """
    return _files(_render_html(rich_message)[1])


def _files(ctx):
    from .. import utils

    result = []
    for file_id, media in ctx.used.items():
        if media is None:
            continue
        try:
            if file_id.startswith('p'):
                result.append(types.InputRichFilePhoto(id=file_id, photo=utils.get_input_photo(media)))
            else:
                result.append(types.InputRichFileDocument(
                    id=file_id, document=utils.get_input_document(media)))
        except (TypeError, ValueError, AttributeError):
            continue
    return result


def to_input(rich_message, *, files=None, rtl=None, noautolink=None):
    """
    Builds an :tl:`InputRichMessageHTML`.

    ``rich_message`` may be HTML (`str`, e.g. from `Builder`) or any rich
    content accepted by `unparse`; in the latter case the original media
    are reused and ``rtl`` is copied unless given.
    """
    if isinstance(rich_message, str):
        html = str(rich_message)
        media = files
    else:
        html, ctx = _render_html(rich_message)
        media = files if files is not None else _files(ctx)
        if rtl is None:
            inner = getattr(rich_message, 'rich_message', None) or rich_message
            rtl = getattr(inner, 'rtl', None)
    return types.InputRichMessageHTML(
        html=html, files=media or None, rtl=rtl or None, noautolink=noautolink or None)

# endregion Rich HTML

# region Plain text and entities


class _Flat:
    """Collects surrogate text and entities"""

    def __init__(self):
        self.parts = []
        self.pos = 0
        self.entities = []
        self.quote_depth = 0

    def write(self, text):
        if text:
            text = add_surrogate(text)
            self.parts.append(text)
            self.pos += len(text)

    def mark(self):
        return len(self.parts), self.pos, len(self.entities)

    def rollback(self, mark):
        del self.parts[mark[0]:]
        self.pos = mark[1]
        del self.entities[mark[2]:]

    def open(self, name, **kwargs):
        cls = getattr(types, name, None)
        if cls is None:
            return None
        entity = cls(offset=self.pos, length=0, **kwargs)
        self.entities.append(entity)
        return entity

    def close(self, entity):
        if entity is not None:
            entity.length = self.pos - entity.offset

    def result(self):
        text = strip_text(''.join(self.parts), self.entities)
        return del_surrogate(text), self.entities


def _plain_text(node):
    """Plain text of a :tl:`RichText` (no entities)"""
    flat = _Flat()
    _flat_text(node, flat, entities=False)
    return del_surrogate(''.join(flat.parts))


def _plain_block(block):
    flat = _Flat()
    _flat_block(block, flat, entities=False)
    return del_surrogate(''.join(flat.parts)).strip()


def _flat_text(node, out, entities=True):
    if node is None:
        return
    if isinstance(node, str):
        out.write(node)
        return

    name = _name(node)
    try:
        if name == 'TextPlain':
            out.write(node.text)
        elif name == 'TextEmpty':
            pass
        elif name == 'TextConcat':
            for text in node.texts:
                _flat_text(text, out, entities)
        elif name == 'TextCustomEmoji':
            entity = out.open('MessageEntityCustomEmoji', document_id=node.document_id) \
                if entities and node.alt else None
            out.write(node.alt)
            out.close(entity)
        elif name == 'TextMath':
            entity = out.open('MessageEntityCode') if entities else None
            out.write(node.source)
            out.close(entity)
        elif name == 'TextImage':
            pass
        else:
            entity = None
            if entities:
                entity = _flat_entity(node, name, out)
            _flat_text(getattr(node, 'text', None), out, entities)
            out.close(entity)
    except Exception:
        try:
            text = getattr(node, 'text', None)
        except Exception:
            return
        if isinstance(text, str):
            out.write(text)


def _flat_entity(node, name, out):
    entity_name = _FLAT_ENTITIES.get(name)
    if entity_name:
        return out.open(entity_name)
    if name == 'TextUrl':
        return out.open('MessageEntityTextUrl', url=node.url)
    if name == 'TextEmail':
        if _plain_text(node.text) == node.email:
            return out.open('MessageEntityEmail')
        return out.open('MessageEntityTextUrl', url='mailto:' + node.email)
    if name == 'TextPhone':
        if _plain_text(node.text) == node.phone:
            return out.open('MessageEntityPhone')
        return out.open('MessageEntityTextUrl', url='tel:' + node.phone)
    if name == 'TextMentionName':
        return out.open('MessageEntityMentionName', user_id=node.user_id)
    if name == 'TextDate':
        flags = {flag: True for _, flag in DATE_FORMAT if getattr(node, flag, None)}
        return out.open('MessageEntityFormattedDate', date=_unix(node.date), **flags)
    if name == 'TextButton':
        url = getattr(node.type, 'url', None)
        if _name(node.type) == 'InlineButtonTypeUrl' and url:
            return out.open('MessageEntityTextUrl', url=url)
    return None


def _flat_blocks(blocks, out, entities, sep='\n\n'):
    first = True
    for block in blocks or ():
        mark = out.mark()
        if not first:
            out.write(sep)
        before = out.pos
        _flat_block(block, out, entities)
        if out.pos == before:
            out.rollback(mark)
        else:
            first = False


def _flat_caption(caption, out, entities):
    if caption is None:
        return
    _flat_text(getattr(caption, 'text', None), out, entities)
    credit = _plain_text(getattr(caption, 'credit', None))
    if credit:
        mark = out.mark()
        out.write(' — ')
        before = out.pos
        _flat_text(caption.credit, out, entities)
        if out.pos == before:
            out.rollback(mark)


def _list_numbers(block):
    items = block.items
    start = block.start if block.start is not None else (len(items) if block.reversed else 1)
    step = -1 if block.reversed else 1
    numbers = []
    current = start
    for item in items:
        if getattr(item, 'value', None) is not None:
            current = item.value
        numbers.append(getattr(item, 'num', None) or str(current))
        current += step
    return numbers


def _flat_list(block, out, entities, ordered):
    numbers = _list_numbers(block) if ordered else None
    first = True
    for index, item in enumerate(block.items):
        if not first:
            out.write('\n')
        first = False
        out.write('{}. '.format(numbers[index]) if ordered else '• ')
        if getattr(item, 'checkbox', None):
            out.write('☑ ' if item.checked else '☐ ')
        blocks = getattr(item, 'blocks', None)
        if blocks is None:
            _flat_text(getattr(item, 'text', None), out, entities)
        else:
            _flat_blocks(blocks, out, entities, '\n')


def _flat_quoted(out, entities, collapsed, render):
    entity = None
    if entities and not out.quote_depth:
        entity = out.open('MessageEntityBlockquote', collapsed=collapsed or None)
    out.quote_depth += 1
    try:
        render()
    finally:
        out.quote_depth -= 1
    out.close(entity)


def _flat_block(block, out, entities=True):
    mark = out.mark()
    try:
        _flat_block_unsafe(block, out, entities)
    except Exception:
        out.rollback(mark)
        for attr in ('text', 'title'):
            try:
                text = getattr(block, attr, None)
            except Exception:
                continue
            if text is not None:
                _flat_text(text, out, False)
                return


def _flat_block_unsafe(block, out, entities):
    name = _name(block)

    if name in _HEADINGS:
        entity = out.open('MessageEntityBold') if entities else None
        _flat_text(block.text, out, entities)
        out.close(entity)
    elif name in ('PageBlockParagraph', 'PageBlockKicker', 'PageBlockFooter',
                  'PageBlockThinking'):
        _flat_text(block.text, out, entities)
    elif name == 'PageBlockPreformatted':
        entity = out.open('MessageEntityPre', language=block.language or '') if entities else None
        out.write(_plain_text(block.text))
        out.close(entity)
    elif name == 'PageBlockDivider':
        out.write('———')
    elif name == 'PageBlockList':
        _flat_list(block, out, entities, False)
    elif name == 'PageBlockOrderedList':
        _flat_list(block, out, entities, True)
    elif name in ('PageBlockBlockquote', 'PageBlockPullquote', 'PageBlockBlockquoteBlocks'):
        def render():
            if name == 'PageBlockBlockquoteBlocks':
                _flat_blocks(block.blocks, out, entities, '\n')
            else:
                _flat_text(block.text, out, entities)
            cite = _plain_text(block.caption)
            if cite:
                out.write('\n— ')
                _flat_text(block.caption, out, entities)
        _flat_quoted(out, entities, getattr(block, 'collapsed', None), render)
    elif name == 'PageBlockDetails':
        entity = out.open('MessageEntityBold') if entities else None
        _flat_text(block.title, out, entities)
        out.close(entity)
        if block.blocks:
            out.write('\n')
            if block.open:
                _flat_blocks(block.blocks, out, entities, '\n')
            else:
                _flat_quoted(out, entities, True,
                             lambda: _flat_blocks(block.blocks, out, entities, '\n'))
    elif name == 'PageBlockTable':
        title = _plain_text(block.title)
        if title:
            entity = out.open('MessageEntityBold') if entities else None
            _flat_text(block.title, out, entities)
            out.close(entity)
            out.write('\n')
        for row_index, row in enumerate(block.rows):
            if row_index:
                out.write('\n')
            for cell_index, cell in enumerate(row.cells):
                if cell_index:
                    out.write(' | ')
                entity = out.open('MessageEntityBold') if entities and cell.header else None
                _flat_text(cell.text, out, entities)
                out.close(entity)
    elif name in ('PageBlockPhoto', 'PageBlockVideo', 'PageBlockAudio', 'PageBlockDocument',
                  'PageBlockMap', 'InputPageBlockMap', 'PageBlockEmbed'):
        if name == 'PageBlockEmbed' and block.url:
            out.write(block.url)
            if _plain_text(getattr(block.caption, 'text', None)):
                out.write('\n')
        _flat_caption(block.caption, out, entities)
    elif name == 'PageBlockCover':
        _flat_block(block.cover, out, entities)
    elif name in ('PageBlockCollage', 'PageBlockSlideshow'):
        _flat_caption(block.caption, out, entities)
    elif name == 'PageBlockEmbedPost':
        if block.author:
            out.write(block.author + '\n')
        _flat_blocks(block.blocks, out, entities)
    elif name == 'PageBlockMath':
        entity = out.open('MessageEntityCode') if entities else None
        out.write(block.source)
        out.close(entity)
    elif name == 'PageBlockButtonRow':
        for index, button in enumerate(block.buttons):
            if index:
                out.write(' | ')
            entity = _flat_entity(button, 'TextButton', out) if entities else None
            _flat_text(button.text, out, entities)
            out.close(entity)
    elif name == 'PageBlockAuthorDate':
        _flat_text(block.author, out, entities)
    elif name == 'PageBlockChannel':
        out.write(getattr(block.channel, 'title', '') or '')
    elif name == 'PageBlockRelatedArticles':
        _flat_text(block.title, out, entities)
        for article in block.articles:
            out.write('\n• ' + (article.title or article.url))
    elif name in ('PageBlockAnchor', 'PageBlockUnsupported'):
        pass
    else:
        for attr in ('text', 'title'):
            if getattr(block, attr, None) is not None:
                _flat_text(getattr(block, attr), out, entities)
                break
        if getattr(block, 'blocks', None):
            _flat_blocks(block.blocks, out, entities)


def to_entities(rich_message, *, sep='\n\n'):
    """
    Converts rich content to ``(text, entities)`` like an ordinary message
    (headings become bold, quotes and closed ``details`` blockquotes, code
    blocks pre with language, lists get ``•`` / ``1.`` prefixes, table
    cells are separated by `` | ``). Features without an entity equivalent
    (sub/superscript, marks, media) keep only their text or caption.
    """
    out = _Flat()
    if _is_rich_text(rich_message) and rich_message is not None:
        _flat_text(rich_message, out)
    else:
        _flat_blocks(_blocks_of(rich_message)[0], out, True, sep)
    return out.result()


def to_text(rich_message, *, sep='\n\n'):
    """Plain text of rich content (same layout as `to_entities`)."""
    out = _Flat()
    if _is_rich_text(rich_message) and rich_message is not None:
        _flat_text(rich_message, out, entities=False)
    else:
        _flat_blocks(_blocks_of(rich_message)[0], out, False, sep)
    return del_surrogate(''.join(out.parts)).strip()

# endregion Plain text and entities

# region Builder


def _default_date_text(date):
    unix = _unix(date)
    return datetime.fromtimestamp(unix, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')


class Html(str):
    """A `str` holding already-safe rich HTML (never escaped again)."""
    __slots__ = ()

    def __add__(self, other):
        return Html(str.__add__(self, _join((other,))))

    def __radd__(self, other):
        return Html(_join((other,)) + str(self))


def raw(html):
    """Marks ``html`` as safe rich HTML (no escaping)."""
    return Html(html)


def escape(text):
    """Escapes ``text`` for rich HTML (already `Html` is returned as is)."""
    if isinstance(text, Html):
        return text
    return Html(_esc('' if text is None else text).replace('\n', '<br>'))


def _join(parts):
    result = []
    for part in parts:
        if part is None or part is False:
            continue
        if isinstance(part, Html):
            result.append(str(part))
        elif isinstance(part, (list, tuple)):
            result.append(_join(part))
        else:
            result.append(str(escape(str(part))))
    return ''.join(result)


def _tag(tag, parts, attrs=()):
    rendered = ''.join(
        ' {}'.format(key) if value is True else ' {}="{}"'.format(key, _attr(value))
        for key, value in attrs if value is not None and value is not False
    )
    return Html('<{0}{1}>{2}</{0}>'.format(tag, rendered, _join(parts)))


def b(*parts): return _tag('b', parts)                   # noqa: E704
def i(*parts): return _tag('i', parts)                   # noqa: E704
def u(*parts): return _tag('u', parts)                   # noqa: E704
def s(*parts): return _tag('s', parts)                   # noqa: E704
def code(*parts): return _tag('code', parts)             # noqa: E704
def spoiler(*parts): return _tag('tg-spoiler', parts)    # noqa: E704
def mark(*parts): return _tag('mark', parts)             # noqa: E704
def sub(*parts): return _tag('sub', parts)               # noqa: E704
def sup(*parts): return _tag('sup', parts)               # noqa: E704
def br(): return Html('<br>')                            # noqa: E704
def hr(): return Html('<hr/>')                           # noqa: E704
def p(*parts): return _tag('p', parts)                   # noqa: E704
def footer(*parts): return _tag('footer', parts)         # noqa: E704


def link(text, url):
    """``<a href>``; ``mailto:``, ``tel:`` and ``tg://user?id=`` links work too."""
    return _tag('a', (text,), (('href', url),))


def mention(text, user_id):
    """Inline mention of a user by id."""
    return link(text, 'tg://user?id={}'.format(int(user_id)))


def emoji(document_id, alt=''):
    """Custom emoji; ``alt`` is the fallback emoji."""
    return Html('<tg-emoji emoji-id="{}">{}</tg-emoji>'.format(int(document_id), _esc(alt)))


def time(date, text=None, format=''):
    """
    ``<tg-time>``: ``date`` is a unix timestamp or `datetime`, ``format`` the
    Bot API letters (``r`` relative, ``w`` weekday, ``d``/``D`` date, ``t``/``T`` time).
    """
    return _tag('tg-time', (text if text not in (None, '') else _default_date_text(date),),
                (('unix', _unix(date)), ('format', format or None)))


def math(source, block=False):
    """LaTeX formula, inline or as a block."""
    return _tag('tg-math-block' if block else 'tg-math', (str(source),))


def button(text, *, url=None, data=None, copy_text=None, web_app=None,
           query=None, current_chat=False, style=None):
    """
    Inline ``<tg-button>`` inside text. Pass exactly one of ``url``,
    ``data`` (callback), ``copy_text``, ``web_app`` (url) or ``query``
    (switch inline; ``current_chat=True`` for the same chat). ``style`` is
    ``'primary'``, ``'danger'``, ``'success'`` or ``'link'``.
    """
    if url is not None:
        attrs = [('type', 'url'), ('url', url)]
    elif data is not None:
        if isinstance(data, bytes):
            data = data.decode('utf-8', 'replace')
        attrs = [('type', 'callback_data'), ('data', data)]
    elif copy_text is not None:
        attrs = [('type', 'copy_text'), ('text', copy_text)]
    elif web_app is not None:
        attrs = [('type', 'web_app'), ('url', web_app)]
    elif query is not None:
        attrs = [('type', 'switch_inline_query_current_chat' if current_chat
                  else 'switch_inline_query'), ('query', query)]
    else:
        attrs = [('type', 'disabled')]
    if style:
        attrs.insert(1, ('style', style))
    return _tag('tg-button', (text,), attrs)


def h(level, *parts):
    """Heading ``<h1>``…``<h6>``."""
    level = min(max(int(level), 1), 6)
    return _tag('h{}'.format(level), parts)


def pre(source, language=None):
    """Code block, optionally with a language."""
    source = _esc(source)
    if language:
        return Html('<pre><code class="language-{}">{}</code></pre>'.format(_attr(language), source))
    return Html('<pre>{}</pre>'.format(source))


def quote(*parts, expandable=False, cite=None):
    """``<blockquote>`` (``expandable`` collapses it), with an optional author."""
    inner = list(parts)
    if cite is not None:
        inner.append(_tag('cite', (cite,)))
    return _tag('blockquote', inner, (('expandable', bool(expandable)),))


def details(summary, *body, open=False):
    """Collapsible ``<details>`` block; ``body`` may contain other blocks."""
    return _tag('details', ((_tag('summary', (summary,))),) + body, (('open', bool(open)),))


def li(*parts, checked=None, value=None):
    """
    List item: ``checked=True/False`` adds a checkbox, ``value`` sets the
    number of an ordered item.
    """
    box = None
    if checked is not None:
        box = Html('<input type="checkbox" checked>' if checked else '<input type="checkbox">')
    return _tag('li', (box,) + parts, (('value', value),))


def _items(items):
    return [item if isinstance(item, Html) and item.startswith('<li') else li(item)
            for item in items]


def ul(*items):
    """Unordered list; items are text/`Html` or `li` results."""
    return _tag('ul', _items(items))


def ol(*items, start=None, type=None, reversed=False):
    """Ordered list; ``type`` is ``'1'``, ``'a'``, ``'A'``, ``'i'`` or ``'I'``."""
    return _tag('ol', _items(items), (('start', start), ('type', type), ('reversed', bool(reversed))))


def table(rows, *, header=False, caption=None, bordered=False, striped=False, compact=False):
    """
    ``<table>`` from a list of rows (lists of cells). ``header=True`` makes
    the first row a header row.
    """
    body = []
    for index, row in enumerate(rows):
        tag = 'th' if header and index == 0 else 'td'
        body.append(_tag('tr', [_tag(tag, (cell,)) for cell in row]))
    if caption is not None:
        body.insert(0, _tag('caption', (caption,)))
    return _tag('table', body, (('bordered', bool(bordered)), ('striped', bool(striped)),
                                ('compact', bool(compact))))


def media(src, kind='photo', *, caption=None, spoiler=False):
    """
    Media block: ``src`` is an HTTP(S) URL or the id of an
    :tl:`InputRichFile` (turned into ``tg://<kind>?id=<src>``). ``kind`` is
    ``'photo'``, ``'video'``, ``'audio'`` or ``'document'``.
    """
    src = str(src)
    if '://' not in src:
        src = 'tg://{}?id={}'.format(kind, src)
    if kind == 'photo':
        element = '<img src="{}"{}/>'.format(_attr(src), ' tg-spoiler' if spoiler else '')
    elif kind == 'video':
        element = '<video src="{}"{}></video>'.format(_attr(src), ' tg-spoiler' if spoiler else '')
    elif kind == 'audio':
        element = '<audio src="{}"></audio>'.format(_attr(src))
    else:
        element = '<tg-document src="{}"></tg-document>'.format(_attr(src))
    if caption is None:
        return Html(element)
    return Html('<figure>{}<figcaption>{}</figcaption></figure>'.format(element, _join((caption,))))


class Builder:
    """
    Accumulates rich HTML blocks::

        page = (rich.Builder()
                .h(1, 'Title')
                .p('Some ', rich.b('bold'), ' text')
                .pre('print(1)', 'python')
                .quote('Wise words', cite='Someone'))
        await client(SendMessageRequest(peer, '', rich_message=page.to_input()))

    Every block method returns the builder; ``str(builder)`` / ``.html``
    gives the HTML.
    """

    def __init__(self, *blocks):
        self.blocks = []
        self.add(*blocks)

    def add(self, *blocks):
        """Appends blocks (`Html` as is, anything else escaped as a paragraph)."""
        for block in blocks:
            if block is None:
                continue
            self.blocks.append(block if isinstance(block, Html) else p(block))
        return self

    def p(self, *parts): return self.add(p(*parts))                          # noqa: E704
    def h(self, level, *parts): return self.add(h(level, *parts))            # noqa: E704
    def pre(self, source, language=None): return self.add(pre(source, language))  # noqa: E704
    def hr(self): return self.add(hr())                                      # noqa: E704
    def footer(self, *parts): return self.add(footer(*parts))                # noqa: E704

    def quote(self, *parts, expandable=False, cite=None):
        return self.add(quote(*parts, expandable=expandable, cite=cite))

    def details(self, summary, *body, open=False):
        return self.add(details(summary, *body, open=open))

    def ul(self, *items): return self.add(ul(*items))                        # noqa: E704

    def ol(self, *items, start=None, type=None, reversed=False):
        return self.add(ol(*items, start=start, type=type, reversed=reversed))

    def table(self, rows, **kwargs): return self.add(table(rows, **kwargs))  # noqa: E704

    def media(self, src, kind='photo', **kwargs):
        return self.add(media(src, kind, **kwargs))

    def math(self, source): return self.add(math(source, block=True))       # noqa: E704

    @property
    def html(self):
        return Html('\n'.join(self.blocks))

    def __str__(self):
        return str(self.html)

    def to_input(self, *, files=None, rtl=None, noautolink=None):
        """:tl:`InputRichMessageHTML` with the built HTML."""
        return to_input(str(self), files=files, rtl=rtl, noautolink=noautolink)

# endregion Builder
