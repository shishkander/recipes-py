# Copyright 2013-2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

from __future__ import absolute_import
import contextlib
import keyword
import re
import types

from functools import wraps

from .recipe_test_api import DisabledTestData, ModuleTestData
from .config import Single

from .util import ModuleInjectionSite

from . import field_composer


class StepFailure(Exception):
  """
  This is the base class for all step failures.

  Raising a StepFailure counts as 'running a step' for the purpose of
  infer_composite_step's logic.
  """
  def __init__(self, name_or_reason, result=None):
    _STEP_CONTEXT['ran_step'][0] = True
    if result:
      self.name = name_or_reason
      self.result = result
      self.reason = self.reason_message()
    else:
      self.name = None
      self.result = None
      self.reason = name_or_reason

    super(StepFailure, self).__init__(self.reason)

  def reason_message(self):
    return "Step({!r}) failed with return_code {}".format(
        self.name, self.result.retcode)

  def __str__(self):  # pragma: no cover
    return "Step Failure in %s" % self.name

  @property
  def retcode(self):
    """
    Returns the retcode of the step which failed. If this was a manual
    failure, returns None
    """
    if not self.result:
      return None
    return self.result.retcode


class StepWarning(StepFailure):
  """
  A subclass of StepFailure, which still fails the build, but which is
  a warning. Need to figure out how exactly this will be useful.
  """
  def reason_message(self):  # pragma: no cover
    return "Warning: Step({!r}) returned {}".format(
          self.name, self.result.retcode)

  def __str__(self):  # pragma: no cover
    return "Step Warning in %s" % self.name


class InfraFailure(StepFailure):
  """
  A subclass of StepFailure, which fails the build due to problems with the
  infrastructure.
  """
  def reason_message(self):
    return "Infra Failure: Step({!r}) returned {}".format(
          self.name, self.result.retcode)

  def __str__(self):
    return "Infra Failure in %s" % self.name


class AggregatedStepFailure(StepFailure):
  def __init__(self, result):
    super(AggregatedStepFailure, self).__init__(
            "Aggregate step failure.", result=result)

  def reason_message(self):
    msg = "{!r} out of {!r} aggregated steps failed. Failures: ".format(
        len(self.result.failures), len(self.result.all_results))
    msg += ', '.join((f.reason or f.name) for f in self.result.failures)
    return msg

  def __str__(self):  # pragma: no cover
    return "Aggregate Step Failure"


_FUNCTION_REGISTRY = {
  'aggregated_result': {'combine': lambda a, b: b},
  'env': {'combine': lambda a, b: dict(a, **b)},
  'name': {'combine': lambda a, b: '%s.%s' % (a, b)},
  'nest_level': {'combine': lambda a, b: a + b},
  'ran_step': {'combine': lambda a, b: b},
}


class AggregatedResult(object):
  """Holds the result of an aggregated run of steps.

  Currently this is only used internally by defer_results, but it may be exposed
  to the consumer of defer_results at some point in the future. For now it's
  expected to be easier for defer_results consumers to do their own result
  aggregation, as they may need to pick and chose (or label) which results they
  really care about.
  """
  def __init__(self):
    self.successes = []
    self.failures = []

    # Needs to be here to be able to treat this as a step result
    self.retcode = None

  @property
  def all_results(self):
    """
    Return a list of two item tuples (x, y), where
      x is whether or not the step succeeded, and
      y is the result of the run
    """
    res = [(True, result) for result in self.successes]
    res.extend([(False, result) for result in self.failures])
    return res

  def add_success(self, result):
    self.successes.append(result)

  def add_failure(self, exception):
    self.failures.append(exception)


class DeferredResult(object):
  def __init__(self, result, failure):
    self._result = result
    self._failure = failure

  @property
  def is_ok(self):
    return self._failure is None

  def get_result(self):
    if not self.is_ok:
      raise self.get_error()
    return self._result

  def get_error(self):
    assert self._failure, "WHAT IS IT ARE YOU DOING???!?!?!? SHTAP NAO"
    return self._failure


_STEP_CONTEXT = field_composer.FieldComposer(
  {'ran_step': [False]}, _FUNCTION_REGISTRY)


def non_step(func):
  """A decorator which prevents a method from automatically being wrapped as
  a infer_composite_step by RecipeApiMeta.

  This is needed for utility methods which don't run any steps, but which are
  invoked within the context of a defer_results().

  @see infer_composite_step, defer_results, RecipeApiMeta
  """
  assert not hasattr(func, "_skip_inference"), \
         "Double-wrapped method %r?" % func
  func._skip_inference = True # pylint: disable=protected-access
  return func

_skip_inference = non_step


@contextlib.contextmanager
def context(fields):
  global _STEP_CONTEXT
  old = _STEP_CONTEXT
  try:
    _STEP_CONTEXT = old.compose(fields)
    yield
  finally:
    _STEP_CONTEXT = old


def infer_composite_step(func):
  """A decorator which possibly makes this step act as a single step, for the
  purposes of the defer_results function.

  Behaves as if this function were wrapped by composite_step, unless this
  function:
    * is already wrapped by non_step
    * returns a result without calling api.step
    * raises an exception which is not derived from StepFailure

  In any of these cases, this function will behave like a normal function.

  This decorator is automatically applied by RecipeApiMeta (or by inheriting
  from RecipeApi). If you want to decalare a method's behavior explicitly, you
  may decorate it with either composite_step or with non_step.
  """
  if getattr(func, "_skip_inference", False):
    return func

  @_skip_inference # to prevent double-wraps
  @wraps(func)
  def _inner(*a, **kw):
    # We're not in a defer_results context, so just run the function normally.
    if _STEP_CONTEXT.get('aggregated_result') is None:
      return func(*a, **kw)

    agg = _STEP_CONTEXT['aggregated_result']

    # Setting the aggregated_result to None allows the contents of func to be
    # written in the same style (e.g. with exceptions) no matter how func is
    # being called.
    with context({'aggregated_result': None, 'ran_step': [False]}):
      try:
        ret = func(*a, **kw)
        if not _STEP_CONTEXT.get('ran_step', [False])[0]:
          return ret
        agg.add_success(ret)
        return DeferredResult(ret, None)
      except StepFailure as ex:
        agg.add_failure(ex)
        return DeferredResult(None, ex)
  return _inner


def composite_step(func):
  """A decorator which makes this step act as a single step, for the purposes of
  the defer_results function.

  This means that this function will not quit during the middle of its execution
  because of a StepFailure, if there is an aggregator active.

  You may use this decorator explicitly if infer_composite_step is detecting
  the behavior of your method incorrectly to force it to behave as a step. You
  may also need to use this if your Api class inherits from RecipeApiPlain and
  so doesn't have its methods automatically wrapped by infer_composite_step.
  """
  @_skip_inference  # to avoid double-wraps
  @wraps(func)
  def _inner(*a, **kw):
    # always counts as running a step
    _STEP_CONTEXT['ran_step'][0] = True

    if _STEP_CONTEXT.get('aggregated_result') is None:
      return func(*a, **kw)

    agg = _STEP_CONTEXT['aggregated_result']

    # Setting the aggregated_result to None allows the contents of func to be
    # written in the same style (e.g. with exceptions) no matter how func is
    # being called.
    with context({'aggregated_result': None}):
      try:
        ret = func(*a, **kw)
        agg.add_success(ret)
        return DeferredResult(ret, None)
      except StepFailure as ex:
        agg.add_failure(ex)
        return DeferredResult(None, ex)
  return _inner


@contextlib.contextmanager
def defer_results():
  """
  Use this to defer step results in your code. All steps which would previously
    return a result or throw an exception will instead return a DeferredResult.

  Any exceptions which were thrown during execution will be thrown when either:
    a. You call get_result() on the step's result.
    b. You exit the suite inside of the with statement

  Example:
    with defer_results():
      api.step('a', ..)
      api.step('b', ..)
      result = api.m.module.im_a_composite_step(...)
      api.m.echo('the data is', result.get_result())

  If 'a' fails, 'b' and 'im a composite step'  will still run.
  If 'im a composite step' fails, then the get_result() call will raise
    an exception.
  If you don't try to use the result (don't call get_result()), an aggregate
    failure will still be raised once you exit the suite inside
    the with statement.
  """
  assert _STEP_CONTEXT.get('aggregated_result') is None, (
      "may not call defer_results in an active defer_results context")
  agg = AggregatedResult()
  with context({'aggregated_result': agg}):
    yield
  if agg.failures:
    raise AggregatedStepFailure(agg)


class RecipeApiMeta(type):
  WHITELIST = ('__init__',)
  def __new__(mcs, name, bases, attrs):
    """Automatically wraps all methods of subclasses of RecipeApi with
    @infer_composite_step. This allows defer_results to work as intended without
    manually decorating every method.
    """
    wrap = lambda f: infer_composite_step(f) if f else f
    for attr in attrs:
      if attr in RecipeApiMeta.WHITELIST:
        continue
      val = attrs[attr]
      if isinstance(val, types.FunctionType):
        attrs[attr] = wrap(val)
      elif isinstance(val, property):
        attrs[attr] = property(
          wrap(val.fget),
          wrap(val.fset),
          wrap(val.fdel),
          val.__doc__)
    return super(RecipeApiMeta, mcs).__new__(mcs, name, bases, attrs)


class RecipeApiPlain(ModuleInjectionSite):
  """
  Framework class for handling recipe_modules.

  Inherit from this in your recipe_modules/<name>/api.py . This class provides
  wiring for your config context (in self.c and methods, and for dependency
  injection (in self.m).

  Dependency injection takes place in load_recipe_modules() in recipe_loader.py.

  USE RecipeApi INSTEAD, UNLESS your RecipeApi subclass derives from something
  which defines its own __metaclass__. Deriving from RecipeApi instead of
  RecipeApiPlain allows your RecipeApi subclass to automatically work with
  defer_results without needing to decorate every methods with
  @infer_composite_step.
  """

  def __init__(self, module=None, engine=None,
               test_data=DisabledTestData(), **_kwargs):
    """Note: Injected dependencies are NOT available in __init__()."""
    super(RecipeApiPlain, self).__init__()

    # |engine| is an instance of annotated_run.RecipeEngine. Modules should not
    # generally use it unless they're low-level framework level modules.
    self._engine = engine
    self._module = module

    assert isinstance(test_data, (ModuleTestData, DisabledTestData))
    self._test_data = test_data

    # If we're the 'root' api, inject directly into 'self'.
    # Otherwise inject into 'self.m'
    self.m = self if module is None else ModuleInjectionSite(self)

    # If our module has a test api, it gets injected here.
    self.test_api = None

    # Config goes here.
    self.c = None

  def get_config_defaults(self):  # pylint: disable=R0201
    """
    Allows your api to dynamically determine static default values for configs.
    """
    return {}

  def make_config(self, config_name=None, optional=False, **CONFIG_VARS):
    """Returns a 'config blob' for the current API."""
    return self.make_config_params(config_name, optional, **CONFIG_VARS)[0]

  def make_config_params(self, config_name, optional=False, **CONFIG_VARS):
    """Returns a 'config blob' for the current API, and the computed params
    for all dependent configurations.

    The params have the following order of precendence. Each subsequent param
    is dict.update'd into the final parameters, so the order is from lowest to
    higest precedence on a per-key basis:
      * if config_name in CONFIG_CTX
        * get_config_defaults()
        * CONFIG_CTX[config_name].DEFAULT_CONFIG_VARS()
        * CONFIG_VARS
      * else
        * get_config_defaults()
        * CONFIG_VARS
    """
    generic_params = self.get_config_defaults()  # generic defaults
    generic_params.update(CONFIG_VARS)           # per-invocation values

    ctx = self._module.CONFIG_CTX
    if optional and not ctx:
      return None, generic_params

    assert ctx, '%s has no config context' % self
    try:
      params = self.get_config_defaults()         # generic defaults
      itm = ctx.CONFIG_ITEMS[config_name] if config_name else None
      if itm:
        params.update(itm.DEFAULT_CONFIG_VARS())  # per-item defaults
      params.update(CONFIG_VARS)                  # per-invocation values

      base = ctx.CONFIG_SCHEMA(**params)
      if config_name is None:
        return base, params
      else:
        return itm(base), params
    except KeyError:
      if optional:
        return None, generic_params
      else:  # pragma: no cover
        raise  # TODO(iannucci): raise a better exception.

  def set_config(self, config_name=None, optional=False, **CONFIG_VARS):
    """Sets the modules and its dependencies to the named configuration."""
    assert self._module
    config, params = self.make_config_params(config_name, optional,
                                             **CONFIG_VARS)
    if config:
      self.c = config

  def apply_config(self, config_name, config_object=None, optional=False):
    """Apply a named configuration to the provided config object or self."""
    self._module.CONFIG_CTX.CONFIG_ITEMS[config_name](
        config_object or self.c, optional=optional)

  def resource(self, *path):
    """Returns path to a file under <recipe module>/resources/ directory.

    Args:
      path: path relative to module's resources/ directory.
    """
    # TODO(vadimsh): Verify that file exists. Including a case like:
    #  module.resource('dir').join('subdir', 'file.py')
    return self._module.MODULE_DIRECTORY.join('resources', *path)

  @property
  def name(self):
    return self._module.NAME


class RecipeApi(RecipeApiPlain):
  __metaclass__ = RecipeApiMeta

# This is a sentinel object for the Property system. This allows users to
# specify a default of None that will actually be respected.
PROPERTY_SENTINEL = object()

class BoundProperty(object):
  """
  A bound, named version of a Property.

  A BoundProperty is different than a Property, in that it requires a name,
  as well as all of the arguments to be provided. It's intended to be
  the declaration of the Property, with no mutation, so the logic about
  what a property does is very clear.

  The reason there is a distinction between this and a Property is because
  we want the user interface for defining properties to be
    PROPERTIES = {
      'prop_name': Property(),
    }

  We don't want to have to duplicate the name in both the key of the dictionary
  and then Property contstructor call, so we need to modify this dictionary
  before we actually use it, and inject knowledge into it about its name. We
  don't want to actually mutate this though, since we're striving for immutable,
  declarative code, so instead we generate a new BoundProperty object from the
  defined Property object.
  """

  MODULE_PROPERTY = 'module'
  RECIPE_PROPERTY = 'recipe'

  @staticmethod
  def legal_name(name, is_param_name=False):
    """
    If this name is a legal property name.

    is_param_name determines if this name in the name of a property, or a
      param_name. See the constructor documentation for more information.

    The rules are as follows:
      * Cannot start with an underscore.
        This is for internal arguments, namely _engine (for the step module).
      * Cannot be 'self'
        This is to avoid conflict with recipe modules, which use the name self.
      * Cannot be a python keyword
    """

    if name.startswith('_'):
      return False

    if name in ('self',):
      return False

    if keyword.iskeyword(name):
      return False

    regex = r'^[a-zA-Z][a-zA-Z0-9_]*$' if is_param_name else r'^[a-zA-Z][.\w]*$'
    return bool(re.match(regex, name))

  def __init__(self, default, help, kind, name, property_type, module,
               param_name=None):
    """
    Constructor for BoundProperty.

    Args:
      default: The default value for this Property. Note: A default
               value of None is allowed. To have no default value, omit
               this argument.
      help: The help text for this Property.
      kind: The type of this Property. You can either pass in a raw python
            type, or a Config Type, using the recipe engine config system.
      name: The name of this Property.
      param_name: The name of the python function parameter this property
                  should be stored in. Can be used to allow for dotted property
                  names, e.g.
        PROPERTIES = {
          'foo.bar.bam': Property(param_name="bizbaz")
        }
      module: The module this Property is a part of.
    """
    if not BoundProperty.legal_name(name):
      raise ValueError("Illegal name '{}'.".format(param_name))

    param_name = param_name or name
    if not BoundProperty.legal_name(param_name, is_param_name=True):
      raise ValueError("Illegal param_name '{}'.".format(param_name))

    self.__default = default
    self.__help = help
    self.__kind = kind
    self.__name = name
    self.__property_type = property_type
    self.__param_name = param_name
    self.__module = module

  @property
  def name(self):
    return self.__name

  @property
  def param_name(self):
    return self.__param_name

  @property
  def default(self):
    return self.__default

  @property
  def kind(self):
    return self.__kind

  @property
  def help(self):
    return self.__help

  @property
  def module(self):
    return self.__module

  def interpret(self, value):
    """
    Interprets the value for this Property.

    Args:
      value: The value to interpret. May be None, which
             means no value provided.

    Returns:
      The value to use for this property. Raises an error if
      this property has no valid interpretation.
    """
    if value is not PROPERTY_SENTINEL:
      if self.kind is not None:
        # The config system handles type checking for us here.
        self.kind.set_val(value)
      return value

    if self.default is not PROPERTY_SENTINEL:
      return self.default

    raise ValueError(
      "No default specified and no value provided for '{}' from {} '{}'".format(
        self.name, self.__property_type, self.module))

class Property(object):
  def __init__(self, default=PROPERTY_SENTINEL, help="", kind=None,
               param_name=None):
    """
    Constructor for Property.

    Args:
      default: The default value for this Property. Note: A default
               value of None is allowed. To have no default value, omit
               this argument.
      help: The help text for this Property.
      kind: The type of this Property. You can either pass in a raw python
            type, or a Config Type, using the recipe engine config system.
    """
    self._default = default
    self.help = help
    self.param_name = param_name

    if isinstance(kind, type):
      if kind in (str, unicode):
        kind = basestring
      kind = Single(kind)
    self.kind = kind

  def bind(self, name, property_type, module):
    """
    Gets the BoundProperty version of this Property. Requires a name.
    """
    return BoundProperty(
        self._default, self.help, self.kind, name, property_type, module,
        self.param_name)

class UndefinedPropertyException(TypeError):
  pass
