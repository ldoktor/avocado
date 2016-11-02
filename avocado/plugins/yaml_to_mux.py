# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
#
# See LICENSE for more details.
#
# Copyright: Red Hat Inc. 2016
# Author: Lukas Doktor <ldoktor@redhat.com>
"""Multiplexer plugin to parse yaml files to params"""

import collections
import itertools
import logging
import os
import re
import sha
import sys

from avocado.core import tree, exit_codes
from avocado.core.plugin_interfaces import CLI


try:
    import yaml
except ImportError:
    MULTIPLEX_CAPABLE = False
else:
    MULTIPLEX_CAPABLE = True
    try:
        from yaml import CLoader as Loader
    except ImportError:
        from yaml import Loader


# Mapping for yaml flags
YAML_INCLUDE = 100
YAML_USING = 101
YAML_REMOVE_NODE = tree.REMOVE_NODE
YAML_REMOVE_VALUE = tree.REMOVE_VALUE
YAML_MUX = 102

__RE_FILE_SPLIT = re.compile(r'(?<!\\):')   # split by ':' but not '\\:'
__RE_FILE_SUBS = re.compile(r'(?<!\\)\\:')  # substitute '\\:' but not '\\\\:'


class Value(tuple):     # Few methods pylint: disable=R0903

    """ Used to mark values to simplify checking for node vs. value """
    pass


class ListOfNodeObjects(list):     # Few methods pylint: disable=R0903

    """
    Used to mark list as list of objects from whose node is going to be created
    """
    pass


class MuxTreeNode(tree.TreeNode):

    """
    Class for bounding nodes into tree-structure with support for
    multiplexation
    """

    def __init__(self, name='', value=None, parent=None, children=None):
        super(MuxTreeNode, self).__init__(name, value, parent, children)
        self.multiplex = None

    def __repr__(self):
        return 'TreeNode(name=%r)' % self.name

    def merge(self, other):
        """
        Merges `other` node into this one without checking the name of the
        other node. New values are appended, existing values overwritten
        and unaffected ones are kept. Then all other node children are
        added as children (recursively they get either appended at the end
        or merged into existing node in the previous position.
        """
        super(MuxTreeNode, self).merge(other)
        if other.multiplex is True:
            self.multiplex = True
        elif other.multiplex is False:
            self.multiplex = False


class MuxTreeNodeDebug(MuxTreeNode, tree.TreeNodeDebug):

    """
    Debug version of TreeNodeDebug
    :warning: Origin of the value is appended to all values thus it's not
    suitable for running tests.
    """

    def __init__(self, name='', value=None, parent=None, children=None,
                 srcyaml=None):
        MuxTreeNode.__init__(self, name, value, parent, children)
        tree.TreeNodeDebug.__init__(self, name, value, parent, children,
                                    srcyaml)

    def merge(self, other):
        MuxTreeNode.merge(self, other)
        tree.TreeNodeDebug.merge(self, other)


def _create_from_yaml(path, cls_node=MuxTreeNode):
    """ Create tree structure from yaml stream """
    def tree_node_from_values(name, values):
        """ Create `name` node and add values  """
        node = cls_node(str(name))
        using = ''
        for value in values:
            if isinstance(value, tree.TreeNode):
                node.add_child(value)
            elif isinstance(value[0], tree.Control):
                if value[0].code == YAML_INCLUDE:
                    # Include file
                    ypath = value[1]
                    if not os.path.isabs(ypath):
                        ypath = os.path.join(os.path.dirname(path), ypath)
                    if not os.path.exists(ypath):
                        raise ValueError("File '%s' included from '%s' does not "
                                         "exist." % (ypath, path))
                    node.merge(_create_from_yaml('/:' + ypath, cls_node))
                elif value[0].code == YAML_USING:
                    if using:
                        raise ValueError("!using can be used only once per "
                                         "node! (%s:%s)" % (path, name))
                    using = value[1]
                    if using[0] == '/':
                        using = using[1:]
                    if using[-1] == '/':
                        using = using[:-1]
                elif value[0].code == YAML_REMOVE_NODE:
                    value[0].value = value[1]   # set the name
                    node.ctrl.append(value[0])    # add "blue pill" of death
                elif value[0].code == YAML_REMOVE_VALUE:
                    value[0].value = value[1]   # set the name
                    node.ctrl.append(value[0])
                elif value[0].code == YAML_MUX:
                    node.multiplex = True
            else:
                node.value[value[0]] = value[1]
        if using:
            if name is not '':
                for name in using.split('/')[::-1]:
                    node = cls_node(name, children=[node])
            else:
                using = using.split('/')[::-1]
                node.name = using.pop()
                while True:
                    if not using:
                        break
                    name = using.pop()  # 'using' is list pylint: disable=E1101
                    node = cls_node(name, children=[node])
                node = cls_node('', children=[node])
        return node

    def mapping_to_tree_loader(loader, node):
        """ Maps yaml mapping tag to TreeNode structure """
        _value = []
        for key_node, value_node in node.value:
            if key_node.tag.startswith('!'):    # reflect tags everywhere
                key = loader.construct_object(key_node)
            else:
                key = loader.construct_python_str(key_node)
            value = loader.construct_object(value_node)
            _value.append((key, value))
        objects = ListOfNodeObjects()
        for name, values in _value:
            if isinstance(values, ListOfNodeObjects):   # New node from list
                objects.append(tree_node_from_values(name, values))
            elif values is None:            # Empty node
                objects.append(cls_node(str(name)))
            else:                           # Values
                objects.append(Value((name, values)))
        return objects

    def mux_loader(loader, obj):
        """
        Special !mux loader which allows to tag node as 'multiplex = True'.
        """
        if not isinstance(obj, yaml.ScalarNode):
            objects = mapping_to_tree_loader(loader, obj)
        else:   # This means it's empty node. Don't call mapping_to_tree_loader
            objects = ListOfNodeObjects()
        objects.append((tree.Control(YAML_MUX), None))
        return objects

    Loader.add_constructor(u'!include',
                           lambda loader, node: tree.Control(YAML_INCLUDE))
    Loader.add_constructor(u'!using',
                           lambda loader, node: tree.Control(YAML_USING))
    Loader.add_constructor(u'!remove_node',
                           lambda loader, node: tree.Control(YAML_REMOVE_NODE))
    Loader.add_constructor(u'!remove_value',
                           lambda loader, node: tree.Control(YAML_REMOVE_VALUE))
    Loader.add_constructor(u'!mux', mux_loader)
    Loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
                           mapping_to_tree_loader)

    # Parse file name ([$using:]$path)
    path = __RE_FILE_SPLIT.split(path, 1)
    if len(path) == 1:
        path = __RE_FILE_SUBS.sub(':', path[0])
        using = ["run"]
    else:
        nodes = __RE_FILE_SUBS.sub(':', path[0]).strip('/').split('/')
        using = [node for node in nodes if node]
        if not path[0].startswith('/'):  # relative path, put into /run
            using.insert(0, 'run')
        path = __RE_FILE_SUBS.sub(':', path[1])

    # Load the tree
    with open(path) as stream:
        loaded_tree = yaml.load(stream, Loader)
        if loaded_tree is None:
            return
        loaded_tree = tree_node_from_values('', loaded_tree)

    # Add prefix
    if using:
        loaded_tree.name = using.pop()
        while True:
            if not using:
                break
            loaded_tree = cls_node(using.pop(), children=[loaded_tree])
        loaded_tree = cls_node('', children=[loaded_tree])
    return loaded_tree


def create_from_yaml(paths, debug=False):
    """
    Create tree structure from yaml-like file
    :param fileobj: File object to be processed
    :raise SyntaxError: When yaml-file is corrupted
    :return: Root of the created tree structure
    """
    def _merge(data, path):
        """ Normal run """
        tmp = _create_from_yaml(path)
        if tmp:
            data.merge(tmp)

    def _merge_debug(data, path):
        """ Use NamedTreeNodeDebug magic """
        node_cls = tree.get_named_tree_cls(path, MuxTreeNodeDebug)
        tmp = _create_from_yaml(path, node_cls)
        if tmp:
            data.merge(tmp)

    if not debug:
        data = MuxTreeNode()
        merge = _merge
    else:
        data = MuxTreeNodeDebug()
        merge = _merge_debug

    path = None
    try:
        for path in paths:
            merge(data, path)
    # Yaml can raise IndexError on some files
    except (yaml.YAMLError, IndexError) as details:
        if 'mapping values are not allowed in this context' in str(details):
            details = ("%s\nMake sure !tags and colons are separated by a "
                       "space (eg. !include :)" % details)
        msg = "Invalid multiplex file '%s': %s" % (path, details)
        raise IOError(2, msg, path)
    return data


def path_parent(path):
    """
    From a given path, return its parent path.

    :param path: the node path as string.
    :return: the parent path as string.
    """
    parent = path.rpartition('/')[0]
    if not parent:
        return '/'
    return parent


def apply_filters(root, filter_only=None, filter_out=None):
    """
    Apply a set of filters to the tree.

    The basic filtering is filter only, which includes nodes,
    and the filter out rules, that exclude nodes.

    Note that filter_out is stronger than filter_only, so if you filter out
    something, you could not bypass some nodes by using a filter_only rule.

    :param filter_only: the list of paths which will include nodes.
    :param filter_out: the list of paths which will exclude nodes.
    :return: the original tree minus the nodes filtered by the rules.
    """
    if filter_only is None:
        filter_only = []
    else:
        filter_only = [_.rstrip('/') for _ in filter_only if _]
    if filter_out is None:
        filter_out = []
    else:
        filter_out = [_.rstrip('/') for _ in filter_out if _]
    for node in root.iter_children_preorder():
        keep_node = True
        for path in filter_only:
            if path == '':
                continue
            if node.path == path:
                keep_node = True
                break
            if node.parent and node.parent.path == path_parent(path):
                keep_node = False
                continue
        for path in filter_out:
            if path == '':
                continue
            if node.path == path:
                keep_node = False
                break
        if not keep_node:
            node.detach()
    return root


class MuxTree(object):

    """
    Object representing part of the tree from the root to leaves or another
    multiplex domain. Recursively it creates multiplexed variants of the full
    tree.
    """

    def __init__(self, root):
        """
        :param root: Root of this tree slice
        """
        self.pools = []
        for node in self._iter_mux_leaves(root):
            if node.is_leaf:
                self.pools.append(node)
            else:
                self.pools.append([MuxTree(child) for child in node.children])

    @staticmethod
    def _iter_mux_leaves(node):
        """ yield leaves or muxes of the tree """
        queue = collections.deque()
        while node is not None:
            if node.is_leaf or getattr(node, "multiplex", False):
                yield node
            else:
                queue.extendleft(reversed(node.children))
            try:
                node = queue.popleft()
            except IndexError:
                raise StopIteration

    def __iter__(self):
        """
        Iterates through variants
        """
        pools = []
        for pool in self.pools:
            if isinstance(pool, list):
                pools.append(itertools.chain(*pool))
            else:
                pools.append(pool)
        pools = itertools.product(*pools)
        while True:
            # TODO: Implement 2nd level filters here
            # TODO: This part takes most of the time, optimize it
            yield list(itertools.chain(*pools.next()))


class MuxPlugin(object):
    """
    Follows the Multiplexer API to produce variants
    """
    def __init__(self, root, mux_path):
        self.root = root
        self.variants = None
        self.default_params = None
        self.mux_path = mux_path
        self.variant_ids = self._get_variant_ids()

    def _get_variant_ids(self):
        variant_ids = []
        for variant in MuxTree(self.root):
            variant.sort(key=lambda x: x.path)
            fingerprint = "-".join(_.fingerprint() for _ in variant)
            variant_ids.append("-".join(node.name for node in variant) + '-' +
                               sha.sha(fingerprint).hexdigest()[:4])
        return variant_ids

    def __iter__(self):
        for vid, variant in itertools.izip(self.variant_ids, self.variants):
            '''
            data = copy.deepcopy(self.default_params)
            for leaf in variant:
                data.get_node(leaf.path, True).merge(leaf)
                data.set_environment_dirty()
            yield i, (data.get_leaves(), self.mux_path)
            '''
            yield {"variant_id": vid,
                   "variant": variant,
                   "mux_path": self.mux_path}

    def __len__(self):
        """
        Report the number of variants
        """
        return len(self.variant_ids)

    def update_defaults(self, defaults):
        if self.default_params:
            self.default_params.merge(defaults)
        self.default_params = defaults
        combination = defaults
        combination.merge(self.root)
        self.variants = MuxTree(combination)

    def str_variants(self):
        if not self.variants:
            return
        out = ""
        tree_repr = tree.tree_view(self.root, verbose=True,
                                   use_utf8=False)
        if not tree_repr:
            return ""
        out += "Multiplex tree representation:\n"
        out += tree_repr
        out += "\n\n"

        for variant in self:
            paths = ', '.join([x.path for x in variant["variant"]])
            out += 'Variant %s:    %s\n' % (variant["variant_id"], paths)

        if not out.endswith("\n"):
            out += "\n"
        return out


class YamlToMux(CLI):

    """
    Registers callback to inject params from yaml file to the
    """

    name = 'yaml_to_mux'
    description = "YamlToMux options for the 'run' subcommand"

    def configure(self, parser):
        """
        Configures "run" and "multiplex" subparsers
        """
        if not MULTIPLEX_CAPABLE:
            return
        for name in ("run", "multiplex"):
            subparser = parser.subcommands.choices.get(name, None)
            if subparser is None:
                continue
            mux = subparser.add_argument_group("yaml to mux options")
            mux.add_argument("-m", "--mux-yaml", nargs='*', metavar="FILE",
                             help="Location of one or more Avocado"
                             " multiplex (.yaml) FILE(s) (order dependent)")
            mux.add_argument('--mux-filter-only', nargs='*', default=[],
                             help='Filter only path(s) from multiplexing')
            mux.add_argument('--mux-filter-out', nargs='*', default=[],
                             help='Filter out path(s) from multiplexing')
            mux.add_argument('--mux-path', nargs='*', default=None,
                             help="List of paths used to determine path "
                             "priority when querying for parameters")
            mux.add_argument('--mux-inject', default=[], nargs='*',
                             help="Inject [path:]key:node values into the "
                             "final multiplex tree.")
            mux = subparser.add_argument_group("yaml to mux options "
                                               "[deprecated]")
            mux.add_argument("--multiplex", nargs='*',
                             default=None, metavar="FILE",
                             help="DEPRECATED: Location of one or more Avocado"
                             " multiplex (.yaml) FILE(s) (order dependent)")
            mux.add_argument("--filter-only", nargs='*', default=[],
                             help="DEPRECATED: Filter only path(s) from "
                             "multiplexing (use --mux-only instead)")
            mux.add_argument("--filter-out", nargs='*', default=[],
                             help="DEPRECATED: Filter out path(s) from "
                             "multiplexing (use --mux-out instead)")

    @staticmethod
    def _log_deprecation_msg(deprecated, current):
        """
        Log a message into the "avocado.app" warning log
        """
        msg = "The use of '%s' is deprecated, please use '%s' instead"
        logging.getLogger("avocado.app").warning(msg, deprecated, current)

    def run(self, args):
        # Deprecated filters
        only = getattr(args, "filter_only", None)
        if only:
            self._log_deprecation_msg("--filter-only", "--mux-only")
            mux_filter_only = getattr(args, "mux_filter_only")
            if mux_filter_only:
                args.mux_filter_only = mux_filter_only + only
            else:
                args.mux_filter_only = only
        out = getattr(args, "filter_out", None)
        if out:
            self._log_deprecation_msg("--filter-out", "--mux-out")
            mux_filter_out = getattr(args, "mux_filter_out")
            if mux_filter_out:
                args.mux_filter_only = mux_filter_out + out
            else:
                args.mux_filter_out = out
        data = MuxTreeNodeDebug() if args.mux.debug else MuxTreeNode()

        # Merge the multiplex
        multiplex_files = getattr(args, "mux_yaml", None)
        if multiplex_files:
            debug = getattr(args, "mux_debug", False)
            try:
                data.merge(create_from_yaml(multiplex_files, debug))
            except IOError as details:
                logging.getLogger("avocado.app").error(details.strerror)
                sys.exit(exit_codes.AVOCADO_JOB_FAIL)

        # Deprecated --multiplex option
        multiplex_files = getattr(args, "multiplex", None)
        if multiplex_files:
            self._log_deprecation_msg("--multiplex", "--mux-yaml")
            debug = getattr(args, "mux_debug", False)
            try:
                data.merge(create_from_yaml(multiplex_files, debug))
            except IOError as details:
                logging.getLogger("avocado.app").error(details.strerror)
                sys.exit(exit_codes.AVOCADO_JOB_FAIL)

        # Extend default multiplex tree of --mux-inject values
        for inject in getattr(args, "mux_inject", []):
            entry = inject.split(':', 3)
            if len(entry) < 2:
                raise ValueError("key:entry pairs required, found only %s"
                                 % (entry))
            elif len(entry) == 2:   # key, entry
                entry.insert(0, '')  # add path='' (root)
            data.get_node(entry[0], True).value[entry[1]] = entry[2]

        mux_path = getattr(args, 'mux_path', None)
        if mux_path is None:
            mux_path = ['/run/*']

        data = apply_filters(data, getattr(args, 'mux_filter_only', None),
                             getattr(args, 'mux_filter_out', None))
        if data != MuxTreeNode():
            args.mux.add_variants_plugin(MuxPlugin(data, mux_path))
