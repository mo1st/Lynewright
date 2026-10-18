"""Integration packages the host may load by manifest reference.

This package is the only root `IntegrationDefinitionCompiler` will import from.
Core services live outside it on purpose: allowlisting `app.services` would let
a manifest name any Core service, turning manifest data into a way to choose
arbitrary code to execute.

An integration package owns its provider implementation and its manifest.
Adding one must require no edit to Core.
"""
