FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 ROBOT_ASSIST_DATA=/data
WORKDIR /app
RUN groupadd --gid 10001 robot-assist && useradd --uid 10001 --gid 10001 robot-assist && mkdir /data && chown robot-assist:robot-assist /data
COPY robot_assist /app/robot_assist
USER 10001:10001
ENTRYPOINT ["python", "-m", "robot_assist"]
CMD ["api", "--host", "0.0.0.0"]
