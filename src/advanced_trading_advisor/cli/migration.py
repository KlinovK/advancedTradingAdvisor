"""Migration dispatch independent of ordinary application composition and dotenv."""

import os

from advanced_trading_advisor.storage.migration import (
    MigrationError,
    import_source,
    load_source,
    rehearse,
    verify_source,
)
from advanced_trading_advisor.storage.postgres_database import PostgresDatabase


def add_parser(commands):
    operations = commands.add_parser("migration").add_subparsers(dest="operation", required=True)
    for operation in ("inspect", "rehearse", "import", "verify"):
        parser = operations.add_parser(operation)
        parser.add_argument("--source", required=True)
        if operation in {"import", "verify"}:
            parser.add_argument("--target-schema", required=True, choices=["public"])


async def run(args, output):
    try:
        source = await load_source(args.source)
        outcome, equal = "inspected", None
        if args.operation != "inspect":
            url = os.environ.get("ADVISOR_DATABASE_URL")
            if not url:
                raise MigrationError("Set ADVISOR_DATABASE_URL in the process environment.")
            if args.operation == "rehearse":
                outcome = await rehearse(source, url)
            else:
                database = PostgresDatabase(url, schema=args.target_schema)
                operation = import_source if args.operation == "import" else verify_source
                outcome = await operation(source, database)
            equal = True
        output(source.manifest.render(outcome=outcome, equal=equal))
    except MigrationError:
        raise
    except Exception:
        raise MigrationError("Migration configuration or validation failed.") from None
