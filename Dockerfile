FROM python:3.12-slim-bookworm@sha256:392307d22300de8b5986851a12d9176dfc0fc073e65bf6523ebd7dcbeb23564e

COPY --from=ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc /uv /uvx /bin/
# Distrust the compromised emSign root before any dependency download.
RUN python -c 'from pathlib import Path; import hashlib, ssl; name="mozilla/emSign_ECC_Root_CA_-_G3.crt"; root=Path("/usr/share/ca-certificates")/name; bad="86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"; assert not root.exists() or hashlib.sha256(ssl.PEM_cert_to_DER_cert(root.read_text())).hexdigest()==bad; config=Path("/etc/ca-certificates.conf"); lines=config.read_text().splitlines(); assert lines.count(name)<=1; config.write_text("\n".join("!"+line if line==name else line for line in lines)+"\n")' \
    && update-ca-certificates --fresh \
    && python -c 'import hashlib, ssl; bad="86a1ecba089c4a8d3bbe2734c612ba341d813e043cf9e8a862cd5c57a36bbe6b"; assert all(hashlib.sha256(cert).hexdigest()!=bad for cert in ssl.create_default_context().get_ca_certs(binary_form=True)); print("container trust: compromised emSign root absent")'
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY agentbus_service.py ./
COPY src/ ./src/
RUN useradd --uid 10001 --create-home agentbus \
    && mkdir /data && chown agentbus:agentbus /data
USER agentbus
ENV AGENTBUS_DB_PATH=/data/messages.sqlite3
EXPOSE 8766
CMD ["/app/.venv/bin/python", "-m", "uvicorn", "agentbus_service:create_app", "--factory", "--host", "0.0.0.0", "--port", "8766", "--no-access-log"]
