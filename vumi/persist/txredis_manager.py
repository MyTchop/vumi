# -*- test-case-name: vumi.persist.tests.test_txredis_manager -*-

# txredis is made of silliness.
# There are two variants, both of which call themselves version 2.2. One has
# everything in txredis.protocol, the other has the client stuff in
# txredis.client.
try:
    import txredis.client as txrc
    txr = txrc
except ImportError:
    import txredis.protocol as txrp
    txr = txrp

from twisted.internet import reactor
from twisted.internet.defer import (
    inlineCallbacks, DeferredList, succeed, Deferred)

from vumi.persist.redis_base import Manager
from vumi.persist.fake_redis import FakeRedis


class VumiRedis(txr.Redis):
    """Wrapper around txredis to make it more suitable for our needs.

    Aside from the various API operations we need to implement to match the
    other redis client, we add a deferred that fires when we've finished
    connecting to the redis server. This avoids problems with trying to use a
    client that hasn't completely connected yet.

    TODO: We need to find a way to test this stuff

    """

    def __init__(self, *args, **kw):
        super(VumiRedis, self).__init__(*args, **kw)
        self.connected_d = Deferred()
        self._disconnected_d = Deferred()
        self._client_shutdown_called = False

    def connectionMade(self):
        d = super(VumiRedis, self).connectionMade()
        d.addCallback(lambda _: self)
        return d.chainDeferred(self.connected_d)

    def connectionLost(self, reason):
        super(VumiRedis, self).connectionLost(reason)
        self._disconnected_d.callback(None)

    def _client_shutdown(self):
        """
        Issue a ``QUIT`` command and wait for the connection to close.

        A single client may be used by multiple manager instances, so we only
        issue the ``QUIT`` once. This still leaves us with a potential race
        condition if the connection is being used elsewhere, but we can't do
        anything useful about that here.
        """
        self.factory.stopTrying()
        d = succeed(None)
        if not self._client_shutdown_called:
            self._client_shutdown_called = True
            d.addCallback(lambda _: self.quit())
        return d.addCallback(lambda _: self._disconnected_d)

    def hget(self, key, field):
        d = super(VumiRedis, self).hget(key, field)
        d.addCallback(lambda r: r.get(field) if r else None)
        return d

    def lrem(self, key, value, num=0):
        return super(VumiRedis, self).lrem(key, value, count=num)

    # lpop() and rpop() are implemented in txredis 2.2.1 (which is in Ubuntu),
    # but not 2.2 (which is in pypi). Annoyingly, pop() in 2.2.1 calls lpop()
    # and rpop(), so we can't just delegate to that as we did before.

    def rpop(self, key):
        self._send('RPOP', key)
        return self.getResponse()

    def lpop(self, key):
        self._send('LPOP', key)
        return self.getResponse()

    def setex(self, key, seconds, value):
        return self.set(key, value, expire=seconds)

    # setnx() is implemented in txredis 2.2.1 (which is in Ubuntu), but not 2.2
    # (which is in pypi). Annoyingly, set() in 2.2.1 calls setnx(), so we can't
    # just delegate to that as we did before.

    def setnx(self, key, value):
        self._send('SETNX', key, value)
        return self.getResponse()

    def zadd(self, key, *args, **kwargs):
        if args:
            if len(args) % 2 != 0:
                raise ValueError("ZADD requires an equal number of "
                                 "values and scores")
        pieces = zip(args[::2], args[1::2])
        pieces.extend(kwargs.iteritems())
        orig_zadd = super(VumiRedis, self).zadd
        deferreds = [orig_zadd(key, member, score) for member, score in pieces]
        d = DeferredList(deferreds, fireOnOneErrback=True)
        d.addCallback(lambda results: sum([result for success, result
                                            in results if success]))
        return d

    def zrange(self, key, start, end, desc=False, withscores=False):
        return super(VumiRedis, self).zrange(key, start, end,
                                             withscores=withscores,
                                             reverse=desc)

    def zrangebyscore(self, key, min, max, start=None, num=None,
                     withscores=False, score_cast_func=float):
        d = super(VumiRedis, self).zrangebyscore(key, min, max,
                        offset=start, count=num, withscores=withscores)
        if withscores:
            d.addCallback(lambda r: [(v, score_cast_func(s)) for v, s in r])
        return d


class VumiRedisClientFactory(txr.RedisClientFactory):
    protocol = VumiRedis

    def buildProtocol(self, addr):
        self.client = self.protocol(*self._args, **self._kwargs)
        self.client.factory = self
        self.resetDelay()
        prev_d, self.deferred = self.deferred, Deferred()
        prev_d.callback(self.client)
        return self.client


class TxRedisManager(Manager):

    call_decorator = staticmethod(inlineCallbacks)

    @classmethod
    def _fake_manager(cls, fake_redis, manager_config):
        if fake_redis is None:
            fake_redis = FakeRedis(async=True)
        manager_config['config']['FAKE_REDIS'] = fake_redis
        manager = cls(fake_redis, **manager_config)
        # Because ._close() assumes a real connection.
        manager._close = fake_redis.teardown
        return succeed(manager)

    @classmethod
    def _manager_from_config(cls, client_config, manager_config):
        """Construct a manager from a dictionary of options.

        :param dict config:
            Dictionary of options for the manager.
        :param str key_prefix:
            Key prefix for namespacing.
        """

        host = client_config.pop('host', '127.0.0.1')
        port = client_config.pop('port', 6379)

        factory = VumiRedisClientFactory(**client_config)
        reactor.connectTCP(host, port, factory)

        d = factory.deferred.addCallback(lambda client: client.connected_d)
        d.addCallback(cls._make_manager, manager_config)
        return d

    @classmethod
    def _make_manager(cls, client, manager_config):
        manager = cls(client, **manager_config)
        cls._attach_reconnector(manager)
        return manager

    @staticmethod
    def _attach_reconnector(manager):
        def set_client(client):
            manager._client = client
            return client

        def reconnect(client):
            client.factory.deferred.addCallback(reconnect)
            return client.connected_d.addCallback(set_client)

        manager._client.factory.deferred.addCallback(reconnect)
        return manager

    def _close(self):
        """
        Close redis connection.
        """
        return self._client._client_shutdown()

    @inlineCallbacks
    def _purge_all(self):
        """Delete *ALL* keys whose names start with this manager's key prefix.

        Use only in tests.
        """
        # Given the races around connection closing, the easiest thing to do
        # here is to create a new manager with the same config for cleanup
        # operations.
        new_manager = yield self.from_config(self._config)
        # If we're a submanager we might have a different key prefix.
        new_manager._key_prefix = self._key_prefix
        yield new_manager._do_purge()
        yield new_manager._close()

    @inlineCallbacks
    def _do_purge(self):
        for key in (yield self.keys()):
            yield self.delete(key)

    def _make_redis_call(self, call, *args, **kw):
        """Make a redis API call using the underlying client library.
        """
        return getattr(self._client, call)(*args, **kw)

    def _filter_redis_results(self, func, results):
        """Filter results of a redis call.
        """
        return results.addCallback(func)
