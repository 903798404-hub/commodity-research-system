"""Website-only bounded reuse of pure computations on complete input values.

Call only after the owning reader has validated its data. Do not wrap readers,
authorization, clocks, collection, saving, renderers or functions with hidden
mutable inputs. The optional background recorder writes derived charts only;
website consumers use the existing read-only data mount and never publish.
"""
from __future__ import annotations

from functools import lru_cache, wraps
from collections import OrderedDict
from hashlib import sha256
from io import BytesIO
import marshal
import pickle
from threading import Lock
from uuid import uuid4
from weakref import WeakKeyDictionary
import base64
import inspect
import json
import os
from pathlib import Path
import stat

MAX_ENTRIES = 256
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_INPUT_BYTES = 16 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024
_namespaces = WeakKeyDictionary()
_lock = Lock()
_budget_entries = OrderedDict()
_budget_bytes = 0
_budget_generation = 0


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


def complete_value_identity(value):
    """Full native-cache dependency identity, including every dataframe row."""
    return sha256(_serialize(value)).hexdigest()


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
        with _lock:
            generation = _budget_generation
        digest = sha256(body).hexdigest()
        try:
            service = _service()
            result = service(namespace, digest, body, function)
        except _UncachedResult as exc:
            return exc.value
        _account_for_result(service, namespace, digest, result, generation)
        return pickle.loads(result)

    return shared


def _account_for_result(service, namespace, digest, result, generation):
    """Bound serialized payload bytes while retaining many inexpensive facts.

Streamlit still coordinates per-key computation. Its own entry-count bound is
retained; this index adds the payload byte bound. A native eviction can leave
an overestimate in this index, which is safe. Delete outside the index lock so
nested computations and other sessions cannot deadlock during eviction.
"""
    global _budget_bytes
    key = (namespace, digest)
    evicted = []
    with _lock:
        if generation != _budget_generation:
            evicted.append(key)
        else:
            _budget_bytes -= _budget_entries.pop(key, 0)
            _budget_entries[key] = len(result)
            _budget_bytes += len(result)
            while _budget_bytes > MAX_CACHE_BYTES or len(_budget_entries) > MAX_ENTRIES:
                old_key, size = _budget_entries.popitem(last=False)
                _budget_bytes -= size
                evicted.append(old_key)
    for old_namespace, old_digest in evicted:
        # The last two arguments are explicitly unhashed by the native cache.
        service.clear(old_namespace, old_digest, b'', None)


def clear_computation_cache():
    """Explicit diagnostics hook; normal invalidation follows input contents."""
    global _budget_bytes, _budget_generation
    with _lock:
        _budget_entries.clear()
        _budget_bytes = 0
        _budget_generation += 1
    if _service.cache_info().currsize:
        _service().clear()


class _UnsupportedDerived(ValueError):
    pass


def _strict_derived_json(raw):
    def pairs(items):
        result={}
        for key,value in items:
            if key in result:raise ValueError('Duplicate derived JSON field')
            result[key]=value
        return result
    def nonfinite(value):raise ValueError('Non-finite derived JSON value')
    return json.loads(raw,object_pairs_hook=pairs,parse_constant=nonfinite)


def _derived_release():
    """Image-baked identity; environment variables cannot claim another image."""
    release=_strict_derived_json((Path(__file__).resolve().parents[1]/'RELEASE.json').read_bytes())
    identity={key:release[key] for key in ('git_commit','git_tree')}
    if any(len(value)!=40 or any(c not in '0123456789abcdef' for c in value) for value in identity.values()):
        raise ValueError('Invalid derived-cache image identity')
    return identity


def _derived_key(function,args,kwargs):
    # Hash locally generated serialization only. Persisted objects are JSON/PNG,
    # never pickles. The exact image also binds dependencies, fonts and options.
    original=inspect.unwrap(function)
    if original.__closure__ is not None:raise _UnsupportedDerived('Closure input is not persistent')
    body=_serialize((args,kwargs))
    if len(body)>MAX_INPUT_BYTES:raise _UnsupportedDerived('Derived input exceeds bound')
    function_id=f'{original.__module__}.{original.__qualname__}'
    version=sha256(marshal.dumps(original.__code__)+_serialize((original.__defaults__,original.__kwdefaults__))).hexdigest()
    return sha256(function_id.encode()+version.encode()+sha256(body).digest()).hexdigest()


def _encode_derived(value):
    if type(value) is bytes:
        return {'kind':'bytes','body':base64.b64encode(value).decode('ascii')}
    if type(value).__module__=='plotly.graph_objs._figure' and value._grid_ref is None:
        import plotly.io as pio
        return {'kind':'plotly','body':json.loads(pio.to_json(value,validate=True,remove_uids=False))}
    raise _UnsupportedDerived('Unsupported derived result')


def _decode_derived(value):
    if not isinstance(value,dict) or set(value)!={'kind','body'}:
        raise ValueError('Invalid derived result envelope')
    if value['kind']=='bytes':return base64.b64decode(value['body'],validate=True)
    if value['kind']=='plotly':
        import plotly.graph_objects as go
        return go.Figure(value['body'],skip_invalid=False)
    raise ValueError('Unsupported derived result')


def _json_bytes(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def _derived_path(release,key,*,record=False):
    if record:
        root=Path(os.environ['MARKET_DATA_PRECOMPUTE_OUTPUT'])
    else:
        configured=os.getenv('PUBLIC_MARKET_DATA_RUNTIME_ROOT','').strip()
        if not configured:return None
        root=Path(configured)/'cache/page-precomputations'
    if not root.is_absolute() or root.resolve()!=root:
        raise ValueError('Derived-cache root alias invalid')
    return root/release['git_commit']/(key+'.json')


def _read_derived(path,release,key):
    if path is None:return None
    # Production derived inputs are root-published under the existing RO data
    # mount. A writer-controlled path, alias or partial result is just a miss.
    if os.name!='posix' or not os.statvfs(path.parent).f_flag & os.ST_RDONLY:
        return None
    for item in (path,*path.parents):
        info=item.lstat()
        if stat.S_ISLNK(info.st_mode) or info.st_uid!=0 or info.st_mode & 0o022:
            return None
    if not path.is_file() or path.stat().st_size>MAX_RESULT_BYTES:return None
    return _parse_derived(path.read_bytes(),release,key)


def _parse_derived(raw,release,key):
    if len(raw)>MAX_RESULT_BYTES:return None
    payload=_strict_derived_json(raw)
    if (set(payload)!={'schema_version','image','key','result','result_sha256'}
        or payload['schema_version']!='page-precomputation/1'
        or payload['image']!=release or payload['key']!=key
        or sha256(_json_bytes(payload['result'])).hexdigest()!=payload['result_sha256']):
        return None
    return _decode_derived(payload['result'])


def _record_derived(path,release,key,value):
    result=_encode_derived(value)
    payload={'schema_version':'page-precomputation/1','image':release,'key':key,'result':result,
             'result_sha256':sha256(_json_bytes(result)).hexdigest()}
    raw=_json_bytes(payload)
    if len(raw)>MAX_RESULT_BYTES:return
    path.parent.mkdir(parents=True,exist_ok=True)
    existing=list(path.parent.glob('*.json'))
    if len(existing)>=MAX_ENTRIES or sum(p.stat().st_size for p in existing)+len(raw)>MAX_CACHE_BYTES:
        raise ValueError('Derived output exceeds cache budget')
    temporary=path.with_name('.'+path.name+'.'+uuid4().hex)
    try:
        with temporary.open('xb') as handle:
            handle.write(raw);handle.flush();os.fsync(handle.fileno())
        os.replace(temporary,path)
    finally:temporary.unlink(missing_ok=True)


def precomputed_chart(function):
    """Optional safe persistent result; authoritative readers still run first.

    Only the isolated background worker records outputs. Website misses,
    unsupported objects, corruption and version changes use the original body.
    """
    @wraps(function)
    def prepared(*args,**kwargs):
        record=os.getenv('MARKET_DATA_PRECOMPUTE_MODE')=='record'
        try:
            release=_derived_release();key=_derived_key(function,args,kwargs)
            path=_derived_path(release,key,record=record)
            if not record:
                result=_read_derived(path,release,key)
                if result is not None:return result
        except _UnsupportedDerived:
            path=None
        except (OSError,ValueError,TypeError,KeyError,pickle.PickleError,AttributeError,RecursionError,OverflowError):
            if record:raise
            path=None
        result=function(*args,**kwargs)
        if record and path is not None:
            try:_record_derived(path,release,key,result)
            except _UnsupportedDerived:pass
        return result
    return prepared
