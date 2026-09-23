"""Measured checks and the quality score. Owned by WS4.

INTERFACE PINNED BY THE FOUNDATION. WS6 (the loop, autofix, the final
verdict) codes against these signatures; WS4 replaces the bodies with the
spec and rubric checks described in plan.json (score, areas, fixes):

    evaluate(facts, spec=None, motion=None, look=None, previous=None) -> QAReport
    checklist_text(report, tier, limit=12) -> str
    report_markdown(report, spec=None, manifest=None) -> str

A check is a dict: {id, area, level: error|warning|info, object, measured,
expected, passed, fix: {tool, arguments} | None, hint}. Every fix names a
tool and arguments that validate against registry.check_args (the live
schema, or tools/contract.json while the tool's owner has not landed).
"""

from dataclasses import asdict, dataclass, field

AREAS = ("presence", "scale", "placement", "materials", "lighting", "camera", "motion", "render")


@dataclass
class QAReport:
    """The verdict on one scene state.

    score      0-100, or None when nothing was measured
    areas      {area: 0-100}
    checks     every check evaluated, passed or not
    blocking   ids of failing checks that must be fixed before finishing
    resolved   ids failing in `previous` that pass now
    new        ids failing now that were not failing in `previous`
    regressed  ids that passed in `previous` and fail now
    delta      score - previous score, or None
    """

    score: float | None = None
    areas: dict = field(default_factory=dict)
    checks: list = field(default_factory=list)
    blocking: list = field(default_factory=list)
    resolved: list = field(default_factory=list)
    new: list = field(default_factory=list)
    regressed: list = field(default_factory=list)
    delta: float | None = None

    @property
    def failing(self):
        return [check for check in self.checks if not check.get("passed")]

    @property
    def passed(self):
        return sum(1 for check in self.checks if check.get("passed"))

    @property
    def total(self):
        return len(self.checks)

    def to_dict(self):
        data = asdict(self)
        data.update(passed=self.passed, total=self.total, failing=self.failing)
        return data


def _failing_ids(report):
    return {check.get("id") for check in (report.failing if report else [])}


def _passing_ids(report):
    return {check.get("id") for check in (report.checks if report else []) if check.get("passed")}


def evaluate(facts, spec=None, motion=None, look=None, previous=None):
    """Run every check that applies and score the scene.

    Placeholder: no checks yet, so the score is None (unmeasured) and only
    the comparison with `previous` is filled. WS4 adds the spec checks and
    the rubric (duplicates, floating, exposure, framing, motion pops...).
    """
    report = QAReport()
    failing, before = _failing_ids(report), _failing_ids(previous)
    report.resolved = sorted(before - failing)
    report.new = sorted(failing - before)
    report.regressed = sorted(failing & _passing_ids(previous))
    if previous is not None and previous.score is not None and report.score is not None:
        report.delta = report.score - previous.score
    return report


def checklist_text(report, tier, limit=12):
    """The failing checks as short lines for the model, most severe first."""
    order = {"error": 0, "warning": 1, "info": 2}
    failing = sorted(report.failing, key=_severity(order))
    if not failing:
        score = "unmeasured" if report.score is None else f"{report.score:.0f}/100"
        return f"QA {score}: no failing checks."
    lines = [f"QA {report.score if report.score is not None else 'unmeasured'}: {len(failing)} failing"]
    for check in failing[: max(1, int(limit))]:
        line = f"- [{check.get('level', 'warning')}] {check.get('id')}"
        if check.get("object"):
            line += f" {check['object']}"
        if "measured" in check or "expected" in check:
            line += f": {check.get('measured')} (want {check.get('expected')})"
        fix = check.get("fix")
        if fix:
            line += f" -> {fix.get('tool')}"
        elif check.get("hint"):
            line += f" -> {check['hint']}"
        lines.append(line)
    if len(failing) > limit:
        lines.append(f"... {len(failing) - limit} more")
    return "\n".join(lines)


def _severity(order):
    def key(check):
        return order.get(check.get("level"), 3)

    return key


def report_markdown(report, spec=None, manifest=None):
    """report.md for the run folder: the verdict from data, not model prose."""
    title = (spec or {}).get("title") or "Astra scene"
    score = "unmeasured" if report.score is None else f"{report.score:.0f}/100"
    lines = [f"# {title}", "", f"**Quality score:** {score} ({report.passed}/{report.total} checks pass)", ""]
    if report.areas:
        lines += ["| Area | Score |", "|---|---|"]
        lines += [f"| {area} | {value:.0f} |" for area, value in report.areas.items()]
        lines.append("")
    if report.failing:
        lines.append("## Open issues")
        lines += [f"- {check.get('id')}: {check.get('hint') or check.get('expected', '')}" for check in report.failing]
    if manifest:
        lines += ["", "## Files", *[f"- {name}" for name in manifest.get("files", [])]]
    return "\n".join(lines) + "\n"
