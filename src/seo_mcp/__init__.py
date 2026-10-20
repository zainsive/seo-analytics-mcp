"""seo-mcp: an MCP server for Google Search Console, GA4 and IndexNow."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("seo-analytics-mcp")
except PackageNotFoundError:  # running from a source tree that was never installed
    # NOTE: this string is the distribution name in pyproject.toml. They must match,
    # or every version shown in doctor output and issue reports is wrong.
    __version__ = "0.0.0.dev0"

__all__ = ["__version__"]
