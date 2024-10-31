# Copyright 2024 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the 'License');
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an 'AS IS' BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Courier server that can run a chainable."""

import threading
import time
from typing import Any, Iterable, TypeVar

from absl import logging
import courier
from ml_metrics._src.chainables import courier_worker
from ml_metrics._src.chainables import lazy_fns
from ml_metrics._src.chainables import transform
from ml_metrics._src.utils import func_utils
from ml_metrics._src.utils import iter_utils


_T = TypeVar('_T')
pickler = lazy_fns.pickler
_INTERVAL_SECS = 30


@func_utils.lru_cache(settable_kwargs=('timeout_secs',))
def _cached_server(name: str | None = None, *, timeout_secs: float = 10200):
  result = CourierServerWrapper(name, timeout_secs=timeout_secs)
  result.build_server()
  result.start(daemon=True)
  return result


class CourierServerWrapper:
  """Courier server that runs a chainable."""

  server_name: str | None
  port: int | None
  prefetch_size: int
  timeout_secs: float
  _server: courier.Server | None
  _thread: threading.Thread | None
  _last_heartbeat: float
  _generator: iter_utils.IteratorQueue | None
  _enqueue_thread: threading.Thread | None
  _shutdown_lock: threading.Condition
  _shutdown_requested: bool

  def __init__(
      self,
      server_name: str | None = None,
      *,
      port: int | None = None,
      prefetch_size: int = 2,
      timeout_secs: float = 10200,
  ):
    self.server_name = server_name
    self.port = port
    self.prefetch_size = prefetch_size
    self.timeout_secs = timeout_secs
    self._server = None
    self._thread = None
    self._last_heartbeat = 0.0
    self._shutdown_lock = threading.Condition()
    self._shutdown_requested = False
    self._generator = None
    self._enqueue_thread = None

  def __str__(self):
    addr = self.server_name or ''
    if self._server is not None:
      addr += f'@{self._server.address}'
    return f'CourierServer("{addr}")'

  @property
  def address(self) -> str:
    if server_name := self.server_name:
      return server_name
    assert self._server is not None, 'Server is not built.'
    return self._server.address

  @property
  def has_started(self) -> bool:
    return self._server is not None and self._server.has_started

  def set_up(self) -> None:
    """Set up (e.g. binding to methods) at server build time."""

    def pickled_maybe_make(maybe_lazy, return_exception: bool = False):
      try:
        result = lazy_fns.maybe_make(maybe_lazy)
      except Exception as e:  # pylint: disable=broad-exception-caught
        lazy_obj = f'{lazy_fns.pickler.loads(maybe_lazy)}'
        if not return_exception:
          raise e
        result = e
        if isinstance(e, (ValueError, TypeError, RuntimeError)):
          logging.exception('chainable: maybe_make exception for %s.', lazy_obj)
      try:
        return pickler.dumps(result)
      except TypeError as e:
        lazy_obj = f'{lazy_fns.pickler.loads(maybe_lazy)}'
        logging.exception(
            'chainable: maybe_make pickle error for %s from %s',
            type(result),
            lazy_obj,
        )
        raise e

    def pickled_init_iterator(maybe_lazy):
      result = lazy_fns.maybe_make(maybe_lazy)
      if not isinstance(result, Iterable):
        raise TypeError(
            f'The {result} is not a generator, but a {type(result)}.'
        )
      if self._generator is not None and not self._generator.exhausted:
        logging.warning(
            'chainable: A new generator is initialized while the previous one'
            ' is not exhausted.'
        )
        self._generator.stop_enqueue()
        if self._enqueue_thread:
          self._enqueue_thread.join()
      self._generator = iter_utils.IteratorQueue(
          self.prefetch_size,
          ignore_error=True,
          name=f'prefetch_queue@{self.address}',
      )
      self._enqueue_thread = threading.Thread(
          target=self._generator.enqueue_from_iterator,
          args=(result,),
          daemon=True,
      )
      self._enqueue_thread.start()
      with self._shutdown_lock:
        self._shutdown_lock.notify_all()

    def pickled_cache_info():
      return pickler.dumps(lazy_fns.cache_info())

    def _next_batch_from_iterator(batch_size: int = 0) -> list[Any]:
      assert self._generator is not None, (
          'Generator is not set, the worker might crashed unexpectedly'
          ' previously.'
      )
      try:
        result = self._generator.flush(batch_size, block=True)
      except Exception as e:  # pylint: disable=broad-exception-caught
        logging.exception('chainable: exception when flushing generator.')
        raise e
      # The sequence of the result will always end with an exception.
      # Any non-StopIteration means the generator crashed.
      if not self._generator:
        if self._generator.exception:
          result.append(self._generator.exception)
        else:
          result.append(StopIteration(*self._generator.returned))
      return result

    def next_batch_from_iterator(batch_size: int | bytes = 0) -> bytes:
      batch_size = lazy_fns.maybe_make(batch_size)
      return pickler.dumps(_next_batch_from_iterator(batch_size))

    def next_from_iterator() -> bytes:
      return pickler.dumps(_next_batch_from_iterator(batch_size=1)[0])

    def heartbeat() -> None:
      self._last_heartbeat = time.time()

    def shutdown() -> None:
      # This ignores the returned thread for remote operation.
      # Cannot directly call self.stop() because this will not return a response
      # to the client.
      self.stop()

    assert self._server is not None, 'Server is not built.'
    self._server.Bind('maybe_make', pickled_maybe_make)
    self._server.Bind('init_generator', pickled_init_iterator)
    self._server.Bind('next_from_generator', next_from_iterator)
    self._server.Bind('next_batch_from_generator', next_batch_from_iterator)
    self._server.Bind('heartbeat', heartbeat)
    self._server.Bind('shutdown', shutdown)
    # TODO: b/318463291 - Add unit tests.
    self._server.Bind('clear_cache', transform.clear_cache)
    self._server.Bind('cache_info', pickled_cache_info)

  def build_server(self) -> courier.Server:
    """Build and run a courier server."""
    assert self.server_name != '', f'illegal {self.server_name=}'  # pylint: disable=g-explicit-bool-comparison
    if self._server is not None:
      return self._server
    self._shutdown_requested = False
    self._server = courier.Server(self.server_name, port=self.port)
    self.set_up()
    logging.info('chainable: constructed server %s', self)
    return self._server

  # TODO: b/372935688 - Makes this optional, and uses to start() and stop().
  def run_until_shutdown(self):
    """Run until shutdown requested."""
    self.build_server()
    assert self._server is not None, 'Server is not built.'
    if not self.has_started:
      self._server.Start()
    self._last_heartbeat = time.time()
    with self._shutdown_lock:
      while not self._shutdown_requested:
        if time.time() - self._last_heartbeat > self.timeout_secs:
          logging.info('chainable: no ping after %ds.', self.timeout_secs)
          self._shutdown_requested = True
          break
        self._shutdown_lock.wait(_INTERVAL_SECS)
    if self.has_started:
      logging.info('chainable: Shutting down for server %s', self)
      self._server.Stop()
      self._server = None

  def start(self, *, daemon: bool = None) -> threading.Thread:
    """Start the server from a different thread."""
    if self.has_started and self._thread is not None:
      return self._thread
    self.build_server()
    server_thread = threading.Thread(
        target=self.run_until_shutdown, daemon=daemon
    )
    server_thread.start()
    self._thread = server_thread
    return server_thread

  def stop(self) -> threading.Thread | None:
    """Stop the server."""
    with self._shutdown_lock:
      self._shutdown_requested = True
      self._shutdown_lock.notify_all()
    return self._thread

  def wait_until_alive(self, deadline_secs: float = 120):
    """Wait until the server is alive."""
    courier_worker.cached_worker(self.address).wait_until_alive(
        deadline_secs=deadline_secs
    )
