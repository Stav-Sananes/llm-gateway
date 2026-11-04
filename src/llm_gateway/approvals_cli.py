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
            by = args.by or getpass.getuser()
            decide = desk.approve if args.action == "approve" else desk.reject
            decided = await decide(args.id, by, args.note)
            print(f"{decided.status} {decided.id} ({decided.tool}) by {decided.decided_by}")
    except (ApprovalNotFoundError, ApprovalNotPendingError, ValueError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except (ApprovalUnavailableError, SQLAlchemyError, OSError, TimeoutError):
        print("error: database unavailable", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()
    return 0


def _summary(approval: Approval) -> str:
    left = (approval.expires_at - utc_now()).total_seconds()
    arguments = json.dumps(approval.arguments, sort_keys=True)
    if len(arguments) > _SHOWN_ARGUMENT_CHARS:
        arguments = arguments[:_SHOWN_ARGUMENT_CHARS] + "… (see `approvals show`)"
    return (
        f"{approval.id}  {approval.tenant_name}  {approval.tool}  "
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
