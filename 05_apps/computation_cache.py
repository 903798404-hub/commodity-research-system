"""Website-only bounded reuse of pure computations on complete input values.

Call only after the owning reader has validated its data. Do not wrap readers,
authorization, clocks, collection, saving, renderers or functions with hidden
mutable inputs. No persistent file or business state is created here.
"""
from __future__ import annotations

from functools import lru_cache, wraps
from hashlib import sha256
from io import BytesIO
import marshal
import pickle
from threading import Lock
from uuid import uuid4
from weakref import WeakKeyDictionary

MAX_ENTRIES = 48
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
_namespaces = WeakKeyDictionary()
_lock = Lock()


def _restore_plotly_figure(figure_type, props, grid_ref, grid_str):
    figure = figure_type(props)
    figure._grid_ref = grid_ref
    figure._grid_str = grid_str
    return figure


class _ValuePickler(pickle.Pickler):
    def reducer_override(self, value):
        # Plotly 6's normal pickle calls to_dict(), which changes ndarray trace
        # properties into encoded dictionaries. Keep the original value arrays
        # and subplot grid metadata so callers retain the original Figure API.
        if type(value).__module__ == 'plotly.graph_objs._figure':
            props = {'data':value._data, 'layout':value._layout,
                     'frames':[frame._props for frame in value.frames]}
            return (_restore_plotly_figure,
                    (type(value), props, value._grid_ref, value._grid_str))
        return NotImplemented


def _serialize(value):
    buffer = BytesIO()
    _ValuePickler(buffer, protocol=5).dump(value)
    return buffer.getvalue()


class _UncachedResult(Exception):
    def __init__(self, value):
        self.value = value


@lru_cache(maxsize=1)
def _service():
    # Import lazily so importing a pure domain never initializes Streamlit.
    import streamlit as st

    @st.cache_data(show_spinner=False, max_entries=MAX_ENTRIES)
    def execute(namespace: str, digest: str, _body: bytes, _function):
        del namespace
        if sha256(_body).hexdigest() != digest:
            raise ValueError('Computation input identity changed')
        args, kwargs = pickle.loads(_body)
        result = _function(*args, **kwargs)
        try:
            encoded = _serialize(result)
        except (pickle.PickleError, TypeError, AttributeError, RecursionError):
            raise _UncachedResult(result) from None
        if len(encoded) > MAX_RESULT_BYTES:
            raise _UncachedResult(result)
        return encoded

    return execute


def cache_computation(function):
    """Reuse a pure function without sampling frames or sharing mutable results.

The complete serialized args/kwargs include all rows, columns, index, dtypes,
nulls, real zeroes, dates, options and metadata. Bytes are generated locally,
never accepted from an external pickle. Distinct callables cannot collide;
wrapping the same callable in multiple consumers shares the same namespace.
Large inputs/results run normally without retaining a cache entry. Exceptions
are not cached, and every successful hit returns an independent result.
"""
    if function.__closure__ is None:
        # Streamlit reexecutes the main script and recreates its functions on
        # every rerun. Their code/defaults, rather than Python object identity,
        # identify the same pure computation across sessions and reruns.
        version = sha256(marshal.dumps(function.__code__) +
                         _serialize((function.__defaults__,function.__kwdefaults__))).hexdigest()
        namespace = f'{function.__module__}.{function.__qualname__}:{version}'
    else:
        # Independent closures may carry different values despite equal code.
        with _lock:
            namespace = _namespaces.setdefault(function, uuid4().hex)

    @wraps(function)
    def shared(*args, **kwargs):
        try:
            body = _serialize((args, kwargs))
        except (pickle.PickleError, TypeError, AttributeError, RecursionError):
            return function(*args, **kwargs)
        if len(body) > MAX_INPUT_BYTES:
            return function(*args, **kwargs)
        try:
            result = _service()(namespace, sha256(body).hexdigest(), body, function)
        except _UncachedResult as exc:
            return exc.value
        return pickle.loads(result)

    return shared


def clear_computation_cache():
    """Explicit diagnostics hook; normal invalidation follows input contents."""
    if _service.cache_info().currsize:
        _service().clear()
