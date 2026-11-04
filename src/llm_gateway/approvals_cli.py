"""`llm-gateway approvals …`: what an admin types to see and decide held tool calls."""

import argparse
import asyncio
import getpass
import json
import sys
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from llm_gateway.approvals.desk import ApprovalDesk, ApprovalNotFoundError, ApprovalNotPendingError
from llm_gateway.approvals.hold import utc_now
from llm_gateway.approvals.model import Approval
from llm_gateway.approvals.ports import ApprovalUnavailableError
from llm_gateway.config import Settings
from llm_gateway.main import build_engine
from llm_gateway.storage.approval_store import PostgresApprovalStore

_SHOWN_ARGUMENT_CHARS = 120


def add_commands(commands: "argparse._SubParsersAction[argparse.ArgumentParser]") -> None:
    approvals = commands.add_parser("approvals", help="see and decide tool calls held for approval")
    actions = approvals.add_subparsers(dest="action", required=True)
    actions.add_parser("list", help="what is waiting, oldest first")
    actions.add_parser("show", help="one approval, arguments in full").add_argument("id", type=UUID)
    for name in ("approve", "reject"):
        decide = actions.add_parser(name, help=f"{name} a waiting call")
        decide.add_argument("id", type=UUID)
        decide.add_argument("--note", help="why; the client sees it on a rejection")
        decide.add_argument("--by", default=None, help="who decides (default: your login)")
        decide.add_argument(
            "--yes", action="store_true", help="don't show the call and ask first (scripts)"
        )


def run(args: argparse.Namespace, settings: Settings) -> int:
    return asyncio.run(_run(args, settings))


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    engine = build_engine(settings)
    desk = ApprovalDesk(PostgresApprovalStore(engine))
    try:
        if args.action == "list":
            for approval in await desk.waiting():
                print(_summary(approval))
        elif args.action == "show":
            print(_details(await desk.show(args.id)))
        else:
            by = args.by if args.by is not None else _login()
            if not args.yes and not _confirmed(await desk.show(args.id), args.action):
                print("nothing decided")
                return 1
            decide = desk.approve if args.action == "approve" else desk.reject
            decided = await decide(args.id, by, args.note)
            print(f"{decided.status} {decided.id} ({_printable(decided.tool)}) by {by}")
    except (ApprovalNotFoundError, ApprovalNotPendingError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (ApprovalUnavailableError, SQLAlchemyError, OSError, TimeoutError):
        print("error: database unavailable", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


def _login() -> str:
    try:
        return getpass.getuser()
    except (KeyError, OSError) as error:  # no passwd entry, as in many containers
        raise ValueError("can't tell who you are; pass --by NAME") from error


def _confirmed(approval: Approval, action: str) -> bool:
    """Show exactly what would run, then ask: the list's one-line summary can be cut off."""
    print(_details(approval))
    return input(f"{action} this call? [y/N] ").strip().lower() == "y"


def _printable(text: str) -> str:
    """Client-controlled text, safe to put on an admin's terminal: no control characters
    (newlines, ANSI escapes) that could forge or hide a line."""
    return "".join(c if c.isprintable() else repr(c)[1:-1] for c in text)


def _summary(approval: Approval) -> str:
    left = (approval.expires_at - utc_now()).total_seconds()
    arguments = json.dumps(approval.arguments, sort_keys=True)
    if len(arguments) > _SHOWN_ARGUMENT_CHARS:
        arguments = arguments[:_SHOWN_ARGUMENT_CHARS] + "… (see `approvals show`)"
    return (
        f"{approval.id}  {_printable(approval.tenant_name)}  {_printable(approval.tool)}  "
        f"expires in {left:.0f}s  {arguments}"
    )


def _details(approval: Approval) -> str:
    return json.dumps(
        {
            "id": str(approval.id),
            "tenant": approval.tenant_name,
            "tool": approval.tool,
            "arguments": approval.arguments,
            "reasons": list(approval.reasons),
            "status": approval.status.value,
            "requested_at": approval.requested_at.isoformat(),
            "expires_at": approval.expires_at.isoformat(),
            "decided_by": approval.decided_by,
            "note": approval.note,
        },
        indent=2,
        sort_keys=True,
    )
