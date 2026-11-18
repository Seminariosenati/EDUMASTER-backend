# EduMaster.ai · Backend, IA e infraestructura

Plataforma empresarial de aprendizaje inteligente: gestión académica, seguimiento en tiempo real, analítica,
recomendaciones y una red neuronal multitarea (PyTorch) con visualización animada conectada al estado real del modelo.

> **Datos DEMO.** Al iniciar por primera vez se carga una institución de demostración con 240 estudiantes
> sintéticos. Todo lo que se ve está marcado como DEMO y no representa a personas reales.

Repositorios:

| Repo | Contenido |
|---|---|
| `edumaster-backend` (este) | API FastAPI, paquete de ML, Docker Compose, scripts de despliegue Linux/Windows, documentación |
| `edumaster-frontend` | SPA React 19 + TypeScript + Vite + Tailwind, visualizador neuronal, Caddy |

## Arquitectura

```
Navegador ──HTTPS──► Caddy (web) ──/api──► FastAPI (monolito modular) ──► PostgreSQL
   ▲                                   │  RBAC/ABAC, reglas de negocio, auditoría
   └──── SSE /api/v1/events/stream ◄───┤  Outbox → bus de eventos → SSE filtrado por organización y rol
                                       └─► Worker de entrenamiento (hilo aparte) ─► PyTorch ─► registro de modelos
```

| Capa | Tecnología | Dónde |
|---|---|---|
| Frontend | React 19, TypeScript, Vite, Tailwind 4, Recharts, Canvas | repo `edumaster-frontend` |
| API | Python 3.13, FastAPI, Pydantic, SQLAlchemy 2 | `apps/api/app` |
| Persistencia | PostgreSQL 16 (SQLite en desarrollo y en Windows) | `apps/api/app/db` |
| Tiempo real | Server-Sent Events + patrón Outbox | `app/core/events.py`, `app/modules/realtime.py` |
| ML | PyTorch, scikit-learn (baseline) | `ml/src/edumaster_ml` |
| Despliegue | Docker Compose + Caddy (HTTPS automático) / Windows Server con tarea programada | `docker-compose.yml`, `infra/` |

Dominios del backend (`apps/api/app/modules`): identidad, académico, seguimiento, analítica, recomendaciones,
certificados, IA, gobierno (auditoría y notificaciones) y tiempo real.

## Cómo funciona la red neuronal

**EduMaster Multi-Task Learning Network** (`ml/src/edumaster_ml/model.py`):

| Bloque | Diseño | Qué aprende |
|---|---|---|
| Entrada | 9 variables tabulares normalizadas + embedding del nivel del curso | perfil académico actual |
| Densas | `Linear → ReLU → Dropout` (por defecto 64 → 32 neuronas) | patrones estáticos |
| Rama temporal | GRU sobre 8 semanas × 4 variables | evolución del aprendizaje |
| Fusión | `Linear → LayerNorm → ReLU → Dropout` | combina ambas ramas |
| 4 cabezas | Sigmoid · Softmax · Sigmoid · Ranking | finalización, dominio, riesgo, competencia a reforzar |

**Entrenamiento** (`ml/src/edumaster_ml/train.py`):

1. El dataset se construye desde los eventos con **cortes temporales**: las variables usan solo lo ocurrido
   antes del corte y las etiquetas lo que pasa en los 28 días siguientes, para evitar fuga de información.
   La división es temporal: train usa los cortes iniciales, val el penúltimo y test el último.
2. La pérdida es multitarea: `L = λ1·BCE(finalización) + λ2·CE(dominio) + λ3·BCE(riesgo) + λ4·ListNet(ranking)`.
3. Se optimiza con AdamW, con *early stopping*, *scheduler* y *gradient clipping*; la semilla es fija y el resultado reproducible.
4. Se compara contra un **baseline de regresión logística**. Si la red no lo supera, no se publica.
5. Se guardan los pesos, el scaler, el esquema de variables, las métricas, la ficha del modelo (model card) y la ficha del dataset (datasheet) con su checksum.

**Memoria por capa.** En una capa Dense los parámetros son `entradas × neuronas + neuronas`. Al entrenar, la memoria
es pesos + gradientes + 2 estados de Adam (≈ 4 × pesos) + activaciones del lote. El endpoint
`POST /api/v1/ml/architecture/preview` calcula esto para cualquier configuración. En el laboratorio de IA se puede
comparar, por ejemplo, `64,32` frente a `128,64,32` antes de entrenar y ver qué capa pasa a ser la más pesada.

## Ejecutar en local

```bash
cd apps/api
python -m venv .venv && source .venv/bin/activate
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements-dev.txt && pip install -e ../../ml
uvicorn app.main:app --reload
```

La API queda en http://127.0.0.1:8000/api/docs. En el repo del frontend: `npm install && npm run dev`
(abre http://localhost:5173 con proxy a la API).

Cuentas DEMO (contraseña `Demo2026!`): `admin@`, `coordinador@`, `docente@`, `estudiante@`, `analista@`,
`ml@`, `auditor@`, `superadmin@` + `edumaster.demo`. Otra institución aislada: `admin@aliada.demo`.

## Pruebas

```bash
python -m pytest -q ml/tests
cd apps/api && python -m pytest -q tests
```

Cubren autenticación, RBAC, aislamiento entre instituciones, seudonimización, idempotencia de eventos, validaciones,
el flujo nota → finalización → certificado → verificación pública, la analítica y la exportación, y el ciclo de vida
del modelo (entrenar → registrar → promover → predecir → recomendar). También cubren la reproducibilidad del
entrenamiento y que las variables no usen información futura.

## Despliegue

### Linux (Ubuntu Server en Azure)

```bash
git clone https://github.com/Seminariosenati/edumaster-backend.git
bash edumaster-backend/infra/linux/deploy.sh
```

El script clona los dos repos en `/opt/edumaster` y genera `.env` con secretos aleatorios. Luego levanta
`db + api + web` con Docker Compose. Con `SITE_ADDRESS` igual al DNS de Azure, Caddy obtiene el certificado HTTPS
automáticamente.

### Windows Server

En PowerShell como Administrador (o en Azure: VM → Ejecutar comando → RunPowerShellScript):

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/Seminariosenati/edumaster-backend/main/infra/windows/deploy.ps1 -OutFile C:\deploy.ps1
powershell -ExecutionPolicy Bypass -File C:\deploy.ps1
```

El script instala Python, Node y Git, compila el frontend y registra la API como tarea al iniciar Windows. La API
sirve también la web en el puerto 80 y detiene el sitio por defecto de IIS. La base de datos es SQLite en `C:\edumaster\data`.

## Endpoints principales (`/api/v1`)

`POST /auth/login` · `GET /dashboard/summary` · `GET /analytics/progress|completion|regions|skill-mastery|export`
· `GET|POST /courses` · `POST /assessments/{id}/grades` · `POST /learning-events` · `GET /students/{id}/progress`
· `GET /recommendations` · `POST /recommendations/{id}/feedback` · `POST /ml/training-runs` · `GET /ml/models`
· `GET /ml/models/{id}/architecture` · `POST /ml/architecture/preview` · `POST /ml/predictions` · `GET /ml/system`
· `GET /certificates/verify/{code}` · `GET /audit-logs` · `GET /events/stream` (SSE)

El contrato OpenAPI completo está en `/api/v1/openapi.json`.
