ARG BACKEND_IMAGE=hypha-backend
FROM node:22-alpine AS frontend
WORKDIR /dashboard
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci --ignore-scripts
COPY dashboard/ ./
RUN npm run build -- --base=/dashboard/

FROM ${BACKEND_IMAGE}
USER root
COPY app/dashboard.py /app/app/dashboard.py
COPY --from=frontend /dashboard/dist /app/app/dashboard_static
RUN chmod -R a+rX /app/app/dashboard.py /app/app/dashboard_static
USER appuser
CMD ["uvicorn", "app.dashboard:public_app", "--factory", "--host", "127.0.0.1", "--port", "8010", "--workers", "1", "--no-access-log"]
