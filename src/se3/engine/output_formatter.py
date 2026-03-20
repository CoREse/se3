"""Output formatter registry for SE3 flow engine.

Provides a central registry for all output formatters in the system.
Formatters can be registered and retrieved by name through this module.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Type

from .formatters import TaskFormatter

logger = logging.getLogger(__name__)


class FormatterRegistry:
    """Registry for output formatters.

    Maintains a mapping of formatter names to formatter classes.
    Provides factory methods for instantiating formatters.
    """

    def __init__(self):
        """Initialize the formatter registry."""
        self._formatters: Dict[str, Type[Any]] = {}
        self._register_default_formatters()

    def _register_default_formatters(self) -> None:
        """Register the default set of formatters."""
        # Register the task formatter
        self.register_formatter("tasks", TaskFormatter)

    def register_formatter(self, name: str, formatter_class: Type[Any]) -> None:
        """Register a formatter class.

        Args:
            name: The name to register the formatter under
            formatter_class: The formatter class to register

        Raises:
            ValueError: If a formatter with this name is already registered
        """
        if name in self._formatters:
            logger.warning(f"Overwriting existing formatter: {name}")
        self._formatters[name] = formatter_class
        logger.debug(f"Registered formatter: {name}")

    def get_formatter(self, name: str, **kwargs: Any) -> Optional[Any]:
        """Get a formatter instance by name.

        Args:
            name: The name of the formatter to retrieve
            **kwargs: Additional arguments to pass to the formatter constructor

        Returns:
            Formatter instance or None if not found
        """
        formatter_class = self._formatters.get(name)
        if formatter_class is None:
            logger.error(f"Formatter not found: {name}")
            return None

        try:
            return formatter_class(**kwargs)
        except Exception as e:
            logger.exception(f"Failed to instantiate formatter '{name}': {e}")
            return None

    def list_formatters(self) -> Dict[str, str]:
        """List all registered formatters.

        Returns:
            Dictionary mapping formatter names to their class names
        """
        return {name: cls.__name__ for name, cls in self._formatters.items()}

    def unregister_formatter(self, name: str) -> bool:
        """Unregister a formatter.

        Args:
            name: The name of the formatter to unregister

        Returns:
            True if the formatter was found and removed, False otherwise
        """
        if name in self._formatters:
            del self._formatters[name]
            logger.debug(f"Unregistered formatter: {name}")
            return True
        return False


# Global registry instance
_registry: Optional[FormatterRegistry] = None


def get_registry() -> FormatterRegistry:
    """Get the global formatter registry.

    Returns:
        The global FormatterRegistry instance
    """
    global _registry
    if _registry is None:
        _registry = FormatterRegistry()
    return _registry


def register_formatter(name: str, formatter_class: Type[Any]) -> None:
    """Register a formatter with the global registry.

    Args:
        name: The name to register the formatter under
        formatter_class: The formatter class to register
    """
    registry = get_registry()
    registry.register_formatter(name, formatter_class)


def get_formatter(name: str, **kwargs: Any) -> Optional[Any]:
    """Get a formatter instance from the global registry.

    Args:
        name: The name of the formatter to retrieve
        **kwargs: Additional arguments to pass to the formatter constructor

    Returns:
        Formatter instance or None if not found
    """
    registry = get_registry()
    return registry.get_formatter(name, **kwargs)


def list_formatters() -> Dict[str, str]:
    """List all registered formatters.

    Returns:
        Dictionary mapping formatter names to their class names
    """
    registry = get_registry()
    return registry.list_formatters()
