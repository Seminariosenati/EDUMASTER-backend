"""Banco de preguntas DEMO por curso y corrección automática de respuestas."""

import unicodedata

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db.models import Assessment, Course, Question

MC, TF, SHORT = "opcion_multiple", "verdadero_falso", "respuesta_corta"

# (tipo, enunciado, opciones, respuesta, explicación). Para MC/TF la respuesta es el índice correcto;
# para respuesta corta, la lista de respuestas aceptadas.
BANK: dict[str, list[tuple]] = {
    "PY101": [
        (MC, "¿Qué función muestra texto en pantalla en Python?", ["echo()", "print()", "write()", "show()"], 1, "print() escribe en la salida estándar."),
        (TF, "En Python la indentación define los bloques de código.", None, 0, "Python usa la indentación en lugar de llaves."),
        (MC, "¿Qué tipo de dato es [1, 2, 3]?", ["tuple", "dict", "list", "set"], 2, "Los corchetes crean una lista."),
        (SHORT, "¿Qué palabra clave define una función en Python?", None, ["def"], "Se escribe def nombre(parámetros):"),
        (MC, "¿Cuál es el resultado de 7 // 2?", ["3.5", "3", "4", "1"], 1, "// es la división entera."),
        (TF, "Una tupla se puede modificar después de crearla.", None, 1, "Las tuplas son inmutables."),
    ],
    "DS201": [
        (MC, "¿Qué estructura principal usa Pandas para datos tabulares?", ["Series", "DataFrame", "ndarray", "Matrix"], 1, "Un DataFrame es una tabla con columnas etiquetadas."),
        (SHORT, "¿Qué método de Pandas muestra las primeras filas de un DataFrame?", None, ["head", "head()", "df.head()"], "head() devuelve las 5 primeras filas por defecto."),
        (MC, "¿Qué método elimina filas con valores faltantes?", ["fillna()", "dropna()", "isna()", "drop_duplicates()"], 1, "dropna() descarta filas con NaN."),
        (TF, "groupby() permite calcular agregados por categoría.", None, 0, "Por ejemplo df.groupby('region').mean()."),
        (MC, "¿Qué función lee un archivo CSV?", ["pd.open_csv", "pd.read_csv", "pd.load", "pd.csv"], 1, "pd.read_csv('archivo.csv')."),
    ],
    "ML201": [
        (MC, "¿Qué tipo de problema es predecir si un estudiante aprobará?", ["Regresión", "Clasificación", "Clustering", "Reducción de dimensión"], 1, "La salida es una categoría (sí/no)."),
        (TF, "El conjunto de prueba se usa para ajustar los parámetros del modelo.", None, 1, "Se usa solo para evaluar al final; ajustar con él causa fuga de información."),
        (SHORT, "¿Cómo se llama el error de memorizar los datos de entrenamiento y fallar con datos nuevos?", None, ["sobreajuste", "overfitting"], "El sobreajuste reduce la capacidad de generalizar."),
        (MC, "¿Qué métrica es útil con clases desbalanceadas?", ["Exactitud", "PR-AUC", "MSE", "R²"], 1, "PR-AUC se enfoca en la clase minoritaria."),
        (MC, "¿Qué hace la regresión logística?", ["Agrupa datos", "Estima una probabilidad", "Reduce dimensiones", "Genera texto"], 1, "Aplica una sigmoide para obtener una probabilidad."),
    ],
    "ML301": [
        (MC, "¿Qué técnica combina muchos árboles de decisión entrenados en muestras distintas?", ["Random Forest", "K-means", "PCA", "SVM lineal"], 0, "Random Forest promedia árboles con bagging."),
        (TF, "La validación cruzada estima mejor el rendimiento que una sola partición.", None, 0, "Promedia el resultado de varias particiones."),
        (SHORT, "¿Cómo se llama la técnica que penaliza pesos grandes para evitar sobreajuste?", None, ["regularización", "regularizacion", "l2", "weight decay"], "L1/L2 o weight decay."),
        (MC, "En datos temporales, ¿cómo conviene dividir train y test?", ["Al azar", "Por fecha", "Por orden alfabético", "No dividir"], 1, "Evita usar información futura para predecir el pasado."),
    ],
    "IA201": [
        (MC, "¿Qué función de activación deja pasar solo valores positivos?", ["Sigmoid", "Tanh", "ReLU", "Softmax"], 2, "ReLU(x) = max(0, x)."),
        (TF, "La retropropagación calcula cómo ajustar cada peso para reducir el error.", None, 0, "Usa la regla de la cadena para obtener los gradientes."),
        (SHORT, "¿Cómo se llama cada pasada completa por todos los datos de entrenamiento?", None, ["época", "epoca", "epoch"], "Una época recorre todo el conjunto una vez."),
        (MC, "¿Qué capa recuerda información a lo largo de una secuencia?", ["Dense", "GRU", "Dropout", "Embedding"], 1, "GRU es una red recurrente con compuertas."),
        (MC, "En una capa Dense con 64 entradas y 32 neuronas, ¿cuántos parámetros hay?", ["2,048", "2,080", "96", "64"], 1, "64 × 32 pesos + 32 sesgos = 2,080."),
    ],
    "IA301": [
        (MC, "¿Qué salida produce Softmax?", ["Un solo número", "Probabilidades que suman 1", "Valores negativos", "Texto"], 1, "Convierte puntajes en una distribución."),
        (TF, "Dropout apaga neuronas al azar solo durante el entrenamiento.", None, 0, "En inferencia se usan todas las neuronas."),
        (SHORT, "¿Cómo se llama una red que aprende varias tareas con una base compartida?", None, ["multitarea", "multi-tarea", "multitask"], "Como EduMaster MTL con 4 cabezas."),
        (MC, "¿Qué optimizador combina momento y tasas adaptativas?", ["SGD", "Adam", "K-means", "Newton"], 1, "Adam / AdamW."),
    ],
    "WEB101": [
        (MC, "¿Qué etiqueta HTML crea un enlace?", ["<link>", "<a>", "<href>", "<url>"], 1, "<a href=\"...\">texto</a>."),
        (TF, "CSS define la estructura y JavaScript define los estilos.", None, 1, "HTML estructura, CSS estiliza, JavaScript da comportamiento."),
        (SHORT, "¿Qué propiedad CSS cambia el color del texto?", None, ["color"], "color: #333;"),
        (MC, "¿Qué método agrega un elemento al final de un arreglo en JavaScript?", ["push()", "pop()", "shift()", "add()"], 0, "arr.push(x)."),
        (MC, "¿Qué display de CSS alinea elementos en una sola dimensión?", ["block", "grid", "flex", "inline"], 2, "Flexbox trabaja en una dimensión."),
    ],
    "WEB201": [
        (MC, "¿Qué hook de React guarda estado local?", ["useEffect", "useState", "useMemo", "useRef"], 1, "const [v, setV] = useState(0)."),
        (TF, "TypeScript detecta algunos errores de tipos antes de ejecutar el código.", None, 0, "El compilador revisa los tipos."),
        (SHORT, "¿Qué hook de React ejecuta efectos secundarios después de renderizar?", None, ["useEffect", "useeffect"], "useEffect(() => { ... }, [deps])."),
        (MC, "¿Qué atributo necesitan los elementos de una lista en React?", ["id", "key", "name", "index"], 1, "key ayuda a React a identificar cada elemento."),
    ],
    "CLD101": [
        (MC, "¿Qué comando muestra el directorio actual en Linux?", ["ls", "pwd", "cd", "whoami"], 1, "pwd = print working directory."),
        (TF, "Una imagen de Docker es una instancia en ejecución de un contenedor.", None, 1, "El contenedor es la instancia; la imagen es la plantilla."),
        (SHORT, "¿Qué comando de Docker lista los contenedores en ejecución?", None, ["docker ps", "ps"], "docker ps (añade -a para ver todos)."),
        (MC, "¿Qué archivo describe varios servicios para levantarlos juntos?", ["Dockerfile", "docker-compose.yml", "package.json", ".env"], 1, "docker compose up -d."),
    ],
    "CLD201": [
        (MC, "¿Qué registro DNS asocia un nombre a una IPv4?", ["MX", "A", "TXT", "CNAME"], 1, "Un registro A apunta a una IPv4."),
        (TF, "Let's Encrypt emite certificados HTTPS gratuitos para direcciones IP públicas.", None, 1, "Solo emite certificados para nombres de dominio."),
        (SHORT, "¿Qué puerto usa HTTPS por defecto?", None, ["443"], "HTTP usa 80 y HTTPS 443."),
        (MC, "¿Qué estado de una VM en Azure deja de cobrar el cómputo?", ["Detenida", "Detenida (desasignada)", "En ejecución", "Reiniciando"], 1, "Solo al desasignar se libera el hardware."),
    ],
}


def _normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text).strip().lower())
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).split())


def build_question(org_id: str, assessment_id: str, position: int, spec: tuple) -> Question:
    qtype, prompt, options, answer, explanation = spec
    if qtype == TF:
        options = ["Verdadero", "Falso"]
    data = {"accepted": list(answer)} if qtype == SHORT else {"correct": int(answer)}
    return Question(organization_id=org_id, assessment_id=assessment_id, position=position, type=qtype,
                    prompt=prompt, options=options or [], answer=data, points=1, explanation=explanation)


def ensure_question_bank(db: Session, organization_id: str) -> int:
    """Completa con preguntas DEMO las evaluaciones que aún no tienen ninguna. Idempotente."""
    with_questions = set(db.scalars(select(Question.assessment_id).where(Question.organization_id == organization_id)
                                    .group_by(Question.assessment_id)))
    rows = db.execute(select(Assessment, Course.code).join(Course, Course.id == Assessment.course_id)
                      .where(Assessment.organization_id == organization_id).order_by(Assessment.due_at)).all()
    created, seen_by_course = 0, {}
    for assessment, code in rows:
        n = seen_by_course.get(code, 0)
        seen_by_course[code] = n + 1
        if assessment.id in with_questions or code not in BANK:
            continue
        pool = BANK[code]
        picked = [pool[(n + i) % len(pool)] for i in range(min(4, len(pool)))]
        for pos, spec in enumerate(picked, start=1):
            db.add(build_question(organization_id, assessment.id, pos, spec))
            created += 1
    db.commit()
    return created


def grade(questions: list[Question], answers: dict) -> tuple[float, list[dict]]:
    """Devuelve (nota 0–100, detalle por pregunta)."""
    total = sum(q.points for q in questions) or 1
    earned, detail = 0.0, []
    for q in questions:
        given = answers.get(q.id)
        if q.type == SHORT:
            ok = given is not None and _normalize(given) in {_normalize(a) for a in q.answer.get("accepted", [])}
            correct_display = q.answer.get("accepted", [""])[0]
        else:
            try:
                ok = given is not None and int(given) == int(q.answer.get("correct", -1))
            except (TypeError, ValueError):
                ok = False
            idx = q.answer.get("correct", 0)
            correct_display = q.options[idx] if 0 <= idx < len(q.options) else ""
        if ok:
            earned += q.points
        detail.append({"question_id": q.id, "correct": ok, "given": given, "expected": correct_display,
                       "explanation": q.explanation})
    return round(earned / total * 100, 1), detail


def question_count(db: Session, assessment_id: str) -> int:
    return db.scalar(select(func.count(Question.id)).where(Question.assessment_id == assessment_id)) or 0
