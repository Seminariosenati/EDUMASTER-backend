"""IA Tutor: respuestas adaptadas al estudiante combinando la red EduMaster MTL (nivel, riesgo, competencia a reforzar),
los errores en sus evaluaciones y una búsqueda TF-IDF sobre el material del curso. No inventa contenido: todo lo que
responde sale del banco de preguntas, el glosario del curso o las predicciones del modelo publicado."""

from __future__ import annotations

import re

import numpy as np
from sklearn.feature_extraction.text import TfidfVectorizer
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import (Assessment, AssessmentAttempt, AttemptSubmission, Course, Enrollment, Question,
                           Skill)
from app.services import ml
from app.services.question_bank import BANK, SHORT, _normalize

GLOSSARY: dict[str, list[tuple[str, str]]] = {
    "PY101": [("Variables y tipos", "Una variable guarda un valor; Python infiere su tipo (int, float, str, bool, list…). Usa type(x) para verlo."),
              ("Funciones", "Se definen con def nombre(parámetros): y devuelven valores con return. Reutilizan lógica y evitan repetir código."),
              ("Listas y bucles", "Una lista guarda elementos en orden; for elemento in lista: recorre cada uno. len(lista) da su tamaño.")],
    "DS201": [("DataFrame", "Tabla de Pandas con filas y columnas etiquetadas. df.head(), df.info() y df.describe() ayudan a explorarla."),
              ("Valores faltantes", "isna() los detecta, dropna() elimina filas con faltantes y fillna(valor) los reemplaza."),
              ("Agrupar", "df.groupby('columna').agg(...) resume por categoría, por ejemplo el promedio por región.")],
    "ML201": [("Entrenamiento y prueba", "Se entrena con un conjunto y se evalúa con otro que el modelo no vio, para medir si generaliza."),
              ("Sobreajuste", "Ocurre cuando el modelo memoriza el entrenamiento y falla con datos nuevos. Se reduce con más datos, regularización o modelos más simples."),
              ("Métricas", "Exactitud, precisión, recall y F1 miden la clasificación; con clases desbalanceadas conviene PR-AUC.")],
    "ML301": [("Validación cruzada", "Divide los datos en k partes y entrena k veces, promediando el resultado para estimar mejor el rendimiento."),
              ("Regularización", "Penaliza pesos grandes (L1/L2, weight decay) para que el modelo no se sobreajuste."),
              ("Datos temporales", "Se divide por fecha: entrenar con el pasado y evaluar con el futuro evita fuga de información.")],
    "IA201": [("Neurona y pesos", "Cada neurona multiplica sus entradas por pesos, suma un sesgo y aplica una función de activación."),
              ("Entrenamiento", "Se predice, se mide el error con la función de pérdida y la retropropagación ajusta los pesos. Una pasada completa es una época."),
              ("Capas Dense", "Parámetros = entradas × neuronas + neuronas. Más neuronas aumentan la capacidad y también la memoria.")],
    "IA301": [("Redes multitarea", "Una base compartida aprende representaciones útiles para varias salidas, como finalización, dominio y riesgo."),
              ("Softmax y Sigmoid", "Sigmoid da una probabilidad entre 0 y 1; Softmax reparte probabilidades entre varias clases que suman 1."),
              ("Optimizadores", "Adam/AdamW ajustan la tasa de aprendizaje de cada peso usando el momento de los gradientes.")],
    "WEB101": [("HTML, CSS y JS", "HTML estructura el contenido, CSS lo estiliza y JavaScript le da comportamiento."),
               ("Flexbox", "display: flex alinea elementos en una dimensión; gap separa y justify-content/align-items los ubican."),
               ("Eventos", "addEventListener('click', fn) ejecuta una función cuando el usuario interactúa.")],
    "WEB201": [("Componentes", "En React la interfaz se divide en componentes que reciben props y devuelven JSX."),
               ("Estado", "useState guarda datos que, al cambiar, vuelven a dibujar el componente."),
               ("Efectos", "useEffect ejecuta código después de renderizar, por ejemplo para pedir datos a una API.")],
    "CLD101": [("Terminal de Linux", "pwd muestra dónde estás, ls lista archivos y cd cambia de carpeta."),
               ("Imagen y contenedor", "Una imagen es la plantilla; un contenedor es una instancia en ejecución creada desde ella."),
               ("Docker Compose", "docker-compose.yml describe varios servicios (api, base de datos, web) y docker compose up -d los levanta.")],
    "CLD201": [("DNS", "Un registro A traduce un nombre de dominio a una IP; así se entra por nombre en lugar de recordar números."),
               ("HTTPS", "Cifra la conexión con un certificado; Let's Encrypt los emite gratis solo para dominios."),
               ("Costos en la nube", "Detener y desasignar una VM deja de cobrar el cómputo; el disco y la IP siguen generando un costo menor.")],
}

STOP = {"que", "qué", "como", "cómo", "es", "el", "la", "los", "las", "un", "una", "de", "del", "en", "y", "o", "a", "por",
        "para", "con", "me", "mi", "se", "lo", "no", "si", "sí", "cual", "cuál", "son", "al", "le", "esto", "eso"}

# Palabras de intención o genéricas: no aportan al buscar material del curso.
GENERIC = {"falle", "falla", "falles", "fallado", "fallaste", "equivoque", "error", "errores", "mal", "reforzar",
           "refuerzo", "mejorar", "estudiar", "practicar", "hago", "debo", "plan", "siguiente", "recomiendas", "voy",
           "como", "curso", "cursos", "avance", "progreso", "nota", "notas", "aprobar", "terminar", "riesgo", "ayuda",
           "ayudame", "explicame", "puedes", "quiero", "saber", "tengo", "estoy", "hola", "gracias", "bien", "algo"}

LEVEL_INTRO = {
    "principiante": "Vamos paso a paso y desde lo básico.",
    "elemental": "Vamos paso a paso, reforzando las bases.",
    "intermedio": "Ya tienes una base; vamos a afinar los detalles.",
    "avanzado": "Vas bien; voy directo a lo importante.",
    "experto": "Tienes buen dominio; te dejo lo esencial.",
}

INTENTS = {
    "errores": re.compile(r"fall|equivoc|error|mal|incorrect|no entend"),
    "plan": re.compile(r"reforz|mejor|estudi|practic|que hago|qué hago|plan|siguiente|recomiend"),
    "avance": re.compile(r"riesgo|aprob|termin|avance|voy|nota|progreso"),
}


def _corpus(db: Session, organization_id: str) -> list[dict]:
    docs = []
    rows = db.execute(select(Question, Course.code, Course.title, Course.id)
                      .join(Assessment, Assessment.id == Question.assessment_id)
                      .join(Course, Course.id == Assessment.course_id)
                      .where(Question.organization_id == organization_id)).all()
    seen = set()
    for q, code, title, cid in rows:
        if q.prompt in seen:
            continue
        seen.add(q.prompt)
        answer = (q.answer.get("accepted") or [""])[0] if q.type == SHORT else (
            q.options[q.answer.get("correct", 0)] if q.options else "")
        docs.append({"course_id": cid, "course": title, "title": q.prompt, "body": f"Respuesta: {answer}. {q.explanation}",
                     "text": f"{q.prompt} {answer} {q.explanation} {title}"})
    courses = {c.code: c for c in db.scalars(select(Course).where(Course.organization_id == organization_id))}
    for code, items in GLOSSARY.items():
        c = courses.get(code)
        if not c:
            continue
        for concept, text in items:
            docs.append({"course_id": c.id, "course": c.title, "title": concept, "body": text,
                         "text": f"{concept} {text} {c.title}"})
    return docs


def search(db: Session, organization_id: str, query: str, course_id: str | None, k: int = 3) -> list[dict]:
    docs = _corpus(db, organization_id)
    words = [w for w in re.findall(r"[a-z0-9]+", _normalize(query)) if w not in STOP and w not in GENERIC]
    if not docs or not words:
        return []
    vec = TfidfVectorizer(preprocessor=_normalize, ngram_range=(1, 2), min_df=1, sublinear_tf=True)
    matrix = vec.fit_transform([d["text"] for d in docs])
    sims = (matrix @ vec.transform([" ".join(words)]).T).toarray().ravel()
    if course_id:
        sims = sims + np.array([0.08 if d["course_id"] == course_id and s > 0 else 0 for d, s in zip(docs, sims)])
    order = np.argsort(-sims)[:k]
    return [{**docs[i], "score": round(float(sims[i]), 3)} for i in order if sims[i] >= 0.12]


def student_context(db: Session, organization_id: str, student_id: str) -> list[dict]:
    """Por curso en curso: nivel, riesgo y competencia a reforzar según el modelo publicado."""
    enrollments = {e.course_id: e for e in db.scalars(select(Enrollment).where(Enrollment.student_id == student_id))}
    courses = {c.id: c for c in db.scalars(select(Course).where(Course.id.in_(list(enrollments))))} if enrollments else {}
    preds = {}
    mv = ml.production_version(db, organization_id)
    if mv and enrollments:
        rows = ml.current_features(db, organization_id, [student_id])
        for p in ml.predict_rows(ml.get_predictor(mv), rows):
            preds[p["course_id"]] = p
    skills = {s.id: s.name for s in db.scalars(select(Skill).where(Skill.organization_id == organization_id))}
    out = []
    for cid, e in enrollments.items():
        c = courses.get(cid)
        if not c:
            continue
        p = preds.get(cid)
        top = None
        if p:
            own = [sid for sid in p["course_skills"] if sid in skills]
            if own:
                top = skills[max(own, key=lambda s: p["skill_scores"].get(s, 0))]
        out.append({"course_id": cid, "code": c.code, "title": c.title, "status": e.status, "progress": e.progress,
                    "mastery_level": p["mastery_level"] if p else None,
                    "risk_probability": p["risk_probability"] if p else None,
                    "completion_probability": p["completion_probability"] if p else None,
                    "skill_to_reinforce": top, "model_version": mv.version if mv else None})
    return sorted(out, key=lambda x: (x["status"] == "completado", x["title"]))


def mistakes(db: Session, student_id: str, course_id: str | None, limit: int = 4) -> list[dict]:
    stmt = (select(AttemptSubmission.detail).join(AssessmentAttempt, AssessmentAttempt.id == AttemptSubmission.attempt_id)
            .join(Assessment, Assessment.id == AssessmentAttempt.assessment_id)
            .where(AssessmentAttempt.student_id == student_id))
    if course_id:
        stmt = stmt.where(Assessment.course_id == course_id)
    rows = db.execute(stmt.order_by(AttemptSubmission.submitted_at.desc())).scalars().all()
    wrong, seen = [], set()
    for detail in rows:
        for d in detail:
            if d.get("correct") or d["question_id"] in seen:
                continue
            seen.add(d["question_id"])
            q = db.get(Question, d["question_id"])
            if q:
                course = db.scalar(select(Course.title).join(Assessment, Assessment.course_id == Course.id)
                                   .where(Assessment.id == q.assessment_id))
                wrong.append({"title": q.prompt, "body": f"Respuesta correcta: {d['expected']}. {d.get('explanation', '')}".strip(),
                              "source": course})
    return wrong[:limit]


def pending_evaluation(db: Session, student_id: str, course_id: str) -> dict | None:
    for a in db.scalars(select(Assessment).where(Assessment.course_id == course_id).order_by(Assessment.due_at)):
        has_q = db.scalar(select(func.count(Question.id)).where(Question.assessment_id == a.id)) or 0
        used = db.scalar(select(func.count(AssessmentAttempt.id)).where(
            AssessmentAttempt.assessment_id == a.id, AssessmentAttempt.student_id == student_id)) or 0
        if has_q and used < a.max_attempts:
            return {"title": a.title, "link": f"/evaluaciones/{a.id}"}
    return None


def answer(db: Session, organization_id: str, student_id: str, course_id: str | None, message: str) -> dict:
    ctx_all = student_context(db, organization_id, student_id)
    ctx = next((c for c in ctx_all if c["course_id"] == course_id), None) or next(
        (c for c in ctx_all if c["status"] != "completado"), ctx_all[0] if ctx_all else None)
    if not ctx:
        return {"text": "Aún no tienes cursos matriculados. Inscríbete en un curso para que pueda ayudarte con su material.",
                "sections": [], "context": None}
    level = ctx["mastery_level"] or "intermedio"
    text_norm = _normalize(message)
    intents = [k for k, rx in INTENTS.items() if rx.search(text_norm)]
    found = search(db, organization_id, message, ctx["course_id"])
    wrong = mistakes(db, student_id, course_id if course_id else None)
    sections = []

    lines = [LEVEL_INTRO.get(level, "")]
    if found:
        lines.append(f"Esto es lo que encontré sobre tu pregunta en el material de {found[0]['course']}:")
        sections.append({"title": "Explicación", "items": [{"title": d["title"], "body": d["body"], "source": d["course"]} for d in found]})
    elif not intents:
        lines.append(f"No encontré material exacto sobre eso en {ctx['title']}. Prueba con otras palabras clave "
                     "o pregúntame qué reforzar, qué fallaste o cómo vas.")

    if "errores" in intents or (not intents and not found and wrong):
        if wrong:
            lines.append("Revisemos las preguntas que fallaste en tus evaluaciones:")
            sections.append({"title": "Lo que fallaste", "items": wrong})
        elif "errores" in intents:
            lines.append("No tienes respuestas incorrectas registradas" + (f" en {ctx['title']}" if course_id else "")
                         + ". ¡Bien! Si quieres, practica con una evaluación pendiente.")

    if "plan" in intents or "avance" in intents or not found:
        plan = []
        if ctx["skill_to_reinforce"]:
            plan.append({"title": f"Refuerza {ctx['skill_to_reinforce']}",
                         "body": "Es la competencia que la red neuronal prioriza para ti en este curso."})
        concept = next(iter(GLOSSARY.get(ctx["code"], [])), None)
        if concept and level in ("principiante", "elemental"):
            plan.append({"title": f"Repasa: {concept[0]}", "body": concept[1]})
        pend = pending_evaluation(db, student_id, ctx["course_id"])
        if pend:
            plan.append({"title": f"Practica con «{pend['title']}»", "body": "Corrección inmediata con la explicación de cada pregunta.",
                         "link": pend["link"]})
        plan.append({"title": f"Continúa {ctx['title']}", "body": f"Llevas {round(ctx['progress'] * 100)}% de avance.",
                     "link": f"/cursos/{ctx['course_id']}"})
        sections.append({"title": "Tu siguiente paso", "items": plan})

    if "avance" in intents and ctx["completion_probability"] is not None:
        risk, comp = ctx["risk_probability"], ctx["completion_probability"]
        if risk >= 0.5:
            tone = "Tu actividad reciente es baja; retomar unas sesiones esta semana marca la diferencia."
        elif comp < 0.3:
            tone = ("Aún te falta bastante para terminar en ese plazo; avanzar actividades y practicar con las "
                    "evaluaciones te ayudará a subir esa estimación.")
        else:
            tone = "Vas con buen ritmo; mantén la constancia."
        lines.append(f"Según el modelo, tu probabilidad estimada de terminar {ctx['title']} en 28 días es "
                     f"{round(ctx['completion_probability'] * 100)}% y tu nivel estimado es {level}. {tone}")

    return {"text": " ".join(l for l in lines if l), "sections": sections,
            "context": {**ctx, "intents": intents, "matches": len(found)},
            "disclaimer": "Respuesta generada con el modelo EduMaster MTL y el material del curso; es apoyo al aprendizaje, no una calificación."}
