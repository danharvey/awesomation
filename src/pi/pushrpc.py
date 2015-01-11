"""Pusher intergration for messages from the cloud."""

import json
import logging
import Queue
import sys
import threading
import uuid

from common import public_creds
from pusherclient import Pusher
import requests


CONFIG_FILE = 'proxy.cfg'
EVENT_URL = ('https://%s.appspot.com/api/device/events'
             % public_creds.appengine_app_id)
AUTH_URL = ('https://%s.appspot.com/api/proxy/channel_auth'
            % public_creds.appengine_app_id)


def read_or_make_config():
  """Read proxy id and secret from file, or make new one."""
  try:
    with open(CONFIG_FILE) as config_file:
      return config_file.read().split(',')
  except:
    proxy_id = str(uuid.uuid4().get_hex())
    proxy_secret = str(uuid.uuid4().get_hex())
    with open(CONFIG_FILE, 'w') as config_file:
      config_file.write('%s,%s' % (proxy_id, proxy_secret))
    return (proxy_id, proxy_secret)


class PushRPC(object):
  """Wrapper for pusher integration."""

  def __init__(self, callback):
    self._proxy_id, self._proxy_secret = read_or_make_config()
    logging.info('I am proxy \'%s\'', self._proxy_id)

    self._exiting = False

    self._events = Queue.Queue()
    self._events_thread = threading.Thread(target=self._post_events_loop)
    self._events_thread.daemon = True
    self._events_thread.start()

    self._callback = callback

    self._pusher = Pusher(public_creds.pusher_key,
                          auth_callback=self._pusher_auth_callback,
                          log_level=logging.ERROR)
    self._pusher.connection.bind(
        'pusher:connection_established',
        self._connect_handler)
    self._pusher.connect()

  def _pusher_auth_callback(self, socket_id, channel_name):
    params = {'socket_id': socket_id, 'channel_name': channel_name}
    response = self._make_request(AUTH_URL, params=params)
    response = response.json()
    return response['auth']

  def _make_request(self, url, method='GET', **kwargs):
    """Make a request to the server with this proxy's auth."""
    response = requests.request(
        method, url,
        auth=(self._proxy_id, self._proxy_secret),
        headers={'content-type': 'application/json',
                 'awesomation-proxy': 'true'},
        **kwargs)
    response.raise_for_status()
    return response

  def _connect_handler(self, _):
    channel_name = 'private-%s' % self._proxy_id
    channel = self._pusher.subscribe(channel_name)
    channel.bind('events', self._callback_handler)

  def _callback_handler(self, data):
    """Callback for when messages are recieved from pusher."""
    try:
      events = json.loads(data)
    except ValueError:
      logging.error('Error parsing message', exc_info=sys.exc_info())
      return

    # pylint: disable=broad-except
    try:
      self._callback(events)
    except Exception:
      logging.error('Error running push callback', exc_info=sys.exc_info())

  def send_event(self, event):
    self._events.put(event)

  def _get_batch_of_events(self, max_size=20):
    """Retrieve as many events from queue as possible without blocking."""
    events = []
    while len(events) < max_size:
      try:
        # First time round we should wait (when list is empty)
        block = len(events) == 0
        event = self._events.get(block)

        # To break out of this thread, we inject a None event in stop()
        if event is None:
          return None

        events.append(event)
      except Queue.Empty:
        break

    assert events
    return events

  def _post_events_loop(self):
    """Send batched of events to server in a loop."""
    logging.info('Starting events thread.')
    while not self._exiting:
      events = self._get_batch_of_events()
      if events is None:
        break

      # pylint: disable=broad-except
      try:
        self._post_events_once(events)
      except Exception:
        logging.error('Exception sending events to server',
                      exc_info=sys.exc_info())
    logging.info('Exiting events thread.')

  def _post_events_once(self, events):
    """Send list of events to server."""
    logging.info('Posting %d events to server', len(events))

    try:
      self._make_request(EVENT_URL, method='POST', data=json.dumps(events))
    except:
      logging.error('Posting events failed', exc_info=sys.exc_info())

  def stop(self):
    """Stop various threads and connections."""
    self._exiting = True
    self._events.put(None)
    self._events_thread.join()

    self._pusher.disconnect()
