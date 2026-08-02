"""Private fzf process adapter for the Muster interface."""

import argparse

from . import actions, ui, viewer


def main(argv=None):
    """Dispatch one private Muster request."""
    parser = argparse.ArgumentParser(prog="/usr/lib/agent-fleet/ui")
    commands = parser.add_subparsers(required=True)

    def command(name, function):
        item = commands.add_parser(name)
        item.set_defaults(function=function)
        return item

    command("items", lambda _: ui.rows(include_header=False))
    command("header", lambda _: print(ui.header()))
    command("cursor", lambda _: print(ui.cursor(), end=""))
    item = command("toggle", lambda args: ui.toggle(args.kind))
    item.add_argument("kind", choices=("language", "python"))
    command("muster", lambda _: ui.muster())
    command("history-ui", lambda _: ui.history())
    command(
        "history-rows",
        lambda _: print(
            "\n".join("\t".join(row) for row in actions.history())
        ),
    )
    item = command("show", lambda args: viewer.show(args.key, args.slot))
    item.add_argument("key")
    item.add_argument("--slot")
    command("create-tab", lambda _: actions.create_tab())
    command("create", lambda _: actions.create_prompt())
    item = command("rename-tab", lambda args: actions.rename_tab(args.key))
    item.add_argument("key")
    item = command("rename-prompt", lambda args: actions.rename_prompt(args.key))
    item.add_argument("key")
    item = command("refresh", lambda args: actions.refresh_report(args.key))
    item.add_argument("key")
    item = command("archive", lambda args: actions.archive_report(args.key))
    item.add_argument("key")
    item = command(
        "preview",
        lambda args: print(actions.pane_preview(args.key, args.columns, args.lines), end=""),
    )
    item.add_argument("key")
    item.add_argument("columns", type=int, nargs="?", default=0)
    item.add_argument("lines", type=int, nargs="?", default=0)
    item = command("open-history", lambda args: actions.open_history_report(args.key))
    item.add_argument("key")

    args = parser.parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    main()
