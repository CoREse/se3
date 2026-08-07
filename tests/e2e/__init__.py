"""Tests for the ``tianluo.e2e`` subsystem.

These tests never touch a real container runtime: every module that shells out
takes an injectable ``runner``, so the whole suite passes on a machine with
neither docker nor podman installed.
"""
