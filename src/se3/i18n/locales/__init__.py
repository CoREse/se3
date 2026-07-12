"""Locale catalogs (``<code>.json``) for the CLI UI text.

WHY this file exists at all — the directory holds only data: making ``locales``
a *regular* package (rather than a namespace package) is what lets
``importlib.resources.files("se3.i18n.locales")`` work on Python 3.9, where
``files()`` does not support namespace packages and raises instead of returning
a traversable. Deleting it would break every ``t()`` call on that interpreter.
"""
