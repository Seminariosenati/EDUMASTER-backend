import time
import uuid

from tests.conftest import auth

API = "/api/v1"


def test_health(client):
    assert client.get(f"{API}/health").json()["status"] == "ok"


def test_login_rejects_bad_password(client):
    r = client.post(f"{API}/auth/login", json={"email": "admin@edumaster.demo", "password": "incorrecta"})
    assert r.status_code == 401
    assert r.json()["code"] == "http_401" and r.json()["correlation_id"]


def test_requires_token(client):
    assert client.get(f"{API}/courses").status_code == 401


def test_student_cannot_manage_users(client, tokens):
    assert client.get(f"{API}/users", headers=auth(tokens["estudiante"])).status_code == 403


def test_tenant_isolation(client, tokens):
    courses = client.get(f"{API}/courses", headers=auth(tokens["admin_aliada"])).json()
    assert courses["total"] == 0
    demo_course = client.get(f"{API}/courses", headers=auth(tokens["admin"])).json()["items"][0]
    r = client.get(f"{API}/courses/{demo_course['id']}", headers=auth(tokens["admin_aliada"]))
    assert r.status_code == 404


def test_instructor_sees_only_assigned_courses(client, tokens):
    mine = client.get(f"{API}/courses", headers=auth(tokens["docente"])).json()
    everything = client.get(f"{API}/courses", headers=auth(tokens["admin"])).json()
    assert 0 < mine["total"] < everything["total"]
    others = {c["id"] for c in everything["items"]} - {c["id"] for c in mine["items"]}
    r = client.get(f"{API}/courses/{next(iter(others))}/students", headers=auth(tokens["docente"]))
    assert r.status_code == 403


def test_student_only_sees_own_progress(client, tokens):
    me = client.get(f"{API}/auth/me", headers=auth(tokens["estudiante"])).json()
    assert client.get(f"{API}/students/{me['student_id']}/progress", headers=auth(tokens["estudiante"])).status_code == 200
    other = client.get(f"{API}/students?limit=2", headers=auth(tokens["admin"])).json()["items"]
    other_id = next(s["id"] for s in other if s["id"] != me["student_id"])
    assert client.get(f"{API}/students/{other_id}/progress", headers=auth(tokens["estudiante"])).status_code == 403


def test_ml_engineer_gets_pseudonymized_students(client, tokens):
    items = client.get(f"{API}/students?limit=5", headers=auth(tokens["ml"])).json()["items"]
    assert items and all(s["full_name"] == "Seudónimo" for s in items)


def test_learning_event_is_idempotent(client, tokens):
    h = auth(tokens["estudiante"])
    course = next(c for c in client.get(f"{API}/courses?mine=true", headers=h).json()["items"]
                  if c["my_enrollment"] and c["my_enrollment"]["status"] != "completado")
    body = {"event_id": str(uuid.uuid4()), "event_type": "activity_completed", "course_id": course["id"],
            "payload": {"duration_seconds": 600}}
    first = client.post(f"{API}/learning-events", json=body, headers=h).json()
    second = client.post(f"{API}/learning-events", json=body, headers=h).json()
    assert first["created"] is True and second["created"] is False
    assert first["progress"] == second["progress"]


def test_event_validation(client, tokens):
    h = auth(tokens["estudiante"])
    course = client.get(f"{API}/courses?mine=true", headers=h).json()["items"][0]
    r = client.post(f"{API}/learning-events", json={"event_type": "hackeo", "course_id": course["id"]}, headers=h)
    assert r.status_code == 422
    r = client.post(f"{API}/learning-events", json={"event_type": "lesson_viewed", "course_id": course["id"],
                                                    "payload": {"duration_seconds": 999999}}, headers=h)
    assert r.status_code == 422


def test_grade_completion_and_certificate_flow(client, tokens):
    admin, h = auth(tokens["admin"]), auth(tokens["docente"])
    skill = client.get(f"{API}/skills", headers=admin).json()[0]
    instructor = next(u for u in client.get(f"{API}/users?role=docente", headers=admin).json()
                      if u["email"] == "docente@edumaster.demo")
    code = f"TST{uuid.uuid4().hex[:5].upper()}"
    course = client.post(f"{API}/courses", headers=admin, json={
        "code": code, "title": "Curso de prueba", "level": "basico", "instructor_id": instructor["id"],
        "n_activities": 2, "skills": {skill["id"]: 1.0}}).json()
    assert client.patch(f"{API}/courses/{course['id']}", headers=admin, json={"status": "publicado"}).status_code == 200
    student = client.get(f"{API}/students?limit=1", headers=admin).json()["items"][0]
    assert client.post(f"{API}/enrollments", headers=admin,
                       json={"student_id": student["id"], "course_id": course["id"]}).status_code == 201
    a = []
    for i in range(2):
        r = client.post(f"{API}/assessments", headers=h, json={
            "course_id": course["id"], "title": f"Eval {i}", "type": "practica", "due_at": "2030-01-01T00:00:00Z"})
        assert r.status_code == 201, r.text
        a.append(r.json()["id"])
    for aid in a:
        assert client.post(f"{API}/assessments/{aid}/grades", headers=h,
                           json={"student_id": student["id"], "score": 90}).status_code == 201
    over = client.post(f"{API}/assessments/{a[0]}/grades", headers=h, json={"student_id": student["id"], "score": 150})
    assert over.status_code == 422
    for _ in range(2):
        client.post(f"{API}/learning-events", headers=h, json={
            "event_type": "activity_completed", "course_id": course["id"], "student_id": student["id"]})
    progress = client.get(f"{API}/students/{student['id']}/progress", headers=admin).json()
    status = next(c["status"] for c in progress["courses"] if c["course_id"] == course["id"])
    assert status == "completado"
    cert = next(c for c in client.get(f"{API}/certificates?limit=200", headers=admin).json()
                if c["course"]["id"] == course["id"])
    public = client.get(f"{API}/certificates/verify/{cert['verification_code']}").json()
    assert public["valid"] is True and "student" not in public and public["holder"].endswith(".")
    dup = client.post(f"{API}/certificates/issue", headers=admin, json={"student_id": student["id"], "course_id": course["id"]})
    assert dup.status_code == 409


def test_dashboard_and_analytics(client, tokens):
    h = auth(tokens["admin"])
    summary = client.get(f"{API}/dashboard/summary", headers=h).json()
    assert {k["key"] for k in summary["kpis"]} >= {"active_students", "courses", "learning_hours", "certificates"}
    assert summary["is_demo"] is True
    assert len(client.get(f"{API}/analytics/progress?weeks=8", headers=h).json()) == 8
    skills = client.get(f"{API}/analytics/skill-mastery", headers=h).json()
    assert skills["levels"][0] == "principiante"
    csv = client.get(f"{API}/analytics/export?kind=progress", headers=h)
    assert csv.status_code == 200 and "semana" in csv.text
    assert client.get(f"{API}/analytics/export", headers=auth(tokens["estudiante"])).status_code == 403


def test_audit_is_restricted(client, tokens):
    assert client.get(f"{API}/audit-logs", headers=auth(tokens["auditor"])).status_code == 200
    assert client.get(f"{API}/audit-logs", headers=auth(tokens["docente"])).status_code == 403


def test_architecture_preview(client, tokens):
    small = client.post(f"{API}/ml/architecture/preview", headers=auth(tokens["ml"]), json={"tab_hidden": [16]}).json()
    big = client.post(f"{API}/ml/architecture/preview", headers=auth(tokens["ml"]), json={"tab_hidden": [256, 128]}).json()
    assert big["total_params"] > small["total_params"]
    assert big["heaviest_dense_layer"] == "densa_2"


def test_training_lifecycle_and_predictions(client, tokens):
    ml, admin = auth(tokens["ml"]), auth(tokens["admin"])
    assert client.post(f"{API}/ml/training-runs", headers=auth(tokens["docente"]), json={}).status_code == 403
    run = client.post(f"{API}/ml/training-runs", headers=ml, json={"epochs": 4, "tab_hidden": [32, 16]})
    assert run.status_code == 202, run.text
    run_id = run.json()["id"]
    for _ in range(120):
        state = client.get(f"{API}/ml/training-runs/{run_id}", headers=ml).json()
        if state["status"] in ("completado", "error"):
            break
        time.sleep(0.5)
    assert state["status"] == "completado", state.get("error")
    assert state["metrics"]["test"]["finalizacion"]["n"] > 0 and "baseline" in state["metrics"]
    version = client.get(f"{API}/ml/models", headers=ml).json()[0]
    assert version["stage"] == "experimental"
    r = client.post(f"{API}/ml/models/{version['id']}/promote", headers=ml,
                    json={"justification": "prueba automatizada", "force": True})
    assert r.status_code == 200 and r.json()["stage"] == "produccion"
    me = client.get(f"{API}/auth/me", headers=auth(tokens["estudiante"])).json()
    pred = client.post(f"{API}/ml/predictions", headers=auth(tokens["estudiante"]), json={"student_id": me["student_id"]}).json()
    if pred["predictions"]:
        p = pred["predictions"][0]
        assert 0 <= p["completion_probability"] <= 1 and "fusion" in p["activations"]
    gen = client.post(f"{API}/recommendations/generate", headers=admin, json={}).json()
    assert gen["students"] > 0
    recs = client.get(f"{API}/recommendations?limit=5", headers=admin).json()
    if recs:
        fb = client.post(f"{API}/recommendations/{recs[0]['id']}/feedback", headers=admin, json={"useful": True})
        assert fb.status_code == 200 and fb.json()["useful"] is True
