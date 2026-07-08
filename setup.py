"""Legacy shim so `pip install -e .` works on older setuptools (< PEP 660).

All metadata lives in pyproject.toml; keep this file empty of configuration.
"""

from setuptools import setup

setup()
