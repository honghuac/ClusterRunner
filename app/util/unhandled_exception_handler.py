from queue import LifoQueue, Queue
import signal
from threading import current_thread, Lock, main_thread

from app.util import log
from app.util.singleton import Singleton


class UnhandledExceptionHandler(Singleton):
    """
    This class implements functionality to catch and log exceptions in a block of code, and also execute a set of
    teardown handlers intended to shut down the application gracefully and do any desired cleanup. It is implemented
    as a singleton because the teardown handlers can have global effects (e.g., stopping the event loop).

    This class is intended to be used as a context manager:
    >>> unhandled_exception_handler = UnhandledExceptionHandler.singleton()
    >>> with unhandled_exception_handler:
    >>>     # code which may throw an exception goes here!
    """

    HANDLED_EXCEPTION_EXIT_CODE = 1

    def __init__(self):
        super().__init__()
        self._handling_lock = Lock()
        self._teardown_callback_stack = LifoQueue()  # we execute callbacks in the reverse order that they were added
        self._logger = log.get_logger(__name__)
        self._handled_exceptions = Queue()

        # Set up a handler to be called when process receives SIGTERM.
        # Note: this will raise if called on a non-main thread, but we should NOT work around that here. (That could
        # prevent the teardown handler from ever being registered!) Calling code should be organized so that this
        # singleton is only ever initialized on the main thread.
        signal.signal(signal.SIGTERM, self._application_teardown_signal_handler)

    def add_teardown_callback(self, callback, *callback_args, **callback_kwargs):
        """
        Add a callback to be executed in the event of application teardown.

        :param callback: The method callback to execute
        :type callback: callable
        :param callback_args: args to be passed to the callback function
        :type callback_args: list
        :param callback_kwargs: kwargs to be passed to the callback function
        :type callback_kwargs: dict
        """
        self._teardown_callback_stack.put((callback, callback_args, callback_kwargs))

    def _application_teardown_signal_handler(self, sig, frame):
        """
        A signal handler that will trigger application teardown.

        :param sig: Signal number of the received signal
        :type sig: int
        :param frame: The interrupted stack frame
        :type frame: frame
        """
        signal_names = {
            signal.SIGTERM: 'SIGTERM',
        }
        self._logger.warning('{} signal received. Triggering teardown.', signal_names[sig])
        raise AppTeardown

    def __enter__(self):
        """
        Enables this to be used as a context manager. No special handling is needed on enter.
        """
        pass

    def __exit__(self, exc_type, exc_value, traceback):
        """
        Enables this to be used as a context manager. If an exception was raised during the execution block (inside the
        "with" statement) then exc_value will be set to the exception object.

        There are four situations in which we can go through this method:
        1. Exception, on main thread
            - The exception is logged and in some cases (e.g., SystemExit) may be immediately reraised.
            - Teardown callbacks are executed.
            - Example: A KeyboardInterrupt exception raised because user presses ctrl-c / sends SIGINT signal

        2. Exception, not on main thread
            - The exception is logged and in some cases may be passed to the main thread to be reraised.
            - Teardown callbacks are executed.
            - Example: Any unhandled exception that is raised on a SafeThread

        3. Normal exit, on main thread
            - We check to see if there was an exception that we need to reraise on the main thread. In almost all cases
              we will *not* reraise an exception on the main thread since it has already been logged and teardown
              callbacks have already been executed on the thread that raised the exception.
            - Teardown callbacks are *not* executed.
            - Example: A SystemExit exception raised by sys.exit() is passed from a SafeThread to the main thread to
                       make Python set the exit code.

        4. Normal exit, not on main thread
            - Do nothing! All is well.
        """
        if exc_value:
            # An exception occurred during execution, so run the teardown callbacks. We use a lock here since multiple
            # threads could raise exceptions at the same time and we only want to execute these once.
            with self._handling_lock:
                if not isinstance(exc_value, (SystemExit, AppTeardown, KeyboardInterrupt)):
                    # It is not very useful to log the SystemExit exception since it is raised by sys.exit(), and thus
                    # application exit is completely expected.
                    self._logger.exception('Unhandled exception handler caught exception.')

                while not self._teardown_callback_stack.empty():
                    callback, args, kwargs = self._teardown_callback_stack.get()
                    try:
                        callback(*args, **kwargs)
                    except:
                        # Also catch any exception that occurs during a teardown callback and log it.
                        self._logger.exception('Teardown callback {} raised exception.', callback)

                self._handled_exceptions.put(exc_value)

        if current_thread() is main_thread():
            # The usage of this class on the main thread is a special case. Generally we won't be handling an exception
            # directly (exc_value will be None) since all we do on the main thread is wait for the app_thread to join.
            # However once the app_thread joins, there may have been exceptions on *other* threads that we want to
            # reraise on the main thread.
            #
            # We check the self._handled_exceptions queue to see if there was an exception that we want to reraise. We
            # only care about the first exception on the queue -- it was the first caught exception so it "wins".
            if not self._handled_exceptions.empty():
                handled_exception = self._handled_exceptions.get()

                # We reraise SystemExit on the main thread -- this specific exception is how Python controls setting
                # the process exit code, and that only works if raised on the main thread.
                if isinstance(handled_exception, SystemExit):
                    raise handled_exception

                # We also want to make sure the process exit code is set non-zero if the UnhandledExceptionHandler
                # handled any Exception at all. (Note: this does not include AppTeardown or KeyboardInterrupt, which
                # both inherit from BaseException.)
                if isinstance(handled_exception, Exception):
                    raise SystemExit(self.HANDLED_EXCEPTION_EXIT_CODE)

        # Returning True from this method tells Python not to re-raise the exc_value exception on the current thread.
        return True


class AppTeardown(BaseException):
    """
    Trigger application teardown. This works similarly to raising SystemExit, but unlike SystemExit this will not be
    reraised on the main thread. Essentially, this would allow execution of main() in main.py to finish naturally
    without being short-circuited after app_thread.join().
    """