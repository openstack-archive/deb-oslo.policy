# -*- coding: utf-8 -*-
#
# Copyright (c) 2012 OpenStack Foundation.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Common Policy Engine Implementation

Policies are expressed as a target and an associated rule::

    "<target>": "<rule>"

The `target` is specific to the service that is conducting policy
enforcement.  Typically, the target refers to an API call.

A rule is made up of zero or more checks, where zero checks will always
allow the action that is being enforced.  A number of different check
types are supported, which can be divided into generic checks and
special checks.

Generic Checks
~~~~~~~~~~~~~~

A :class:`generic check <oslo_policy.policy.GenericCheck>` is used
to perform matching against attributes that are sent along with the API
calls.  These attributes can be used by the policy engine (on the right
side of the expression), by using the following syntax::

    <some_attribute>:%(user.id)s

The value on the right-hand side is either a string or resolves to a
string using regular Python string substitution.  The available attributes
and values are dependent on the program that is using the common policy
engine.

All of these attributes (related to users, API calls, and context) can be
checked against each other or against constants.  It is important to note
that these attributes are specific to the service that is conducting
policy enforcement.

Generic checks can be used to perform policy checks on the following user
attributes obtained through a token:

    - user_id
    - domain_id or project_id (depending on the token scope)
    - list of roles held for the given token scope

For example, a check on the user_id would be defined as::

    user_id:<some_value>

Together with the previously shown example, a complete generic check
would be::

    user_id:%(user.id)s

It is also possible to perform checks against other attributes that
represent the credentials.  This is done by adding additional values to
the ``creds`` dict that is passed to the
:meth:`~oslo_policy.policy.Enforcer.enforce` method.

Special Checks
~~~~~~~~~~~~~~

Special checks allow for more flexibility than is possible using generic
checks.  The built-in special check types are ``role``, ``rule``, and ``http``
checks.

Role Check
^^^^^^^^^^

A :class:`role check <oslo_policy.policy.RoleCheck>` is used to
check if a specific role is present in the supplied credentials.  A role
check is expressed as::

    "role:<role_name>"

Rule Check
^^^^^^^^^^

A :class:`rule check <oslo_policy.policy.RuleCheck>` is used to
reference another defined rule by its name.  This allows for common
checks to be defined once as a reusable rule, which is then referenced
within other rules.  It also allows one to define a set of checks as a
more descriptive name to aid in readabilty of policy.  A rule check is
expressed as::

    "rule:<rule_name>"

The following example shows a role check that is defined as a rule,
which is then used via a rule check::

    "admin_required": "role:admin"
    "<target>": "rule:admin_required"

HTTP Check
^^^^^^^^^^

An :class:`http check <oslo_policy.policy.HttpCheck>` is used to
make an HTTP request to a remote server to determine the results of the
check.  The target and credentials are passed to the remote server for
evaluation.  The action is authorized if the remote server returns a
response of ``True``. An http check is expressed as::

    "http:<target URI>"

It is expected that the target URI contains a string formatting keyword,
where the keyword is a key from the target dictionary.  An example of an
http check where the `name` key from the target is used to construct the
URL is would be defined as::

    "http://server.test/%(name)s"

Registering New Special Checks
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

It is also possible for additional special check types to be registered
using the :func:`~oslo_policy.policy.register` function.

Policy Rule Expressions
~~~~~~~~~~~~~~~~~~~~~~~

Policy rules can be expressed in one of two forms: A list of lists, or a
string written in the new policy language.

In the list-of-lists representation, each check inside the innermost
list is combined as with an "and" conjunction--for that check to pass,
all the specified checks must pass.  These innermost lists are then
combined as with an "or" conjunction. As an example, take the following
rule, expressed in the list-of-lists representation::

    [["role:admin"], ["project_id:%(project_id)s", "role:projectadmin"]]

This is the original way of expressing policies, but there now exists a
new way: the policy language.

In the policy language, each check is specified the same way as in the
list-of-lists representation: a simple "a:b" pair that is matched to
the correct class to perform that check:

 +--------------------------------+------------------------------------------+
 |            TYPE                |                SYNTAX                    |
 +================================+==========================================+
 |User's Role                     |              role:admin                  |
 +--------------------------------+------------------------------------------+
 |Rules already defined on policy |          rule:admin_required             |
 +--------------------------------+------------------------------------------+
 |Against URLs¹                   |         http://my-url.org/check          |
 +--------------------------------+------------------------------------------+
 |User attributes²                |    project_id:%(target.project.id)s      |
 +--------------------------------+------------------------------------------+
 |Strings                         |        - <variable>:'xpto2035abc'        |
 |                                |        - 'myproject':<variable>          |
 +--------------------------------+------------------------------------------+
 |                                |         - project_id:xpto2035abc         |
 |Literals                        |         - domain_id:20                   |
 |                                |         - True:%(user.enabled)s          |
 +--------------------------------+------------------------------------------+

¹URL checking must return ``True`` to be valid

²User attributes (obtained through the token): user_id, domain_id or project_id

Conjunction operators are available, allowing for more expressiveness
in crafting policies. So, in the policy language, the previous check in
list-of-lists becomes::

    role:admin or (project_id:%(project_id)s and role:projectadmin)

The policy language also has the ``not`` operator, allowing a richer
policy rule::

    project_id:%(project_id)s and not role:dunce

Finally, two special policy checks should be mentioned; the policy
check "@" will always accept an access, and the policy check "!" will
always reject an access.  (Note that if a rule is either the empty
list ("[]") or the empty string, this is equivalent to the "@" policy
check.)  Of these, the "!" policy check is probably the most useful,
as it allows particular rules to be explicitly disabled.

Default Rule
~~~~~~~~~~~~

A default rule can be defined, which will be enforced when a rule does
not exist for the target that is being checked.  By default, the rule
associated with the rule name of ``default`` will be used as the default
rule.  It is possible to use a different rule name as the default rule
by setting the ``policy_default_rule`` configuration setting to the
desired rule name.
"""

import abc
import ast
import copy
import os
import re

from oslo_config import cfg
from oslo_log import log as logging
from oslo_serialization import jsonutils
import six
import six.moves.urllib.parse as urlparse
import six.moves.urllib.request as urlrequest

from oslo_policy.openstack.common import fileutils
from oslo_policy.openstack.common._i18n import _, _LE, _LI


policy_opts = [
    cfg.StrOpt('policy_file',
               default='policy.json',
               help=_('The JSON file that defines policies.')),
    cfg.StrOpt('policy_default_rule',
               default='default',
               help=_('Default rule. Enforced when a requested rule is not '
                      'found.')),
    cfg.MultiStrOpt('policy_dirs',
                    default=['policy.d'],
                    help=_('Directories where policy configuration files are '
                           'stored. They can be relative to any directory '
                           'in the search path defined by the config_dir '
                           'option, or absolute paths. The file defined by '
                           'policy_file must exist for these directories to '
                           'be searched.')),
]


LOG = logging.getLogger(__name__)

_checks = {}


def list_opts():
    """Entry point for oslo-config-generator."""
    return [(None, copy.deepcopy(policy_opts))]


class PolicyNotAuthorized(Exception):
    """Default exception raised for policy enforcement failure."""

    def __init__(self, rule):
        msg = _("Policy doesn't allow %s to be performed.") % rule
        super(PolicyNotAuthorized, self).__init__(msg)


class Rules(dict):
    """A store for rules. Handles the default_rule setting directly."""

    @classmethod
    def load_json(cls, data, default_rule=None):
        """Allow loading of JSON rule data."""

        # Suck in the JSON data and parse the rules
        rules = dict((k, parse_rule(v)) for k, v in
                     jsonutils.loads(data).items())

        return cls(rules, default_rule)

    def __init__(self, rules=None, default_rule=None):
        """Initialize the Rules store."""

        super(Rules, self).__init__(rules or {})
        self.default_rule = default_rule

    def __missing__(self, key):
        """Implements the default rule handling."""

        if isinstance(self.default_rule, dict):
            raise KeyError(key)

        # If the default rule isn't actually defined, do something
        # reasonably intelligent
        if not self.default_rule:
            raise KeyError(key)

        if isinstance(self.default_rule, BaseCheck):
            return self.default_rule

        # We need to check this or we can get infinite recursion
        if self.default_rule not in self:
            raise KeyError(key)

        elif isinstance(self.default_rule, six.string_types):
            return self[self.default_rule]

    def __str__(self):
        """Dumps a string representation of the rules."""

        # Start by building the canonical strings for the rules
        out_rules = {}
        for key, value in self.items():
            # Use empty string for singleton TrueCheck instances
            if isinstance(value, TrueCheck):
                out_rules[key] = ''
            else:
                out_rules[key] = str(value)

        # Dump a pretty-printed JSON representation
        return jsonutils.dumps(out_rules, indent=4)


class Enforcer(object):
    """Responsible for loading and enforcing rules.

    :param conf: A configuration object.
    :param policy_file: Custom policy file to use, if none is
                        specified, ``conf.policy_file`` will be
                        used.
    :param rules: Default dictionary / Rules to use. It will be
                  considered just in the first instantiation. If
                  :meth:`load_rules` with ``force_reload=True``,
                  :meth:`clear` or :meth:`set_rules` with ``overwrite=True``
                  is called this will be overwritten.
    :param default_rule: Default rule to use, conf.default_rule will
                         be used if none is specified.
    :param use_conf: Whether to load rules from cache or config file.
    :param overwrite: Whether to overwrite existing rules when reload rules
                      from config file.
    """

    def __init__(self, conf, policy_file=None, rules=None,
                 default_rule=None, use_conf=True, overwrite=True):
        self.conf = conf
        self.conf.register_opts(policy_opts)

        self.default_rule = default_rule or self.conf.policy_default_rule
        self.rules = Rules(rules, self.default_rule)

        self.policy_path = None

        self.policy_file = policy_file or self.conf.policy_file
        self.use_conf = use_conf
        self.overwrite = overwrite

    def set_rules(self, rules, overwrite=True, use_conf=False):
        """Create a new :class:`Rules` based on the provided dict of rules.

        :param dict rules: New rules to use.
        :param overwrite: Whether to overwrite current rules or update them
                          with the new rules.
        :param use_conf: Whether to reload rules from cache or config file.
        """

        if not isinstance(rules, dict):
            raise TypeError(_("Rules must be an instance of dict or Rules, "
                            "got %s instead") % type(rules))
        self.use_conf = use_conf
        if overwrite:
            self.rules = Rules(rules, self.default_rule)
        else:
            self.rules.update(rules)

    def clear(self):
        """Clears :class:`Enforcer` rules, policy's cache and policy's path."""
        self.set_rules({})
        fileutils.delete_cached_file(self.policy_path)
        self.default_rule = None
        self.policy_path = None

    def load_rules(self, force_reload=False):
        """Loads policy_path's rules.

        Policy file is cached and will be reloaded if modified.

        :param force_reload: Whether to reload rules from config file.
        """

        if force_reload:
            self.use_conf = force_reload

        if self.use_conf:
            if not self.policy_path:
                self.policy_path = self._get_policy_path(self.policy_file)

            self._load_policy_file(self.policy_path, force_reload,
                                   overwrite=self.overwrite)
            for path in self.conf.policy_dirs:
                try:
                    path = self._get_policy_path(path)
                except cfg.ConfigFilesNotFoundError:
                    LOG.info(_LI("Can not find policy directory: %s"), path)
                    continue
                self._walk_through_policy_directory(path,
                                                    self._load_policy_file,
                                                    force_reload, False)

    @staticmethod
    def _walk_through_policy_directory(path, func, *args):
        # We do not iterate over sub-directories.
        policy_files = next(os.walk(path))[2]
        policy_files.sort()
        for policy_file in [p for p in policy_files if not p.startswith('.')]:
            func(os.path.join(path, policy_file), *args)

    def _load_policy_file(self, path, force_reload, overwrite=True):
            reloaded, data = fileutils.read_cached_file(
                path, force_reload=force_reload)
            if reloaded or not self.rules or not overwrite:
                rules = Rules.load_json(data, self.default_rule)
                self.set_rules(rules, overwrite=overwrite, use_conf=True)
                LOG.debug("Rules successfully reloaded")

    def _get_policy_path(self, path):
        """Locate the policy json data file/path.

        :param path: It's value can be a full path or related path. When
                     full path specified, this function just returns the full
                     path. When related path specified, this function will
                     search configuration directories to find one that exists.

        :returns: The policy path

        :raises: ConfigFilesNotFoundError if the file/path couldn't
                 be located.
        """
        policy_path = self.conf.find_file(path)

        if policy_path:
            return policy_path

        raise cfg.ConfigFilesNotFoundError((path,))

    def enforce(self, rule, target, creds, do_raise=False,
                exc=None, *args, **kwargs):
        """Checks authorization of a rule against the target and credentials.

        :param rule: The rule to evaluate.
        :type rule: string or :class:`BaseCheck`
        :param dict target: As much information about the object being operated
                            on as possible.
        :param dict creds: As much information about the user performing the
                           action as possible.
        :param do_raise: Whether to raise an exception or not if check
                        fails.
        :param exc: Class of the exception to raise if the check fails.
                    Any remaining arguments passed to :meth:`enforce` (both
                    positional and keyword arguments) will be passed to
                    the exception class. If not specified,
                    :class:`PolicyNotAuthorized` will be used.

        :return: ``False`` if the policy does not allow the action and `exc` is
                 not provided; otherwise, returns a value that evaluates to
                 ``True``.  Note: for rules using the "case" expression, this
                 ``True`` value will be the specified string from the
                 expression.
        """

        self.load_rules()

        # Allow the rule to be a Check tree
        if isinstance(rule, BaseCheck):
            result = rule(target, creds, self)
        elif not self.rules:
            # No rules to reference means we're going to fail closed
            result = False
        else:
            try:
                # Evaluate the rule
                result = self.rules[rule](target, creds, self)
            except KeyError:
                LOG.debug("Rule [%s] doesn't exist" % rule)
                # If the rule doesn't exist, fail closed
                result = False

        # If it is False, raise the exception if requested
        if do_raise and not result:
            if exc:
                raise exc(*args, **kwargs)

            raise PolicyNotAuthorized(rule)

        return result


@six.add_metaclass(abc.ABCMeta)
class BaseCheck(object):
    """Abstract base class for Check classes."""

    @abc.abstractmethod
    def __str__(self):
        """String representation of the Check tree rooted at this node."""

        pass

    @abc.abstractmethod
    def __call__(self, target, cred, enforcer):
        """Triggers if instance of the class is called.

        Performs the check. Returns False to reject the access or a
        true value (not necessary True) to accept the access.
        """

        pass


class FalseCheck(BaseCheck):
    """A policy check that always returns ``False`` (disallow)."""

    def __str__(self):
        """Return a string representation of this check."""

        return "!"

    def __call__(self, target, cred, enforcer):
        """Check the policy."""

        return False


class TrueCheck(BaseCheck):
    """A policy check that always returns ``True`` (allow)."""

    def __str__(self):
        """Return a string representation of this check."""

        return "@"

    def __call__(self, target, cred, enforcer):
        """Check the policy."""

        return True


class Check(BaseCheck):
    """A base class to allow for user-defined policy checks.

    :param kind: The kind of the check, i.e., the field before the ``:``.
    :param match: The match of the check, i.e., the field after the ``:``.

    """

    def __init__(self, kind, match):
        self.kind = kind
        self.match = match

    def __str__(self):
        """Return a string representation of this check."""

        return "%s:%s" % (self.kind, self.match)


class NotCheck(BaseCheck):
    """Implements the "not" logical operator.

    A policy check that inverts the result of another policy check.

    :param rule: The rule to negate.  Must be a Check.

    """

    def __init__(self, rule):
        self.rule = rule

    def __str__(self):
        """Return a string representation of this check."""

        return "not %s" % self.rule

    def __call__(self, target, cred, enforcer):
        """Check the policy.

        Returns the logical inverse of the wrapped check.
        """

        return not self.rule(target, cred, enforcer)


class AndCheck(BaseCheck):
    """Implements the "and" logical operator.

    A policy check that requires that a list of other checks all return True.

    :param list rules: rules that will be tested.

    """

    def __init__(self, rules):
        self.rules = rules

    def __str__(self):
        """Return a string representation of this check."""

        return "(%s)" % ' and '.join(str(r) for r in self.rules)

    def __call__(self, target, cred, enforcer):
        """Check the policy.

        Requires that all rules accept in order to return True.
        """

        for rule in self.rules:
            if not rule(target, cred, enforcer):
                return False

        return True

    def add_check(self, rule):
        """Adds rule to be tested.

        Allows addition of another rule to the list of rules that will
        be tested.

        :returns: self
        :rtype: :class:`.AndCheck`
        """

        self.rules.append(rule)
        return self


class OrCheck(BaseCheck):
    """Implements the "or" operator.

    A policy check that requires that at least one of a list of other
    checks returns ``True``.

    :param rules: A list of rules that will be tested.

    """

    def __init__(self, rules):
        self.rules = rules

    def __str__(self):
        """Return a string representation of this check."""

        return "(%s)" % ' or '.join(str(r) for r in self.rules)

    def __call__(self, target, cred, enforcer):
        """Check the policy.

        Requires that at least one rule accept in order to return True.
        """

        for rule in self.rules:
            if rule(target, cred, enforcer):
                return True
        return False

    def add_check(self, rule):
        """Adds rule to be tested.

        Allows addition of another rule to the list of rules that will
        be tested.  Returns the OrCheck object for convenience.
        """

        self.rules.append(rule)
        return self


def _parse_check(rule):
    """Parse a single base check rule into an appropriate Check object."""

    # Handle the special checks
    if rule == '!':
        return FalseCheck()
    elif rule == '@':
        return TrueCheck()

    try:
        kind, match = rule.split(':', 1)
    except Exception:
        LOG.exception(_LE("Failed to understand rule %s") % rule)
        # If the rule is invalid, we'll fail closed
        return FalseCheck()

    # Find what implements the check
    if kind in _checks:
        return _checks[kind](kind, match)
    elif None in _checks:
        return _checks[None](kind, match)
    else:
        LOG.error(_LE("No handler for matches of kind %s") % kind)
        return FalseCheck()


def _parse_list_rule(rule):
    """Translates the old list-of-lists syntax into a tree of Check objects.

    Provided for backwards compatibility.
    """

    # Empty rule defaults to True
    if not rule:
        return TrueCheck()

    # Outer list is joined by "or"; inner list by "and"
    or_list = []
    for inner_rule in rule:
        # Elide empty inner lists
        if not inner_rule:
            continue

        # Handle bare strings
        if isinstance(inner_rule, six.string_types):
            inner_rule = [inner_rule]

        # Parse the inner rules into Check objects
        and_list = [_parse_check(r) for r in inner_rule]

        # Append the appropriate check to the or_list
        if len(and_list) == 1:
            or_list.append(and_list[0])
        else:
            or_list.append(AndCheck(and_list))

    # If we have only one check, omit the "or"
    if not or_list:
        return FalseCheck()
    elif len(or_list) == 1:
        return or_list[0]

    return OrCheck(or_list)


# Used for tokenizing the policy language
_tokenize_re = re.compile(r'\s+')


def _parse_tokenize(rule):
    """Tokenizer for the policy language.

    Most of the single-character tokens are specified in the
    _tokenize_re; however, parentheses need to be handled specially,
    because they can appear inside a check string.  Thankfully, those
    parentheses that appear inside a check string can never occur at
    the very beginning or end ("%(variable)s" is the correct syntax).
    """

    for tok in _tokenize_re.split(rule):
        # Skip empty tokens
        if not tok or tok.isspace():
            continue

        # Handle leading parens on the token
        clean = tok.lstrip('(')
        for i in range(len(tok) - len(clean)):
            yield '(', '('

        # If it was only parentheses, continue
        if not clean:
            continue
        else:
            tok = clean

        # Handle trailing parens on the token
        clean = tok.rstrip(')')
        trail = len(tok) - len(clean)

        # Yield the cleaned token
        lowered = clean.lower()
        if lowered in ('and', 'or', 'not'):
            # Special tokens
            yield lowered, clean
        elif clean:
            # Not a special token, but not composed solely of ')'
            if len(tok) >= 2 and ((tok[0], tok[-1]) in
                                  [('"', '"'), ("'", "'")]):
                # It's a quoted string
                yield 'string', tok[1:-1]
            else:
                yield 'check', _parse_check(clean)

        # Yield the trailing parens
        for i in range(trail):
            yield ')', ')'


class ParseStateMeta(type):
    """Metaclass for the :class:`.ParseState` class.

    Facilitates identifying reduction methods.
    """

    def __new__(mcs, name, bases, cls_dict):
        """Create the class.

        Injects the 'reducers' list, a list of tuples matching token sequences
        to the names of the corresponding reduction methods.
        """

        reducers = []

        for key, value in cls_dict.items():
            if not hasattr(value, 'reducers'):
                continue
            for reduction in value.reducers:
                reducers.append((reduction, key))

        cls_dict['reducers'] = reducers

        return super(ParseStateMeta, mcs).__new__(mcs, name, bases, cls_dict)


def reducer(*tokens):
    """Decorator for reduction methods.

    Arguments are a sequence of tokens, in order, which should trigger running
    this reduction method.
    """

    def decorator(func):
        # Make sure we have a list of reducer sequences
        if not hasattr(func, 'reducers'):
            func.reducers = []

        # Add the tokens to the list of reducer sequences
        func.reducers.append(list(tokens))

        return func

    return decorator


@six.add_metaclass(ParseStateMeta)
class ParseState(object):
    """Implement the core of parsing the policy language.

    Uses a greedy reduction algorithm to reduce a sequence of tokens into
    a single terminal, the value of which will be the root of the
    :class:`Check` tree.

    .. note::

        Error reporting is rather lacking.  The best we can get with this
        parser formulation is an overall "parse failed" error. Fortunately, the
        policy language is simple enough that this shouldn't be that big a
        problem.
    """

    def __init__(self):
        """Initialize the ParseState."""

        self.tokens = []
        self.values = []

    def reduce(self):
        """Perform a greedy reduction of the token stream.

        If a reducer method matches, it will be executed, then the
        :meth:`reduce` method will be called recursively to search for any more
        possible reductions.
        """

        for reduction, methname in self.reducers:
            if (len(self.tokens) >= len(reduction) and
                    self.tokens[-len(reduction):] == reduction):
                # Get the reduction method
                meth = getattr(self, methname)

                # Reduce the token stream
                results = meth(*self.values[-len(reduction):])

                # Update the tokens and values
                self.tokens[-len(reduction):] = [r[0] for r in results]
                self.values[-len(reduction):] = [r[1] for r in results]

                # Check for any more reductions
                return self.reduce()

    def shift(self, tok, value):
        """Adds one more token to the state.

        Calls :meth:`reduce`.
        """

        self.tokens.append(tok)
        self.values.append(value)

        # Do a greedy reduce...
        self.reduce()

    @property
    def result(self):
        """Obtain the final result of the parse.

        :raises ValueError: If the parse failed to reduce to a single result.
        """

        if len(self.values) != 1:
            raise ValueError("Could not parse rule")
        return self.values[0]

    @reducer('(', 'check', ')')
    @reducer('(', 'and_expr', ')')
    @reducer('(', 'or_expr', ')')
    def _wrap_check(self, _p1, check, _p2):
        """Turn parenthesized expressions into a 'check' token."""

        return [('check', check)]

    @reducer('check', 'and', 'check')
    def _make_and_expr(self, check1, _and, check2):
        """Create an 'and_expr'.

        Join two checks by the 'and' operator.
        """

        return [('and_expr', AndCheck([check1, check2]))]

    @reducer('and_expr', 'and', 'check')
    def _extend_and_expr(self, and_expr, _and, check):
        """Extend an 'and_expr' by adding one more check."""

        return [('and_expr', and_expr.add_check(check))]

    @reducer('check', 'or', 'check')
    def _make_or_expr(self, check1, _or, check2):
        """Create an 'or_expr'.

        Join two checks by the 'or' operator.
        """

        return [('or_expr', OrCheck([check1, check2]))]

    @reducer('or_expr', 'or', 'check')
    def _extend_or_expr(self, or_expr, _or, check):
        """Extend an 'or_expr' by adding one more check."""

        return [('or_expr', or_expr.add_check(check))]

    @reducer('not', 'check')
    def _make_not_expr(self, _not, check):
        """Invert the result of another check."""

        return [('check', NotCheck(check))]


def _parse_text_rule(rule):
    """Parses policy to the tree.

    Translates a policy written in the policy language into a tree of
    Check objects.
    """

    # Empty rule means always accept
    if not rule:
        return TrueCheck()

    # Parse the token stream
    state = ParseState()
    for tok, value in _parse_tokenize(rule):
        state.shift(tok, value)

    try:
        return state.result
    except ValueError:
        # Couldn't parse the rule
        LOG.exception(_LE("Failed to understand rule %s") % rule)

        # Fail closed
        return FalseCheck()


def parse_rule(rule):
    """Parses a policy rule into a tree of :class:`.Check` objects."""

    # If the rule is a string, it's in the policy language
    if isinstance(rule, six.string_types):
        return _parse_text_rule(rule)
    return _parse_list_rule(rule)


def register(name, func=None):
    """Register a function or :class:`.Check` class as a policy check.

    :param name: Gives the name of the check type, e.g., "rule",
                 "role", etc.  If name is ``None``, a default check type
                 will be registered.
    :param func: If given, provides the function or class to register.
                 If not given, returns a function taking one argument
                 to specify the function or class to register,
                 allowing use as a decorator.
    """

    # Perform the actual decoration by registering the function or
    # class.  Returns the function or class for compliance with the
    # decorator interface.
    def decorator(func):
        _checks[name] = func
        return func

    # If the function or class is given, do the registration
    if func:
        return decorator(func)

    return decorator


@register("rule")
class RuleCheck(Check):
    """Recursively checks credentials based on the defined rules."""

    def __call__(self, target, creds, enforcer):
        try:
            return enforcer.rules[self.match](target, creds, enforcer)
        except KeyError:
            # We don't have any matching rule; fail closed
            return False


@register("role")
class RoleCheck(Check):
    """Check that there is a matching role in the ``creds`` dict."""

    def __call__(self, target, creds, enforcer):
        return self.match.lower() in [x.lower() for x in creds['roles']]


@register('http')
class HttpCheck(Check):
    """Check ``http:`` rules by calling to a remote server.

    This example implementation simply verifies that the response
    is exactly ``True``.
    """

    def __call__(self, target, creds, enforcer):
        url = ('http:' + self.match) % target

        # Convert instances of object() in target temporarily to
        # empty dict to avoid circular reference detection
        # errors in jsonutils.dumps().
        temp_target = copy.deepcopy(target)
        for key in target.keys():
            element = target.get(key)
            if type(element) is object:
                temp_target[key] = {}

        data = {'target': jsonutils.dumps(temp_target),
                'credentials': jsonutils.dumps(creds)}
        post_data = urlparse.urlencode(data)
        f = urlrequest.urlopen(url, post_data)
        return f.read() == "True"


@register(None)
class GenericCheck(Check):
    """Check an individual match.

    Matches look like:

        - tenant:%(tenant_id)s
        - role:compute:admin
        - True:%(user.enabled)s
        - 'Member':%(role.name)s
    """

    def __call__(self, target, creds, enforcer):
        try:
            match = self.match % target
        except KeyError:
            # While doing GenericCheck if key not
            # present in Target return false
            return False

        try:
            # Try to interpret self.kind as a literal
            leftval = ast.literal_eval(self.kind)
        except ValueError:
            try:
                kind_parts = self.kind.split('.')
                leftval = creds
                for kind_part in kind_parts:
                    leftval = leftval[kind_part]
            except KeyError:
                return False
        return match == six.text_type(leftval)
