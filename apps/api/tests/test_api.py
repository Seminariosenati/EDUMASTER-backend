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


def test_live_inference_event(client, tokens):
    from app.services import live
    me = client.get(f"{API}/auth/me", headers=auth(tokens["admin"])).json()
    event = live.run_once(me["organization_id"])
    assert event and event["event_type"] == "model.inference.live"
    p = event["payload"]
    assert "estudiante" not in p["audience_roles"]
    assert 0 <= p["completion_probability"] <= 1 and p["student_code"].startswith("DEMO-")
    assert {"entrada", "fusion"} <= set(p["activations"])


def test_evaluation_bank_submit_and_grade(client, tokens):
    st, doc = auth(tokens["estudiante"]), auth(tokens["docente"])
    evals = client.get(f"{API}/evaluations", headers=st).json()
    target = next(e for e in evals if e["questions"] > 0)
    detail = client.get(f"{API}/evaluations/{target['id']}", headers=st).json()
    assert all("answer" not in q for q in detail["questions"])
    incomplete = client.post(f"{API}/evaluations/{target['id']}/submit", headers=st, json={"answers": {}})
    assert incomplete.status_code == 422
    answers = {q["id"]: (0 if q["options"] else "respuesta") for q in detail["questions"]}
    if detail["remaining_attempts"] == 0:
        assert client.post(f"{API}/evaluations/{target['id']}/submit", headers=st, json={"answers": answers}).status_code == 409
        return
    r = client.post(f"{API}/evaluations/{target['id']}/submit", headers=st, json={"answers": answers})
    assert r.status_code == 201, r.text
    body = r.json()
    assert 0 <= body["score"] <= 100 and body["total"] == len(detail["questions"])
    assert all("expected" in d for d in body["detail"])
    assert client.post(f"{API}/evaluations/{target['id']}/questions", headers=st,
                       json={"type": "verdadero_falso", "prompt": "Pregunta de prueba", "correct": 0}).status_code == 403


def test_teacher_manages_question_bank(client, tokens):
    doc = auth(tokens["docente"])
    ev = client.get(f"{API}/evaluations", headers=doc).json()[0]
    bad = client.post(f"{API}/evaluations/{ev['id']}/questions", headers=doc,
                      json={"type": "opcion_multiple", "prompt": "¿Cuál es correcta?", "options": ["A"], "correct": 0})
    assert bad.status_code == 422
    q = client.post(f"{API}/evaluations/{ev['id']}/questions", headers=doc, json={
        "type": "respuesta_corta", "prompt": "¿Qué comando lista archivos?", "accepted": ["ls"]}).json()
    staff_view = client.get(f"{API}/evaluations/{ev['id']}", headers=doc).json()
    assert any(x["id"] == q["id"] and x["answer"]["accepted"] == ["ls"] for x in staff_view["questions"])
    assert client.delete(f"{API}/evaluations/questions/{q['id']}", headers=doc).status_code == 204


def test_only_student_enrolls_from_route_recommendation(client, tokens):
    admin, st = auth(tokens["admin"]), auth(tokens["estudiante"])
    me = client.get(f"{API}/auth/me", headers=st).json()
    client.post(f"{API}/recommendations/generate", headers=admin, json={})
    recs = client.get(f"{API}/recommendations?status=nueva&limit=1000", headers=admin).json()
    staff_route = next((r for r in recs if r["type"] == "ruta" and r["student"]["id"] != me["student_id"]), None)
    if staff_route:
        res = client.post(f"{API}/recommendations/{staff_route['id']}/feedback", headers=admin, json={"status": "aceptada"}).json()
        assert "matricul" not in res["action"] and res["enrolled"] is False
    own = next((r for r in recs if r["type"] == "ruta" and r["student"]["id"] == me["student_id"] and not r["enrolled"]), None)
    if own:
        assert client.post(f"{API}/recommendations/{own['id']}/feedback", headers=admin, json={"status": "aceptada"}).json()["enrolled"] is False
        res = client.post(f"{API}/recommendations/{own['id']}/feedback", headers=st, json={"status": "aceptada"}).json()
        assert res["enrolled"] is True and "inscribiste" in res["action"]
    m = client.get(f"{API}/recommendations/metrics", headers=admin).json()
    assert m["total"] > 0 and set(m["por_tipo"]) == {"acompanamiento", "completar", "ruta", "refuerzo"}
    assert client.get(f"{API}/recommendations/metrics", headers=st).status_code == 403


def test_dashboard_filters_by_course(client, tokens):
    admin = auth(tokens["admin"])
    course = client.get(f"{API}/courses?limit=1", headers=admin).json()["items"][0]
    allc = client.get(f"{API}/analytics/completion", headers=admin).json()["total"]
    one = client.get(f"{API}/analytics/completion?course_id={course['id']}", headers=admin).json()["total"]
    assert 0 < one < allc
    s = client.get(f"{API}/dashboard/summary?course_id={course['id']}&cohort=2026-A&days=90", headers=admin)
    assert s.status_code == 200 and s.json()["period_days"] == 90


def test_tutor_answers_with_course_material_and_model(client, tokens):
    st = auth(tokens["estudiante"])
    ctx = client.get(f"{API}/tutor/context", headers=st).json()
    assert ctx and {"mastery_level", "skill_to_reinforce"} <= set(ctx[0])
    course = next(c for c in ctx if c["status"] != "completado") if any(c["status"] != "completado" for c in ctx) else ctx[0]
    r = client.post(f"{API}/tutor/ask", headers=st, json={"course_id": course["course_id"], "message": "¿Qué debo reforzar?"})
    assert r.status_code == 201, r.text
    ans = r.json()["answer"]
    assert ans["role"] == "tutor" and any(s["title"] == "Tu siguiente paso" for s in ans["payload"]["sections"])
    q = client.post(f"{API}/tutor/ask", headers=st, json={"message": "¿qué es el sobreajuste en machine learning?"}).json()
    assert q["answer"]["content"]
    assert len(client.get(f"{API}/tutor/history", headers=st).json()) >= 4
    assert client.post(f"{API}/tutor/ask", headers=auth(tokens["admin"]), json={"message": "hola"}).status_code == 403
    dudas = client.get(f"{API}/tutor/questions", headers=auth(tokens["admin"])).json()
    assert any("reforzar" in d["question"] for d in dudas)


def test_community_flow_permissions_and_moderation(client, tokens):
    st, admin = auth(tokens["estudiante"]), auth(tokens["admin"])
    spaces = client.get(f"{API}/community/spaces", headers=st).json()
    assert spaces and all(s["role"] == "miembro" for s in spaces)
    cid = spaces[0]["course_id"]
    assert client.post(f"{API}/community/posts", headers=st, json={"course_id": cid, "kind": "anuncio", "title": "Hola", "body": "No permitido"}).status_code == 403
    post = client.post(f"{API}/community/posts", headers=st, json={"course_id": cid, "kind": "debate", "title": "Duda sobre GRU", "body": "¿Cómo recuerda la secuencia?"}).json()
    assert post["status"] == "visible"
    c = client.post(f"{API}/community/posts/{post['id']}/comments", headers=admin, json={"body": "Usa compuertas de actualización y reinicio."})
    assert c.status_code == 201
    bad = client.post(f"{API}/community/posts/{post['id']}/comments", headers=st, json={"body": "eres un idiota"}).json()
    assert bad["status"] == "en_revision" and bad["notice"]
    reports = client.get(f"{API}/community/reports", headers=admin).json()
    auto = next(r for r in reports if r["automatic"] and r["post_id"] == post["id"])
    assert client.get(f"{API}/community/reports", headers=st).json() == []
    assert client.post(f"{API}/community/reports/{auto['id']}/resolve", headers=admin, json={"action": "ocultar"}).json()["status"] == "resuelto"
    detail = client.get(f"{API}/community/posts/{post['id']}", headers=st).json()
    assert len([x for x in detail["comments"] if x["status"] == "visible"]) == 1
    assert client.post(f"{API}/community/posts/{post['id']}/moderate", headers=st, json={"action": "fijar"}).status_code == 403
    assert client.post(f"{API}/community/posts/{post['id']}/moderate", headers=admin, json={"action": "fijar"}).json()["pinned"] is True
    assert client.post(f"{API}/community/spaces/{cid}/mute", headers=st, json={"muted": True}).json()["muted"] is True
    other = client.get(f"{API}/community/spaces", headers=auth(tokens["admin_aliada"])).json()
    assert all(s["course_id"] != cid for s in other)


def test_direct_messages_between_classmates(client, tokens):
    from app.modules import identity
    identity._attempts.clear()
    st = auth(tokens["estudiante"])
    r = client.post(f"{API}/auth/login", json={"email": "estudiante2@edumaster.demo", "password": "Demo2026!"})
    assert r.status_code == 200, "falta la cuenta DEMO del compañero"
    mate = auth(r.json()["access_token"])
    mate_id = r.json()["user"]["id"]
    contacts = client.get(f"{API}/messages/contacts", headers=st).json()
    assert any(c["id"] == mate_id and c["context"].startswith("Compañero") for c in contacts)
    conv = client.post(f"{API}/messages/conversations", headers=st, json={"user_id": mate_id}).json()
    again = client.post(f"{API}/messages/conversations", headers=st, json={"user_id": mate_id}).json()
    assert conv["id"] == again["id"]
    sent = client.post(f"{API}/messages/conversations/{conv['id']}/messages", headers=st, json={"body": "¡Hola! ¿Repasamos?"})
    assert sent.status_code == 201
    assert client.get(f"{API}/messages/unread", headers=mate).json()["unread"] >= 1
    thread = client.get(f"{API}/messages/conversations/{conv['id']}", headers=mate).json()
    assert thread["messages"][-1]["body"] == "¡Hola! ¿Repasamos?"
    client.post(f"{API}/messages/conversations/{conv['id']}/read", headers=mate)
    assert client.get(f"{API}/messages/unread", headers=mate).json()["unread"] == 0
    assert client.get(f"{API}/messages/conversations/{conv['id']}", headers=auth(tokens["admin_aliada"])).status_code == 404
    outsider = client.get(f"{API}/users", headers=auth(tokens["admin"])).json()
    ml_user = next(u for u in outsider if u["role"] == "ml_engineer")
    assert client.post(f"{API}/messages/conversations", headers=st, json={"user_id": ml_user["id"]}).status_code == 403


def test_private_events_reach_only_recipients():
    from app.core.events import Subscriber
    import asyncio
    a = Subscriber(queue=asyncio.Queue(), organization_id="o", role="estudiante", student_id="s1", user_id="u1")
    b = Subscriber(queue=asyncio.Queue(), organization_id="o", role="admin", student_id=None, user_id="u2")
    ev = {"organization_id": "o", "payload": {"user_ids": ["u1"], "conversation_id": "c"}}
    assert a.accepts(ev) and not b.accepts(ev)
