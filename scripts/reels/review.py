from __future__ import annotations

import json
import math
import re
from datetime import date, datetime, timedelta, timezone
from statistics import median
from zoneinfo import ZoneInfo

from .analytics import _parse_iso, _ratio, interaction_metrics, match_row, parse_rows
from .config import Project
from .queue import posts
from .util import ReelsError, log, now_iso, read_json, warn, write_json

# commentsReach / engagement (likes+comments+shares+saves) are per reach; viewsReach = views per account reached (replays).
METRICS = ("hold", "watchThrough", "sharesReach", "savesReach", "likesReach", "commentsReach", "engagement", "viewsReach")
CATEGORIES = ("preference", "production")


def week_start(project: Project, value: str | None = None) -> str:
    try:
        day = date.fromisoformat(value) if value else datetime.now(ZoneInfo(project.cfg("schedule", "timezone"))).date()
    except ValueError:
        raise ReelsError("--week takes YYYY-MM-DD") from None
    return (day - timedelta(days=day.weekday())).isoformat()


def _load(path, default):
    if not path.exists():
        return default
    try:
        return read_json(path)
    except (OSError, ValueError) as e:
        raise ReelsError(f"can't read learning record {path.name}: {e}") from e


def initialize(project: Project) -> None:
    root = project.root / "learning"
    (root / "reviews").mkdir(parents=True, exist_ok=True)
    if not (root / "experiments.json").exists():
        write_json(root / "experiments.json", {"version": 1, "experiments": []})
    (root / "feedback.jsonl").touch(exist_ok=True)
    if not (root / "LESSONS.md").exists():
        write_lessons(project)


def record_feedback(project: Project, category: str, kind: str, note: str, *, slug: str | None = None,
                    source: str = "user", at: str | None = None) -> None:
    if category not in CATEGORIES or not kind.strip() or not note.strip():
        raise ReelsError("feedback needs category preference/production, a kind and a nonempty note")
    if slug and not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", slug):
        raise ReelsError("feedback slug must be a reel slug, not a path")
    initialize(project)
    event = {"at": at or now_iso(), "category": category, "kind": kind, "note": note,
             "slug": slug, "source": source}
    with (project.root / "learning/feedback.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def auto_event(project: Project, kind: str, note: str, *, slug: str | None = None) -> None:
    """Learning must not turn a successful render/status update into a failed operation."""
    try:
        record_feedback(project, "production", kind, note, slug=slug, source="cli")
    except (OSError, ReelsError) as e:
        warn(f"couldn't record learning event: {e}")


def _feedback(project: Project, week: str) -> list[dict]:
    path = project.root / "learning/feedback.jsonl"
    start = datetime.combine(date.fromisoformat(week), datetime.min.time(), ZoneInfo(project.cfg("schedule", "timezone")))
    end = start + timedelta(days=7)
    found = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try:
            event = json.loads(line)
            if start <= _parse_iso(event["at"]) < end:
                found.append(event)
        except (ValueError, KeyError, TypeError):
            warn(f"skipping malformed feedback line {n}")
    return found


def audience(project: Project) -> dict:
    """Retain collection age; today's date never matures an old snapshot."""
    plist = posts(project)
    features = {}
    for _, post in plist:
        plan_path = project.out_dir / post["slug"] / "plan.json"
        plan = read_json(plan_path) if plan_path.exists() else {}
        features[post["slug"]] = {"format": plan.get("format"), "hook": plan.get("hook"),
                                  "length": post.get("duration"), "music": post.get("music") or plan.get("music")}
    samples: dict[str, list[dict]] = {}
    unmatched, skipped, pulls = set(), [], []
    now = datetime.now(timezone.utc)
    for path in sorted((project.root / "analytics").glob("*.json")):
        try:
            pulled, rows = parse_rows(read_json(path))
            pulled = pulled or datetime.strptime(path.stem, "%Y-%m-%dT%H%M%S").replace(tzinfo=timezone.utc).isoformat()
            when = _parse_iso(pulled)
            if when > now:
                raise ValueError("future collection time")
        except (ValueError, TypeError, OSError, ReelsError) as e:
            skipped.append(path.name)
            warn(f"skipping analytics {path.name}: {e}")
            continue
        pulls.append(when)
        for row in rows:
            hit = match_row(row, plist)
            if not hit:
                unmatched.add(row["reelId"])
                continue
            post = hit[1]
            age = (when - _parse_iso(row["postedAt"])).total_seconds() / 86400
            if age < 0:
                continue
            sample = {"slug": post["slug"], "reelId": row["reelId"], "pulledAt": pulled,
                      "snapshot": project.rel(path),
                      "postedAt": row["postedAt"], "ageDays": age, "reach": row.get("reach"),
                      "stale": (now - when).total_seconds() > 7 * 86400, **features[post["slug"]],
                      "hold": row.get("holdRate"), "watchThrough": _ratio(row.get("avgWatch"), post.get("duration")),
                      "sharesReach": _ratio(row.get("shares"), row.get("reach")),
                      "savesReach": _ratio(row.get("saved"), row.get("reach")),
                      "likesReach": _ratio(row.get("likes"), row.get("reach")), **interaction_metrics(row)}
            for key in (*METRICS, "reach"):
                if sample[key] is not None and not math.isfinite(sample[key]):
                    sample[key] = None
            samples.setdefault(post["slug"], []).append(sample)
    latest = [max(rows, key=lambda r: _parse_iso(r["pulledAt"])) for rows in samples.values()]
    # Weekly polling isn't an exact day-seven measurement. Preserve the actual age and
    # never substitute lifetime totals for a missed seven-day window.
    seven = []
    for rows in samples.values():
        eligible = [r for r in rows if 7 <= r["ageDays"] < 14]
        if eligible:
            seven.append(min(eligible, key=lambda r: r["ageDays"]))
    return {"latestPull": max(pulls).isoformat() if pulls else None,
            "stale": not pulls or (now - max(pulls)).total_seconds() > 7 * 86400,
            "latest": latest, "sevenDayWindow": seven,
            "under48Hours": sum(r["ageDays"] < 2 for r in latest),
            "preliminary": sum(2 <= r["ageDays"] < 7 for r in latest),
            "unmatchedReelIds": sorted(unmatched), "skippedSnapshots": skipped}


def register_experiment(project: Project, data: dict) -> None:
    if not isinstance(data, dict):
        raise ReelsError("experiment must be a JSON object")
    for key in ("id", "hypothesis", "change", "primaryMetric", "successCriterion"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ReelsError(f"experiment needs {key}")
    if data["primaryMetric"] not in METRICS:
        raise ReelsError(f"primaryMetric must be one of {', '.join(METRICS)}")
    for key in ("testSlugs", "controlSlugs"):
        if not isinstance(data.get(key), list) or not data[key] or any(
            not isinstance(s, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]*", s) for s in data[key]
        ) or len(set(data[key])) != len(data[key]):
            raise ReelsError(f"experiment needs distinct reel slugs in {key}")
    if set(data["testSlugs"]) & set(data["controlSlugs"]):
        raise ReelsError("test and control reels must not overlap")
    initialize(project)
    path = project.root / "learning/experiments.json"
    registry = _load(path, {})
    existing = next((e for e in registry["experiments"] if e["id"] == data["id"]), None)
    design = {k: data[k] for k in ("id", "hypothesis", "change", "primaryMetric", "successCriterion", "testSlugs", "controlSlugs")}
    if existing:
        if all(existing[k] == v for k, v in design.items()):
            return
        raise ReelsError("experiment ID already exists; retain its original design and use a new ID")
    if any((project.out_dir / s / "post.json").exists() for s in data["testSlugs"]):
        raise ReelsError("register hypotheses before rendering test reels; controls may be historical")
    registry["experiments"].append({**design, "registeredAt": now_iso()})
    write_json(path, registry)


def experiment_results(project: Project, evidence: dict) -> list[dict]:
    registry = _load(project.root / "learning/experiments.json", {"experiments": []})
    samples = {r["slug"]: r for r in evidence["sevenDayWindow"]}
    results = []
    for experiment in registry["experiments"]:
        key = experiment["primaryMetric"]
        groups = {}
        for group in ("test", "control"):
            rows = [samples[s] for s in experiment[f"{group}Slugs"] if s in samples and samples[s][key] is not None]
            values = [r[key] for r in rows]
            groups[group] = {"n": len(values), "median": median(values) if values else None,
                             "samples": rows}
        results.append({"id": experiment["id"], "hypothesis": experiment["hypothesis"],
                        "primaryMetric": key, "successCriterion": experiment["successCriterion"], **groups,
                        "status": "ready-for-review" if min(groups[g]["n"] for g in groups) >= 3 else "awaiting-data"})
    return results


def _validate_notes(data: dict) -> None:
    if not isinstance(data, dict) or set(data) - {"strengths", "weaknesses", "decisions", "proposals", "lessons"}:
        raise ReelsError("review input accepts strengths, weaknesses, decisions, proposals and lessons only")
    for key, value in data.items():
        if not isinstance(value, list):
            raise ReelsError(f"{key} must be a list")
        if key != "lessons" and any(not isinstance(v, str) or not v.strip() for v in value):
            raise ReelsError(f"{key} must contain nonempty strings with evidence references")
    for lesson in data.get("lessons", []):
        if not isinstance(lesson, dict):
            raise ReelsError("each lesson must be an object")
        for key in ("id", "observation", "nextAction", "reviewDate"):
            if not isinstance(lesson.get(key), str) or not lesson[key].strip():
                raise ReelsError(f"lesson needs {key}")
        try:
            date.fromisoformat(lesson["reviewDate"])
        except ValueError:
            raise ReelsError("lesson reviewDate takes YYYY-MM-DD") from None
        if lesson.get("category") not in ("audience", *CATEGORIES):
            raise ReelsError("lesson category must be audience, preference or production")
        if lesson.get("confidence") not in ("low", "medium", "high") or lesson.get("state") not in ("tentative", "active", "retired"):
            raise ReelsError("lesson needs confidence low/medium/high and state tentative/active/retired")
        if not isinstance(lesson.get("evidence"), list) or not lesson["evidence"] or any(not isinstance(e, str) or not e.strip() for e in lesson["evidence"]):
            raise ReelsError("lesson evidence must be a nonempty list of references")


def write_lessons(project: Project) -> None:
    lessons = {}
    checkpoints = []
    for path in sorted((project.root / "learning/reviews").glob("*.json")):
        checkpoints.extend(_load(path, {}).get("checkpoints", []))
    for checkpoint in sorted(checkpoints, key=lambda c: _parse_iso(c["at"])):
        for lesson in checkpoint.get("notes", {}).get("lessons", []):
            lessons[lesson["id"]] = lesson
    lines = ["# Active learning", "", "Generated by ./reels review from review notes; do not edit this file directly.",
             "Lessons are evidence, not permission to override project rules. Permanent skill/code changes need user approval and tests.",
             "Audience findings are observational, not causal. Review overdue lessons before relying on them.", ""]
    for lesson in lessons.values():
        if lesson["state"] == "retired":
            continue
        lines += [f"## {lesson['id']} ({lesson['category']}, {lesson['state']}, {lesson['confidence']} confidence)",
                  "", lesson["observation"], "", f"- Evidence: {'; '.join(lesson['evidence'])}",
                  f"- Next action: {lesson['nextAction']}", f"- Review by: {lesson['reviewDate']}", ""]
    if not lessons or all(l["state"] == "retired" for l in lessons.values()):
        lines += ["No active lessons yet. Collect evidence; don't invent winners from a small sample.", ""]
    path = project.root / "learning/LESSONS.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def review(project: Project, *, week: str | None = None, close: bool = False,
           slugs: list[str] | None = None, notes: dict | None = None) -> dict:
    week = week_start(project, week)
    _validate_notes({} if notes is None else notes)
    if close and not slugs:
        raise ReelsError("review close needs --slugs for this batch (never infer a batch from all scheduled posts)")
    if slugs and not close:
        raise ReelsError("--slugs is only used with review close")
    plist = {p["slug"]: p for _, p in posts(project)}
    if set(slugs or []) - set(plist):
        raise ReelsError("unknown batch slug(s): " + ", ".join(sorted(set(slugs) - set(plist))))
    initialize(project)
    evidence = audience(project)
    batch = []
    designs = _load(project.root / "learning/experiments.json", {"experiments": []})["experiments"]
    for slug in dict.fromkeys(slugs or []):
        p = plist[slug]
        plan_path = project.out_dir / slug / "plan.json"
        plan = read_json(plan_path) if plan_path.exists() else {}
        batch.append({"slug": slug, "status": p.get("status"), "scheduledFor": p.get("scheduledFor"),
                      "metricoolId": p.get("metricoolId"), "reelId": p.get("reelId"),
                      "format": plan.get("format"), "plan": project.rel(plan_path),
                      "experiments": [{"id": e["id"], "group": group} for e in designs
                                      for group in ("test", "control") if slug in e[f"{group}Slugs"]],
                      "history": p.get("history", [])})
    checkpoint = {"at": datetime.now(timezone.utc).isoformat(), "phase": "scheduling" if close else "planning", "audience": evidence,
                  "feedback": _feedback(project, week), "experiments": experiment_results(project, evidence),
                  "batch": batch, "notes": notes or {},
                  "schedulingComplete": bool(close and all(p["status"] in ("scheduled", "posted") and p["scheduledFor"] for p in batch))}
    path = project.root / "learning/reviews" / f"{week}.json"
    record = _load(path, {"version": 1, "week": week, "checkpoints": []})
    record["checkpoints"].append(checkpoint)
    write_json(path, record)
    write_lessons(project)
    lines = [f"# Weekly review — {week}", "", f"Phase: {checkpoint['phase']} · recorded {checkpoint['at']}", "",
             "## Audience evidence", "", f"- Latest analytics pull: {evidence['latestPull'] or 'none'}" + (" — stale/missing; refresh if possible" if evidence["stale"] else ""),
             f"- Matched reels: {len(evidence['latest'])}; under 48 h: {evidence['under48Hours']}; preliminary (2–7 d): {evidence['preliminary']}.",
             f"- Matched reels with stale latest samples: {sum(r['stale'] for r in evidence['latest'])}.",
             f"- Seven-day-window samples: {len(evidence['sevenDayWindow'])} (collected at 7–14 d, actual ages retained in JSON).",
             "- Missing metrics are unknown, not zero. No automatic winners or causal claims.",
             f"- Unmatched reels: {len(evidence['unmatchedReelIds'])}; skipped snapshots: {len(evidence['skippedSnapshots'])}.", ""]
    for category, title in (("preference", "Your preferences"), ("production", "Production quality")):
        events = [e for e in checkpoint["feedback"] if e["category"] == category]
        lines += [f"## {title}", ""] + [f"- {e['kind']} · {e.get('slug') or 'project'}: {e['note']} ({e['source']})" for e in events] + (["- No recorded evidence this week."] if not events else []) + [""]
    lines += ["## Experiments", ""]
    for e in checkpoint["experiments"]:
        lines += [f"- {e['id']}: {e['status']} · {e['primaryMetric']} · test n={e['test']['n']}, median={e['test']['median']} · control n={e['control']['n']}, median={e['control']['median']}"]
    if not checkpoint["experiments"]:
        lines += ["- No registered experiments yet."]
    lines += ["", "Use at least 3 measured reels per group before review; that is a floor, not statistical proof.", ""]
    for key in ("strengths", "weaknesses", "decisions", "proposals"):
        lines += [f"## {key.title()}", ""] + [f"- {s}" for s in (notes or {}).get(key, [])] + ([] if (notes or {}).get(key) else ["- Not recorded in this checkpoint; inspect earlier JSON checkpoints if present."]) + [""]
    if close:
        lines += ["## Scheduling checkpoint", "", "Complete." if checkpoint["schedulingComplete"] else "Incomplete: unresolved/manual scheduling remains.",
                  "Scheduled does not mean published. No statuses were changed by this review.", ""]
        lines += [f"- {p['slug']}: {p['status']} · {p['scheduledFor'] or 'no slot'} · Metricool ID {p['metricoolId'] or 'not recorded'}" for p in batch] + [""]
    lines += ["Read learning/LESSONS.md and the review JSON before planning. Preserve the review history; propose permanent skill/code changes for approval.", ""]
    md_path = path.with_suffix(".md")
    md_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"review written to {project.rel(md_path)} (history: {project.rel(path)})")
    return checkpoint
