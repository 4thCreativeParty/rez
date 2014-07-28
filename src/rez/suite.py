from rez.util import propertycache, create_forwarding_script, columnise
from rez.exceptions import SuiteError
from rez.resolved_context import ResolvedContext
from rez.colorize import heading, warning, stream_is_tty
from rez.vendor import yaml
from rez.vendor.yaml.error import YAMLError
from collections import defaultdict
import os.path
import shutil
import sys


class Suite(object):
    """A collection of contexts.

    A suite is a collection of contexts. A suite stores its contexts in a
    single directory, and creates wrapper scripts for each tool in each context,
    which it stores into a single bin directory. When a tool is invoked, it
    executes the actual tool in its associated context. When you add a suite's
    bin directory to PATH, you have access to all these tools, which will
    automatically run in correctly configured environments.

    Tool clashes can occur when a tool of the same name is present in more than
    one context. When a context is added to a suite, or prefixed/suffixed, that
    context's tools override tools from other contexts.

    There are several ways to avoid tool name clashes:
    - Hide a tool. This removes it from the suite even if it does not clash;
    - Prefix/suffix a context. When you do this, all the tools in the context
      have the prefix/suffix applied;
    - Explicitly alias a tool using the `alias_tool` method. This takes
      precedence over context prefix/suffixing.
    """
    def __init__(self):
        """Create a suite."""
        self.load_path = None
        self.contexts = {}
        self.next_priority = 1

        self.tools = None
        self.tool_conflicts = None

    @property
    def context_names(self):
        return self.contexts.keys()

    def __str__(self):
        return "%s(%s)" % (self.__class__.__name__, " ".join(self.context_names))

    def context(self, name):
        """Get a context.

        Args:
            name (str): Name to store the context under.

        Returns:
            `ResolvedContext` object.
        """
        data = self._context(name)
        context = data.get("context")
        if context:
            return context

        assert self.load_path
        context_path = os.path.join(self.load_path, "contexts", "%s.rxt" % name)
        context = ResolvedContext.load(context_path)
        data["context"] = context
        return context

    def add_context(self, name, context, description=None):
        """Add a context to the suite.

        Args:
            name (str): Name to store the context under.
            context (ResolvedContext): Context to add.
            description (str): Optional description of the context, for example
                "Maya for effects artists."
        """
        if name in self.contexts:
            raise SuiteError("Context already in suite: %r" % name)
        if not context.success:
            raise SuiteError("Context is not resolved: %r" % name)
        self.contexts[name] = dict(name=name,
                                   context=context,
                                   tool_aliases={},
                                   hidden_tools=set(),
                                   description=description,
                                   priority=self._next_priority)
        self._flush_tools()

    def remove_context(self, name):
        """Remove a context from the suite.

        Args:
            name (str): Name of the context to remove.
        """
        _ = self._context(name)
        del self.contexts[name]
        self._flush_tools()

    def set_context_prefix(self, name, prefix):
        """Set a context's prefix.

        This will be applied to all wrappers for the tools in this context. For
        example, a tool called 'foo' would appear as '<prefix>foo' in the
        suite's bin path.

        Args:
            name (str): Name of the context to prefix.
            prefix (str): Prefix to apply to tools.
        """
        data = self._context(name)
        data["prefix"] = prefix
        data["priority"] = self._next_priority
        self._flush_tools()

    def set_context_suffix(self, name, suffix):
        """Set a context's suffix.

        This will be applied to all wrappers for the tools in this context. For
        example, a tool called 'foo' would appear as 'foo<prefix>' in the
        suite's bin path.

        Args:
            name (str): Name of the context to prefix.
            prefix (str): Prefix to apply to tools.
        """
        data = self._context(name)
        data["suffix"] = suffix
        data["priority"] = self._next_priority
        self._flush_tools()

    def bump_context(self, name):
        """Causes the context's tools to take priority over all others."""
        data = self._context(name)
        data["priority"] = self._next_priority
        self._flush_tools()

    def hide_tool(self, context_name, tool_name):
        """Hide a tool so that it is not exposed in the suite.

        Args:
            context_name (str): Context containing the tool.
            tool_name (str): Name of tool to hide.
        """
        data = self._context(context_name)
        hidden_tools = data["hidden_tools"]
        if tool_name not in hidden_tools:
            hidden_tools.add(tool_name)
            self._flush_tools()

    def unhide_tool(self, context_name, tool_name):
        """Unhide a tool so that it may be exposed in a suite.

        Note that unhiding a tool doesn't guarantee it can be seen - a tool of
        the same name from a different context may be overriding it.

        Args:
            context_name (str): Context containing the tool.
            tool_name (str): Name of tool to unhide.
        """
        data = self._context(context_name)
        hidden_tools = data["hidden_tools"]
        if tool_name in hidden_tools:
            hidden_tools.remove(tool_name)
            self._flush_tools()

    def alias_tool(self, context_name, tool_name, tool_alias):
        """Register an alias for a specific tool.

        Note that a tool alias takes precedence over a context prefix/suffix.

        Args:
            context_name (str): Context containing the tool.
            tool_name (str): Name of tool to unhide.
            tool_alias (str): Alias to give the tool.
        """
        data = self._context(context_name)
        aliases = data["tool_aliases"]
        if tool_name not in aliases:
            aliases[tool_name] = tool_alias
            self._flush_tools()

    def dealias_tool(self, context_name, tool_name):
        """Deregister an alias for a specific tool.

        Args:
            context_name (str): Context containing the tool.
            tool_name (str): Name of tool to unhide.
        """
        data = self._context(context_name)
        aliases = data["tool_aliases"]
        if tool_name in aliases:
            del aliases[tool_name]
            self._flush_tools()

    def get_tools(self):
        """Get the tools exposed by this suite.

        Returns:
            A dict, keyed by aliased tool name, with dict entries:
            - tool_name (str): The original, non-aliased name of the tool;
            - tool_alias (str): Aliased tool name (same as key);
            - context_name (str): Name of the context containing the tool;
            - variant (`Variant`): Variant providing the tool.
        """
        self._update_tools()
        return self.tools

    def get_conflicting_aliases(self):
        """Get a list of tool aliases that have one or more conflicts.

        Returns:
            List of strings.
        """
        self._update_tools()
        return self.tool_conflicts.keys()

    def get_alias_conflicts(self, tool_alias):
        """Get a list of conflicts on the given tool.

        Returns: None if the alias has no conflicts, or a list of dicts, where
            each dict contains:
            - tool_name (str): The original, non-aliased name of the tool;
            - tool_alias (str): Aliased tool name (same as key);
            - context_name (str): Name of the context containing the tool;
            - variant (`Variant`): Variant providing the tool.
        """
        self._update_tools()
        return self.tool_conflicts.get(tool_alias)

    def to_dict(self):
        contexts_ = {}
        for k, data in self.contexts.iteritems():
            data_ = data.copy()
            if "context" in data_:
                del data_["context"]
            contexts_[k] = data_

        return dict(contexts=contexts_)

    @classmethod
    def from_dict(cls, d):
        s = Suite.__new__(Suite)
        s.load_path = None
        s.tools = None
        s.tool_conflicts = None
        s.contexts = d["contexts"]
        s.next_priority = max(x["priority"]
                              for x in s.contexts.itervalues()) + 1
        return s

    def save(self, path, verbose=False):
        if os.path.exists(path):
            if verbose:
                print "deleting previous files at %r..." % path
            shutil.rmtree(path)
        contexts_path = os.path.join(path, "contexts")
        os.makedirs(contexts_path)

        # write suite data
        data = self.to_dict()
        filepath = os.path.join(path, "suite.yaml")
        with open(filepath, "w") as f:
            f.write(yaml.dump(data))

        # write contexts
        for context_name in self.context_names:
            context = self.context(context_name)
            filepath = os.path.join(contexts_path, "%s.rxt" % context_name)
            if verbose:
                print "writing %r..." % filepath
            context.save(filepath)

        # create alias wrappers
        bin_path = os.path.join(path, "bin")
        os.makedirs(bin_path)
        if verbose:
            print "creating alias wrappers in %r..." % bin_path

        tools = self.get_tools()
        for tool_alias, d in tools.iteritems():
            tool_name = d["tool_name"]
            context_name = d["context_name"]
            if verbose:
                print ("creating %r -> %r (%s context)..."
                       % (tool_alias, tool_name, context_name))
            filepath = os.path.join(bin_path, tool_alias)
            create_forwarding_script(filepath,
                                     module="suite",
                                     func_name="_FWD__invoke_suite_tool_alias",
                                     context_name=context_name,
                                     tool_name=tool_name)

    @classmethod
    def load(cls, path):
        if not os.path.exists(path):
            open(path)  # raise IOError
        filepath = os.path.join(path, "suite.yaml")
        if not os.path.isfile(filepath):
            raise SuiteError("Not a suite: %r" % path)

        try:
            with open(filepath) as f:
                data = yaml.load(f.read())
        except YAMLError as e:
            raise SuiteError("Failed loading suite: %s" % str(e))

        s = cls.from_dict(data)
        s.load_path = path
        return s

    def print_info(self, buf=sys.stdout, verbose=False):
        """Prints a message summarising the contents of the suite."""
        def _pr(s='', style=None):
            if style and stream_is_tty(buf):
                s = style(s)
            print >> buf, s

        _pr("contexts:", heading)
        rows = []
        for data in self._sorted_contexts():
            context_name = data["name"]
            description = data.get("description", "")
            rows.append((context_name, description))
        _pr("\n".join(columnise(rows)))

        _pr()
        _pr("tools:", heading)
        rows = [["TOOL", "ALIASING", "PACKAGE", "CONTEXT", ""],
                ["----", "--------", "-------", "-------", ""]]
        colors = [None, None]

        def _get_row(entry):
            context_name = entry["context_name"]
            tool_alias = entry["tool_alias"]
            tool_name = entry["tool_name"]
            package = entry["variant"].qualified_package_name
            if tool_name == tool_alias:
                tool_name = "-"
            return [tool_alias, tool_name, package, context_name]

        tools = self.get_tools().values()
        for data in self._sorted_contexts():
            context_name = data["name"]
            entries = [x for x in tools if x["context_name"] == context_name]
            entries = sorted(entries, key=lambda x: x["tool_alias"])
            col = None

            for entry in entries:
                t = _get_row(entry)
                rows.append(t + [""])
                colors.append(col)

                if verbose:
                    conflicts = self.get_alias_conflicts(t[0])
                    if conflicts:
                        for conflict in conflicts:
                            t = _get_row(conflict)
                            rows.append(t + ["(not visible)"])
                            colors.append(warning)

        for col, line in zip(colors, columnise(rows)):
            _pr(line, col)

    def _context(self, name):
        data = self.contexts.get(name)
        if not data:
            raise SuiteError("No such context: %r" % name)
        return data

    def _sorted_contexts(self):
        return sorted(self.contexts.values(), key=lambda x: x["priority"])

    @property
    def _next_priority(self):
        p = self.next_priority
        self.next_priority += 1
        return p

    def _flush_tools(self):
        self.tools = None
        self.tool_conflicts = None

    def _update_tools(self):
        if self.tools is not None:
            return
        self.tools = {}
        self.tool_conflicts = defaultdict(list)

        for data in reversed(self._sorted_contexts()):
            context_name = data["name"]
            tool_aliases = data["tool_aliases"]
            hidden_tools = data["hidden_tools"]
            prefix = data.get("prefix", "")
            suffix = data.get("suffix", "")

            context = self.context(context_name)
            context_tools = context.get_tools(request_only=True)
            for variant, tool_names in context_tools.itervalues():
                for tool_name in tool_names:
                    if tool_name in hidden_tools:
                        continue
                    alias = tool_aliases.get(tool_name)
                    if alias is None:
                        alias = "%s%s%s" % (prefix, tool_name, suffix)

                    entry = dict(tool_name=tool_name,
                                 tool_alias=alias,
                                 context_name=context_name,
                                 variant=variant)

                    if alias in self.tools:
                        self.tool_conflicts[alias].append(entry)
                    else:
                        self.tools[alias] = entry


class Alias(object):
    """Main execution point of an 'alias' script in a suite.
    """
    def __init__(self, context_name, context, tool_name, cli_args):
        self.context_name = context_name
        self.context = context
        self.tool_name = tool_name
        self.cli_args = cli_args

    def run(self):
        """Invoke the wrapped script.

        Returns:
            Return code of the process.
        """
        from rez.vendor import argparse
        parser = argparse.ArgumentParser(prog=self.tool_name, prefix_chars="+")

        # alias-specific options
        parser.add_argument(
            "+i", "++interactive", action="store_true",
            help="launch an interactive shell within the tool's configured "
            "environment")
        parser.add_argument(
            "+c", "++command", type=str, nargs='+', metavar=("COMMAND", "ARG"),
            help="read commands from string, rather than executing the tool")
        parser.add_argument(
            "++rcfile", type=str,
            help="source this file instead of the target shell's "
            "standard startup scripts, if possible")
        parser.add_argument(
            "++norc", action="store_true",
            help="skip loading of startup scripts")
        parser.add_argument(
            "+s", "++stdin", action="store_true",
            help="read commands from standard input, rather than executing the tool")

        opts, tool_args = parser.parse_known_args()
        if opts.stdin:
            # generally shells will behave as though the '-s' flag was not present
            # when no stdin is available. So here we replicate this behaviour.
            import select
            if not select.select([sys.stdin], [], [], 0.0)[0]:
                opts.stdin = False

        # construct context, if necessary
        context = self.context

        # construct command
        cmd = None
        if opts.command:
            cmd = opts.command
        elif opts.interactive:
            from rez.config import config
            config.override("prompt", "%s>" % self.context_name)
            cmd = None
        else:
            cmd = [self.tool_name] + tool_args

        retcode, _, _ = context.execute_shell(command=cmd,
                                              stdin=opts.stdin,
                                              rcfile=opts.rcfile,
                                              norc=opts.norc,
                                              block=True)
        return retcode


def _FWD__invoke_suite_tool_alias(context_name, tool_name, _script, _cli_args):
    suite_path = os.path.dirname(os.path.dirname(_script))
    path = os.path.join(suite_path, "contexts", "%s.rxt" % context_name)
    context = ResolvedContext.load(path)

    alias = Alias(context_name, context, tool_name, _cli_args)
    retcode = alias.run()
    sys.exit(retcode)
