import json
import logging
import time

from urlparse import urlparse, parse_qs
from StringIO import StringIO

from twisted.internet import reactor as _reactor
from twisted.internet.task import LoopingCall
from twisted.internet.defer import Deferred, DeferredQueue
from twisted.internet.protocol import Protocol
from twisted.internet.error import ConnectingCancelledError
from twisted.web.client import Agent
from twisted.web.client import HTTPConnectionPool
from twisted.web.http_headers import Headers
from twisted.web.client import FileBodyProducer

from . import utils
from .workers import SubscribeMessageWorker
from .pubnub_core import PubNubCore
from .managers import SubscriptionManager, PublishSequenceManager

from .enums import PNStatusCategory, PNHeartbeatNotificationOptions
from .errors import PNERR_CLIENT_ERROR, PNERR_CONNECTION_ERROR, \
    PNERR_SERVER_ERROR, PNERR_JSON_DECODING_FAILED
from .exceptions import PubNubException
from .structures import ResponseInfo

from .endpoints.pubsub.subscribe import Subscribe
from .endpoints.presence.leave import Leave
from .endpoints.presence.heartbeat import Heartbeat

logger = logging.getLogger("pubnub")


class PubNubResponse(Protocol):
    def __init__(self, finished, code):
        self.finished = finished
        self.code = code

    def dataReceived(self, body):
        self.finished.callback(TwistedResponse(body, self.code))


class TwistedSubscribeMessageWorker(SubscribeMessageWorker):
    def run(self):
        self._take_message()

    def _take_message(self):
        self._queue.get().addCallback(self.send_message_to_processing)

    def send_message_to_processing(self, message):
        if message is not None:
            self._pubnub.reactor.callInThread(self._process_incoming_payload, message)


class TwistedSubscriptionManager(SubscriptionManager):
    def __init__(self, pubnub_instance):
        self._message_queue = DeferredQueue()
        self.worker_loop = None
        self._heartbeat_loop = None
        self._heartbeat_call = None
        super(TwistedSubscriptionManager, self).__init__(pubnub_instance)

    def _announce_status(self, status):
        self._listener_manager.announce_status(status)

    def _start_worker(self):
        consumer = TwistedSubscribeMessageWorker(self._pubnub, self._listener_manager, self._message_queue, None)
        self.worker_loop = LoopingCall(consumer.run).start(0.1, False)

    def _set_consumer_event(self):
        raise NotImplementedError

    def _message_queue_put(self, message):
        self._message_queue.put(message)

    def _start_subscribe_loop(self):
        self._stop_subscribe_loop()

        combined_channels = self._subscription_state.prepare_channel_list(True)
        combined_groups = self._subscription_state.prepare_channel_group_list(True)

        if len(combined_channels) == 0 and len(combined_groups) == 0:
            return

        def continue_subscribe_loop(envelope):
            try:
                self._handle_endpoint_call(envelope.raw_result, envelope.status)
            except Exception as ex:
                return ex
            self._start_subscribe_loop()

        def manage_failure(failure):
            if failure.type == PubNubTwistedException:
                self._announce_status(failure.value.status)
                if failure.value.status.category in (PNStatusCategory.PNDisconnectedCategory,
                                                     PNStatusCategory.PNUnexpectedDisconnectCategory,
                                                     PNStatusCategory.PNCancelledCategory,
                                                     PNStatusCategory.PNBadRequestCategory,
                                                     PNStatusCategory.PNMalformedFilterExpressionCategory):
                    time.sleep(30)  # TODO: SET VALUE ACCORDING TO DOCS
                    self._start_subscribe_loop()
            else:
                print(failure)
                return failure

        try:
            self._subscribe_request_task = Subscribe(self._pubnub) \
                .channels(combined_channels) \
                .channel_groups(combined_groups) \
                .timetoken(self._timetoken) \
                .region(self._region) \
                .filter_expression(self._pubnub.config.filter_expression) \
                .deferred() \
                .addCallbacks(continue_subscribe_loop, manage_failure)

        except Exception as ex:
            raise ex

    def _stop_subscribe_loop(self):
        if self._subscribe_request_task is not None and not self._subscribe_request_task.called:
            self._subscribe_request_task.cancel()

    def _stop_heartbeat_timer(self):
        if self._heartbeat_call is not None:
            self._heartbeat_call.cancel()

        if self._heartbeat_loop is not None:
            self._heartbeat_loop.cancel()

    def _register_heartbeat_timer(self):
        super(TwistedSubscriptionManager, self)._register_heartbeat_timer()
        self._heartbeat_loop = LoopingCall(self._perform_heartbeat_loop)
        interval = self._pubnub.config.heartbeat_interval / 2 - 1
        self._heartbeat_loop.start(interval, True)

    def _perform_heartbeat_loop(self):
        def heartbeat_callback(_, status):
            heartbeat_verbosity = self._pubnub.config.heartbeat_notification_options
            if heartbeat_verbosity == PNHeartbeatNotificationOptions.ALL or (
               status.is_error() is True and heartbeat_verbosity == PNHeartbeatNotificationOptions.FAILURES):
                self._listener_manager.announce_status(status)

        if self._heartbeat_call is not None:
            self._heartbeat_call.cancel()

        state_payload = self._subscription_state.state_payload()
        channels = self._subscription_state.prepare_channel_list(False)
        channel_groups = self._subscription_state.prepare_channel_group_list(False)

        self._heartbeat_call = Heartbeat(self._pubnub) \
            .channels(channels) \
            .channel_groups(channel_groups) \
            .state(state_payload) \
            .async(heartbeat_callback)

    def _send_leave(self, unsubscribe_operation):
        def announce_leave_status(response, status):
            self._listener_manager.announce_status(status)

        Leave(self._pubnub) \
            .channels(unsubscribe_operation.channels) \
            .channel_groups(unsubscribe_operation.channel_groups) \
            .async(announce_leave_status)

    def reconnect(self):
        if self.worker_loop is not None:
            self._start_subscribe_loop()
            self._register_heartbeat_timer()
        else:
            # TODO: actual error
            pass


class PubNubTwisted(PubNubCore):
    """PubNub Python API for Twisted framework"""

    def sdk_platform(self):
        return "-Twisted"

    def __init__(self, config, pool=None, reactor=None):
        super(PubNubTwisted, self).__init__(config)

        self._publish_sequence_manager = PublishSequenceManager(PubNubCore.MAX_SEQUENCE)
        self._subscription_manager = TwistedSubscriptionManager(self)

        self.disconnected_times = 0

        if reactor is None:
            self.reactor = _reactor
        else:
            self.reactor = reactor

        if pool is None:
            self.pnconn_pool = HTTPConnectionPool(self.reactor, persistent=True)
            self.pnconn_pool.maxPersistentPerHost = 3
            self.pnconn_pool.cachedConnectionTimeout = self.config.subscribe_request_timeout
            self.pnconn_pool.retryAutomatically = False
        else:
            self.pnconn_pool = pool

        self.headers = {
            'User-Agent': [self.sdk_name],
        }

    def start(self):
        self.reactor.run()

    def stop(self):
        self.reactor.stop()

    def add_listener(self, listener):
        if self._subscription_manager is not None:
            self._subscription_manager.add_listener(listener)
        else:
            raise Exception("Subscription manager is not enabled for this instance")

    def request_async(self, options_func, create_response, create_status_response, callback, cancellation_event):
        """
        :param options_func:
        :param create_response:
        :param create_status_response:
        :param callback:
        :param cancellation_event:
        """

        def async_request(options_func, create_response, create_status_response, cancellation_event, callback):
            def manage_failures(failure):
                # Cancelled
                if failure.type == ConnectingCancelledError:
                    return
                elif failure.type == PubNubTwistedException:
                    callback(failure.value)
                else:
                    return failure

            request = self.request_deferred(options_func, create_response, create_status_response, cancellation_event)
            request.addCallbacks(callback, manage_failures)

        self.reactor.callInThread(async_request,
                                  options_func,
                                  create_response,
                                  create_status_response,
                                  cancellation_event,
                                  callback)

        return

    def request_deferred(self, options_func, create_response, create_status_response, cancellation_event):
        options = options_func()
        reactor = self.reactor
        pnconn_pool = self.pnconn_pool
        headers = self.headers

        url = utils.build_url(self.config.scheme(), self.config.origin,
                              options.path, options.query_string)

        logger.debug("%s %s %s" % (options.method_string, url, options.data))

        def handler():
            agent = Agent(reactor, pool=pnconn_pool)

            if options.data is not None:
                body = FileBodyProducer(StringIO(options.data))
            else:
                body = None

            request = agent.request(
                options.method_string,
                url,
                Headers(headers),
                FileBodyProducer(StringIO(body)))

            def received(response):
                finished = Deferred()
                response.deliverBody(PubNubResponse(finished, response.code))
                return finished

            def success(response, req_url, request):
                parsed_url = urlparse(req_url)
                query = parse_qs(parsed_url.query)
                uuid = None
                auth_key = None

                if 'uuid' in query and len(query['uuid']) > 0:
                    uuid = query['uuid'][0]

                if 'auth_key' in query and len(query['auth_key']) > 0:
                    auth_key = query['auth_key'][0]

                response_body = response.body
                code = response.code
                d = Deferred()

                response_info = ResponseInfo(
                    status_code=response.code,
                    tls_enabled='https' == parsed_url.scheme,
                    origin=parsed_url.netloc,
                    uuid=uuid,
                    auth_key=auth_key,
                    client_request=request
                )

                if code != 200:
                    if code == 403:
                        status_category = PNStatusCategory.PNAccessDeniedCategory
                    elif code == 400:
                        status_category = PNStatusCategory.PNBadRequestCategory
                    else:
                        status_category = self

                    if code >= 500:
                        error = PNERR_SERVER_ERROR
                    else:
                        error = PNERR_CLIENT_ERROR
                else:
                    error = None
                    status_category = PNStatusCategory.PNAcknowledgmentCategory

                try:
                    data = json.loads(response_body)
                except ValueError:
                    try:
                        data = json.loads(response_body.decode("utf-8"))
                    except ValueError:
                        raise PubNubTwistedException(
                            result=create_response(None),
                            status=create_status_response(
                                status_category,
                                response_info,
                                PubNubException(
                                    pn_error=PNERR_JSON_DECODING_FAILED,
                                    errormsg='json decode error'
                                )
                            )
                        )

                if error:
                    raise PubNubTwistedException(
                        result=data,
                        status=create_status_response(status_category, data, response_info,
                                                      PubNubException(
                                                          errormsg=data,
                                                          pn_error=error,
                                                          status_code=response.code
                                                      )))

                envelope = TwistedEnvelope(
                    create_response(data),
                    create_status_response(
                        status_category,
                        response,
                        response_info,
                        error),
                    data
                )
                d.callback(envelope)
                return d

            def failed(failure):
                raise PubNubTwistedException(
                    result=None,
                    status=create_status_response(PNStatusCategory.PNTLSConnectionFailedCategory,
                                                  None,
                                                  None,
                                                  PubNubException(
                                                      errormsg='Connection error',
                                                      pn_error=PNERR_CONNECTION_ERROR,
                                                      status_code=0
                                                  )))

            request.addErrback(failed)
            request.addCallback(received)
            request.addCallback(success, url, request)
            return request

        return handler()

    def disconnected(self):
        return self.disconnected_times > 0


class TwistedEnvelope(object):
    def __init__(self, result, status, raw_result=None):
        self.result = result
        self.status = status
        self.raw_result = raw_result


class TwistedResponse(object):
    def __init__(self, body, code):
        self.body = body
        self.code = code


class PubNubTwistedException(Exception):
    def __init__(self, result, status):
        self.result = result
        self.status = status

    def __str__(self):
        return str(self.status.error_data.exception)
