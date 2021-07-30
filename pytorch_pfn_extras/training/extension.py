from typing import Optional


PRIORITY_WRITER = 300
PRIORITY_EDITOR = 200
PRIORITY_READER = 100
PRIORITY_SNAPSHOT = -100


class Extension:

    """Base class of extensions.

    An extension is a callable object that takes the manager
    object as the argument. It also provides some default configurations as its
    attributes, e.g. the default trigger and the default priority. This class
    provides a set of typical default values for these attributes.

    There are three ways to define users' own extensions: inheriting this
    class, decorating closures by :func:`make_extension`, or using any callable
    including lambda functions as extensions. Decorator can slightly reduce the
    overhead and is much easier to use, while this class provides more
    flexibility (for example, it can have methods to configure the behavior).
    Using a lambda function allows one-line coding for simple purposes, but
    users have to specify the configurations as arguments to
    :meth:`ExtensionsManager.extend`. For a callable not inheriting this class,
    the default configurations of this class are used unless the user
    explicitly specifies them in :meth:`ExtensionsManager.extend` method.

    Attributes:
        trigger: Default value of trigger for this extension. It is set to
            ``(1, 'iteration')`` by default.
        priority: Default priority of the extension. It is set to
            ``PRIORITY_READER`` by default.
        ~Extension.name: Name of the extension. It is set to
            ``None`` by default. This value will be overwritten when
            registering an extension to a manager. See
            :meth:`pytorch_pfn_extras.ExtensionsManager.extend` for details.

    """
    trigger = 1, 'iteration'
    priority = PRIORITY_READER

    def __init__(self):
        self.name: Optional[str] = None

    @property
    def default_name(self):
        """Default name of the extension.

        It is the name of the class by default. Implementation can override
        this property, or provide a class attribute to hide it.

        """
        return type(self).__name__

    def __call__(self, manager):
        """Invokes the extension.

        Implementations should override this operator. This method is called
        at iterations which the corresponding trigger accepts.

        Args:
            manager (~pytorch_pfn_extras.training.ExtensionsManager):
                Manager object to call this operator.

        """
        raise NotImplementedError(
            'Extension implementation must override __call__.')

    def __getattr__(self, name):
        if name == 'invoke_before_training':
            raise AttributeError(
                'invoke_before_training has been removed since Chainer '
                'v2.0.0. Use Extension.initialize instead.')
        raise AttributeError('{} object has no attribute {}'.format(
            type(self).__name__, name))

    def finalize(self):
        """Finalizes the extension.

        This method is called at the end of the training loop.

        """
        pass

    def initialize(self, manager):
        """Initializes up the manager state.

        This method is called before entering the training loop. An extension
        modifying the state of :class:`~pytorch_pfn_extras.ExtensionsManager`
        can override this method to initialize it.

        When the manager has been restored from a snapshot, this method has to
        recover an appropriate part of the state of the manager.

        Args:
            manager (~pytorch_pfn_extras.training.ExtensionsManager):
                Manager object to call this extension.

        """
        pass

    def on_error(self, manager, exc, tb):
        """Handles the error raised during training before finalization.

        This method is called when an exception is thrown during the
        training loop, before finalize. An extension that needs
        different error handling from finalize, can override this
        method to handle errors.

        Args:
            manager (~pytorch_pfn_extras.training.ExtensionsManager):
            Manager object to call this extension.
            exc (Exception): arbitrary exception thrown during update loop.
            tb (traceback): traceback object of the exception

        """
        pass

    def state_dict(self):
        """Serializes the extension state.

        It is called when a manager that owns this extension is serialized. It
        serializes nothing by default.

        """
        pass

    def load_state_dict(self, to_load):
        pass


class _WrappedExtension(Extension):

    def __init__(self, ext):
        self._ext = ext
        self.trigger = getattr(self._ext, 'trigger', Extension.trigger)
        self.priority = getattr(self._ext, 'priority', Extension.priority)
        super().__init__()

    @property
    def default_name(self):
        return getattr(self._ext, 'default_name', None) or super().default_name

    def __call__(self, manager):
        return self._ext(manager)

    def finalize(self):
        getattr(self._ext, 'finalize', super().finalize)()

    def initialize(self, manager):
        getattr(self._ext, 'initialize', super().initialize)(manager)

    def on_error(self, manager, exc, tb):
        getattr(self._ext, 'on_error', super().on_error)(manager, exc, tb)


def make_extension(
        trigger=Extension.trigger,
        default_name=None,
        priority=Extension.priority,
        finalizer=lambda: None,
        initializer=lambda manager: None,
        on_error=lambda manager, exc, tb: None,
):
    """Decorator to make given function into an extension.

    This decorator just adds some attributes to a given function. The value of
    the attributes are given by the arguments of this decorator.

    See :class:`Extension` for details of extensions. Most of the
    default values of arguments also follow those for this class.

    Args:
        trigger: Default trigger of the extension.
        default_name: Default name of the extension. The name of a given
            function is used by default.
        priority (int): Default priority of the extension.
        finalizer: Finalizer function of this extension. It is
            called at the end of the training loop.
        initializer: Initializer function of this extension. It is called at
            the beginning of the training loop.
        on_error: Error handler callback function of this extension. It is
            called after an error is raised during the training loop.

    """

    if trigger is None:
        trigger = Extension.trigger
    if priority is None:
        priority = Extension.priority

    def decorator(ext):
        ext.trigger = trigger
        ext.default_name = default_name or ext.__name__
        ext.priority = priority
        ext.finalize = finalizer
        ext.on_error = on_error
        ext.initialize = initializer
        return ext

    return decorator


def _as_extension(ext) -> Extension:
    return ext if isinstance(ext, Extension) else _WrappedExtension(ext)
