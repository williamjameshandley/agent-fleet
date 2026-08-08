"""Private fzf process adapter for the Muster interface."""

import sys

HOT = {"items", "header", "cursor", "preview"}


def main(argv=None):
    """Dispatch one private Muster request."""
    argv = sys.argv[1:] if argv is None else list(argv)
    if argv and argv[0] in HOT:
        from . import hot
        return hot.main(argv)

    import argparse

    from . import actions, ui, viewer

    parser = argparse.ArgumentParser(prog="/usr/lib/agent-fleet/ui")
    commands = parser.add_subparsers(required=True)

    def command(name, function):
        item = commands.add_parser(name)
        item.set_defaults(function=function)
        return item

    item = command("toggle", lambda args: ui.toggle(args.kind))
    item.add_argument("kind", choices=("python",))
    item = command("fold", lambda args: ui.fold(args.action, args.key))
    item.add_argument("action", choices=("open", "close"))
    item.add_argument("key")
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
    item = command("refresh", lambda args: actions.refresh(args.key))
    item.add_argument("key")
    item = command("archive", lambda args: actions.archive(args.key))
    item.add_argument("key")
    item = command("open-history", lambda args: actions.open_history(args.key))
    item.add_argument("key")

    args = parser.parse_args(argv)
    return args.function(args)


if __name__ == "__main__":
    main()
