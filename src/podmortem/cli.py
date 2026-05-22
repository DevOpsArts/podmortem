"""CLI interface for podmortem - query and manage restart history."""

import logging
import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from podmortem.storage import init_db, query_restarts, delete_restarts
from podmortem.watcher import PodRestartWatcher

console = Console()


@click.group()
def cli():
    """Podmortem - Pod Restart Root Cause Logger."""
    pass


@cli.command()
@click.option("--namespace", "-n", default=None, help="Namespace to watch (default: all)")
@click.option("--db-path", default=None, help="Path to SQLite database file")
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def watch(namespace: Optional[str], db_path: Optional[str], verbose: bool):
    """Start watching for pod restarts."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    db = Path(db_path) if db_path else None
    watcher = PodRestartWatcher(namespace=namespace, db_path=db)
    watcher.run()


@cli.command()
@click.option("--namespace", "-n", default=None, help="Filter by namespace")
@click.option("--pod", "-p", default=None, help="Filter by pod name (substring match)")
@click.option("--since", "-s", default=None, help="Show restarts since (ISO timestamp)")
@click.option("--limit", "-l", default=20, help="Max results (default: 20)")
@click.option("--db-path", default=None, help="Path to SQLite database file")
@click.option("--show-logs", is_flag=True, help="Show last logs in output")
def history(
    namespace: Optional[str],
    pod: Optional[str],
    since: Optional[str],
    limit: int,
    db_path: Optional[str],
    show_logs: bool,
):
    """Query pod restart history."""
    db = Path(db_path) if db_path else None
    conn = init_db(db)

    results = query_restarts(conn, namespace=namespace, pod_name=pod, since=since, limit=limit)

    if not results:
        console.print("[yellow]No restart records found.[/yellow]")
        return

    table = Table(title=f"Pod Restart History ({len(results)} records)")
    table.add_column("ID", justify="right", style="dim")
    table.add_column("Timestamp", style="cyan", no_wrap=True)
    table.add_column("Namespace", style="green")
    table.add_column("Pod", style="white")
    table.add_column("Container", style="blue")
    table.add_column("Restart #", justify="right", style="red")
    table.add_column("Reason", style="yellow")
    table.add_column("Exit Code", justify="right")

    for r in results:
        table.add_row(
            str(r["id"]),
            r["timestamp"][:19],
            r["namespace"],
            r["pod_name"],
            r["container_name"],
            str(r["restart_count"]),
            r["reason"],
            str(r["exit_code"] or "-"),
        )

    console.print(table)

    if show_logs and results:
        for r in results:
            console.print(f"\n[bold]--- {r['pod_name']}/{r['container_name']} ---[/bold]")
            console.print(f"[dim]{r['last_logs'][:500]}[/dim]")


@cli.command()
@click.argument("record_id", type=int)
@click.option("--db-path", default=None, help="Path to SQLite database file")
def detail(record_id: int, db_path: Optional[str]):
    """Show full details for a specific restart record by ID."""
    db = Path(db_path) if db_path else None
    conn = init_db(db)

    row = conn.execute("SELECT * FROM restarts WHERE id = ?", (record_id,)).fetchone()
    if not row:
        console.print(f"[red]Record {record_id} not found.[/red]")
        return

    r = dict(row)
    console.print(f"[bold cyan]Restart Record #{r['id']}[/bold cyan]")
    console.print(f"  Pod:       {r['namespace']}/{r['pod_name']}")
    console.print(f"  Container: {r['container_name']}")
    console.print(f"  Node:      {r['node_name'] or 'N/A'}")
    console.print(f"  Restart #: {r['restart_count']}")
    console.print(f"  Reason:    {r['reason']}")
    console.print(f"  Exit Code: {r['exit_code'] or 'N/A'}")
    console.print(f"  Time:      {r['timestamp']}")
    console.print("\n[bold]Last Logs:[/bold]")
    console.print(r["last_logs"] or "<empty>")
    console.print("\n[bold]Events:[/bold]")
    console.print(r["events"] or "<empty>")


@cli.command()
@click.option("--id", "record_id", type=int, default=None, help="Delete a specific record by ID")
@click.option("--namespace", "-n", default=None, help="Delete all records in namespace")
@click.option("--pod", "-p", default=None, help="Delete records matching pod name (substring)")
@click.option("--before", "-b", default=None, help="Delete records before ISO timestamp")
@click.option("--all", "all_records", is_flag=True, help="Delete ALL records")
@click.option("--db-path", default=None, help="Path to SQLite database file")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def purge(
    record_id: Optional[int],
    namespace: Optional[str],
    pod: Optional[str],
    before: Optional[str],
    all_records: bool,
    db_path: Optional[str],
    yes: bool,
):
    """Delete restart history records."""
    if not any([record_id, namespace, pod, before, all_records]):
        console.print("[red]Specify at least one filter (--id, -n, -p, --before, or --all).[/red]")
        raise SystemExit(1)

    db = Path(db_path) if db_path else None
    conn = init_db(db)

    # Build description of what will be deleted
    parts = []
    if all_records:
        parts.append("ALL records")
    if record_id:
        parts.append(f"record #{record_id}")
    if namespace:
        parts.append(f"namespace={namespace}")
    if pod:
        parts.append(f"pod matching '{pod}'")
    if before:
        parts.append(f"before {before}")

    desc = ", ".join(parts)

    if not yes:
        click.confirm(f"Delete {desc}?", abort=True)

    count = delete_restarts(
        conn,
        record_id=record_id,
        namespace=namespace,
        pod_name=pod,
        before=before,
        all_records=all_records,
    )
    console.print(f"[green]Deleted {count} record(s).[/green]")


if __name__ == "__main__":
    cli()
