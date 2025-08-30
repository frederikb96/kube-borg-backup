FROM alpine:3.19
RUN apk add --no-cache borgbackup openssh-client
COPY run.sh /usr/local/bin/run.sh
RUN chmod +x /usr/local/bin/run.sh
ENTRYPOINT ["/usr/local/bin/run.sh"]
