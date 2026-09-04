"""Browser dashboard for runs and the lint gate."""
import webbrowser

import typer


def dashboard(port: int = typer.Option(8765, "--port", "-p", help="Starting port; the next free one is used if busy."),
              open_: bool = typer.Option(False, "--open", help="Open the browser after starting.")):
    """Serve a live view of harness runs (tool calls, hooks, denials, cost) and lint decisions.

    Reads harness/state/logs only, so it shows runs in progress, past runs,
    and runs launched from other terminals. Ctrl-C to stop.
    """
    from .. import dashboard as D, paths
    repo = paths.repo_root()
    srv, p = D.serve(repo, port)
    url = f"http://127.0.0.1:{p}/"
    typer.echo(f"mina-agent dashboard: {url}  (logs: {paths.logs_dir()})", err=True)
    if open_:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        typer.echo("dashboard stopped", err=True)
