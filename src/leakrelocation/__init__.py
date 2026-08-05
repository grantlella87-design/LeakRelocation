"""Shared support code for the leak relocation workflow.

Importing this package has no side effects: it opens no network connections,
creates no folders and reads no data. Configuration lives in
`leakrelocation.config`; pure classification logic lives in
`leakrelocation.matching`.
"""

__all__ = ["config", "matching"]
