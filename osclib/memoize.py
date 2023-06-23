from datetime import datetime
import fcntl
from functools import wraps
import os
from osclib.cache_manager import CacheManager
import shelve
import pickle

# Where the cache files are stored
CACHEDIR = CacheManager.directory('memoize')


def memoize(ttl=None, session=False, add_invalidate=False):
    """Decorator function to implement a persistent cache.

    >>> @memoize()
    ... def test_func(a):
    ...     return a

    Internally, the memoized function has a cache:

    >>> cache = [c.cell_contents for c in test_func.func_closure if 'sync' in dir(c.cell_contents)][0]
    >>> 'sync' in dir(cache)
    True

    There is a limit of the size of the cache

    >>> for k in cache:
    ...     del cache[k]
    >>> len(cache)
    0

    >>> for i in range(4095):
    ...     test_func(i)
    ... len(cache)
    4095

    >>> test_func(0)
    0

    >>> len(cache)
    4095

    >>> test_func(4095)
    4095

    >>> len(cache)
    3072

    >>> test_func(0)
    0

    >>> len(cache)
    3073

    >>> from datetime import timedelta
    >>> k = [k for k in cache if cPickle.loads(k) == ((0,), {})][0]
    >>> t, v = cache[k]
    >>> t = t - timedelta(days=10)
    >>> cache[k] = (t, v)
    >>> test_func(0)
    0
    >>> t2, v = cache[k]
    >>> t != t2
    True

    """

    # Configuration variables
    SLOTS = 4096            # Number of slots in the cache file
    NCLEAN = 1024           # Number of slots to remove when limit reached
    TIMEOUT = 60 * 60 * 2   # Time to live for every cache slot (seconds)
    memoize.session_functions = []

    def _memoize(fn):
        # Implement a POSIX lock / unlock extension for shelves. Inspired
        # on ActiveState Code recipe #576591
        def _lock(filename):
            lckfile = open(f'{filename}.lck', 'w')
            fcntl.flock(lckfile.fileno(), fcntl.LOCK_EX)
            return lckfile

        def _unlock(lckfile):
            fcntl.flock(lckfile.fileno(), fcntl.LOCK_UN)
            lckfile.close()

        def _open_cache(cache_name):
            if not session:
                lckfile = _lock(cache_name)
                cache = shelve.open(cache_name, protocol=-1)
                # Store a reference to the lckfile to avoid to be
                # closed by gc
                cache.lckfile = lckfile
            else:
                if not hasattr(fn, '_memoize_session_cache'):
                    fn._memoize_session_cache = {}
                    memoize.session_functions.append(fn)
                cache = fn._memoize_session_cache
            return cache

        def _close_cache(cache):
            if not session:
                cache.close()
                _unlock(cache.lckfile)

        def _clean_cache(cache):
            len_cache = len(cache)
            if len_cache >= SLOTS:
                nclean = NCLEAN + len_cache - SLOTS
                keys_to_delete = sorted(cache, key=lambda k: cache[k][0])[:nclean]
                for key in keys_to_delete:
                    del cache[key]

        def _key(obj):
            # Pickle doesn't guarantee that there is a single
            # representation for every serialization.  We can try to
            # picke / depickle twice to have a canonical
            # representation.
            key = pickle.dumps(obj, protocol=-1)
            key = pickle.dumps(pickle.loads(key), protocol=-1)
            return key

        def _invalidate(*args, **kwargs):
            key = _key((args, kwargs))
            cache = _open_cache(cache_name)
            if key in cache:
                del cache[key]

        def _invalidate_all():
            cache = _open_cache(cache_name)
            cache.clear()

        def _add_invalidate_method(_self):
            name = f'_invalidate_{fn.__name__}'
            if not hasattr(_self, name):
                setattr(_self, name, _invalidate)

            name = '_invalidate_all'
            if not hasattr(_self, name):
                setattr(_self, name, _invalidate_all)

        @wraps(fn)
        def _fn(*args, **kwargs):
            def total_seconds(td):
                return (td.microseconds + (td.seconds + td.days * 24 * 3600.) * 10**6) / 10**6

            now = datetime.now()
            if add_invalidate:
                _self = args[0]
                _add_invalidate_method(_self)
            first = str(args[0]) if isinstance(args[0], object) else args[0]
            key = _key((first, args[1:], kwargs))
            updated = False
            cache = _open_cache(cache_name)
            if key in cache:
                timestamp, value = cache[key]
                updated = total_seconds(now - timestamp) < ttl
            if not updated:
                value = fn(*args, **kwargs)
                cache[key] = (now, value)
            _clean_cache(cache)
            _close_cache(cache)
            return value

        cache_name = os.path.join(CACHEDIR, fn.__name__)
        return _fn

    ttl = ttl if ttl else TIMEOUT
    return _memoize


def memoize_session_reset():
    """Reset all session caches."""
    for i, _ in enumerate(memoize.session_functions):
        memoize.session_functions[i]._memoize_session_cache = {}
