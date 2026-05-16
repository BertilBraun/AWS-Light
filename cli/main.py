from __future__ import annotations

import sys
from pathlib import Path

import click
import httpx
from rich.console import Console
from rich.table import Table

console = Console()

_TOKEN_FILE = Path.home() / ".aws-light" / "token"
_DEFAULT_API_URL = "http://localhost:8000"


def _get_token() -> str:
    if not _TOKEN_FILE.exists():
        console.print("[red]Not logged in. Run: aws-light login[/red]")
        sys.exit(1)
    return _TOKEN_FILE.read_text().strip()


def _auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_get_token()}"}


def _api_url() -> str:
    return _DEFAULT_API_URL


def _handle_error(response: httpx.Response) -> None:
    if not response.is_success:
        try:
            detail = response.json().get("detail", response.text)
        except Exception:
            detail = response.text
        console.print(f"[red]Error {response.status_code}:[/red] {detail}")
        sys.exit(1)


@click.group()
def cli() -> None:
    pass


@cli.command()
@click.option("--user", default="admin", show_default=True)
@click.option("--password", prompt=True, hide_input=True)
def login(user: str, password: str) -> None:
    response = httpx.post(
        f"{_api_url()}/api/v1/auth/login",
        json={"username": user, "password": password},
    )
    _handle_error(response)
    token = response.json()["access_token"]
    _TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TOKEN_FILE.write_text(token)
    console.print(f"[green]Logged in as {user}[/green]")


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
def apply(file_path: str) -> None:
    yaml_text = Path(file_path).read_text()
    response = httpx.post(
        f"{_api_url()}/api/v1/manifests/apply",
        json={"yaml_text": yaml_text},
        headers=_auth_headers(),
    )
    _handle_error(response)
    results = response.json()
    table = Table(title="Apply Results")
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("Action")
    table.add_column("Detail")
    for result in results:
        action_color = {
            "created": "green",
            "updated": "yellow",
            "unchanged": "dim",
            "error": "red",
        }.get(result["action"], "white")
        table.add_row(
            result["kind"],
            result["name"],
            f"[{action_color}]{result['action']}[/{action_color}]",
            result.get("detail", ""),
        )
    console.print(table)


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
def diff(file_path: str) -> None:
    yaml_text = Path(file_path).read_text()
    response = httpx.post(
        f"{_api_url()}/api/v1/manifests/diff",
        json={"yaml_text": yaml_text},
        headers=_auth_headers(),
    )
    _handle_error(response)
    diffs = response.json()
    table = Table(title="Diff")
    table.add_column("Kind")
    table.add_column("Name")
    table.add_column("Action")
    table.add_column("Changed Fields")
    for manifest_diff in diffs:
        action_color = {"create": "green", "update": "yellow", "none": "dim"}.get(
            manifest_diff["action"], "white"
        )
        table.add_row(
            manifest_diff["kind"],
            manifest_diff["name"],
            f"[{action_color}]{manifest_diff['action']}[/{action_color}]",
            ", ".join(manifest_diff.get("changed_fields", [])),
        )
    console.print(table)


@cli.command()
@click.argument("file_path", type=click.Path(exists=True))
def destroy(file_path: str) -> None:
    yaml_text = Path(file_path).read_text()
    response = httpx.post(
        f"{_api_url()}/api/v1/manifests/destroy",
        json={"yaml_text": yaml_text},
        headers=_auth_headers(),
    )
    _handle_error(response)
    results = response.json()
    for result in results:
        console.print(f"[red]destroyed[/red] {result['kind']}/{result['name']}")


@cli.command()
@click.argument("service_name", required=False)
def status(service_name: str | None) -> None:
    if service_name:
        response = httpx.get(
            f"{_api_url()}/api/v1/services/{service_name}",
            headers=_auth_headers(),
        )
        _handle_error(response)
        service = response.json()
        _print_service(service)
    else:
        response = httpx.get(f"{_api_url()}/api/v1/services", headers=_auth_headers())
        _handle_error(response)
        services = response.json()
        table = Table(title="Services")
        table.add_column("Name")
        table.add_column("Image")
        table.add_column("Status")
        table.add_column("Replicas")
        for service in services:
            status_color = {"running": "green", "degraded": "yellow", "failed": "red"}.get(
                service["status"], "white"
            )
            table.add_row(
                service["spec"]["name"],
                service["spec"]["image"],
                f"[{status_color}]{service['status']}[/{status_color}]",
                str(len(service["replicas"])),
            )
        console.print(table)


def _print_service(service: dict) -> None:  # type: ignore[type-arg]
    spec = service["spec"]
    console.print(f"[bold]{spec['name']}[/bold]  {service['status']}")
    console.print(f"  image: {spec['image']}")
    console.print(f"  replicas: {len(service['replicas'])}/{spec['replicas']}")
    for replica in service["replicas"]:
        console.print(
            f"    [{replica['status']}] {replica['replica_id'][:12]}  port={replica['host_port']}"
        )


@cli.group()
def secret() -> None:
    pass


@secret.command(name="set")
@click.argument("name")
@click.argument("value")
def secret_set(name: str, value: str) -> None:
    response = httpx.post(
        f"{_api_url()}/api/v1/secrets",
        json={"name": name, "value": value},
        headers=_auth_headers(),
    )
    _handle_error(response)
    console.print(f"[green]Secret '{name}' created[/green]")


@secret.command(name="get")
@click.argument("name")
def secret_get(name: str) -> None:
    response = httpx.get(
        f"{_api_url()}/api/v1/secrets/{name}",
        headers=_auth_headers(),
    )
    _handle_error(response)
    console.print(response.json()["value"])


@secret.command(name="delete")
@click.argument("name")
def secret_delete(name: str) -> None:
    response = httpx.delete(
        f"{_api_url()}/api/v1/secrets/{name}",
        headers=_auth_headers(),
    )
    _handle_error(response)
    console.print(f"[red]Secret '{name}' deleted[/red]")


@secret.command(name="list")
def secret_list() -> None:
    response = httpx.get(f"{_api_url()}/api/v1/secrets", headers=_auth_headers())
    _handle_error(response)
    for name in response.json():
        console.print(name)


@cli.group()
def storage() -> None:
    pass


@storage.command(name="ls")
@click.argument("bucket", required=False)
def storage_ls(bucket: str | None) -> None:
    if bucket:
        response = httpx.get(
            f"{_api_url()}/api/v1/storage/buckets/{bucket}/objects",
            headers=_auth_headers(),
        )
        _handle_error(response)
        for obj in response.json():
            console.print(f"{obj['key']}  ({obj['size_bytes']} bytes)")
    else:
        response = httpx.get(f"{_api_url()}/api/v1/storage/buckets", headers=_auth_headers())
        _handle_error(response)
        for bucket_info in response.json():
            console.print(bucket_info["name"])


@storage.command(name="cp")
@click.argument("source")
@click.argument("destination")
def storage_cp(source: str, destination: str) -> None:
    if source.startswith("s3://"):
        bucket, key = _parse_s3_uri(source)
        response = httpx.get(
            f"{_api_url()}/api/v1/storage/buckets/{bucket}/objects/{key}",
            headers=_auth_headers(),
        )
        _handle_error(response)
        Path(destination).write_bytes(response.content)
        console.print(f"[green]Downloaded {source} -> {destination}[/green]")
    elif destination.startswith("s3://"):
        bucket, key = _parse_s3_uri(destination)
        data = Path(source).read_bytes()
        response = httpx.put(
            f"{_api_url()}/api/v1/storage/buckets/{bucket}/objects/{key}",
            content=data,
            headers={**_auth_headers(), "content-type": "application/octet-stream"},
        )
        _handle_error(response)
        console.print(f"[green]Uploaded {source} -> {destination}[/green]")
    else:
        console.print("[red]One of source or destination must be an s3:// URI[/red]")
        sys.exit(1)


@storage.command(name="rm")
@click.argument("uri")
def storage_rm(uri: str) -> None:
    bucket, key = _parse_s3_uri(uri)
    response = httpx.delete(
        f"{_api_url()}/api/v1/storage/buckets/{bucket}/objects/{key}",
        headers=_auth_headers(),
    )
    _handle_error(response)
    console.print(f"[red]Deleted {uri}[/red]")


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    without_scheme = uri.removeprefix("s3://")
    parts = without_scheme.split("/", 1)
    if len(parts) < 2:
        console.print(f"[red]Invalid s3:// URI: {uri}[/red]")
        sys.exit(1)
    return parts[0], parts[1]
