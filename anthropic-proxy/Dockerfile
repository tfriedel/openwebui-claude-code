FROM caddy:2-alpine

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

EXPOSE 8081 8082
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
