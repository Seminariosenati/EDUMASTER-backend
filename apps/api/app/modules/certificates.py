from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.audit import audit
from app.core.security import ADMIN, MANAGERS, STUDENT, Principal, current_user, require, visible_course_ids
from app.db.models import Certificate, Course, Enrollment, Organization, Student
from app.db.session import get_db
from app.services import academic

router = APIRouter(tags=["certificados"])


class IssueIn(BaseModel):
    student_id: str
    course_id: str


def _out(db: Session, c: Certificate) -> dict:
    student, course = db.get(Student, c.student_id), db.get(Course, c.course_id)
    return {"id": c.id, "folio": c.folio, "verification_code": c.verification_code, "status": c.status,
            "final_score": c.final_score, "issued_at": c.issued_at,
            "student": {"id": student.id, "full_name": student.full_name},
            "course": {"id": course.id, "code": course.code, "title": course.title}}


@router.get("/certificates")
def list_certificates(limit: int = Query(50, le=200), user: Principal = Depends(current_user),
                      db: Session = Depends(get_db)):
    stmt = select(Certificate).where(Certificate.organization_id == user.organization_id)
    if user.role == STUDENT:
        stmt = stmt.where(Certificate.student_id == user.student_id)
    allowed = visible_course_ids(db, user)
    if allowed is not None:
        stmt = stmt.where(Certificate.course_id.in_(allowed))
    return [_out(db, c) for c in db.scalars(stmt.order_by(Certificate.issued_at.desc()).limit(limit))]


@router.post("/certificates/issue", status_code=201)
def issue(body: IssueIn, user: Principal = Depends(require(*MANAGERS)), db: Session = Depends(get_db)):
    enr = db.scalar(select(Enrollment).where(Enrollment.student_id == body.student_id,
                                             Enrollment.course_id == body.course_id,
                                             Enrollment.organization_id == user.organization_id))
    if not enr:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Matrícula no encontrada")
    cert = academic.issue_certificate(db, enr, user.user_id)
    audit(db, user, "emitir_certificado", "certificate", cert.id, folio=cert.folio)
    db.commit()
    return _out(db, cert)


@router.post("/certificates/{cert_id}/revoke")
def revoke(cert_id: str, user: Principal = Depends(require(*MANAGERS)), db: Session = Depends(get_db)):
    cert = db.get(Certificate, cert_id)
    if not cert or cert.organization_id != user.organization_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Certificado no encontrado")
    cert.status = "anulado"
    audit(db, user, "anular_certificado", "certificate", cert.id, folio=cert.folio)
    db.commit()
    return _out(db, cert)


@router.get("/certificates/verify/{code}")
def verify(code: str, db: Session = Depends(get_db)):
    """Verificación pública: expone solo los datos mínimos."""
    cert = db.scalar(select(Certificate).where(Certificate.verification_code == code.upper()))
    if not cert:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Código no encontrado")
    student, course = db.get(Student, cert.student_id), db.get(Course, cert.course_id)
    org = db.get(Organization, cert.organization_id)
    parts = student.full_name.split()
    return {"valid": cert.status == "emitido", "status": cert.status, "folio": cert.folio,
            "holder": f"{parts[0]} {parts[-1][0]}." if len(parts) > 1 else parts[0],
            "course": course.title, "institution": org.name, "issued_at": cert.issued_at.date().isoformat(),
            "is_demo": org.is_demo}
