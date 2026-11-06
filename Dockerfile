FROM alpine:3.24

ENV SSH_PORT=16161

RUN apk add --update --no-cache openssh-client-default python3

WORKDIR /app
COPY docker/umbrel_ssh_service.py /app/umbrel_ssh_service.py
COPY docker/entrypoint.sh /app/entrypoint.sh
RUN chmod +x /app/entrypoint.sh

EXPOSE 16161
ENTRYPOINT [ "/app/entrypoint.sh" ]
