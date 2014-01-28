# -*- test-case-name: vumi.tests.test_sentry -*-

import logging

from twisted.python import log
from twisted.web.client import HTTPClientFactory, _makeGetterFactory
from twisted.internet.defer import DeferredList
from twisted.application.service import Service


DEFAULT_LOG_CONTEXT_SENTINEL = "_SENTRY_CONTEXT_"


class QuietHTTPClientFactory(HTTPClientFactory):
    """HTTP client factory that doesn't log starting and stopping."""
    noisy = False


def quiet_get_page(url, contextFactory=None, *args, **kwargs):
    """A version of getPage that uses QuietHTTPClientFactory."""
    return _makeGetterFactory(
        url,
        QuietHTTPClientFactory,
        contextFactory=contextFactory,
        *args, **kwargs).deferred


def vumi_raven_client(dsn, log_context_sentinel=None):
    """Construct a custom raven client and transport-set pair.

    The raven client assumes that sends via transports return success or
    failure immediate in a blocking fashion and doesn't provide transports
    access to the client.

    We circumvent this by constructing a once-off transport class and
    raven client pair that work together. Instances of the transport feed
    information back success and failure back to the client instance once
    deferreds complete.

    Pull-requests with better solutions welcomed.
    """

    import raven
    try:
        from raven.transport.twisted import TwistedHTTPTransport
    except ImportError:
        # Prior to 3.6, TwistedHTTPTransport lived elsewhere.
        from raven.transport.base import TwistedHTTPTransport
    from raven.transport.registry import TransportRegistry

    deferreds = set()

    class VumiRavenHTTPTransport(TwistedHTTPTransport):

        scheme = ['http', 'https']

        def async_send(self, data, headers, success_cb, failure_cb):
            d = quiet_get_page(self._url, method='POST',
                               postdata=data, headers=headers)
            deferreds.add(d)
            d.addBoth(self._untrack_deferred, d)
            d.addCallback(lambda r: success_cb())
            d.addErrback(lambda f: failure_cb(f.value))

        def _untrack_deferred(self, result, d):
            deferreds.discard(d)
            return result

    class VumiRavenClient(raven.Client):

        _registry = TransportRegistry(transports=[
            VumiRavenHTTPTransport
        ])

        def wait(self):
            return DeferredList(deferreds)

    return VumiRavenClient(dsn)


class SentryLogObserver(object):
    """Twisted log observer that logs to a Raven Sentry client."""

    DEFAULT_ERROR_LEVEL = logging.ERROR
    DEFAULT_LOG_LEVEL = logging.INFO
    LOG_LEVEL_THRESHOLD = logging.WARN

    def __init__(self, client, logger_name, worker_id,
                 log_context_sentinel=None):
        if log_context_sentinel is None:
            log_context_sentinel = DEFAULT_LOG_CONTEXT_SENTINEL
        self.client = client
        self.logger_name = logger_name
        self.worker_id = worker_id
        self.log_context_sentinel = log_context_sentinel
        self.log_context = {self.log_context_sentinel: True}

    def level_for_event(self, event):
        level = event.get('logLevel')
        if level is not None:
            return level
        if event.get('isError'):
            return self.DEFAULT_ERROR_LEVEL
        return self.DEFAULT_LOG_LEVEL

    def logger_for_event(self, event):
        system = event.get('system', '-')
        parts = [self.logger_name]
        if system != '-':
            parts.extend(system.split(','))
        logger = ".".join(parts)
        return logger.lower()

    def _log_to_sentry(self, event):
        level = self.level_for_event(event)
        if level < self.LOG_LEVEL_THRESHOLD:
            return
        data = {
            "logger": self.logger_for_event(event),
            "level": level,
        }
        tags = {
            "worker-id": self.worker_id,
        }
        failure = event.get('failure')
        if failure:
            exc_info = (failure.type, failure.value, failure.tb)
            self.client.captureException(exc_info, data=data, tags=tags)
        else:
            msg = log.textFromEventDict(event)
            self.client.captureMessage(msg, data=data, tags=tags)

    def __call__(self, event):
        if self.log_context_sentinel in event:
            return
        log.callWithContext(self.log_context, self._log_to_sentry, event)


class SentryLoggerService(Service):

    def __init__(self, dsn, logger_name, worker_id, logger=None):
        self.setName('Sentry Logger')
        self.dsn = dsn
        self.client = vumi_raven_client(dsn=dsn)
        self.sentry_log_observer = SentryLogObserver(self.client,
                                                     logger_name,
                                                     worker_id)
        self.logger = logger if logger is not None else log.theLogPublisher

    def startService(self):
        self.logger.addObserver(self.sentry_log_observer)
        return Service.startService(self)

    def stopService(self):
        if self.running:
            self.logger.removeObserver(self.sentry_log_observer)
            return self.client.wait()
        return Service.stopService(self)

    def registered(self):
        return self.sentry_log_observer in self.logger.observers
