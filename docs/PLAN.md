# Plan de trabajo EduMaster.ai

Equipo de 4 · Avance: **viernes 20 de noviembre** · Entrega final: **viernes 27 de noviembre de 2026**

## 1. Alcance acordado (MVP de los documentos)

Los dos documentos describen la versión enterprise completa (18–29 semanas para un equipo de unos 10 roles). Para
dos semanas se construye un MVP **funcional de punta a punta** que cubre los tres puntos clave del profesor
(**arquitectura, análisis y habilidad**). Lo que queda fuera está listado y justificado en la sección 4.

## 2. Estado actual

| Sección del documento | Estado | Dónde se ve |
|---|---|---|
| 1. Dashboard con KPIs, fuente y fecha | Hecho | Resumen |
| 2. Exclusiones (sin sanciones automáticas, sin biometría) | Respetadas | Model card, textos de la UI |
| 3. Roles y permisos (8 roles, RBAC/ABAC en backend) | Hecho | Login con cada cuenta DEMO |
| 4.2 Rutas de aprendizaje | Hecho: crear y editar con versiones, validar prerrequisitos, publicar, asignar a cohorte o estudiantes, avance por paso, desbloqueo e historial | Planes de aprendizaje |
| 4.3 Cursos (crear, publicar, archivar, matricular) | Hecho | Cursos |
| 4.4 Analítica con filtros y exportación CSV auditada | Hecho | Análisis |
| 4.5 / 4.11 Recomendaciones con explicación y feedback | Hecho | Recomendaciones |
| Recomendaciones que actúan al aceptarse (matrícula, notificación), expiración a 14 días y métricas de aceptación/utilidad | Hecho | Recomendaciones |
| Filtros globales del Resumen por curso, cohorte y periodo | Hecho | Resumen |
| 4.5 IA Tutor: respuestas adaptadas al nivel estimado por la red, errores del estudiante y búsqueda TF-IDF en el material del curso; vista de dudas para el docente | Hecho | IA Tutor |
| 4.8 Comunidad por curso: anuncios, debates, comentarios, moderación (fijar/ocultar), reportes, filtro de lenguaje y silenciar avisos | Hecho | Comunidad |
| Mensajería directa 1 a 1 en tiempo real (SSE dirigido por usuario), no leídos y "visto" | Hecho | Mensajes |
| 4.6 Evaluaciones, nota oficial separada de la estimación IA | Hecho | Curso → Calificar |
| 4.6 Banco de preguntas y evaluación respondida por el estudiante con corrección automática | Hecho | Evaluaciones |
| 4.7 Certificados: folio, QR, verificación pública, anulación | Hecho | Certificados, `/verificar` |
| 4.9 Skill mastery: validado frente a estimado por IA | Hecho | Análisis → Dominio |
| 4.10 Arquitectura neuronal animada con estados reales | Hecho | Resumen, IA · Red neuronal |
| 4.12 Actividad y notificaciones en tiempo real (SSE) | Hecho | Indicador "En vivo", feed |
| 4.13 Auditoría | Hecho | Auditoría |
| 6. Monolito modular + servicio de ML aparte, Outbox | Hecho | `apps/api`, `ml/` |
| 7. Modelo de datos con UUID, `organization_id`, UTC | Hecho | `app/db/models.py` |
| 8. Dataset versionado, calidad, datasheet, split temporal | Hecho | IA → Registro |
| 9. Red multitarea, baseline, métricas por tarea, model card | Hecho | IA · Red neuronal |
| 10. Endpoints y eventos del documento | Hecho | `/api/docs` |
| 12. Seguridad: JWT, bcrypt, límite de intentos, CORS, auditoría | Hecho | `app/core/security.py` |
| 13. Pruebas (19 automatizadas) + CI | Hecho | `pytest`, GitHub Actions |
| 16. Despliegue Linux y Windows con DNS | Hecho | `infra/` |

Requisitos extra del profesor:

- **Cómo se entrena la red**: curva de *loss* en vivo, época a época, en el laboratorio de IA. Se explica en el README.
- **4–5 capas**: entrada → densa 1 → densa 2 (configurable) → GRU → fusión → 4 cabezas de salida.
- **Qué capa Dense requiere más memoria**: tabla de capas con parámetros y memoria. El experimento compara configuraciones antes de entrenar.
- **"Administrador de tareas"**: monitor en vivo de CPU, RAM y núcleos del servidor, y del proceso, durante el entrenamiento.
- **Linux y Windows con dominio/DNS**: `edumaster-linux.northcentralus.cloudapp.azure.com` y `edumaster-win.northcentralus.cloudapp.azure.com`.

## 3. Reparto de responsabilidades

| Integrante | Rol | Responsable de explicar | Tareas de la semana 2 |
|---|---|---|---|
| 1 | DevOps / Infraestructura | Azure, VMs, DNS, Docker, HTTPS, CI | Desplegar en ambos servidores, HTTPS en Windows, backup de la base |
| 2 | Backend | API, base de datos, roles, eventos, auditoría | Migraciones Alembic, paginación por cursor, más pruebas |
| 3 | IA / Datos | Dataset, red, entrenamiento, métricas, memoria por capa | Experimentos con neuronas y su informe, ajuste de pesos λ |
| 4 | Frontend / QA | Dashboard, visualizador, flujos por rol | Pruebas E2E con Playwright, accesibilidad, guion de la demo |

Regla del equipo: cada cambio entra por *pull request* con CI en verde (Definition of Done del documento, 13.3).

## 4. Pendiente y decisiones justificadas

| Del documento | Decisión en el MVP | Para la entrega final |
|---|---|---|
| Redis + Celery/RQ | Worker en un hilo aparte + Outbox con despachador; una sola instancia | Redis Streams si se escala a varias instancias |
| MLflow + DVC | Registro propio en base de datos: versión, métricas, artefactos y checksum del dataset | Opcional: exportar runs a MLflow |
| Alembic | `create_all` al iniciar | Migraciones versionadas (integrante 2) |
| OpenTelemetry / Prometheus | `correlation_id` en cada respuesta, health check y monitor de recursos | Exportar métricas a Prometheus |
| Lecciones editables | El curso cuenta actividades; no hay editor de contenidos | Editor básico de unidades |
| Ingresos (imagen de referencia) | Excluido: pagos fuera de alcance; se usan métricas académicas | — |

## 5. Guion de demo (8 minutos)

1. **Infra (1 min)**: abrir los dos dominios; mostrar Azure y el certificado HTTPS en Linux.
2. **Admin (2 min)**: dashboard con KPIs, fecha y fuente, mapa por región, finalización y heatmap. Exportar CSV.
3. **Docente (1 min)**: calificar a un estudiante; el feed se actualiza en vivo.
4. **Estudiante (1 min)**: completar una actividad, ver su avance y aceptar una recomendación.
5. **ML Engineer (2,5 min)**: en IA · Red neuronal, cambiar a `128, 64, 32` y ver la memoria por capa. Entrenar y
   mostrar la *loss* en vivo con el pico de CPU en el monitor. Comparar con el baseline en el registro, hacer una
   inferencia con un estudiante y ver las activaciones y los factores.
6. **Auditor (0,5 min)**: mostrar la bitácora con el entrenamiento, la promoción y la exportación.
