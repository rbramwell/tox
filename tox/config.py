import argparse
import os
import random
from fnmatch import fnmatchcase
import sys
import re
import shlex
import string
import pkg_resources
import itertools
import pluggy
from subprocess import list2cmdline

import tox.interpreters
from tox import hookspecs
from tox._verlib import NormalizedVersion

import py

import tox

iswin32 = sys.platform == "win32"

default_factors = {'jython': 'jython', 'pypy': 'pypy', 'pypy3': 'pypy3',
                   'py': sys.executable, 'py2': 'python2', 'py3': 'python3'}
for version in '26,27,32,33,34,35,36,37'.split(','):
    default_factors['py' + version] = 'python%s.%s' % tuple(version)

hookimpl = pluggy.HookimplMarker("tox")

_dummy = object()


def get_plugin_manager(plugins=()):
    # initialize plugin manager
    import tox.venv
    pm = pluggy.PluginManager("tox")
    pm.add_hookspecs(hookspecs)
    pm.register(tox.config)
    pm.register(tox.interpreters)
    pm.register(tox.venv)
    pm.register(tox.session)
    pm.load_setuptools_entrypoints("tox")
    for plugin in plugins:
        pm.register(plugin)
    pm.check_pending()
    return pm


class Parser:
    """ command line and ini-parser control object. """

    def __init__(self):
        self.argparser = argparse.ArgumentParser(
            description="tox options", add_help=False)
        self._testenv_attr = []

    def add_argument(self, *args, **kwargs):
        """ add argument to command line parser.  This takes the
        same arguments that ``argparse.ArgumentParser.add_argument``.
        """
        return self.argparser.add_argument(*args, **kwargs)

    def add_testenv_attribute(self, name, type, help, default=None, postprocess=None):
        """ add an ini-file variable for "testenv" section.

        Types are specified as strings like "bool", "line-list", "string", "argv", "path",
        "argvlist".

        The ``postprocess`` function will be called for each testenv
        like ``postprocess(testenv_config=testenv_config, value=value)``
        where ``value`` is the value as read from the ini (or the default value)
        and ``testenv_config`` is a :py:class:`tox.config.TestenvConfig` instance
        which will receive all ini-variables as object attributes.

        Any postprocess function must return a value which will then be set
        as the final value in the testenv section.
        """
        self._testenv_attr.append(VenvAttribute(name, type, default, help, postprocess))

    def add_testenv_attribute_obj(self, obj):
        """ add an ini-file variable as an object.

        This works as the ``add_testenv_attribute`` function but expects
        "name", "type", "help", and "postprocess" attributes on the object.
        """
        assert hasattr(obj, "name")
        assert hasattr(obj, "type")
        assert hasattr(obj, "help")
        assert hasattr(obj, "postprocess")
        self._testenv_attr.append(obj)

    def _parse_args(self, args):
        return self.argparser.parse_args(args)

    def _format_help(self):
        return self.argparser.format_help()


class VenvAttribute:
    def __init__(self, name, type, default, help, postprocess):
        self.name = name
        self.type = type
        self.default = default
        self.help = help
        self.postprocess = postprocess


class DepOption:
    name = "deps"
    type = "line-list"
    help = "each line specifies a dependency in pip/setuptools format."
    default = ()

    def postprocess(self, testenv_config, value):
        deps = []
        config = testenv_config.config
        for depline in value:
            m = re.match(r":(\w+):\s*(\S+)", depline)
            if m:
                iname, name = m.groups()
                ixserver = config.indexserver[iname]
            else:
                name = depline.strip()
                ixserver = None
            name = self._replace_forced_dep(name, config)
            deps.append(DepConfig(name, ixserver))
        return deps

    def _replace_forced_dep(self, name, config):
        """
        Override the given dependency config name taking --force-dep-version
        option into account.

        :param name: dep config, for example ["pkg==1.0", "other==2.0"].
        :param config: Config instance
        :return: the new dependency that should be used for virtual environments
        """
        if not config.option.force_dep:
            return name
        for forced_dep in config.option.force_dep:
            if self._is_same_dep(forced_dep, name):
                return forced_dep
        return name

    @classmethod
    def _is_same_dep(cls, dep1, dep2):
        """
        Returns True if both dependency definitions refer to the
        same package, even if versions differ.
        """
        dep1_name = pkg_resources.Requirement.parse(dep1).project_name
        try:
            dep2_name = pkg_resources.Requirement.parse(dep2).project_name
        except pkg_resources.RequirementParseError:
            # we couldn't parse a version, probably a URL
            return False
        return dep1_name == dep2_name


class PosargsOption:
    name = "args_are_paths"
    type = "bool"
    default = True
    help = "treat positional args in commands as paths"

    def postprocess(self, testenv_config, value):
        config = testenv_config.config
        args = config.option.args
        if args:
            if value:
                args = []
                for arg in config.option.args:
                    if arg:
                        origpath = config.invocationcwd.join(arg, abs=True)
                        if origpath.check():
                            arg = testenv_config.changedir.bestrelpath(origpath)
                    args.append(arg)
            testenv_config._reader.addsubstitutions(args)
        return value


class InstallcmdOption:
    name = "install_command"
    type = "argv"
    default = "pip install {opts} {packages}"
    help = "install command for dependencies and package under test."

    def postprocess(self, testenv_config, value):
        if '{packages}' not in value:
            raise tox.exception.ConfigError(
                "'install_command' must contain '{packages}' substitution")
        return value


def parseconfig(args=None, plugins=()):
    """
    :param list[str] args: Optional list of arguments.
    :type pkg: str
    :rtype: :class:`Config`
    :raise SystemExit: toxinit file is not found
    """

    pm = get_plugin_manager(plugins)

    if args is None:
        args = sys.argv[1:]

    # prepare command line options
    parser = Parser()
    pm.hook.tox_addoption(parser=parser)

    # parse command line options
    option = parser._parse_args(args)
    interpreters = tox.interpreters.Interpreters(hook=pm.hook)
    config = Config(pluginmanager=pm, option=option, interpreters=interpreters)
    config._parser = parser
    config._testenv_attr = parser._testenv_attr

    # parse ini file
    basename = config.option.configfile
    if os.path.isfile(basename):
        inipath = py.path.local(basename)
    elif os.path.isdir(basename):
        # Assume 'tox.ini' filename if directory was passed
        inipath = py.path.local(os.path.join(basename, 'tox.ini'))
    else:
        for path in py.path.local().parts(reverse=True):
            inipath = path.join(basename)
            if inipath.check():
                break
        else:
            inipath = py.path.local().join('setup.cfg')
            if not inipath.check():
                helpoptions = option.help or option.helpini
                feedback("toxini file %r not found" % (basename),
                         sysexit=not helpoptions)
                if helpoptions:
                    return config

    try:
        parseini(config, inipath)
    except tox.exception.InterpreterNotFound:
        exn = sys.exc_info()[1]
        # Use stdout to match test expectations
        py.builtin.print_("ERROR: " + str(exn))

    # post process config object
    pm.hook.tox_configure(config=config)

    return config


def feedback(msg, sysexit=False):
    py.builtin.print_("ERROR: " + msg, file=sys.stderr)
    if sysexit:
        raise SystemExit(1)


class VersionAction(argparse.Action):
    def __call__(self, argparser, *args, **kwargs):
        version = tox.__version__
        py.builtin.print_("%s imported from %s" % (version, tox.__file__))
        raise SystemExit(0)


class SetenvDict:
    def __init__(self, dict, reader):
        self.reader = reader
        self.definitions = dict
        self.resolved = {}
        self._lookupstack = []

    def __contains__(self, name):
        return name in self.definitions

    def get(self, name, default=None):
        try:
            return self.resolved[name]
        except KeyError:
            try:
                if name in self._lookupstack:
                    raise KeyError(name)
                val = self.definitions[name]
            except KeyError:
                return os.environ.get(name, default)
            self._lookupstack.append(name)
            try:
                self.resolved[name] = res = self.reader._replace(val)
            finally:
                self._lookupstack.pop()
            return res

    def __getitem__(self, name):
        x = self.get(name, _dummy)
        if x is _dummy:
            raise KeyError(name)
        return x

    def keys(self):
        return self.definitions.keys()

    def __setitem__(self, name, value):
        self.definitions[name] = value
        self.resolved[name] = value


@hookimpl
def tox_addoption(parser):
    # formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--version", nargs=0, action=VersionAction,
                        dest="version",
                        help="report version information to stdout.")
    parser.add_argument("-h", "--help", action="store_true", dest="help",
                        help="show help about options")
    parser.add_argument("--help-ini", "--hi", action="store_true", dest="helpini",
                        help="show help about ini-names")
    parser.add_argument("-v", action='count', dest="verbosity", default=0,
                        help="increase verbosity of reporting output. -vv mode turns off "
                        "output redirection for package installation")
    parser.add_argument("--showconfig", action="store_true",
                        help="show configuration information for all environments. ")
    parser.add_argument("-l", "--listenvs", action="store_true",
                        dest="listenvs", help="show list of test environments")
    parser.add_argument("-c", action="store", default="tox.ini",
                        dest="configfile",
                        help="config file name or directory with 'tox.ini' file.")
    parser.add_argument("-e", action="append", dest="env",
                        metavar="envlist",
                        help="work against specified environments (ALL selects all).")
    parser.add_argument("--notest", action="store_true", dest="notest",
                        help="skip invoking test commands.")
    parser.add_argument("--sdistonly", action="store_true", dest="sdistonly",
                        help="only perform the sdist packaging activity.")
    parser.add_argument("--installpkg", action="store", default=None,
                        metavar="PATH",
                        help="use specified package for installation into venv, instead of "
                             "creating an sdist.")
    parser.add_argument("--develop", action="store_true", dest="develop",
                        help="install package in the venv using 'setup.py develop' via "
                             "'pip -e .'")
    parser.add_argument('-i', action="append",
                        dest="indexurl", metavar="URL",
                        help="set indexserver url (if URL is of form name=url set the "
                             "url for the 'name' indexserver, specifically)")
    parser.add_argument("--pre", action="store_true", dest="pre",
                        help="install pre-releases and development versions of dependencies. "
                             "This will pass the --pre option to install_command "
                             "(pip by default).")
    parser.add_argument("-r", "--recreate", action="store_true",
                        dest="recreate",
                        help="force recreation of virtual environments")
    parser.add_argument("--result-json", action="store",
                        dest="resultjson", metavar="PATH",
                        help="write a json file with detailed information "
                        "about all commands and results involved.")

    # We choose 1 to 4294967295 because it is the range of PYTHONHASHSEED.
    parser.add_argument("--hashseed", action="store",
                        metavar="SEED", default=None,
                        help="set PYTHONHASHSEED to SEED before running commands.  "
                             "Defaults to a random integer in the range [1, 4294967295] "
                             "([1, 1024] on Windows). "
                             "Passing 'noset' suppresses this behavior.")
    parser.add_argument("--force-dep", action="append",
                        metavar="REQ", default=None,
                        help="Forces a certain version of one of the dependencies "
                             "when configuring the virtual environment. REQ Examples "
                             "'pytest<2.7' or 'django>=1.6'.")
    parser.add_argument("--sitepackages", action="store_true",
                        help="override sitepackages setting to True in all envs")
    parser.add_argument("--alwayscopy", action="store_true",
                        help="override alwayscopy setting to True in all envs")
    parser.add_argument("--skip-missing-interpreters", action="store_true",
                        help="don't fail tests for missing interpreters")
    parser.add_argument("--workdir", action="store",
                        dest="workdir", metavar="PATH", default=None,
                        help="tox working directory")

    parser.add_argument("args", nargs="*",
                        help="additional arguments available to command positional substitution")

    parser.add_testenv_attribute(
        name="envdir", type="path", default="{toxworkdir}/{envname}",
        help="set venv directory -- be very careful when changing this as tox "
             "will remove this directory when recreating an environment")

    # add various core venv interpreter attributes
    def setenv(testenv_config, value):
        setenv = value
        config = testenv_config.config
        if "PYTHONHASHSEED" not in setenv and config.hashseed is not None:
            setenv['PYTHONHASHSEED'] = config.hashseed
        return setenv

    parser.add_testenv_attribute(
        name="setenv", type="dict_setenv", postprocess=setenv,
        help="list of X=Y lines with environment variable settings")

    def basepython_default(testenv_config, value):
        if value is None:
            for f in testenv_config.factors:
                if f in default_factors:
                    return default_factors[f]
            return sys.executable
        return str(value)

    parser.add_testenv_attribute(
        name="basepython", type="string", default=None, postprocess=basepython_default,
        help="executable name or path of interpreter used to create a "
             "virtual test environment.")

    parser.add_testenv_attribute(
        name="envtmpdir", type="path", default="{envdir}/tmp",
        help="venv temporary directory")

    parser.add_testenv_attribute(
        name="envlogdir", type="path", default="{envdir}/log",
        help="venv log directory")

    parser.add_testenv_attribute(
        name="downloadcache", type="string", default=None,
        help="(ignored) has no effect anymore, pip-8 uses local caching by default")

    parser.add_testenv_attribute(
        name="changedir", type="path", default="{toxinidir}",
        help="directory to change to when running commands")

    parser.add_testenv_attribute_obj(PosargsOption())

    parser.add_testenv_attribute(
        name="skip_install", type="bool", default=False,
        help="Do not install the current package. This can be used when "
             "you need the virtualenv management but do not want to install "
             "the current package")

    parser.add_testenv_attribute(
        name="ignore_errors", type="bool", default=False,
        help="if set to True all commands will be executed irrespective of their "
             "result error status.")

    def recreate(testenv_config, value):
        if testenv_config.config.option.recreate:
            return True
        return value

    parser.add_testenv_attribute(
        name="recreate", type="bool", default=False, postprocess=recreate,
        help="always recreate this test environment.")

    def passenv(testenv_config, value):
        # Flatten the list to deal with space-separated values.
        value = list(
            itertools.chain.from_iterable(
                [x.split(' ') for x in value]))

        passenv = set(["PATH", "PIP_INDEX_URL", "LANG", "LD_LIBRARY_PATH"])

        # read in global passenv settings
        p = os.environ.get("TOX_TESTENV_PASSENV", None)
        if p is not None:
            env_values = [x for x in p.split() if x]
            value.extend(env_values)

        # we ensure that tmp directory settings are passed on
        # we could also set it to the per-venv "envtmpdir"
        # but this leads to very long paths when run with jenkins
        # so we just pass it on by default for now.
        if sys.platform == "win32":
            passenv.add("SYSTEMDRIVE")  # needed for pip6
            passenv.add("SYSTEMROOT")   # needed for python's crypto module
            passenv.add("PATHEXT")      # needed for discovering executables
            passenv.add("COMSPEC")      # needed for distutils cygwincompiler
            passenv.add("TEMP")
            passenv.add("TMP")
        else:
            passenv.add("TMPDIR")
        for spec in value:
            for name in os.environ:
                if fnmatchcase(name.upper(), spec.upper()):
                    passenv.add(name)
        return passenv

    parser.add_testenv_attribute(
        name="passenv", type="line-list", postprocess=passenv,
        help="environment variables needed during executing test commands "
             "(taken from invocation environment). Note that tox always "
             "passes through some basic environment variables which are "
             "needed for basic functioning of the Python system. "
             "See --showconfig for the eventual passenv setting.")

    parser.add_testenv_attribute(
        name="whitelist_externals", type="line-list",
        help="each lines specifies a path or basename for which tox will not warn "
             "about it coming from outside the test environment.")

    parser.add_testenv_attribute(
        name="platform", type="string", default=".*",
        help="regular expression which must match against ``sys.platform``. "
             "otherwise testenv will be skipped.")

    def sitepackages(testenv_config, value):
        return testenv_config.config.option.sitepackages or value

    def alwayscopy(testenv_config, value):
        return testenv_config.config.option.alwayscopy or value

    parser.add_testenv_attribute(
        name="sitepackages", type="bool", default=False, postprocess=sitepackages,
        help="Set to ``True`` if you want to create virtual environments that also "
             "have access to globally installed packages.")

    parser.add_testenv_attribute(
        name="alwayscopy", type="bool", default=False, postprocess=alwayscopy,
        help="Set to ``True`` if you want virtualenv to always copy files rather "
             "than symlinking.")

    def pip_pre(testenv_config, value):
        return testenv_config.config.option.pre or value

    parser.add_testenv_attribute(
        name="pip_pre", type="bool", default=False, postprocess=pip_pre,
        help="If ``True``, adds ``--pre`` to the ``opts`` passed to "
             "the install command. ")

    def develop(testenv_config, value):
        option = testenv_config.config.option
        return not option.installpkg and (value or option.develop)

    parser.add_testenv_attribute(
        name="usedevelop", type="bool", postprocess=develop, default=False,
        help="install package in develop/editable mode")

    parser.add_testenv_attribute_obj(InstallcmdOption())

    parser.add_testenv_attribute(
        name="list_dependencies_command",
        type="argv",
        default="pip freeze",
        help="list dependencies for a virtual environment")

    parser.add_testenv_attribute_obj(DepOption())

    parser.add_testenv_attribute(
        name="commands", type="argvlist", default="",
        help="each line specifies a test command and can use substitution.")

    parser.add_testenv_attribute(
        "ignore_outcome", type="bool", default=False,
        help="if set to True a failing result of this testenv will not make "
             "tox fail, only a warning will be produced")

    parser.add_testenv_attribute(
        "extras", type="line-list",
        help="list of extras to install with the source distribution or "
             "develop install")


class Config(object):
    """ Global Tox config object. """
    def __init__(self, pluginmanager, option, interpreters):
        #: dictionary containing envname to envconfig mappings
        self.envconfigs = {}
        self.invocationcwd = py.path.local()
        self.interpreters = interpreters
        self.pluginmanager = pluginmanager
        #: option namespace containing all parsed command line options
        self.option = option

    @property
    def homedir(self):
        homedir = get_homedir()
        if homedir is None:
            homedir = self.toxinidir  # XXX good idea?
        return homedir


class TestenvConfig:
    """ Testenv Configuration object.
    In addition to some core attributes/properties this config object holds all
    per-testenv ini attributes as attributes, see "tox --help-ini" for an overview.
    """
    def __init__(self, envname, config, factors, reader):
        #: test environment name
        self.envname = envname
        #: global tox config object
        self.config = config
        #: set of factors
        self.factors = factors
        self._reader = reader

    def get_envbindir(self):
        """ path to directory where scripts/binaries reside. """
        if (sys.platform == "win32"
                and "jython" not in self.basepython
                and "pypy" not in self.basepython):
            return self.envdir.join("Scripts")
        else:
            return self.envdir.join("bin")

    @property
    def envbindir(self):
        return self.get_envbindir()

    @property
    def envpython(self):
        """ path to python executable. """
        return self.get_envpython()

    def get_envpython(self):
        """ path to python/jython executable. """
        if "jython" in str(self.basepython):
            name = "jython"
        else:
            name = "python"
        return self.envbindir.join(name)

    def get_envsitepackagesdir(self):
        """ return sitepackagesdir of the virtualenv environment.
        (only available during execution, not parsing)
        """
        x = self.config.interpreters.get_sitepackagesdir(
            info=self.python_info,
            envdir=self.envdir)
        return x

    @property
    def python_info(self):
        """ return sitepackagesdir of the virtualenv environment. """
        return self.config.interpreters.get_info(envconfig=self)

    def getsupportedinterpreter(self):
        if sys.platform == "win32" and self.basepython and \
                "jython" in self.basepython:
            raise tox.exception.UnsupportedInterpreter(
                "Jython/Windows does not support installing scripts")
        info = self.config.interpreters.get_info(envconfig=self)
        if not info.executable:
            raise tox.exception.InterpreterNotFound(self.basepython)
        if not info.version_info:
            raise tox.exception.InvocationError(
                'Failed to get version_info for %s: %s' % (info.name, info.err))
        if info.version_info < (2, 6):
            raise tox.exception.UnsupportedInterpreter(
                "python2.5 is not supported anymore, sorry")
        return info.executable


testenvprefix = "testenv:"


def get_homedir():
    try:
        return py.path.local._gethomedir()
    except Exception:
        return None


def make_hashseed():
    max_seed = 4294967295
    if sys.platform == 'win32':
        max_seed = 1024
    return str(random.randint(1, max_seed))


class parseini:
    def __init__(self, config, inipath):
        config.toxinipath = inipath
        config.toxinidir = config.toxinipath.dirpath()

        self._cfg = py.iniconfig.IniConfig(config.toxinipath)
        config._cfg = self._cfg
        self.config = config

        if inipath.basename == 'setup.cfg':
            prefix = 'tox'
        else:
            prefix = None
        ctxname = getcontextname()
        if ctxname == "jenkins":
            reader = SectionReader("tox:jenkins", self._cfg, prefix=prefix,
                                   fallbacksections=['tox'])
            distshare_default = "{toxworkdir}/distshare"
        elif not ctxname:
            reader = SectionReader("tox", self._cfg, prefix=prefix)
            distshare_default = "{homedir}/.tox/distshare"
        else:
            raise ValueError("invalid context")

        if config.option.hashseed is None:
            hashseed = make_hashseed()
        elif config.option.hashseed == 'noset':
            hashseed = None
        else:
            hashseed = config.option.hashseed
        config.hashseed = hashseed

        reader.addsubstitutions(toxinidir=config.toxinidir,
                                homedir=config.homedir)
        # As older versions of tox may have bugs or incompatabilities that
        # prevent parsing of tox.ini this must be the first thing checked.
        config.minversion = reader.getstring("minversion", None)
        if config.minversion:
            minversion = NormalizedVersion(self.config.minversion)
            toxversion = NormalizedVersion(tox.__version__)
            if toxversion < minversion:
                raise tox.exception.MinVersionError(
                    "tox version is %s, required is at least %s" % (
                        toxversion, minversion))
        if config.option.workdir is None:
            config.toxworkdir = reader.getpath("toxworkdir", "{toxinidir}/.tox")
        else:
            config.toxworkdir = config.toxinidir.join(config.option.workdir, abs=True)

        if not config.option.skip_missing_interpreters:
            config.option.skip_missing_interpreters = \
                reader.getbool("skip_missing_interpreters", False)

        # determine indexserver dictionary
        config.indexserver = {'default': IndexServerConfig('default')}
        prefix = "indexserver"
        for line in reader.getlist(prefix):
            name, url = map(lambda x: x.strip(), line.split("=", 1))
            config.indexserver[name] = IndexServerConfig(name, url)

        override = False
        if config.option.indexurl:
            for urldef in config.option.indexurl:
                m = re.match(r"\W*(\w+)=(\S+)", urldef)
                if m is None:
                    url = urldef
                    name = "default"
                else:
                    name, url = m.groups()
                    if not url:
                        url = None
                if name != "ALL":
                    config.indexserver[name].url = url
                else:
                    override = url
        # let ALL override all existing entries
        if override:
            for name in config.indexserver:
                config.indexserver[name] = IndexServerConfig(name, override)

        reader.addsubstitutions(toxworkdir=config.toxworkdir)
        config.distdir = reader.getpath("distdir", "{toxworkdir}/dist")
        reader.addsubstitutions(distdir=config.distdir)
        config.distshare = reader.getpath("distshare", distshare_default)
        reader.addsubstitutions(distshare=config.distshare)
        config.sdistsrc = reader.getpath("sdistsrc", None)
        config.setupdir = reader.getpath("setupdir", "{toxinidir}")
        config.logdir = config.toxworkdir.join("log")

        config.envlist, all_envs = self._getenvdata(reader)

        # factors used in config or predefined
        known_factors = self._list_section_factors("testenv")
        known_factors.update(default_factors)
        known_factors.add("python")

        # factors stated in config envlist
        stated_envlist = reader.getstring("envlist", replace=False)
        if stated_envlist:
            for env in _split_env(stated_envlist):
                known_factors.update(env.split('-'))

        # configure testenvs
        for name in all_envs:
            section = testenvprefix + name
            factors = set(name.split('-'))
            if section in self._cfg or factors <= known_factors:
                config.envconfigs[name] = \
                    self.make_envconfig(name, section, reader._subs, config)

        all_develop = all(name in config.envconfigs
                          and config.envconfigs[name].usedevelop
                          for name in config.envlist)

        config.skipsdist = reader.getbool("skipsdist", all_develop)

    def _list_section_factors(self, section):
        factors = set()
        if section in self._cfg:
            for _, value in self._cfg[section].items():
                exprs = re.findall(r'^([\w{}\.,-]+)\:\s+', value, re.M)
                factors.update(*mapcat(_split_factor_expr, exprs))
        return factors

    def make_envconfig(self, name, section, subs, config):
        factors = set(name.split('-'))
        reader = SectionReader(section, self._cfg, fallbacksections=["testenv"],
                               factors=factors)
        vc = TestenvConfig(config=config, envname=name, factors=factors, reader=reader)
        reader.addsubstitutions(**subs)
        reader.addsubstitutions(envname=name)
        reader.addsubstitutions(envbindir=vc.get_envbindir,
                                envsitepackagesdir=vc.get_envsitepackagesdir,
                                envpython=vc.get_envpython)

        for env_attr in config._testenv_attr:
            atype = env_attr.type
            if atype in ("bool", "path", "string", "dict", "dict_setenv", "argv", "argvlist"):
                meth = getattr(reader, "get" + atype)
                res = meth(env_attr.name, env_attr.default)
            elif atype == "space-separated-list":
                res = reader.getlist(env_attr.name, sep=" ")
            elif atype == "line-list":
                res = reader.getlist(env_attr.name, sep="\n")
            else:
                raise ValueError("unknown type %r" % (atype,))

            if env_attr.postprocess:
                res = env_attr.postprocess(testenv_config=vc, value=res)
            setattr(vc, env_attr.name, res)

            if atype == "path":
                reader.addsubstitutions(**{env_attr.name: res})

        return vc

    def _getenvdata(self, reader):
        envstr = self.config.option.env                                \
            or os.environ.get("TOXENV")                                \
            or reader.getstring("envlist", replace=False) \
            or []
        envlist = _split_env(envstr)

        # collect section envs
        all_envs = set(envlist) - set(["ALL"])
        for section in self._cfg:
            if section.name.startswith(testenvprefix):
                all_envs.add(section.name[len(testenvprefix):])
        if not all_envs:
            all_envs.add("python")

        if not envlist or "ALL" in envlist:
            envlist = sorted(all_envs)

        return envlist, all_envs


def _split_env(env):
    """if handed a list, action="append" was used for -e """
    if not isinstance(env, list):
        if '\n' in env:
            env = ','.join(env.split('\n'))
        env = [env]
    return mapcat(_expand_envstr, env)


def _split_factor_expr(expr):
    partial_envs = _expand_envstr(expr)
    return [set(e.split('-')) for e in partial_envs]


def _expand_envstr(envstr):
    # split by commas not in groups
    tokens = re.split(r'((?:\{[^}]+\})+)|,', envstr)
    envlist = [''.join(g).strip()
               for k, g in itertools.groupby(tokens, key=bool) if k]

    def expand(env):
        tokens = re.split(r'\{([^}]+)\}', env)
        parts = [token.split(',') for token in tokens]
        return [''.join(variant) for variant in itertools.product(*parts)]

    return mapcat(expand, envlist)


def mapcat(f, seq):
    return list(itertools.chain.from_iterable(map(f, seq)))


class DepConfig:
    def __init__(self, name, indexserver=None):
        self.name = name
        self.indexserver = indexserver

    def __str__(self):
        if self.indexserver:
            if self.indexserver.name == "default":
                return self.name
            return ":%s:%s" % (self.indexserver.name, self.name)
        return str(self.name)
    __repr__ = __str__


class IndexServerConfig:
    def __init__(self, name, url=None):
        self.name = name
        self.url = url


#: Check value matches substitution form
#: of referencing value from other section. E.g. {[base]commands}
is_section_substitution = re.compile("{\[[^{}\s]+\]\S+?}").match


class SectionReader:
    def __init__(self, section_name, cfgparser, fallbacksections=None,
                 factors=(), prefix=None):
        if prefix is None:
            self.section_name = section_name
        else:
            self.section_name = "%s:%s" % (prefix, section_name)
        self._cfg = cfgparser
        self.fallbacksections = fallbacksections or []
        self.factors = factors
        self._subs = {}
        self._subststack = []
        self._setenv = None

    def get_environ_value(self, name):
        if self._setenv is None:
            return os.environ.get(name)
        return self._setenv.get(name)

    def addsubstitutions(self, _posargs=None, **kw):
        self._subs.update(kw)
        if _posargs:
            self.posargs = _posargs

    def getpath(self, name, defaultpath):
        toxinidir = self._subs['toxinidir']
        path = self.getstring(name, defaultpath)
        if path is not None:
            return toxinidir.join(path, abs=True)

    def getlist(self, name, sep="\n"):
        s = self.getstring(name, None)
        if s is None:
            return []
        return [x.strip() for x in s.split(sep) if x.strip()]

    def getdict(self, name, default=None, sep="\n"):
        value = self.getstring(name, None)
        return self._getdict(value, default=default, sep=sep)

    def getdict_setenv(self, name, default=None, sep="\n"):
        value = self.getstring(name, None, replace=True, crossonly=True)
        definitions = self._getdict(value, default=default, sep=sep)
        self._setenv = SetenvDict(definitions, reader=self)
        return self._setenv

    def _getdict(self, value, default, sep):
        if value is None:
            return default or {}

        d = {}
        for line in value.split(sep):
            if line.strip():
                name, rest = line.split('=', 1)
                d[name.strip()] = rest.strip()

        return d

    def getbool(self, name, default=None):
        s = self.getstring(name, default)
        if not s:
            s = default
        if s is None:
            raise KeyError("no config value [%s] %s found" % (
                           self.section_name, name))

        if not isinstance(s, bool):
            if s.lower() == "true":
                s = True
            elif s.lower() == "false":
                s = False
            else:
                raise tox.exception.ConfigError(
                    "boolean value %r needs to be 'True' or 'False'")
        return s

    def getargvlist(self, name, default=""):
        s = self.getstring(name, default, replace=False)
        return _ArgvlistReader.getargvlist(self, s)

    def getargv(self, name, default=""):
        return self.getargvlist(name, default)[0]

    def getstring(self, name, default=None, replace=True, crossonly=False):
        x = None
        for s in [self.section_name] + self.fallbacksections:
            try:
                x = self._cfg[s][name]
                break
            except KeyError:
                continue

        if x is None:
            x = default
        else:
            x = self._apply_factors(x)

        if replace and x and hasattr(x, 'replace'):
            x = self._replace(x, name=name, crossonly=crossonly)
        # print "getstring", self.section_name, name, "returned", repr(x)
        return x

    def _apply_factors(self, s):
        def factor_line(line):
            m = re.search(r'^([\w{}\.,-]+)\:\s+(.+)', line)
            if not m:
                return line

            expr, line = m.groups()
            if any(fs <= self.factors for fs in _split_factor_expr(expr)):
                return line

        lines = s.strip().splitlines()
        return '\n'.join(filter(None, map(factor_line, lines)))

    def _replace(self, value, name=None, section_name=None, crossonly=False):
        if '{' not in value:
            return value

        section_name = section_name if section_name else self.section_name
        self._subststack.append((section_name, name))
        try:
            return Replacer(self, crossonly=crossonly).do_replace(value)
        finally:
            assert self._subststack.pop() == (section_name, name)


class Replacer:
    RE_ITEM_REF = re.compile(
        r'''
        (?<!\\)[{]
        (?:(?P<sub_type>[^[:{}]+):)?    # optional sub_type for special rules
        (?P<substitution_value>(?:\[[^,{}]*\])?[^:,{}]*)  # substitution key
        (?::(?P<default_value>[^{}]*))?   # default value
        [}]
        ''', re.VERBOSE)

    def __init__(self, reader, crossonly=False):
        self.reader = reader
        self.crossonly = crossonly

    def do_replace(self, x):
        return self.RE_ITEM_REF.sub(self._replace_match, x)

    def _replace_match(self, match):
        g = match.groupdict()
        sub_value = g['substitution_value']
        if self.crossonly:
            if sub_value.startswith("["):
                return self._substitute_from_other_section(sub_value)
            # in crossonly we return all other hits verbatim
            start, end = match.span()
            return match.string[start:end]

        # special case: all empty values means ":" which is os.pathsep
        if not any(g.values()):
            return os.pathsep

        # special case: opts and packages. Leave {opts} and
        # {packages} intact, they are replaced manually in
        # _venv.VirtualEnv.run_install_command.
        if sub_value in ('opts', 'packages'):
            return '{%s}' % sub_value

        try:
            sub_type = g['sub_type']
        except KeyError:
            raise tox.exception.ConfigError(
                "Malformed substitution; no substitution type provided")

        if sub_type == "env":
            return self._replace_env(match)
        if sub_type is not None:
            raise tox.exception.ConfigError(
                "No support for the %s substitution type" % sub_type)
        return self._replace_substitution(match)

    def _replace_env(self, match):
        envkey = match.group('substitution_value')
        if not envkey:
            raise tox.exception.ConfigError(
                'env: requires an environment variable name')

        default = match.group('default_value')

        envvalue = self.reader.get_environ_value(envkey)
        if envvalue is None:
            if default is None:
                raise tox.exception.ConfigError(
                    "substitution env:%r: unknown environment variable %r "
                    " or recursive definition." %
                    (envkey, envkey))
            return default
        return envvalue

    def _substitute_from_other_section(self, key):
        if key.startswith("[") and "]" in key:
            i = key.find("]")
            section, item = key[1:i], key[i + 1:]
            cfg = self.reader._cfg
            if section in cfg and item in cfg[section]:
                if (section, item) in self.reader._subststack:
                    raise ValueError('%s already in %s' % (
                        (section, item), self.reader._subststack))
                x = str(cfg[section][item])
                return self.reader._replace(x, name=item, section_name=section,
                                            crossonly=self.crossonly)

        raise tox.exception.ConfigError(
            "substitution key %r not found" % key)

    def _replace_substitution(self, match):
        sub_key = match.group('substitution_value')
        val = self.reader._subs.get(sub_key, None)
        if val is None:
            val = self._substitute_from_other_section(sub_key)
        if py.builtin.callable(val):
            val = val()
        return str(val)


class _ArgvlistReader:
    @classmethod
    def getargvlist(cls, reader, value):
        """Parse ``commands`` argvlist multiline string.

        :param str name: Key name in a section.
        :param str value: Content stored by key.

        :rtype: list[list[str]]
        :raise :class:`tox.exception.ConfigError`:
            line-continuation ends nowhere while resolving for specified section
        """
        commands = []
        current_command = ""
        for line in value.splitlines():
            line = line.rstrip()
            if not line:
                continue
            if line.endswith("\\"):
                current_command += " " + line[:-1]
                continue
            current_command += line

            if is_section_substitution(current_command):
                replaced = reader._replace(current_command, crossonly=True)
                commands.extend(cls.getargvlist(reader, replaced))
            else:
                commands.append(cls.processcommand(reader, current_command))
            current_command = ""
        else:
            if current_command:
                raise tox.exception.ConfigError(
                    "line-continuation ends nowhere while resolving for [%s] %s" %
                    (reader.section_name, "commands"))
        return commands

    @classmethod
    def processcommand(cls, reader, command):
        posargs = getattr(reader, "posargs", "")
        posargs_string = list2cmdline([x for x in posargs if x])

        # Iterate through each word of the command substituting as
        # appropriate to construct the new command string. This
        # string is then broken up into exec argv components using
        # shlex.
        newcommand = ""
        for word in CommandParser(command).words():
            if word == "{posargs}" or word == "[]":
                newcommand += posargs_string
                continue
            elif word.startswith("{posargs:") and word.endswith("}"):
                if posargs:
                    newcommand += posargs_string
                    continue
                else:
                    word = word[9:-1]
            new_arg = ""
            new_word = reader._replace(word)
            new_word = reader._replace(new_word)
            new_word = new_word.replace('\\{', '{').replace('\\}', '}')
            new_arg += new_word
            newcommand += new_arg

        # Construct shlex object that will not escape any values,
        # use all values as is in argv.
        shlexer = shlex.shlex(newcommand, posix=True)
        shlexer.whitespace_split = True
        shlexer.escape = ''
        return list(shlexer)


class CommandParser(object):

    class State(object):
        def __init__(self):
            self.word = ''
            self.depth = 0
            self.yield_words = []

    def __init__(self, command):
        self.command = command

    def words(self):
        ps = CommandParser.State()

        def word_has_ended():
            return ((cur_char in string.whitespace and ps.word and
                     ps.word[-1] not in string.whitespace) or
                    (cur_char == '{' and ps.depth == 0 and not ps.word.endswith('\\')) or
                    (ps.depth == 0 and ps.word and ps.word[-1] == '}') or
                    (cur_char not in string.whitespace and ps.word and
                     ps.word.strip() == ''))

        def yield_this_word():
            yieldword = ps.word
            ps.word = ''
            if yieldword:
                ps.yield_words.append(yieldword)

        def yield_if_word_ended():
            if word_has_ended():
                yield_this_word()

        def accumulate():
            ps.word += cur_char

        def push_substitution():
            ps.depth += 1

        def pop_substitution():
            ps.depth -= 1

        for cur_char in self.command:
            if cur_char in string.whitespace:
                if ps.depth == 0:
                    yield_if_word_ended()
                accumulate()
            elif cur_char == '{':
                yield_if_word_ended()
                accumulate()
                push_substitution()
            elif cur_char == '}':
                accumulate()
                pop_substitution()
            else:
                yield_if_word_ended()
                accumulate()

        if ps.word.strip():
            yield_this_word()
        return ps.yield_words


def getcontextname():
    if any(env in os.environ for env in ['JENKINS_URL', 'HUDSON_URL']):
        return 'jenkins'
    return None
