"""Comunidad por curso: anuncios, debates, comentarios, moderación, reportes y silenciar notificaciones."""

import re

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.events import emit
from app.core.security import (ADMIN, AUDITOR, COORDINATOR, INSTRUCTOR, STAFF, STUDENT, SUPERADMIN, Principal,
                               current_user)
from app.db.models import (CommunityComment, CommunityMute, CommunityPost, CommunityReport, Course, Enrollment,
                           Notification, Student, User)
from app.db.session import get_db

router = APIRouter(prefix="/community", tags=["comunidad"])
MODERATOR_ROLES = (ADMIN, SUPERADMIN, COORDINATOR)
WRITER_STAFF = (ADMIN, SUPERADMIN, COORDINATOR)
# Filtro básico: el contenido queda en revisión hasta que un moderador lo apruebe.
BLOCKED = re.compile(r"\b(idiota|estupid[oa]s?|imbecil|imbécil|tarad[oa]|inutil(es)?|inútil(es)?|mierda|put[oa]s?)\b", re.I)


class PostIn(BaseModel):
    course_id: str
    kind: str = Field(pattern="^(anuncio|debate)$")
    title: str = Field(min_length=3, max_length=200)
    body: str = Field(min_length=3, max_length=5000)


class CommentIn(BaseModel):
    body: str = Field(min_length=1, max_length=3000)


class ModerateIn(BaseModel):
    action: str = Field(pattern="^(fijar|desfijar|ocultar|mostrar)$")


class ReportIn(BaseModel):
    target_type: str = Field(pattern="^(post|comment)$")
    target_id: str
    reason: str = Field(min_length=3, max_length=300)


class ResolveIn(BaseModel):
    action: str = Field(pattern="^(ocultar|descartar)$")


class MuteIn(BaseModel):
    muted: bool


def _course(db: Session, user: Principal, course_id: str) -> Course:
    c = db.get(Course, course_id)
    if not c or c.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Espacio no encontrado")
    return c


def _role_in(db: Session, user: Principal, c: Course) -> str | None:
    """'moderador', 'miembro', 'lector' o None según la relación del usuario con el curso."""
    if user.role in MODERATOR_ROLES or (user.role == INSTRUCTOR and c.instructor_id == user.user_id):
        return "moderador"
    if user.role == STUDENT:
        enrolled = db.scalar(select(Enrollment.id).where(Enrollment.student_id == user.student_id,
                                                         Enrollment.course_id == c.id))
        return "miembro" if enrolled else None
    if user.role in (AUDITOR, *STAFF):
        return "lector"
    return None


def _require(db: Session, user: Principal, c: Course, *allowed: str) -> str:
    role = _role_in(db, user, c)
    if role is None or (allowed and role not in allowed):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "No tienes permiso en este espacio")
    return role


def _authors(db: Session, ids: set[str]) -> dict[str, dict]:
    if not ids:
        return {}
    return {u.id: {"id": u.id, "name": u.full_name, "role": u.role} for u in db.scalars(select(User).where(User.id.in_(ids)))}


def _muted(db: Session, user_id: str, course_id: str) -> bool:
    return db.get(CommunityMute, (user_id, course_id)) is not None


def _notify(db: Session, org: str, user_id: str, course_id: str, title: str, body: str) -> None:
    if _muted(db, user_id, course_id):
        return
    db.add(Notification(organization_id=org, user_id=user_id, type="comunidad", title=title, body=body[:300]))


def _changed(db: Session, user: Principal, course_id: str, kind: str, ref: str) -> None:
    emit(db, user.organization_id, "community.updated", {"course_id": course_id, "kind": kind, "ref": ref},
         aggregate_id=ref)


def _post_out(p: CommunityPost, authors: dict, comments: int, mine: bool) -> dict:
    return {"id": p.id, "course_id": p.course_id, "kind": p.kind, "title": p.title, "body": p.body,
            "pinned": p.pinned, "status": p.status, "created_at": p.created_at, "author": authors.get(p.author_id),
            "comments": comments, "mine": mine}


@router.get("/spaces")
def spaces(user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(Course).where(Course.organization_id == user.organization_id, Course.status == "publicado")
    if user.role == STUDENT:
        stmt = stmt.where(Course.id.in_(select(Enrollment.course_id).where(Enrollment.student_id == user.student_id)))
    elif user.role == INSTRUCTOR:
        stmt = stmt.where(Course.instructor_id == user.user_id)
    elif user.role not in (AUDITOR, *STAFF):
        return []
    courses = list(db.scalars(stmt.order_by(Course.code)))
    ids = [c.id for c in courses]
    stats = {cid: (n, last) for cid, n, last in db.execute(
        select(CommunityPost.course_id, func.count(CommunityPost.id), func.max(CommunityPost.created_at))
        .where(CommunityPost.course_id.in_(ids), CommunityPost.status == "visible").group_by(CommunityPost.course_id))} if ids else {}
    open_reports = dict(db.execute(select(CommunityReport.course_id, func.count(CommunityReport.id))
                                   .where(CommunityReport.course_id.in_(ids), CommunityReport.status == "abierto")
                                   .group_by(CommunityReport.course_id)).all()) if ids else {}
    out = []
    for c in courses:
        role = _role_in(db, user, c)
        n, last = stats.get(c.id, (0, None))
        out.append({"course_id": c.id, "code": c.code, "title": c.title, "posts": n, "last_activity": last,
                    "role": role, "muted": _muted(db, user.user_id, c.id),
                    "open_reports": open_reports.get(c.id, 0) if role == "moderador" else 0})
    return out


@router.get("/posts")
def list_posts(course_id: str, kind: str | None = Query(default=None, pattern="^(anuncio|debate)$"),
               user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    c = _course(db, user, course_id)
    role = _require(db, user, c)
    stmt = select(CommunityPost).where(CommunityPost.course_id == c.id, CommunityPost.status != "eliminado")
    if role != "moderador":
        stmt = stmt.where((CommunityPost.status == "visible") | (CommunityPost.author_id == user.user_id))
    if kind:
        stmt = stmt.where(CommunityPost.kind == kind)
    posts = list(db.scalars(stmt.order_by(CommunityPost.pinned.desc(), CommunityPost.created_at.desc()).limit(100)))
    counts = dict(db.execute(select(CommunityComment.post_id, func.count(CommunityComment.id))
                             .where(CommunityComment.post_id.in_([p.id for p in posts]), CommunityComment.status == "visible")
                             .group_by(CommunityComment.post_id)).all()) if posts else {}
    authors = _authors(db, {p.author_id for p in posts})
    return {"role": role, "posts": [_post_out(p, authors, counts.get(p.id, 0), p.author_id == user.user_id) for p in posts]}


@router.post("/posts", status_code=201)
def create_post(body: PostIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    c = _course(db, user, body.course_id)
    role = _require(db, user, c, "moderador", "miembro")
    if body.kind == "anuncio" and role != "moderador":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo docentes y coordinación publican anuncios")
    flagged = bool(BLOCKED.search(f"{body.title} {body.body}"))
    p = CommunityPost(organization_id=user.organization_id, course_id=c.id, author_id=user.user_id, kind=body.kind,
                      title=body.title.strip(), body=body.body.strip(), status="en_revision" if flagged else "visible")
    db.add(p)
    db.flush()
    if flagged:
        db.add(CommunityReport(organization_id=user.organization_id, course_id=c.id, target_type="post", target_id=p.id,
                               reason="Filtro automático: lenguaje inapropiado"))
    elif body.kind == "anuncio":
        for uid in db.scalars(select(Student.user_id).join(Enrollment, Enrollment.student_id == Student.id)
                              .where(Enrollment.course_id == c.id, Student.user_id.is_not(None))):
            _notify(db, user.organization_id, uid, c.id, f"Anuncio en {c.title}: {p.title}", p.body)
    _changed(db, user, c.id, "post", p.id)
    audit(db, user, "publicar_comunidad", "community_post", p.id, course_id=c.id, tipo=body.kind, en_revision=flagged)
    db.commit()
    return {**_post_out(p, _authors(db, {user.user_id}), 0, True),
            "notice": "Tu publicación quedó en revisión por el filtro de lenguaje." if flagged else None}


@router.get("/posts/{post_id}")
def get_post(post_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(CommunityPost, post_id)
    if not p or p.organization_id != user.organization_id or p.status == "eliminado":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Publicación no encontrada")
    c = db.get(Course, p.course_id)
    role = _require(db, user, c)
    if role != "moderador" and p.status != "visible" and p.author_id != user.user_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Publicación no encontrada")
    stmt = select(CommunityComment).where(CommunityComment.post_id == p.id, CommunityComment.status != "eliminado")
    if role != "moderador":
        stmt = stmt.where((CommunityComment.status == "visible") | (CommunityComment.author_id == user.user_id))
    comments = list(db.scalars(stmt.order_by(CommunityComment.created_at)))
    authors = _authors(db, {p.author_id, *(x.author_id for x in comments)})
    return {"role": role, "course": {"id": c.id, "code": c.code, "title": c.title},
            "post": _post_out(p, authors, len(comments), p.author_id == user.user_id),
            "comments": [{"id": x.id, "body": x.body, "status": x.status, "created_at": x.created_at,
                          "author": authors.get(x.author_id), "mine": x.author_id == user.user_id} for x in comments]}


@router.post("/posts/{post_id}/comments", status_code=201)
def comment(post_id: str, body: CommentIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(CommunityPost, post_id)
    if not p or p.organization_id != user.organization_id or p.status != "visible":
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Publicación no encontrada")
    c = db.get(Course, p.course_id)
    _require(db, user, c, "moderador", "miembro")
    flagged = bool(BLOCKED.search(body.body))
    x = CommunityComment(organization_id=user.organization_id, post_id=p.id, author_id=user.user_id,
                         body=body.body.strip(), status="en_revision" if flagged else "visible")
    db.add(x)
    db.flush()
    if flagged:
        db.add(CommunityReport(organization_id=user.organization_id, course_id=c.id, target_type="comment",
                               target_id=x.id, reason="Filtro automático: lenguaje inapropiado"))
    elif p.author_id != user.user_id:
        _notify(db, user.organization_id, p.author_id, c.id, f"Nuevo comentario en «{p.title}»", body.body)
    _changed(db, user, c.id, "comment", p.id)
    audit(db, user, "comentar_comunidad", "community_comment", x.id, post_id=p.id, en_revision=flagged)
    db.commit()
    return {"id": x.id, "status": x.status,
            "notice": "Tu comentario quedó en revisión por el filtro de lenguaje." if flagged else None}


@router.post("/posts/{post_id}/moderate")
def moderate_post(post_id: str, body: ModerateIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(CommunityPost, post_id)
    if not p or p.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Publicación no encontrada")
    _require(db, user, db.get(Course, p.course_id), "moderador")
    if body.action in ("fijar", "desfijar"):
        p.pinned = body.action == "fijar"
    else:
        p.status = "oculto" if body.action == "ocultar" else "visible"
    _changed(db, user, p.course_id, "post", p.id)
    audit(db, user, "moderar_comunidad", "community_post", p.id, accion=body.action)
    db.commit()
    return {"id": p.id, "pinned": p.pinned, "status": p.status}


@router.post("/comments/{comment_id}/moderate")
def moderate_comment(comment_id: str, body: ModerateIn, user: Principal = Depends(current_user),
                     db: Session = Depends(get_db)):
    x = db.get(CommunityComment, comment_id)
    if not x or x.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Comentario no encontrado")
    p = db.get(CommunityPost, x.post_id)
    _require(db, user, db.get(Course, p.course_id), "moderador")
    if body.action not in ("ocultar", "mostrar"):
        raise HTTPException(422, "Acción no válida para comentarios")
    x.status = "oculto" if body.action == "ocultar" else "visible"
    _changed(db, user, p.course_id, "comment", p.id)
    audit(db, user, "moderar_comunidad", "community_comment", x.id, accion=body.action)
    db.commit()
    return {"id": x.id, "status": x.status}


@router.delete("/posts/{post_id}", status_code=204)
def delete_post(post_id: str, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    p = db.get(CommunityPost, post_id)
    if not p or p.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Publicación no encontrada")
    role = _require(db, user, db.get(Course, p.course_id))
    if p.author_id != user.user_id and role != "moderador":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo el autor o un moderador puede eliminarla")
    p.status = "eliminado"
    _changed(db, user, p.course_id, "post", p.id)
    audit(db, user, "eliminar_comunidad", "community_post", p.id)
    db.commit()


@router.post("/reports", status_code=201)
def report(body: ReportIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    if body.target_type == "post":
        target = db.get(CommunityPost, body.target_id)
        course_id = target.course_id if target else None
    else:
        target = db.get(CommunityComment, body.target_id)
        course_id = db.get(CommunityPost, target.post_id).course_id if target else None
    if not target or target.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Contenido no encontrado")
    c = db.get(Course, course_id)
    _require(db, user, c, "moderador", "miembro")
    r = CommunityReport(organization_id=user.organization_id, course_id=c.id, target_type=body.target_type,
                        target_id=body.target_id, reporter_id=user.user_id, reason=body.reason.strip())
    db.add(r)
    if c.instructor_id:
        db.add(Notification(organization_id=user.organization_id, user_id=c.instructor_id, type="comunidad",
                            title=f"Nuevo reporte en {c.title}", body=body.reason[:300]))
    _changed(db, user, c.id, "report", r.id)
    audit(db, user, "reportar_comunidad", "community_report", r.id, objetivo=body.target_type)
    db.commit()
    return {"id": r.id, "status": r.status}


@router.get("/reports")
def list_reports(course_id: str | None = None, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    stmt = select(CommunityReport).where(CommunityReport.organization_id == user.organization_id,
                                         CommunityReport.status == "abierto")
    if course_id:
        stmt = stmt.where(CommunityReport.course_id == course_id)
    out = []
    for r in db.scalars(stmt.order_by(CommunityReport.created_at.desc()).limit(100)):
        c = db.get(Course, r.course_id)
        if _role_in(db, user, c) != "moderador":
            continue
        target = db.get(CommunityPost if r.target_type == "post" else CommunityComment, r.target_id)
        author = db.get(User, target.author_id) if target else None
        post_id = (target.id if r.target_type == "post" else target.post_id) if target else None
        out.append({"id": r.id, "course": {"id": c.id, "title": c.title}, "target_type": r.target_type,
                    "reason": r.reason, "automatic": r.reporter_id is None, "created_at": r.created_at,
                    "post_id": post_id, "excerpt": (target.body if target else "")[:200],
                    "target_status": target.status if target else None, "author": author.full_name if author else None})
    return out


@router.post("/reports/{report_id}/resolve")
def resolve(report_id: str, body: ResolveIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    r = db.get(CommunityReport, report_id)
    if not r or r.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Reporte no encontrado")
    _require(db, user, db.get(Course, r.course_id), "moderador")
    target = db.get(CommunityPost if r.target_type == "post" else CommunityComment, r.target_id)
    if target:
        target.status = "oculto" if body.action == "ocultar" else "visible"
    r.status, r.resolution, r.resolved_by = "resuelto", body.action, user.user_id
    _changed(db, user, r.course_id, "report", r.id)
    audit(db, user, "resolver_reporte", "community_report", r.id, accion=body.action)
    db.commit()
    return {"id": r.id, "status": r.status, "resolution": r.resolution}


@router.post("/spaces/{course_id}/mute")
def mute(course_id: str, body: MuteIn, user: Principal = Depends(current_user), db: Session = Depends(get_db)):
    c = _course(db, user, course_id)
    _require(db, user, c)
    existing = db.get(CommunityMute, (user.user_id, c.id))
    if body.muted and not existing:
        db.add(CommunityMute(user_id=user.user_id, course_id=c.id))
    elif not body.muted and existing:
        db.delete(existing)
    db.commit()
    return {"course_id": c.id, "muted": body.muted}


def ensure_community_demo(db: Session, organization_id: str) -> int:
    """Publicaciones DEMO iniciales si la comunidad está vacía. Idempotente."""
    if db.scalar(select(CommunityPost.id).where(CommunityPost.organization_id == organization_id).limit(1)):
        return 0
    created = 0
    student_user = db.scalar(select(User).where(User.organization_id == organization_id, User.role == STUDENT))
    student = db.scalar(select(Student).where(Student.user_id == student_user.id)) if student_user else None
    my_courses = set(db.scalars(select(Enrollment.course_id).where(Enrollment.student_id == student.id))) if student else set()
    for c in db.scalars(select(Course).where(Course.organization_id == organization_id, Course.status == "publicado")):
        if not c.instructor_id:
            continue
        db.add(CommunityPost(organization_id=organization_id, course_id=c.id, author_id=c.instructor_id, kind="anuncio",
                             pinned=True, title=f"Bienvenida a {c.title}",
                             body="Este es el espacio del curso. Usa Debates para preguntar y ayudar a tus compañeros; "
                                  "los anuncios importantes aparecerán fijados aquí."))
        created += 1
        if c.id in my_courses:
            post = CommunityPost(organization_id=organization_id, course_id=c.id, author_id=student_user.id, kind="debate",
                                 title=f"¿Algún consejo para la próxima evaluación de {c.title}?",
                                 body="Estoy repasando con el IA Tutor y la evaluación de práctica. ¿Qué temas les costaron más?")
            db.add(post)
            db.flush()
            classmate = db.scalar(select(User.id).join(Student, Student.user_id == User.id)
                                  .join(Enrollment, Enrollment.student_id == Student.id)
                                  .where(Enrollment.course_id == c.id, User.id != student_user.id).limit(1))
            if classmate:
                db.add(CommunityComment(organization_id=organization_id, post_id=post.id, author_id=classmate,
                                        body="A mí me costó la parte práctica. ¿Armamos un grupo de estudio esta semana?"))
            db.add(CommunityComment(organization_id=organization_id, post_id=post.id, author_id=c.instructor_id,
                                    body="Buena pregunta. Revisen la explicación de cada pregunta al corregir y "
                                         "usen el tutor con «¿qué fallé?»."))
            created += 2
    db.commit()
    return created
