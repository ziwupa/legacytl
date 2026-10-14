"""
Parallel file transfers.

Telegram throttles every MTProto connection on its own, which is what caps
a sequential download or upload at a few MB/s. Big transfers are therefore
split in parts that ``workers`` coroutines move at the same time, each one
over its own connection to the data center that holds the file:

* worker 0 uses the connection the sequential code would use (the client's
  own sender, or the one borrowed through ``_borrow_exported_sender``);
* the other workers open extra connections that reuse the *same*
  authorization key (a new MTProto session per connection, so no
  ``auth.exportAuthorization`` nor key generation is needed). Those are kept
  in a small per-client pool for a while after the transfer so that
  consecutive transfers don't pay for the handshake again.

Downloads keep at most ``window`` parts in memory and hand them out strictly
in order; uploads read the file in order (so MD5 and encryption stay
sequential) and send up to ``window`` parts ahead.

Errors are handled per part: transient server errors are retried with a
backoff, flood waits up to ``flood_sleep_threshold`` pause *all* workers
(``FLOOD_PREMIUM_WAIT`` also retires one worker, slowing down the transfer
as Telegram asks), a dropped connection moves its part to another worker,
and anything else aborts the whole transfer. Cancelling the caller cancels
the workers and closes their connections.

This module only deals with "senders" through ``send(request)`` (returning
an awaitable), ``disconnect()`` and ``is_connected()``, so tests can drive
it with fakes.
"""
import asyncio
import collections
import copy
import logging
import time
import weakref

from .. import errors, helpers

#: Size of the parts of parallel transfers (the maximum for both
#: ``upload.getFile`` and ``upload.saveBigFilePart``).
PART_SIZE = 512 * 1024

#: ``upload.getFile`` limits: ``limit`` multiple of 4 KiB, a request must not
#: cross a 1 MiB boundary (``1 MiB % limit == 0`` and ``offset % limit == 0``).
MIN_PART_SIZE = 4096
MiB = 1024 * 1024

#: Files smaller than this are transferred sequentially unless ``workers``
#: is given explicitly (it's also where Telegram switches to "big" uploads).
PARALLEL_THRESHOLD = 10 * MiB

#: Upper bound for ``workers``.
MAX_WORKERS = 16

#: How many times a part is retried on transient errors.
PART_RETRIES = 5

#: Seconds an idle extra connection is kept for the next transfer.
SENDER_IDLE_TIMEOUT = 60

_log = logging.getLogger(__name__)


def auto_workers(size):
    """Default amount of workers for a file of ``size`` bytes."""
    if not size or size < PARALLEL_THRESHOLD:
        return 1
    if size < 64 * MiB:
        return 4
    return 8


def resolve_workers(workers, default, size, part_size=PART_SIZE):
    """
    Final amount of workers given the ``workers=`` argument, the client-wide
    ``default`` (both `None` meaning "automatic") and the file ``size``.
    """
    if workers is None:
        workers = default
    if workers is None:
        workers = auto_workers(size)
    try:
        workers = int(workers)
    except (TypeError, ValueError):
        raise TypeError('workers must be an int or None, not {!r}'.format(workers)) from None
    workers = max(1, min(workers, MAX_WORKERS))
    if size:
        workers = min(workers, max(1, -(-size // part_size)))
    return workers


def valid_download_part_size(part_size, offset=0):
    """Whether ``upload.getFile`` accepts ``part_size`` at ``offset`` (and every next one)."""
    return (MIN_PART_SIZE <= part_size <= MiB
            and part_size % MIN_PART_SIZE == 0
            and MiB % part_size == 0
            and offset % part_size == 0)


class _Retire(Exception):
    """Raised inside a worker that should stop and leave its part to the rest."""


def _transient_errors():
    names = ('RpcCallFailError', 'RpcMcgetFailError', 'InterdcCallErrorError',
             'InterdcCallRichErrorError')
    found = [errors.ServerError, errors.TimedOutError]
    for name in names:
        cls = getattr(errors, name, None)
        if isinstance(cls, type):
            found.append(cls)
    return tuple(found)


def _connection_errors():
    found = [ConnectionError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
             errors.AuthKeyNotFound, errors.InvalidBufferError]
    return tuple(found)


def _is_premium_wait(error):
    return type(error).__name__ == 'FloodPremiumWaitError' or 'PREMIUM' in (error.message or '').upper()


async def _send(sender, request):
    """
    Sends ``request`` through ``sender`` and waits for the result. A future
    cancelled by the sender (because it disconnected) becomes a
    `ConnectionError` instead of a `asyncio.CancelledError` which would be
    indistinguishable from the cancellation of the transfer itself.
    """
    future = asyncio.ensure_future(sender.send(request))
    try:
        await asyncio.wait((future,))
    except asyncio.CancelledError:
        future.cancel()
        raise
    if future.cancelled():
        raise ConnectionError('the request was cancelled by its connection')
    return future.result()


class _Transfer:
    """Common machinery: workers, their senders, flood gate and retries."""

    def __init__(self, *, primary, open_sender=None, close_sender=None, workers=1,
                 window=None, flood_sleep_threshold=60, retries=PART_RETRIES,
                 retry_delay=0.5, log=None):
        self._primary = primary
        self._open_sender = open_sender
        self._close_sender = close_sender
        self.workers = max(1, int(workers))
        self.window = max(self.workers, int(window or 2 * self.workers))
        self._flood_threshold = flood_sleep_threshold
        self._retries = retries
        self._retry_delay = retry_delay
        self._log = log or _log

        self._waiters = []
        self._alive = 0
        self._error = None
        self._tasks = []
        self._extra = []      # extra senders opened for this transfer
        self._broken = set()  # ids of senders that failed and must not be reused
        self._flood_until = 0.0
        self._generation = 0  # bumped whenever the requests' input changes
        self._started = False
        self._closed = False
        self._loop = None

        self._transient = _transient_errors()
        self._connection = _connection_errors()

        #: Statistics, useful for tests and benchmarks.
        self.in_flight = 0
        self.peak_in_flight = 0
        self.requests = 0
        self.retried = 0
        self.flood_waits = 0
        self.retired = 0
        self.connections = 1

    # Synchronisation (single-threaded: wake everyone after every change)

    def _wake(self):
        waiters, self._waiters = self._waiters, []
        for future in waiters:
            if not future.done():
                future.set_result(None)

    async def _wait(self):
        future = asyncio.get_running_loop().create_future()
        self._waiters.append(future)
        await future

    def _fail(self, error):
        if self._error is None:
            self._error = error
        self._wake()

    def _check(self):
        if self._error is not None:
            raise self._error

    # Lifecycle

    def start(self):
        if self._started:
            return
        self._started = True
        loop = self._loop = asyncio.get_running_loop()
        self._alive = self.workers
        self._tasks = [loop.create_task(self._run_worker(n)) for n in range(self.workers)]

    def cancel_nowait(self):
        """Cancels the workers without waiting."""
        for task in self._tasks:
            if not task.done():
                task.cancel()

    def abandon(self):
        """
        Closes the transfer in the background (for finalizers of iterators
        which were dropped before being exhausted or closed).
        """
        loop = self._loop
        if self._closed or loop is None:
            return
        if loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._spawn_close)
        except RuntimeError:  # loop closed meanwhile
            pass

    def _spawn_close(self):
        if not self._closed:
            asyncio.get_running_loop().create_task(self.close(success=False))

    async def close(self, *, success=None):
        """
        Stops all workers and releases their senders. Extra senders are kept
        for later transfers only if the transfer completed successfully.
        """
        if self._closed:
            return
        self._closed = True
        if success is None:
            success = self._error is None and self._done()

        self.cancel_nowait()
        tasks = self._tasks
        if tasks:
            # Shield the clean-up so that cancelling the caller (which may be
            # why we're closing) doesn't leave connections behind.
            gathering = asyncio.gather(*tasks, return_exceptions=True)
            try:
                await asyncio.shield(gathering)
            except asyncio.CancelledError:
                await asyncio.shield(self._release_all(success=False))
                raise

        await asyncio.shield(self._release_all(success=success))
        self._wake()

    async def _release_all(self, *, success):
        extra, self._extra = self._extra, []
        for sender in extra:
            reusable = success and id(sender) not in self._broken
            try:
                if self._close_sender is not None:
                    await self._close_sender(sender, reusable)
                else:
                    await sender.disconnect()
            except Exception:
                self._log.exception('Error while releasing a transfer connection')

    def _done(self):
        raise NotImplementedError

    # Workers

    async def _run_worker(self, n):
        retired = False
        try:
            if n == 0 or self._open_sender is None:
                sender = self._primary
            else:
                try:
                    sender = await self._open_sender()
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    self._log.warning('Could not open connection #%d for a parallel '
                                      'transfer, continuing with less: %s: %s',
                                      n, e.__class__.__name__, e)
                    return
                self._extra.append(sender)
                self.connections += 1

            try:
                await self._work(sender)
            except _Retire:
                retired = True
        except asyncio.CancelledError:
            raise
        except BaseException as e:
            self._fail(e)
        finally:
            if not retired:
                self._alive -= 1
            self._wake()

    def _try_retire(self):
        """Whether the calling worker may stop (it's not the last one)."""
        if self._alive > 1:
            self._alive -= 1
            self.retired += 1
            return True
        return False

    async def _flood_gate(self):
        loop = asyncio.get_running_loop()
        while True:
            delay = self._flood_until - loop.time()
            if delay <= 0:
                return
            await asyncio.sleep(delay)

    async def _invoke(self, sender, make_request):
        """
        Invokes ``make_request()`` (a new request every attempt) on
        ``sender`` dealing with retries, flood waits and dead connections.
        """
        loop = asyncio.get_running_loop()
        attempt = 0
        while True:
            await self._flood_gate()
            self._check()
            generation = self._generation
            self.in_flight += 1
            self.peak_in_flight = max(self.peak_in_flight, self.in_flight)
            self.requests += 1
            try:
                return await _send(sender, make_request())
            except errors.RPCError as e:
                seconds = getattr(e, 'seconds', None)
                if isinstance(e, errors.FloodError) and isinstance(seconds, int):
                    seconds = max(seconds, 1)  # FLOOD_WAIT_0 has been seen
                    if seconds > self._flood_threshold:
                        raise
                    self.flood_waits += 1
                    self._log.info('Parallel transfer got %s, all workers pause for %ds',
                                   e.__class__.__name__, seconds)
                    self._flood_until = max(self._flood_until, loop.time() + seconds)
                    if _is_premium_wait(e) and self._try_retire():
                        self._log.info('Slowing down: one less worker (%d left)', self._alive)
                        raise _Retire from e
                    continue

                if isinstance(e, self._transient):
                    attempt += 1
                    if attempt > self._retries:
                        raise
                    self.retried += 1
                    self._log.info('Retrying part after %s (attempt %d)', e.__class__.__name__, attempt)
                    await asyncio.sleep(min(self._retry_delay * 2 ** (attempt - 1), 8))
                    continue

                if await self._handle_rpc_error(e, generation):
                    continue
                raise
            except self._connection as e:
                self._broken.add(id(sender))
                if self._try_retire():
                    self._log.info('A transfer connection failed (%s: %s); its part is '
                                   'moved to the other workers', e.__class__.__name__, e)
                    raise _Retire from e
                # Last worker: retry only if its sender is still trying to
                # reconnect (a disconnected sender won't come back by itself)
                attempt += 1
                is_connected = getattr(sender, 'is_connected', None)
                if (attempt > self._retries or sender is not self._primary
                        or not callable(is_connected) or not is_connected()):
                    raise
                self.retried += 1
                await asyncio.sleep(min(self._retry_delay * 2 ** (attempt - 1), 8))
            finally:
                self.in_flight -= 1

    async def _handle_rpc_error(self, error, generation):
        """
        Return `True` to retry the request after handling ``error``.
        ``generation`` is the value `_generation` had when it was sent.
        """
        return False

    async def _work(self, sender):
        raise NotImplementedError


class ParallelDownload(_Transfer):
    """
    Downloads parts ``first .. first + count - 1`` (``make_request(index)``
    returns the request for a part, ``parse(result)`` its bytes). Parts are
    obtained in order through `next`; a part shorter than ``part_size`` is
    treated as the end of the file.
    """

    def __init__(self, *, make_request, first, count, part_size, parse=None,
                 refresh=None, **kwargs):
        super().__init__(**kwargs)
        self._make_request = make_request
        self._parse = parse or (lambda result: result.bytes)
        self._refresh = refresh
        self._refresh_lock = asyncio.Lock()
        self._refreshes = 0
        self._part_size = part_size
        self._end = first + count
        self._next_fetch = first
        self._next_get = first
        self._parts = {}
        self._fetching = set()
        self._retry = collections.deque()
        self._eof = None  # index of the last (short) part, once seen

    def _limit(self):
        return self._end if self._eof is None else min(self._end, self._eof + 1)

    def _done(self):
        return self._next_get >= self._limit()

    async def _take(self):
        while True:
            if self._error is not None:
                return None
            limit = self._limit()
            while self._retry:
                index = self._retry.popleft()
                if index < limit:
                    self._fetching.add(index)
                    return index
            if self._next_fetch >= limit:
                if not self._fetching:
                    return None
            elif self._next_fetch < self._next_get + self.window:
                index = self._next_fetch
                self._next_fetch += 1
                self._fetching.add(index)
                return index
            await self._wait()

    async def _work(self, sender):
        while True:
            index = await self._take()
            if index is None:
                return
            try:
                result = await self._invoke(sender, lambda: self._make_request(index))
                data = self._parse(result)
            except BaseException:
                self._fetching.discard(index)
                self._retry.appendleft(index)
                raise
            self._fetching.discard(index)
            if len(data) < self._part_size and (self._eof is None or index < self._eof):
                self._eof = index
            self._parts[index] = data
            self._wake()

    async def _handle_rpc_error(self, error, generation):
        if self._refresh is None or not isinstance(error, (
                errors.FileReferenceExpiredError, errors.FilerefUpgradeNeededError)):
            return False
        # Only one worker refreshes; the rest (including those whose request
        # was sent before the refresh finished) just retry with the result
        async with self._refresh_lock:
            if self._generation != generation:
                return True
            if self._refreshes >= 3:
                return False
            self._refreshes += 1
            if not await self._refresh(error):
                return False
            self._generation += 1
            return True

    async def next(self):
        """The next part in order, or `None` after the last one."""
        index = self._next_get
        while index not in self._parts:
            self._check()
            if index >= self._limit():
                return None
            if self._alive <= 0:
                self._check()
                raise RuntimeError('parallel download stopped before part {}'.format(index))
            await self._wait()
        data = self._parts.pop(index)
        self._next_get += 1
        self._wake()
        return data


class ParallelUpload(_Transfer):
    """
    Uploads parts given in order through `put` (``make_request(index, data)``
    returns the request), then `join` waits for all of them. ``progress`` is
    called (from `put`/`join`) with the amount of bytes confirmed so far,
    always increasing, and finally with all of them.
    """

    def __init__(self, *, make_request, part_count, total=None, progress=None, **kwargs):
        super().__init__(**kwargs)
        self._make_request = make_request
        self._part_count = part_count
        self._total = total
        self._progress = progress
        self._queue = collections.deque()
        self._sending = 0
        self._reading_done = False
        self._acked = 0
        self._acked_bytes = 0
        self._reported = 0

    def _done(self):
        return self._acked >= self._part_count

    async def put(self, index, data, size=None):
        """Queues part ``index``; waits while ``window`` parts are pending."""
        await self._report()
        while len(self._queue) + self._sending >= self.window:
            self._check()
            await self._report()
            if self._alive <= 0:
                raise RuntimeError('parallel upload stopped before part {}'.format(index))
            await self._wait()
        self._check()
        self._queue.append((index, data, len(data) if size is None else size))
        self._wake()

    async def join(self):
        """Waits until every part was confirmed by Telegram."""
        self._reading_done = True
        self._wake()
        while not self._done():
            self._check()
            await self._report()
            if self._alive <= 0:
                raise RuntimeError('parallel upload stopped with {} of {} parts sent'
                                   .format(self._acked, self._part_count))
            await self._wait()
        self._check()
        await self._report()

    async def _take(self):
        while True:
            if self._error is not None:
                return None
            if self._queue:
                self._sending += 1
                return self._queue.popleft()
            if self._reading_done and not self._sending:
                return None
            await self._wait()

    async def _work(self, sender):
        while True:
            item = await self._take()
            if item is None:
                return
            index, data, size = item
            try:
                result = await self._invoke(sender, lambda: self._make_request(index, data))
            except BaseException:
                self._sending -= 1
                self._queue.appendleft(item)
                self._wake()
                raise
            self._sending -= 1
            if not result:
                raise RuntimeError('Failed to upload file part {}.'.format(index))
            self._acked += 1
            self._acked_bytes += size
            self._wake()

    async def _report(self):
        # Called from the task that feeds the parts (`put`/`join`), like the
        # sequential upload did, so callbacks never run concurrently and an
        # exception raised by them reaches the caller
        if not self._progress:
            return
        value = self._acked_bytes
        if value <= self._reported:
            return
        self._reported = value
        await helpers._maybe_await(self._progress(value, self._total))


# region Client integration

class _NullQueue:
    """Updates queue for extra senders: they never need to deliver updates."""
    @staticmethod
    def put_nowait(item):
        pass


class _SenderPool:
    """Idle extra senders of one client, closed after `SENDER_IDLE_TIMEOUT`."""

    def __init__(self):
        self._idle = {}  # (dc_id, key_id) -> [(sender, since)]
        self._timer = None
        self._watched = None

    def watch(self, main_sender):
        """Closes the idle senders as soon as the client's own sender disconnects."""
        try:
            future = main_sender._disconnected
        except Exception:
            return
        if isinstance(future, asyncio.Future) and future is not self._watched:
            self._watched = future
            future.add_done_callback(self._on_main_disconnected)

    def _on_main_disconnected(self, future):
        if future is self._watched:
            self._watched = None
        loop = future.get_loop()
        if self._idle and not loop.is_closed():
            loop.create_task(self.close())

    def take(self, dc_id, key_id):
        senders = self._idle.get((dc_id, key_id))
        while senders:
            sender, _ = senders.pop()
            if sender.is_connected():
                return sender
        return None

    def give(self, dc_id, key_id, sender):
        self._idle.setdefault((dc_id, key_id), []).append((sender, time.monotonic()))
        if self._timer is None:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(SENDER_IDLE_TIMEOUT, self._on_timer, loop)

    def _on_timer(self, loop):
        self._timer = None
        if not loop.is_closed():
            loop.create_task(self.close(older_than=SENDER_IDLE_TIMEOUT))

    async def close(self, older_than=None):
        now = time.monotonic()
        to_close = []
        for key, senders in list(self._idle.items()):
            keep = []
            for sender, since in senders:
                if older_than is None or now - since >= older_than - 0.5:
                    to_close.append(sender)
                else:
                    keep.append((sender, since))
            if keep:
                self._idle[key] = keep
            else:
                del self._idle[key]

        for sender in to_close:
            try:
                await sender.disconnect()
            except Exception:
                _log.exception('Error while closing an idle transfer connection')

        if self._idle and self._timer is None:
            loop = asyncio.get_running_loop()
            self._timer = loop.call_later(SENDER_IDLE_TIMEOUT / 2, self._on_timer, loop)
        elif not self._idle and self._timer is not None:
            self._timer.cancel()
            self._timer = None


_pools = weakref.WeakKeyDictionary()


def get_pool(client):
    try:
        return _pools[client]
    except KeyError:
        return _pools.setdefault(client, _SenderPool())
    except TypeError:  # not weak-referenceable / hashable
        pool = getattr(client, '_transfer_pool', None)
        if pool is None:
            pool = _SenderPool()
            client._transfer_pool = pool
        return pool


def _layer():
    # Looked up every time, like the client does for its own connection
    from ..tl import alltlobjects
    return alltlobjects.LAYER


async def open_sender(client, dc_id, primary):
    """
    A connected sender to ``dc_id`` sharing ``primary``'s authorization key
    (taken from the pool if possible).
    """
    from ..crypto import AuthKey
    from ..network import MTProtoSender
    from ..tl import functions

    key = primary.auth_key
    if not key:
        raise ConnectionError('the primary connection has no authorization key')

    pool = get_pool(client)
    sender = pool.take(dc_id, key.key_id)
    if sender is not None:
        return sender

    session = client.session
    if dc_id == session.dc_id:
        address, port = session.server_address, session.port
    else:
        dc = await client._get_dc(dc_id)
        address, port = dc.ip_address, dc.port

    sender = MTProtoSender(
        AuthKey(key.key),
        loggers=client._log,
        retries=client._connection_retries,
        delay=client._retry_delay,
        auto_reconnect=client._auto_reconnect,
        connect_timeout=client._timeout,
        updates_queue=_NullQueue(),
    )
    sender.dc_id = dc_id
    # Same clock correction and salt as the primary: saves a round-trip
    # fixing them through bad_msg_notification / bad_server_salt
    try:
        sender._state.time_offset = primary._state.time_offset
        sender._state.salt = primary._state.salt
    except AttributeError:
        pass

    try:
        await sender.connect(client._connection(
            address, port, dc_id,
            loggers=client._log,
            proxy=client._proxy,
            local_addr=client._local_addr
        ))
        init = copy.copy(client._init_request)
        init.query = functions.help.GetNearestDcRequest()
        await _send(sender, functions.InvokeWithLayerRequest(
            _layer(), functions.InvokeWithoutUpdatesRequest(init)))
    except BaseException:
        await asyncio.shield(sender.disconnect())
        raise
    return sender


def sender_factory(client, dc_id, primary):
    """``(open_sender, close_sender)`` callables for `ParallelDownload`/`ParallelUpload`."""
    key_id = primary.auth_key.key_id if primary.auth_key else None

    async def open_():
        return await open_sender(client, dc_id, primary)

    async def close(sender, reusable):
        main = getattr(client, '_sender', None)
        main_alive = main is not None and main.is_connected()
        if reusable and main_alive and sender.is_connected() and key_id is not None:
            pool = get_pool(client)
            pool.give(dc_id, key_id, sender)
            pool.watch(main)
        else:
            await sender.disconnect()

    return open_, close


async def close_pool(client):
    """Disconnects every idle extra sender of ``client``."""
    try:
        pool = _pools.get(client) or getattr(client, '_transfer_pool', None)
    except TypeError:
        pool = getattr(client, '_transfer_pool', None)
    if pool is not None:
        await pool.close()

# endregion
