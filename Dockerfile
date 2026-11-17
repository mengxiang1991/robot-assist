FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ORBIT_DATA=/data
WORKDIR /app
RUN groupadd --gid 10001 orbit && useradd --uid 10001 --gid 10001 orbit && mkdir /data && chown orbit:orbit /data
COPY orbit /app/orbit
USER 10001:10001
ENTRYPOINT ["python", "-m", "orbit"]
CMD ["api", "--host", "0.0.0.0"]
