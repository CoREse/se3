"""SE3 directory structure migration command.

Migrates legacy directory structures to the new consolidated se3/ format.
"""

import shutil
from pathlib import Path
from typing import List, Tuple

import typer


def run_migration(project_root: str, dry_run: bool, force: bool) -> None:
    """Run the migration process."""
    root = Path(project_root).resolve()

    if not root.exists():
        typer.echo(f"Error: Project root does not exist: {root}", err=True)
        raise typer.Exit(code=1)

    # Check for legacy directories
    legacy_human_calls = root / "human-calls"
    legacy_collab = root / ".collab"
    legacy_hidden_se3 = root / ".se3"  # Earlier 2.x/3.0 with hidden directory
    legacy_visible_se3 = root / "se3"  # Current se3/ directory (for merging)
    legacy_specs = root / "specs"  # Specs at project root
    legacy_config = root / "se3.config.yaml"  # Old config name
    legacy_tmp_files = list(root.glob("tmp*.prompt"))

    # Target directory (VISIBLE, not hidden)
    se3_dir = root / "se3"

    # Check if already migrated
    has_legacy = (
        legacy_human_calls.exists() or
        legacy_collab.exists() or
        legacy_hidden_se3.exists() or
        legacy_specs.exists() or
        legacy_config.exists() or
        legacy_tmp_files
    )
    if se3_dir.exists() and not force and not has_legacy:
        typer.echo("✓ Project already migrated (use --force to merge)")
        raise typer.Exit(code=0)

    # Determine what needs migration
    migrations: List[Tuple[Path, Path, str]] = []

    # Human calls migration
    if legacy_human_calls.exists() and legacy_human_calls.is_dir():
        active_files = []
        archive_files = []

        for f in legacy_human_calls.iterdir():
            if f.is_file():
                if ".archived" in f.name or f.name.endswith(".archived"):
                    archive_files.append(f)
                else:
                    active_files.append(f)

        for f in active_files:
            migrations.append((f, se3_dir / "calls" / "active" / f.name, "active call"))
        for f in archive_files:
            new_name = f.name.replace(".archived", "")
            migrations.append((f, se3_dir / "calls" / "archive" / new_name, "archived call"))

    # Collab migration
    if legacy_collab.exists() and legacy_collab.is_dir():
        for item in legacy_collab.rglob("*"):
            if item.is_file():
                rel_path = item.relative_to(legacy_collab)
                migrations.append((item, se3_dir / "collab" / rel_path, "collab file"))

    # Hidden .se3/ to visible se3/ migration (earlier 2.x/3.0)
    if legacy_hidden_se3.exists() and legacy_hidden_se3.is_dir():
        for item in legacy_hidden_se3.rglob("*"):
            if item.is_file():
                rel_path = item.relative_to(legacy_hidden_se3)
                # Remap spec_index.json to cache/spec_index.json
                if rel_path.name == "spec_index.json" and len(rel_path.parts) == 1:
                    rel_path = Path("cache") / rel_path.name
                migrations.append((item, se3_dir / rel_path, "from hidden .se3"))

    # specs/ → se3/specs/ migration
    if legacy_specs.exists() and legacy_specs.is_dir() and not (se3_dir / "specs").exists():
        for item in legacy_specs.rglob("*"):
            if item.is_file():
                rel_path = item.relative_to(legacy_specs)
                migrations.append((item, se3_dir / "specs" / rel_path, "specs → se3/specs"))

    # se3.config.yaml → se3.yaml migration
    if legacy_config.exists():
        migrations.append((legacy_config, root / "se3.yaml", "config rename"))

    # Tmp files cleanup
    tmp_deletions: List[Path] = []
    for tmp_file in legacy_tmp_files:
        tmp_deletions.append(tmp_file)

    # Report
    typer.echo(f"\n{'=' * 60}")
    typer.echo("SE 3.0 Directory Migration")
    typer.echo(f"{'=' * 60}")
    typer.echo(f"\nProject: {root}")
    typer.echo(f"Mode: {'DRY RUN (no changes made)' if dry_run else 'LIVE'}")

    if not migrations and not tmp_deletions:
        typer.echo("\n✓ No legacy directories found - nothing to migrate")
        raise typer.Exit(code=0)

    typer.echo(f"\nPlanned migrations: {len(migrations)} items")
    if migrations:
        typer.echo("\n  Files to migrate:")
        for src, dst, kind in migrations[:10]:  # Show first 10
            typer.echo(f"    [{kind}] {src.name} → {dst}")
        if len(migrations) > 10:
            typer.echo(f"    ... and {len(migrations) - 10} more")

    if tmp_deletions:
        typer.echo(f"\n  Temporary files to delete: {len(tmp_deletions)}")
        for f in tmp_deletions[:5]:
            typer.echo(f"    - {f.name}")
        if len(tmp_deletions) > 5:
            typer.echo(f"    ... and {len(tmp_deletions) - 5} more")

    # Directories to create
    dirs_to_create = [
        se3_dir / "calls" / "active",
        se3_dir / "calls" / "archive",
        se3_dir / "collab",
        se3_dir / "tmp",
        se3_dir / "state",
        se3_dir / "cache",
        se3_dir / "specs" / "_changelog",
    ]

    if dry_run:
        typer.echo(f"\n  Directories to create:")
        for d in dirs_to_create:
            typer.echo(f"    - {d.relative_to(root)}")
        typer.echo(f"\n{'=' * 60}")
        typer.echo("Dry run complete - no changes made")
        raise typer.Exit(code=0)

    # Perform migration
    typer.echo(f"\n{'=' * 60}")
    typer.echo("Executing migration...")

    # Create directories
    created_dirs = 0
    for d in dirs_to_create:
        d.mkdir(parents=True, exist_ok=True)
        created_dirs += 1
    typer.echo(f"  ✓ Created {created_dirs} directories")

    # Migrate files
    migrated = 0
    errors = 0
    for src, dst, kind in migrations:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            migrated += 1
        except Exception as e:
            typer.echo(f"  ✗ Error migrating {src}: {e}", err=True)
            errors += 1

    if migrated:
        typer.echo(f"  ✓ Migrated {migrated} files")
    if errors:
        typer.echo(f"  ✗ {errors} errors during migration")

    # Clean up tmp files
    deleted = 0
    for tmp_file in tmp_deletions:
        try:
            tmp_file.unlink()
            deleted += 1
        except Exception:
            pass

    if deleted:
        typer.echo(f"  ✓ Cleaned up {deleted} temporary files")

    # Clean up empty legacy directories
    removed_dirs = 0
    if legacy_human_calls.exists() and not any(legacy_human_calls.iterdir()):
        legacy_human_calls.rmdir()
        removed_dirs += 1
    if legacy_collab.exists() and not any(legacy_collab.iterdir()):
        legacy_collab.rmdir()
        removed_dirs += 1
    if legacy_hidden_se3.exists() and not any(legacy_hidden_se3.iterdir()):
        legacy_hidden_se3.rmdir()
        removed_dirs += 1
    if legacy_specs.exists() and not any(legacy_specs.iterdir()):
        legacy_specs.rmdir()
        removed_dirs += 1

    if removed_dirs:
        typer.echo(f"  ✓ Removed {removed_dirs} empty legacy directories")

    # Update .gitignore if needed
    gitignore = root / ".gitignore"
    if gitignore.exists():
        content = gitignore.read_text()
        needs_update = False
        if ".se3/" in content:
            content = content.replace(".se3/", "se3/")
            needs_update = True
        if "se3/tmp/" not in content and "se3/" not in content:
            content += "\n# SE3 runtime (gitignored)\nse3/state/\nse3/cache/\nse3/logs/\nse3/tmp/\nse3/collab/\nse3/calls/\n"
            needs_update = True
        if needs_update:
            with open(gitignore, "w") as f:
                f.write(content)
            typer.echo("  ✓ Updated .gitignore")

    typer.echo(f"\n{'=' * 60}")
    typer.echo("Migration complete!")
    typer.echo(f"\nNew unified se3/ structure:")
    typer.echo(f"  se3/specs/         - Project specifications")
    typer.echo(f"  se3/state/         - Flow engine state")
    typer.echo(f"  se3/cache/         - Cached indexes")
    typer.echo(f"  se3/logs/          - Execution logs")
    typer.echo(f"  se3/calls/         - Human call management")
    typer.echo(f"  se3/collab/        - Multi-agent collaboration")
    typer.echo(f"  se3/tmp/           - Temporary files")
    typer.echo(f"  se3.yaml           - Project configuration")
