# Task CLI

A simple CLI task manager for testing SE3 workflows.

## Features

- Add tasks with priority and due dates
- List all tasks in a formatted table
- Mark tasks as done
- Delete tasks

## Installation

```bash
pip install -e ".[dev]"
```

## Usage

```bash
# Add a task
task add "Buy groceries" -p high

# List tasks
task list

# Mark as done
task done 1

# Delete a task
task delete 1
```

## Development

```bash
# Run tests
pytest

# Format code
black src tests
ruff check src tests
```

## Version

Current version: 0.1.0
