FROM python:3.12-slim

WORKDIR /workspace

RUN pip install --no-cache-dir devsandbox

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["sleep", "infinity"]
