"""Descriptor for _repr_mimebundle_ attribute that manages the comm channel.

`MimeBundleDescriptor()` takes the place of a `_repr_mimebundle_` method on a class.

- `MimeBundleDescriptor` is a
  [descriptor](https://docs.python.org/3/howto/descriptor.html). When the
  `_repr_mimebundle_` attribute is accessed on an instance of the decorated class, a
  `ReprMimeBundle` instance is created and returned.
- A `ReprMimeBundle` is first and foremost a callable object that implements the
  `_repr_mimebundle_` protocol that jupyter expects.  However, it also manages an
  ipykernel Comm instance that is used to send the state of the python model to the
  javascript view.  This is done lazily, so that the Comm is only created when the
  `_repr_mimebundle_` is first accessed.
- `ReprMimeBundle` has the logic to get/set the state of the python model, and will keep
  the two in sync ("bind"/"unbind_instance" methods can be used to control this).
"""

from __future__ import annotations

import contextlib
import json
import sys
import warnings
import weakref
from dataclasses import asdict, is_dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable, overload

from ._util import put_buffers, remove_buffers
from ._version import __version__
from .widget import DEFAULT_ESM

if TYPE_CHECKING:  # pragma: no cover
    import psygnal
    import pydantic
    import traitlets
    from ipykernel.comm import Comm
    from typing_extensions import TypeGuard

    from ._protocols import CommMessage

__all__ = ["MimeBundleDescriptor", "ReprMimeBundle"]

_JUPYTER_MIME = "application/vnd.jupyter.widget-view+json"
_REPR_ATTR = "_repr_mimebundle_"
_STATE_GETTER_NAME = "_get_anywidget_state"
_STATE_SETTER_NAME = "_set_anywidget_state"
_ANYWIDGET_ID_KEY = "_anywidget_id"
_ESM_KEY = "_esm"

_PROTOCOL_VERSION_MAJOR = 2
_PROTOCOL_VERSION_MINOR = 1
_PROTOCOL_VERSION = f"{_PROTOCOL_VERSION_MAJOR}.{_PROTOCOL_VERSION_MINOR}.0"
_TARGET_NAME = "jupyter.widget"
_ANYWIDGET_MODEL_NAME = "AnyModel"
_ANYWIDGET_VIEW_NAME = "AnyView"
_ANYWIDGET_JS_MODULE = "anywidget"
_ANYWIDGET_STATE = {
    "_model_module": _ANYWIDGET_JS_MODULE,
    "_model_name": _ANYWIDGET_MODEL_NAME,
    "_model_module_version": __version__,
    "_view_module": _ANYWIDGET_JS_MODULE,
    "_view_name": _ANYWIDGET_VIEW_NAME,
    "_view_module_version": __version__,
    "_view_count": None,
}


def open_comm(
    target_name: str = _TARGET_NAME, version: str = _PROTOCOL_VERSION, **kwargs
) -> Comm:
    from ipykernel.comm import Comm

    return Comm(
        target_name=target_name,
        metadata={"version": version},
        data={"state": _ANYWIDGET_STATE},
    )


# cache of comms: mapp of id(obj) -> Comm.
# we use id(obj) rather than WeakKeyDictionary because we can't assume that the
# object has a __hash__ method
_COMMS: dict[int, Comm] = {}


def _comm_for(obj: object) -> Comm:
    """Get or create a communcation channel for a given object.

    Comms are cached by object id, so that if the same object is used in multiple
    places, the same comm will be used. Comms are deleted when the object is garbage
    collected.
    """
    # NOTE: this is not a totally safe way to create an id for an object
    # since it's possible that the id could be reused.  However, that will only happen
    # after object deletion, so the "risk" seems rather minimal.
    obj_id = id(obj)
    if obj_id not in _COMMS:
        _COMMS[obj_id] = open_comm()
        # when the object is garbage collected, remove the comm from the cache
        with contextlib.suppress(TypeError):
            # if the object is not weakrefable, we can't do anything
            # they'll receive a warning from the init of ReprMimeBundle
            weakref.finalize(obj, _COMMS.pop, obj_id)
    return _COMMS[obj_id]


class MimeBundleDescriptor:
    """Descriptor that builds a ReprMimeBundle when accessed on an instance.

    The `__get__` method is called when the descriptor's name is accessed on a class or
    instance.  It returns a `ReprMimeBundle` instance, which is a callable that
    implements the `_repr_mimebundle_` protocol that jupyter expects, but also manages
    the comm channel between the python model and the javascript view.

    For more on descriptors, see: <https://docs.python.org/3/howto/descriptor.html>

    Parameters
    ----------
    follow_changes : bool, optional
        If `True` (default), the state of the python model will be updated whenever the
        state of the javascript view changes (and vice versa).
    autodetect_observer : bool, optional
        If `True` (default), an attempt will be made to find a known observer-pattern
        API on the object (such as a psygnal.SignalGroup or traitlets.HasTraits) and
        use it to automatically send state changes to the javascript view.  If `False`,
        the javascript view will only be updated when the `send_state()` method is
        explicitly called.
    **extra_state : Any, optional
        Any extra state that should be sent to the javascript view (for example,
        for the `_esm` anywidget field.)  By default, `{'_esm': DEFAULT_ESM}` is added
        to the state.

    Examples
    --------
    Note that *technically* you could name the attribute anything you want
    but it probably only makes sense to call it '_repr_mimebundle_'.

    >>> class Foo:
    ...     _repr_mimebundle_ = MimeBundleDescriptor()
    >>> foo = Foo()

    in a jupyter notebook, this line will now access `_repr_mimebundle_`, and turn the
    descriptor into an instance of `ReprMimeBundle`, which spins up a comm channel, sets
    up state synchronization, and, when called, returns a mimebundle dict that includes
    the comm id.

    >>> foo
    """

    def __init__(
        self,
        *,
        follow_changes: bool = True,
        autodetect_observer: bool = True,
        **extra_state: Any,
    ) -> None:
        extra_state.setdefault(_ESM_KEY, DEFAULT_ESM)
        self._extra_state = extra_state
        self._name = _REPR_ATTR
        self._follow_changes = follow_changes
        self._autodetect_observer = autodetect_observer

    def __set_name__(self, owner: type, name: str) -> None:
        """Called when this descriptor is assigned to an attribute on a class.

        In most cases, we won't *want* `name` to be anything other than
        `'_repr_mimebundle_'`.
        """
        # TODO:  conceivably emit a warning if name != '_repr_mimebundle_'
        self._name = name

    @overload
    def __get__(self, instance: None, owner: type) -> MimeBundleDescriptor:
        ...

    @overload
    def __get__(self, instance: object, owner: type) -> ReprMimeBundle:
        ...

    def __get__(
        self, instance: object | None, owner: type
    ) -> ReprMimeBundle | MimeBundleDescriptor:
        """Called when this descriptor's name is accessed on a class or instance.

        Examples
        --------
        >>> class Foo:
        ...     _repr_mimebundle_ = MimeBundleDescriptor()
        ...
        >>> Foo._repr_mimebundle_  # same as Foo._repr_mimebundle_.__get__(None, Foo)
        >>> foo = Foo()
        >>> foo._repr_mimebundle_  # same as Foo._repr_mimebundle_.__get__(foo, Foo)
        """
        if instance is None:
            # we're being accessed on the class, just return the descriptor itself.
            return self  # pragma: no cover

        # we're being accessed on an instance ...
        # create the ReprMimeBundle serves as a _repr_mimebundle_ method on the instance
        try:
            repr_obj = ReprMimeBundle(
                instance,
                autodetect_observer=self._autodetect_observer,
                extra_state=self._extra_state,
            )
            if self._follow_changes:
                # set up two way data binding
                repr_obj.sync_object_with_view()
        except Exception as e:  # pragma: no cover
            # when IPython accesses _repr_mimebundle_ on an object, it catches
            # exceptions and swallows them.  We want to make sure that the user
            # knows that something went wrong, so we'll print the exception here.
            warnings.warn(f"Error in Anywidget repr:\n{e}")
            raise

        with contextlib.suppress((AttributeError, ValueError)):
            # this line overrides the attribute on the instance with the ReprMimeBundle
            # we just created. This is so that the next time the attribute is accessed,
            # we don't have to create a new ReprMimeBundle.
            setattr(instance, self._name, repr_obj)
            # We catch AttributeError, as this MAY fail in cases of __slots__ other
            # setattr restrictions. The "cost" would be that the ReprMimeBundle will be
            # rebuilt on each access but it should still work.
            # You can call `del instance._repr_mimebundle_` to remove the RepMimeBundle

        return repr_obj


class ReprMimeBundle:
    """Callable object that behaves like a `_repr_mimebundle_` method...

    which is to say, it returns a mimebundle (mapping of mimetypes to data) when called.

    This object *also* controls an ipykernel.Comm channel between the front-end js view
    and some python model object (`obj`),

    Parameters
    ----------
    obj : object
        The python model object which is being represented by the view.  Most likely
        this will be a dataclass instance that has been made "evented" by the anywidget
        decorator... but we type it as `object` to allow for other use cases, to make it
        clearer what protocols we expect from the object.
    autodetect_observer : bool, optional
        If `True` (default), an attempt will be made to find a known observer-pattern
        API on the object (such as a psygnal.SignalGroup or traitlets.HasTraits) and
        use it to automatically send state changes to the javascript view.  If `False`,
        the javascript view will only be updated when the `send_state()` method is
        explicitly called.
    extra_state : dict, optional
        Any extra state that should be sent to the javascript view (for example,
        for the `_esm` anywidget field.)  By default, `{'_esm': DEFAULT_ESM}` is added
        to the state.
    """

    def __init__(
        self,
        obj: object,
        autodetect_observer: bool = True,
        extra_state: dict[str, Any] | None = None,
    ):
        self._autodetect_observer = autodetect_observer
        self._extra_state = extra_state or {}
        self._extra_state.setdefault(_ANYWIDGET_ID_KEY, _anywidget_id(obj))

        try:
            self._obj = weakref.ref(obj, self._on_obj_deleted)
        except TypeError:
            # obj is not weakrefable, so we'll just hold a strong reference to it.
            self._obj = lambda: obj
            warnings.warn(
                f"Anywidget: {obj} is not weakrefable, so it will not be garbage "
                "collected until the view is closed. Please consider adding "
                "`__slots__ = ('__weakref__',)` to your class definition."
            )

        self._comm = _comm_for(obj)

        # a set of callables that disconnect the connection between the python object
        # and the javascript view.
        self._disconnectors: set[Callable] = set()

        # figure out what type of object we're working with, and how it "get state".
        self._get_state = determine_state_getter(obj)
        self._set_state = determine_state_setter(obj)

    def _on_obj_deleted(self, ref: weakref.ReferenceType | None = None) -> None:
        """Called when the python object is deleted."""
        self.unsync_object_with_view()
        self._comm.close()
        # could swap out esm here for a "deleted" message, or any number of things.

    def send_state(self, include: str | Iterable[str] | None = None) -> None:
        """Send state update to the front-end view.

        Parameters
        ----------
        include : set of str, optional
            If provided, only send the state for the keys in this set.  Otherwise,
            send all state.
        """
        obj = self._obj()
        if obj is None:
            return  # pragma: no cover  ... the python object has been deleted

        state = {**self._get_state(obj), **self._extra_state}
        if include is not None:
            include = {include} if isinstance(include, str) else set(include)
            state = {k: v for k, v in state.items() if k in include}

        if not state:
            return  # pragma: no cover

        # if self._property_lock: ... # TODO
        state, buffer_paths, buffers = remove_buffers(state)
        if self._comm.kernel is not None:  # type: ignore
            msg = {"method": "update", "state": state, "buffer_paths": buffer_paths}
            self._comm.send(data=msg, buffers=buffers)

    def _handle_msg(self, msg: CommMessage):
        """Called when a msg is received from the front-end.

        (assuming `sync_object_with_view` has been called.)
        """
        obj = self._obj()
        if obj is None:
            return  # pragma: no cover  ... the python object has been deleted

        data = msg["content"]["data"]
        method = data["method"]
        if method == "update":
            if "state" in data:
                state = data["state"]
                if "buffer_paths" in data:
                    put_buffers(state, data["buffer_paths"], msg["buffers"])
                self._set_state(obj, state)

        elif method == "request_state":
            self.send_state()

        # elif method == "custom":
        # Handle a custom msg from the front-end.
        # if "content" in data:
        #     self._handle_custom_msg(data["content"], msg["buffers"])
        else:  # pragma: no cover
            raise ValueError(
                f"Unrecognized method: {data['method']}.  Please report this at "
                "https://github.com/manzt/anywidget/issues"
            )

    # def _handle_custom_msg(self, content: Any, buffers: list[memoryview]):
    #     # TODO: handle custom callbacks
    #     # https://github.com/jupyter-widgets/ipywidgets/blob/6547f840edc1884c75e60386ec7fb873ba13f21c/python/ipywidgets/ipywidgets/widgets/widget.py#L662
    #     ...

    def __call__(self, **kwargs) -> dict:
        """Called when _repr_mimebundle_ is called on the python object."""
        # NOTE: this could conceivably be a method on a Comm subclass
        # (i.e. the comm knows how to represent itself as a mimebundle)
        return {
            "text/plain": repr(self),
            _JUPYTER_MIME: {
                "version_major": _PROTOCOL_VERSION_MAJOR,
                "version_minor": _PROTOCOL_VERSION_MINOR,
                "model_id": self._comm.comm_id,
            },
        }

    def sync_object_with_view(
        self, py_to_js: bool = True, js_to_py: bool = True
    ) -> None:
        """Connect the front-end to changes in the model, and vice versa.

        Parameters
        ----------
        py_to_js : bool, optional
            If True (the default), changes in the python model will be reflected in the
            front-end.
        js_to_py : bool, optional
            If True (the default), changes in the front-end will be reflected in the
            python model.
        """
        if js_to_py:
            # connect changes in the view to the instance
            self._comm.on_msg(self._handle_msg)
            self.send_state()

        if py_to_js and self._autodetect_observer:
            # connect changes in the instance to the view
            obj = self._obj()
            if obj is None:
                raise RuntimeError("Cannot sync a deleted object")

            if self._disconnectors:
                warnings.warn("Refusing to re-sync a synced object.")
                return

            # each of these _connect_* functions receives the python object, and the
            # send_state method.  They are responsible connect an event that calls
            # send_state({'attr_name'}) whenever attr_name changes. If successful, they
            # return a callable that undoes the connection when called, otherwise None.

            # check for psygnal
            for connector in (_connect_psygnal, _connect_traitlets):
                disconnect = connector(obj, self.send_state)
                if disconnect:
                    self._disconnectors.add(disconnect)
                    break
            else:
                warnings.warn(
                    f"Could not find a notifier on {obj} (e.g. psygnal, traitlets). "
                    "Changes to the python object will not be reflected in the JS view."
                )

    def unsync_object_with_view(self) -> None:
        """Disconnect the view from changes in a model instance, and vice versa."""
        self._comm.on_msg(None)

        while self._disconnectors:
            with contextlib.suppress(Exception):
                self._disconnectors.pop()()

    def _send_hmr_update(self, esm: str | None = None, css: str | None = None):
        """Send new ESM or CSS for front end to load and re-render the current views.

        Parameters
        ----------
        esm : string, optional
            anywidget front-end JavaScript code. Can be raw text or URL.
        css : string, optional
            anywidget front-end CSS code. Can be raw text or URL.
        """
        update = {}

        if esm is not None:
            update["_esm"] = esm

        if css is not None:
            update["_css"] = css

        self._extra_state.update(update)
        self.send_state(update.keys())


# ------------- Helper function --------------


def _anywidget_id(obj: object) -> str:
    """Return a unique id for an object, to send to the JS side."""
    return f"{type(obj).__module__}.{type(obj).__name__}"


def determine_state_getter(obj: object) -> Callable[[object], dict]:
    """Autodetect how `obj` can be serialized to a dict.

    This looks for various special methods and patterns on the object (e.g. dataclass,
    pydantic, etc...), and returns a callable that can be used to get the state of the
    object as a dict.

    As an escape hatch it first looks for a special method on the object called
    `_get_anywidget_state`.

    Returns
    -------
    state_getter : Callable[[object], dict]
        A callable that takes an object and returns a dict of its state.
    """
    # check on the class for our special state getter method
    if hasattr(type(obj), _STATE_GETTER_NAME):
        # note that we return the *unbound* method on the class here, so that it can be
        # called with the object as the first argument
        return getattr(type(obj), _STATE_GETTER_NAME)

    if is_dataclass(obj):
        # caveat: if the dict is not JSON serializeable... you still need to
        # provide an API for the user to customize serialization
        return asdict

    if _is_traitlets_object(obj):
        return _get_traitlets_state

    if _is_pydantic_model(obj):
        return _get_pydantic_state

    # pickle protocol ... probably not type-safe enough for our purposes
    # https://docs.python.org/3/library/pickle.html#object.__getstate__
    # if hasattr(type(obj), "__getstate__"):
    #     return type(obj).__getstate__

    raise TypeError(  # pragma: no cover
        f"Cannot determine a state-getting method for {obj!r}. "
        "Please implement a `_get_anywidget_state()` method that returns a dict."
    )


def _default_set_state(obj: object, state: dict) -> None:
    """A default state setter that just sets attributes on the object."""
    for key, val in state.items():
        setattr(obj, key, val)


def determine_state_setter(obj: object) -> Callable[[object, dict], None]:
    """Autodetect how `obj` can update its state from a dict.

    The default implementation just sets attributes on the object.

    Returns
    -------
    state_setter : Callable[[object, dict], None]
        A callable that takes an object and a dict of its state, and updates the object
        accordingly.
    """
    if hasattr(type(obj), _STATE_SETTER_NAME):
        return getattr(type(obj), _STATE_SETTER_NAME)

    return _default_set_state


# ------------- Psygnal support --------------


def _get_psygnal_signal_group(obj: object) -> psygnal.SignalGroup | None:
    """Look for a psygnal.SignalGroup on the obj."""
    psygnal = sys.modules.get("psygnal")
    if psygnal is None:
        return None

    # most likely case: signal group is called "events"
    events = getattr(obj, "events", None)
    if isinstance(events, psygnal.SignalGroup):
        return events

    # try exhaustive search
    with contextlib.suppress(
        (AttributeError, RecursionError, TypeError)
    ):  # pragma: no cover
        for attr in vars(obj).values():
            if isinstance(attr, psygnal.SignalGroup):
                return attr


def _connect_psygnal(obj: object, send_state: Callable) -> Callable | None:
    """Check if an object has a psygnal.SignalGroup, and connect it to send_state.

    Returns
    -------
    disconnect : Callable | None
        A callable that disconnects the psygnal.SignalGroup from send_state, or None
        if no psygnal.SignalGroup was found.
    """
    events = _get_psygnal_signal_group(obj)

    if events is not None:

        @events.connect
        def _on_psygnal_event(event: psygnal.EmissionInfo):
            send_state({event.signal.name})

        def _disconnect():
            events.disconnect(_on_psygnal_event)

        return _disconnect
    return None


# ------------- Traitlets support --------------


def _is_traitlets_object(obj: Any) -> TypeGuard[traitlets.HasTraits]:
    """Return `True` if an object is an instance of traitlets.HasTraits."""
    traitlets = sys.modules.get("traitlets")
    return isinstance(obj, traitlets.HasTraits) if traitlets is not None else False


# a tag that can be added to a traitlet to indicate that it should be synced
# this apparently comes from ipywidgets, not traitlets
_TRAITLETS_SYNC_FLAG = "sync"


# TODO: decide about usage of "sync" being opt-in or opt-out
# users of traitlets who *don't* use ipywidgets might be surprised when their
# state isn't being synced without opting in.


def _get_traitlets_state(obj: traitlets.HasTraits) -> dict:
    """Get the state of a traitlets.HasTraits instance."""
    return obj.trait_values(**{_TRAITLETS_SYNC_FLAG: True})


def _connect_traitlets(obj: object, send_state: Callable) -> Callable | None:
    """Check if an object is a traitlets.HasTraits, and connect it to send_state.

    Only traits with tagged with `sync=True` will be synced.

    Returns
    -------
    disconnect : Callable | None
        A callable that disconnects the traitlets.HasTraits from send_state, or None
        if no traitlets.HasTraits was found.
    """
    if not _is_traitlets_object(obj):
        return None

    def _on_trait_change(change: dict):
        send_state({change["name"]})

    obj.observe(_on_trait_change, names=list(obj.traits(sync=True)))

    obj_ref = weakref.ref(obj)

    def _disconnect():
        obj = obj_ref()
        if obj is not None:
            obj.unobserve(_on_trait_change)

    return _disconnect


# ------------- Pydantic support --------------


def _is_pydantic_model(obj: Any) -> TypeGuard[pydantic.BaseModel]:
    """Return `True` if an object is an instance of pydantic.BaseModel."""
    pydantic = sys.modules.get("pydantic")
    return isinstance(obj, pydantic.BaseModel) if pydantic is not None else False


def _get_pydantic_state(obj: pydantic.BaseModel) -> dict:
    """Get the state of a pydantic BaseModel instance.

    To take advantage of pydantic's support for custom encoders (with json_encoders)
    we call obj.json() here, and then cast back to a dict (which is what the comm
    expects.)
    """
    return json.loads(obj.json())
