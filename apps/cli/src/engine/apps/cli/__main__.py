"""Run the OpenEngine daemon and drive its graphs, runs and loops from a terminal."""

from __future__ import annotations

import argparse
from importlib.metadata import version

from engine.apps.cli import connect, daemon, session
from engine.cli import commands as graph_commands

EXIT_OK = 0
OUTPUT = """\
output:
  List commands (backends, graphs, runs, loops, nodes, agents, connections)
  print a table; add --json for JSON, such as `engine runs --json`.
  Other commands print JSON; add --pretty for readable output.
"""


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        prog="engine", description=__doc__, epilog=OUTPUT, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    result.add_argument("--version", action="version", version=version("engine-cli"))
    commands = result.add_subparsers(dest="command")
    connect.add_parsers(commands)
    daemon.add_parser(commands)
    session.add_parser(graph_commands.add_parsers(commands))
    commands.metavar = "{" + ",".join(name for name in commands.choices if name not in graph_commands.HIDDEN) + "}"
    return result


def main(argv: list[str] | None = None) -> int:
    arguments = parser().parse_args(argv)
    if arguments.command is None:
        parser().print_help()
        return EXIT_OK
    if arguments.command == "connect":
        return connect.main(arguments)
    if arguments.command == "connections":
        return connect.connections(arguments)
    if arguments.command == "disconnect":
        return connect.disconnect(arguments)
    if arguments.command == "daemon":
        return daemon.main(arguments)
    if arguments.command == "agent" and arguments.action in session.AGENTS:
        return session.main(arguments)
    if arguments.command in graph_commands.COMMANDS:
        return graph_commands.main(arguments)
    raise AssertionError("unreachable command")


if __name__ == "__main__":
    raise SystemExit(main())
