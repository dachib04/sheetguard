"""Static HTML report (FR-41, FR-42).

* Jinja2 autoescaping is always on: every string from the workbook is
  HTML-escaped, so the report cannot become an XSS vector.
* Defence in depth: a Content-Security-Policy meta tag forbids all scripts,
  remote resources and frames, so even an escaping bug could not run code.
* No JavaScript, no external fonts, styles or images.
"""

from __future__ import annotations

from jinja2 import Environment, PackageLoader, StrictUndefined

from sheetguard.checks import REGISTRY
from sheetguard.report.schema import Report
from sheetguard.security.envelope import UntrustedText

_env = Environment(
    loader=PackageLoader("sheetguard.report", "templates"),
    autoescape=True,
    undefined=StrictUndefined,
    trim_blocks=True,
    lstrip_blocks=True,
)
_env.tests["untrusted"] = lambda v: isinstance(v, UntrustedText)


def to_html(report: Report, scanner_enabled: bool = True) -> str:
    template = _env.get_template("report.html.j2")
    titles = {cid: cls.title for cid, cls in REGISTRY.items()}
    return str(template.render(r=report, titles=titles, scanner_enabled=scanner_enabled))
