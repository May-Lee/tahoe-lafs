from __future__ import print_function
from __future__ import absolute_import
from __future__ import division
from __future__ import unicode_literals

from future.utils import PY2
if PY2:
    from future.builtins import filter, map, zip, ascii, chr, hex, input, next, oct, open, pow, round, super, bytes, dict, list, object, range, str, max, min  # noqa: F401
import warnings
import os, sys
from six.moves import StringIO
import six

try:
    from allmydata.scripts.types_ import SubCommands
except ImportError:
    pass

from twisted.python import usage
from twisted.internet import defer, task, threads

from allmydata.scripts.common import get_default_nodedir
from allmydata.scripts import debug, create_node, cli, \
    admin, tahoe_run, tahoe_invite
from allmydata.util.encodingutil import quote_local_unicode_path
from allmydata.util.eliotutil import (
    opt_eliot_destination,
    opt_help_eliot_destinations,
    eliot_logging_service,
)

from .. import (
    __full_version__,
)

_default_nodedir = get_default_nodedir()

NODEDIR_HELP = ("Specify which Tahoe node directory should be used. The "
                "directory should either contain a full Tahoe node, or a "
                "file named node.url that points to some other Tahoe node. "
                "It should also contain a file named '"
                + os.path.join('private', 'aliases') +
                "' which contains the mapping from alias name to root "
                "dirnode URI.")
if _default_nodedir:
    NODEDIR_HELP += " [default for most commands: " + quote_local_unicode_path(_default_nodedir) + "]"


# XXX all this 'dispatch' stuff needs to be unified + fixed up
_control_node_dispatch = {
    "run": tahoe_run.run,
}

process_control_commands = [
    ("run", None, tahoe_run.RunOptions, "run a node without daemonizing"),
]  # type: SubCommands


class Options(usage.Options):
    # unit tests can override these to point at StringIO instances
    stdin = sys.stdin
    stdout = sys.stdout
    stderr = sys.stderr

    subCommands = (     create_node.subCommands
                    +   admin.subCommands
                    +   process_control_commands
                    +   debug.subCommands
                    +   cli.subCommands
                    +   tahoe_invite.subCommands
                    )

    optFlags = [
        ["quiet", "q", "Operate silently."],
        ["version", "V", "Display version numbers."],
        ["version-and-path", None, "Display version numbers and paths to their locations."],
    ]
    optParameters = [
        ["node-directory", "d", None, NODEDIR_HELP],
        ["wormhole-server", None, u"ws://wormhole.tahoe-lafs.org:4000/v1", "The magic wormhole server to use.", six.text_type],
        ["wormhole-invite-appid", None, u"tahoe-lafs.org/invite", "The appid to use on the wormhole server.", six.text_type],
    ]

    def opt_version(self):
        print(__full_version__, file=self.stdout)
        self.no_command_needed = True

    opt_version_and_path = opt_version

    opt_eliot_destination = opt_eliot_destination
    opt_help_eliot_destinations = opt_help_eliot_destinations

    def __str__(self):
        return ("\nUsage: tahoe [global-options] <command> [command-options]\n"
                + self.getUsage())

    synopsis = "\nUsage: tahoe [global-options]" # used only for subcommands

    def getUsage(self, **kwargs):
        t = usage.Options.getUsage(self, **kwargs)
        t = t.replace("Options:", "\nGlobal options:", 1)
        return t + "\nPlease run 'tahoe <command> --help' for more details on each command.\n"

    def postOptions(self):
        if not hasattr(self, 'subOptions'):
            if not hasattr(self, 'no_command_needed'):
                raise usage.UsageError("must specify a command")
            sys.exit(0)


create_dispatch = {}
for module in (create_node,):
    create_dispatch.update(module.dispatch)  # type: ignore

def parse_options(argv, config=None):
    if not config:
        config = Options()
    config.parseOptions(argv) # may raise usage.error
    return config

def parse_or_exit_with_explanation(argv, stdout=sys.stdout):
    config = Options()
    try:
        parse_options(argv, config=config)
    except usage.error as e:
        c = config
        while hasattr(c, 'subOptions'):
            c = c.subOptions
        print(str(c), file=stdout)
        print("%s:  %s\n" % (sys.argv[0], e), file=stdout)
        sys.exit(1)
    return config

def dispatch(config,
             stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr):
    command = config.subCommand
    so = config.subOptions
    if config['quiet']:
        stdout = StringIO()
    so.stdout = stdout
    so.stderr = stderr
    so.stdin = stdin

    if command in create_dispatch:
        f = create_dispatch[command]
    elif command in _control_node_dispatch:
        f = _control_node_dispatch[command]
    elif command in debug.dispatch:
        f = debug.dispatch[command]
    elif command in admin.dispatch:
        f = admin.dispatch[command]
    elif command in cli.dispatch:
        # these are blocking, and must be run in a thread
        f0 = cli.dispatch[command]
        f = lambda so: threads.deferToThread(f0, so)
    elif command in tahoe_invite.dispatch:
        f = tahoe_invite.dispatch[command]
    else:
        raise usage.UsageError()

    d = defer.maybeDeferred(f, so)
    # the calling convention for CLI dispatch functions is that they either:
    # 1: succeed and return rc=0
    # 2: print explanation to stderr and return rc!=0
    # 3: raise an exception that should just be printed normally
    # 4: return a Deferred that does 1 or 2 or 3
    def _raise_sys_exit(rc):
        sys.exit(rc)
    d.addCallback(_raise_sys_exit)
    return d

def _maybe_enable_eliot_logging(options, reactor):
    if options.get("destinations"):
        service = eliot_logging_service(reactor, options["destinations"])
        # There is no Twisted "Application" around to hang this on so start
        # and stop it ourselves.
        service.startService()
        reactor.addSystemEventTrigger("after", "shutdown", service.stopService)
    # Pass on the options so we can dispatch the subcommand.
    return options

def run():
    if not six.PY2:
        warnings.warn("Support for Python 3 is experimental. Use at your own risk.")

    if sys.platform == "win32":
        from allmydata.windows.fixups import initialize
        initialize()
    # doesn't return: calls sys.exit(rc)
    task.react(_run_with_reactor)


def _setup_coverage(reactor):
    """
    Arrange for coverage to be collected if the 'coverage' package is
    installed
    """
    # can we put this _setup_coverage call after we hit
    # argument-parsing?
    if '--coverage' not in sys.argv:
        return
    sys.argv.remove('--coverage')

    try:
        import coverage
    except ImportError:
        raise RuntimeError(
                "The 'coveage' package must be installed to use --coverage"
        )

    # this doesn't change the shell's notion of the environment, but
    # it makes the test in process_startup() succeed, which is the
    # goal here.
    os.environ["COVERAGE_PROCESS_START"] = '.coveragerc'

    # maybe-start the global coverage, unless it already got started
    cov = coverage.process_startup()
    if cov is None:
        cov = coverage.process_startup.coverage

    def write_coverage_data():
        """
        Make sure that coverage has stopped; internally, it depends on
        ataxit handlers running which doesn't always happen (Twisted's
        shutdown hook also won't run if os._exit() is called, but it
        runs more-often than atexit handlers).
        """
        cov.stop()
        cov.save()
    reactor.addSystemEventTrigger('after', 'shutdown', write_coverage_data)


def _run_with_reactor(reactor):

    _setup_coverage(reactor)

    d = defer.maybeDeferred(parse_or_exit_with_explanation, sys.argv[1:])
    d.addCallback(_maybe_enable_eliot_logging, reactor)
    d.addCallback(dispatch)
    def _show_exception(f):
        # when task.react() notices a non-SystemExit exception, it does
        # log.err() with the failure and then exits with rc=1. We want this
        # to actually print the exception to stderr, like it would do if we
        # weren't using react().
        if f.check(SystemExit):
            return f # dispatch function handled it
        f.printTraceback(file=sys.stderr)
        sys.exit(1)
    d.addErrback(_show_exception)
    return d

if __name__ == "__main__":
    run()
