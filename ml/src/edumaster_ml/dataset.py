"""Construcción del dataset a partir de eventos y evidencias, con corte temporal para evitar fuga de información."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import numpy as np

FEATURE_SCHEMA_VERSION = "v1"

TABULAR_FEATURES = [
    "active_days_14d",
    "sessions_14d",
    "completion_ratio",
    "assessment_mean",
    "assessment_trend",
    "late_submissions_30d",
    "content_engagement",
    "skill_evidence_score",
    "days_since_last_activity",
]
SEQ_FEATURES = ["sesiones", "lecciones", "actividades", "puntaje_medio"]
SEQ_WEEKS = 8
MASTERY_LEVELS = ["principiante", "elemental", "intermedio", "avanzado", "experto"]
LEVELS = ["basico", "intermedio", "avanzado"]


@dataclass
class Dataset:
    x_tab: np.ndarray
    level: np.ndarray
    seq: np.ndarray
    y_completion: np.ndarray
    y_mastery: np.ndarray
    y_risk: np.ndarray
    y_rank: np.ndarray
    rank_mask: np.ndarray
    split: np.ndarray
    keys: list[tuple[str, str]]
    skills: list[str]
    meta: dict = field(default_factory=dict)
    quality: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.split)

    def subset(self, name: str) -> dict[str, np.ndarray]:
        idx = self.split == name
        return {k: getattr(self, k)[idx] for k in
                ("x_tab", "level", "seq", "y_completion", "y_mastery", "y_risk", "y_rank", "rank_mask")}

    def checksum(self) -> str:
        h = hashlib.sha256()
        for arr in (self.x_tab, self.level, self.seq, self.y_completion, self.y_mastery, self.y_risk, self.y_rank):
            h.update(np.ascontiguousarray(arr).tobytes())
        return h.hexdigest()


def mastery_bucket(score: float) -> int:
    for i, limit in enumerate((40, 55, 70, 85)):
        if score < limit:
            return i
    return 4


def build_features(enroll_events, attempts, evidence, course, course_skill_idx, n_skills, cutoff):
    """Variables de un par estudiante–curso usando solo información anterior a `cutoff`."""
    past = [e for e in enroll_events if e["occurred_at"] < cutoff]
    if not past:
        return None
    d14, d30 = cutoff - timedelta(days=14), cutoff - timedelta(days=30)
    recent = [e for e in past if e["occurred_at"] >= d14]
    past_attempts = sorted((a for a in attempts if a["submitted_at"] < cutoff), key=lambda a: a["submitted_at"])
    scores = [a["score"] for a in past_attempts]
    past_evidence = [ev["score"] for ev in evidence if ev["observed_at"] < cutoff and ev["skill_id"] in course_skill_idx]
    tab = [
        len({e["occurred_at"].date() for e in recent}),
        len({e["payload"].get("session_id") for e in recent if e["payload"].get("session_id")}),
        sum(e["event_type"] == "activity_completed" for e in past) / max(course["n_activities"], 1),
        (sum(scores) / len(scores) / 100) if scores else 0.0,
        ((scores[-1] - scores[0]) / 100) if len(scores) >= 2 else 0.0,
        sum(1 for a in past_attempts if a["is_late"] and a["submitted_at"] >= d30),
        sum(e["payload"].get("duration_seconds", 0) for e in past
            if e["event_type"] == "lesson_viewed" and e["occurred_at"] >= d30) / 3600,
        (sum(past_evidence) / len(past_evidence)) if past_evidence else 0.0,
        (cutoff - max(e["occurred_at"] for e in past)).total_seconds() / 86400,
    ]
    seq = np.zeros((SEQ_WEEKS, len(SEQ_FEATURES)), dtype=np.float32)
    for w in range(SEQ_WEEKS):
        lo = cutoff - timedelta(weeks=SEQ_WEEKS - w)
        hi = lo + timedelta(weeks=1)
        week = [e for e in past if lo <= e["occurred_at"] < hi]
        week_scores = [a["score"] for a in past_attempts if lo <= a["submitted_at"] < hi]
        seq[w] = [
            len({e["payload"].get("session_id") for e in week if e["payload"].get("session_id")}) / 5,
            sum(e["event_type"] == "lesson_viewed" for e in week) / 10,
            sum(e["event_type"] == "activity_completed" for e in week) / 3,
            (sum(week_scores) / len(week_scores) / 100) if week_scores else 0.0,
        ]
    level = LEVELS.index(course["level"]) if course["level"] in LEVELS else 0
    return np.asarray(tab, dtype=np.float32), level, seq


def build_dataset(records, cutoff_weeks=(5, 6, 7, 8), horizon_days: int = 28) -> Dataset:
    """`records` debe exponer start, courses, skills, course_skills, assessments, enrollments, events, attempts y skill_evidence."""
    start: datetime = records.start
    skills = [s["id"] for s in records.skills]
    skill_index = {sid: i for i, sid in enumerate(skills)}
    courses = {c["id"]: c for c in records.courses}
    course_skills = defaultdict(list)
    for cs in records.course_skills:
        course_skills[cs["course_id"]].append(cs["skill_id"])
    assessment_course = {a["id"]: a["course_id"] for a in records.assessments}
    events_by_enrollment = defaultdict(list)
    seen_ids, duplicates = set(), 0
    for e in records.events:
        if e["event_id"] in seen_ids:
            duplicates += 1
            continue
        seen_ids.add(e["event_id"])
        events_by_enrollment[e["enrollment_id"]].append(e)
    attempts_by_key = defaultdict(list)
    for a in records.attempts:
        attempts_by_key[(a["student_id"], assessment_course.get(a["assessment_id"]))].append(a)
    evidence_by_student = defaultdict(list)
    for ev in records.skill_evidence:
        evidence_by_student[ev["student_id"]].append(ev)

    rows = defaultdict(list)
    split_names = ["train"] * (len(cutoff_weeks) - 2) + ["val", "test"]
    for week, split in zip(cutoff_weeks, split_names):
        cutoff = start + timedelta(weeks=week)
        horizon = cutoff + timedelta(days=horizon_days)
        for enr in records.enrollments:
            completed_at = enr.get("completed_at")
            if enr["enrolled_at"] >= cutoff or (completed_at and completed_at < cutoff):
                continue
            course = courses[enr["course_id"]]
            cskills = {sid for sid in course_skills[course["id"]]}
            events = events_by_enrollment[enr["id"]]
            attempts = attempts_by_key[(enr["student_id"], course["id"])]
            evidence = evidence_by_student[enr["student_id"]]
            feats = build_features(events, attempts, evidence, course, cskills, len(skills), cutoff)
            if feats is None:
                continue
            tab, level, seq = feats
            completed_in_window = bool(completed_at and cutoff <= completed_at < horizon)
            future_events = any(cutoff <= e["occurred_at"] < horizon for e in events)
            future_scores = [a["score"] for a in attempts if cutoff <= a["submitted_at"] < horizon]
            rank, mask = np.zeros(len(skills), np.float32), np.zeros(len(skills), bool)
            for sid in cskills:
                obs = [ev["score"] for ev in evidence if ev["skill_id"] == sid and ev["observed_at"] < horizon]
                gap = 1 - (sum(obs) / len(obs) if obs else 0.0)
                rank[skill_index[sid]] = gap
                mask[skill_index[sid]] = True
            logits = np.where(mask, rank * 5, -np.inf)
            dist = np.exp(logits - logits[mask].max())
            dist = np.where(mask, dist, 0) / dist[mask].sum()
            rows["x_tab"].append(tab)
            rows["level"].append(level)
            rows["seq"].append(seq)
            rows["y_completion"].append(float(completed_in_window))
            rows["y_mastery"].append(mastery_bucket(sum(future_scores) / len(future_scores)) if future_scores else -1)
            rows["y_risk"].append(float(not future_events and not completed_in_window))
            rows["y_rank"].append(dist.astype(np.float32))
            rows["rank_mask"].append(mask)
            rows["split"].append(split)
            rows["keys"].append((enr["student_id"], course["id"]))

    ds = Dataset(
        x_tab=np.stack(rows["x_tab"]).astype(np.float32),
        level=np.asarray(rows["level"], dtype=np.int64),
        seq=np.stack(rows["seq"]).astype(np.float32),
        y_completion=np.asarray(rows["y_completion"], dtype=np.float32),
        y_mastery=np.asarray(rows["y_mastery"], dtype=np.int64),
        y_risk=np.asarray(rows["y_risk"], dtype=np.float32),
        y_rank=np.stack(rows["y_rank"]).astype(np.float32),
        rank_mask=np.stack(rows["rank_mask"]),
        split=np.asarray(rows["split"]),
        keys=rows["keys"],
        skills=skills,
    )
    ds.meta = {
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "tabular_features": TABULAR_FEATURES,
        "sequence_features": SEQ_FEATURES,
        "sequence_weeks": SEQ_WEEKS,
        "levels": LEVELS,
        "mastery_levels": MASTERY_LEVELS,
        "cutoff_weeks": list(cutoff_weeks),
        "horizon_days": horizon_days,
        "split_strategy": "temporal: train=cortes iniciales, val=penúltimo corte, test=último corte",
    }
    ds.quality = quality_report(ds, duplicates)
    return ds


def quality_report(ds: Dataset, duplicates_removed: int) -> dict:
    finite = bool(np.isfinite(ds.x_tab).all() and np.isfinite(ds.seq).all())
    in_range = bool((ds.x_tab[:, 2] >= 0).all() and (ds.x_tab[:, 2] <= 1.5).all() and (ds.x_tab[:, 3] <= 1).all())
    balance = {}
    for split in ("train", "val", "test"):
        idx = ds.split == split
        balance[split] = {
            "filas": int(idx.sum()),
            "tasa_finalizacion": round(float(ds.y_completion[idx].mean()), 3) if idx.any() else None,
            "tasa_riesgo": round(float(ds.y_risk[idx].mean()), 3) if idx.any() else None,
            "con_etiqueta_dominio": int((ds.y_mastery[idx] >= 0).sum()),
        }
    return {
        "completitud_campos_criticos": 1.0 if finite else 0.0,
        "duplicados_eliminados": duplicates_removed,
        "rangos_validos": in_range,
        "balance": balance,
        "aprobado": finite and in_range,
    }


def datasheet(ds: Dataset, source: str) -> dict:
    return {
        "nombre": "edumaster-learning-snapshots",
        "fuente": source,
        "es_demo": source == "demo",
        "advertencia": "Dataset sintético DEMO: sirve para probar el pipeline, no para afirmar validez educativa."
        if source == "demo" else "Dataset institucional autorizado.",
        "filas": len(ds),
        "checksum_sha256": ds.checksum(),
        **ds.meta,
        "calidad": ds.quality,
    }


def to_json(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
