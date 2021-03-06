# Copyright 2015, Google Inc.
# All rights reserved.
#
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are
# met:
#
#     * Redistributions of source code must retain the above copyright
# notice, this list of conditions and the following disclaimer.
#     * Redistributions in binary form must reproduce the above
# copyright notice, this list of conditions and the following disclaimer
# in the documentation and/or other materials provided with the
# distribution.
#     * Neither the name of Google Inc. nor the names of its
# contributors may be used to endorse or promote products derived from
# this software without specific prior written permission.
#
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
# "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
# LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
# A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
# OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
# SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
# LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
# DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
# THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
# (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

from grpc.framework.face import exceptions as face_exceptions
from grpc.framework.face import interfaces as face_interfaces
from grpc.framework.foundation import future
from grpc.early_adopter import exceptions
from grpc.early_adopter import interfaces

_ABORTION_REEXPORT = {
    face_interfaces.Abortion.CANCELLED: interfaces.Abortion.CANCELLED,
    face_interfaces.Abortion.EXPIRED: interfaces.Abortion.EXPIRED,
    face_interfaces.Abortion.NETWORK_FAILURE:
        interfaces.Abortion.NETWORK_FAILURE,
    face_interfaces.Abortion.SERVICED_FAILURE:
        interfaces.Abortion.SERVICED_FAILURE,
    face_interfaces.Abortion.SERVICER_FAILURE:
        interfaces.Abortion.SERVICER_FAILURE,
}


class _RpcError(exceptions.RpcError):
  pass


def _reexport_error(face_rpc_error):
  if isinstance(face_rpc_error, face_exceptions.CancellationError):
    return exceptions.CancellationError()
  elif isinstance(face_rpc_error, face_exceptions.ExpirationError):
    return exceptions.ExpirationError()
  else:
    return _RpcError()


def _as_face_abortion_callback(abortion_callback):
  def face_abortion_callback(face_abortion):
    abortion_callback(_ABORTION_REEXPORT[face_abortion])
  return face_abortion_callback


class _ReexportedFuture(future.Future):

  def __init__(self, face_future):
    self._face_future = face_future

  def cancel(self):
    return self._face_future.cancel()

  def cancelled(self):
    return self._face_future.cancelled()

  def running(self):
    return self._face_future.running()

  def done(self):
    return self._face_future.done()

  def result(self, timeout=None):
    try:
      return self._face_future.result(timeout=timeout)
    except face_exceptions.RpcError as e:
      raise _reexport_error(e)

  def exception(self, timeout=None):
    face_error = self._face_future.exception(timeout=timeout)
    return None if face_error is None else _reexport_error(face_error)

  def traceback(self, timeout=None):
    return self._face_future.traceback(timeout=timeout)

  def add_done_callback(self, fn):
    self._face_future.add_done_callback(lambda unused_face_future: fn(self))


def _call_reexporting_errors(behavior, *args, **kwargs):
  try:
    return behavior(*args, **kwargs)
  except face_exceptions.RpcError as e:
    raise _reexport_error(e)


def _reexported_future(face_future):
  return _ReexportedFuture(face_future)


class _CancellableIterator(interfaces.CancellableIterator):

  def __init__(self, face_cancellable_iterator):
    self._face_cancellable_iterator = face_cancellable_iterator

  def __iter__(self):
    return self

  def next(self):
    return _call_reexporting_errors(self._face_cancellable_iterator.next)

  def cancel(self):
    self._face_cancellable_iterator.cancel()


class _RpcContext(interfaces.RpcContext):

  def __init__(self, face_rpc_context):
    self._face_rpc_context = face_rpc_context

  def is_active(self):
    return self._face_rpc_context.is_active()

  def time_remaining(self):
    return self._face_rpc_context.time_remaining()

  def add_abortion_callback(self, abortion_callback):
    self._face_rpc_context.add_abortion_callback(
        _as_face_abortion_callback(abortion_callback))


class _UnaryUnarySyncAsync(interfaces.UnaryUnarySyncAsync):

  def __init__(self, face_unary_unary_sync_async):
    self._underlying = face_unary_unary_sync_async

  def __call__(self, request, timeout):
    return _call_reexporting_errors(
        self._underlying, request, timeout)

  def async(self, request, timeout):
    return _ReexportedFuture(self._underlying.async(request, timeout))


class _StreamUnarySyncAsync(interfaces.StreamUnarySyncAsync):

  def __init__(self, face_stream_unary_sync_async):
    self._underlying = face_stream_unary_sync_async

  def __call__(self, request_iterator, timeout):
    return _call_reexporting_errors(
        self._underlying, request_iterator, timeout)

  def async(self, request_iterator, timeout):
    return _ReexportedFuture(self._underlying.async(request_iterator, timeout))


class _Stub(interfaces.Stub):

  def __init__(self, assembly_stub, cardinalities):
    self._assembly_stub = assembly_stub
    self._cardinalities = cardinalities

  def __enter__(self):
    self._assembly_stub.__enter__()
    return self

  def __exit__(self, exc_type, exc_val, exc_tb):
    self._assembly_stub.__exit__(exc_type, exc_val, exc_tb)
    return False

  def __getattr__(self, attr):
    underlying_attr = self._assembly_stub.__getattr__(attr)
    cardinality = self._cardinalities.get(attr)
    # TODO(nathaniel): unify this trick with its other occurrence in the code.
    if cardinality is None:
      for name, cardinality in self._cardinalities.iteritems():
        last_slash_index = name.rfind('/')
        if 0 <= last_slash_index and name[last_slash_index + 1:] == attr:
          break
      else:
        raise AttributeError(attr)
    if cardinality is interfaces.Cardinality.UNARY_UNARY:
      return _UnaryUnarySyncAsync(underlying_attr)
    elif cardinality is interfaces.Cardinality.UNARY_STREAM:
      return lambda request, timeout: _CancellableIterator(
          underlying_attr(request, timeout))
    elif cardinality is interfaces.Cardinality.STREAM_UNARY:
      return _StreamUnarySyncAsync(underlying_attr)
    elif cardinality is interfaces.Cardinality.STREAM_STREAM:
      return lambda request_iterator, timeout: _CancellableIterator(
          underlying_attr(request_iterator, timeout))
    else:
      raise AttributeError(attr)

def rpc_context(face_rpc_context):
  return _RpcContext(face_rpc_context)


def stub(assembly_stub, cardinalities):
  return _Stub(assembly_stub, cardinalities)
